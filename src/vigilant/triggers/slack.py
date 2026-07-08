"""Slack trigger (Socket Mode) - milestone 004.

Listens over Slack Socket Mode (an outbound WebSocket, so no inbound ports are
needed - it fits the same self-hosted daemon model as `watch`) and runs a review
when the operator:
  - invokes the `/review <pr-url>` slash command,
  - @-mentions the app with a PR link in the message, or
  - adds a trigger reaction (default :eyes:) to a message containing a PR link.

Every review posts on behalf of the GitHub token the process holds. Because that
is *your* identity, restrict who can trigger it with SLACK_ALLOWED_USERS unless
you are the only member of the workspace.

Requires the optional `slack` extra (slack_bolt). Imported lazily so the core
package stays dependency-free.

Environment:
  SLACK_BOT_TOKEN      xoxb-...  (bot token; needs chat:write, app_mentions:read,
                                  commands, reactions:read, channels:history)
  SLACK_APP_TOKEN      xapp-...  (app-level token with connections:write)
  SLACK_ALLOWED_USERS  optional comma-separated Slack user IDs allowed to trigger
  VIGILANT_TRIGGER_EMOJIS  optional comma-separated reacji names (default: eyes)
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

from ..engine import Config
from .core import DEFAULT_TRIGGER_EMOJIS, format_reply, review_from_text


def _require_bolt() -> tuple[Any, Any]:
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as e:
        raise SystemExit(
            "The Slack trigger needs the 'slack' extra. Install with:\n"
            "  pipx install 'vigilant-pr[slack]'   (or: uv tool install '.[slack]')\n"
            "or use the container image, which already includes it."
        ) from e
    return App, SocketModeHandler


def _allowed_users() -> set[str]:
    raw = os.environ.get("SLACK_ALLOWED_USERS", "")
    return {u.strip() for u in raw.split(",") if u.strip()}


def _trigger_emojis() -> set[str]:
    raw = os.environ.get("VIGILANT_TRIGGER_EMOJIS", "")
    names = {e.strip().strip(":") for e in raw.split(",") if e.strip()}
    return names or set(DEFAULT_TRIGGER_EMOJIS)


def _is_allowed(user_id: str | None, allowed: set[str]) -> bool:
    if not allowed:
        return True  # open workspace; startup logs a warning in this case
    return bool(user_id) and user_id in allowed


def _run_and_reply(text: str, config: Config, say: Any, thread_ts: str | None) -> None:
    """Review every PR link in `text` and post the result back to Slack."""
    outcomes = review_from_text(text, config)
    say(text=format_reply(outcomes), thread_ts=thread_ts)


def run_slack(config: Config) -> int:
    """Start the Slack Socket Mode listener. Blocks until interrupted."""
    App, SocketModeHandler = _require_bolt()

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        sys.stderr.write(
            "SLACK_BOT_TOKEN (xoxb-...) and SLACK_APP_TOKEN (xapp-...) are required "
            "for Socket Mode. See the README Slack section.\n"
        )
        return 1
    if not config.anthropic_api_key:
        sys.stderr.write("ANTHROPIC_API_KEY not set; reviews cannot run.\n")
        return 1

    allowed = _allowed_users()
    emojis = _trigger_emojis()
    if not allowed:
        sys.stderr.write(
            "WARNING: SLACK_ALLOWED_USERS is unset - anyone in the workspace can post "
            "reviews under YOUR GitHub identity. Set it to your Slack user ID(s).\n"
        )

    app = App(token=bot_token)

    @app.command("/review")
    def handle_review_command(ack: Any, command: dict[str, Any], say: Any) -> None:
        ack()
        user = command.get("user_id")
        if not _is_allowed(user, allowed):
            say(text="Sorry, you are not authorized to trigger reviews here.")
            return
        text = command.get("text", "") or ""
        say(text="On it - reviewing now. This usually takes a minute or two.")
        threading.Thread(
            target=_run_and_reply, args=(text, config, say, None), daemon=True
        ).start()

    @app.event("app_mention")
    def handle_mention(event: dict[str, Any], say: Any) -> None:
        if not _is_allowed(event.get("user"), allowed):
            return
        text = event.get("text", "") or ""
        thread_ts = event.get("thread_ts") or event.get("ts")
        say(text="On it - reviewing now.", thread_ts=thread_ts)
        threading.Thread(
            target=_run_and_reply, args=(text, config, say, thread_ts), daemon=True
        ).start()

    @app.event("reaction_added")
    def handle_reaction(event: dict[str, Any], client: Any, say: Any) -> None:
        if event.get("reaction") not in emojis:
            return
        if not _is_allowed(event.get("user"), allowed):
            return
        item = event.get("item", {}) or {}
        channel = item.get("channel")
        ts = item.get("ts")
        if item.get("type") != "message" or not channel or not ts:
            return
        try:
            resp = client.conversations_history(
                channel=channel, latest=ts, oldest=ts, inclusive=True, limit=1
            )
            messages = resp.get("messages", [])
        except Exception as e:  # noqa: BLE001 - never crash the socket loop
            sys.stderr.write(f"Could not fetch reacted message: {e}\n")
            return
        if not messages:
            return
        text = messages[0].get("text", "") or ""
        if not text:
            return

        def _work() -> None:
            outcomes = review_from_text(text, config)
            if outcomes:
                say(text=format_reply(outcomes), thread_ts=ts, channel=channel)

        threading.Thread(target=_work, daemon=True).start()

    sys.stderr.write(
        f"Vigilant PR Slack listener started (trigger emojis: "
        f"{', '.join(sorted(emojis))}). Ctrl-C to stop.\n"
    )
    SocketModeHandler(app, app_token).start()
    return 0
