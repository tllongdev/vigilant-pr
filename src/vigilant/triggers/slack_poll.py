"""Poll-based Slack monitor (no Slack app, no admin approval) - the default
Slack surface for locked-down workspaces.

Modeled on the YTB workflow: authenticate with a token you already have
(``xoxc-`` session token + ``d`` cookie, or a ``xoxb-``/``xoxp-`` OAuth token),
then poll ``conversations.history`` for one or more channels. When a new message
(a) @-mentions you and (b) contains a GitHub PR link, Vigilant reviews the PR as
your GitHub identity and (optionally) replies in-thread with the outcome.

This never installs anything into the workspace and never opens an inbound
socket - it just reads a channel you can already read. Contrast with
``triggers/slack.py`` (Socket Mode), which is cleaner but requires creating and
installing a Slack app.

Config comes from the environment:
  SLACK_TOKEN               xoxc-/xoxb-/xoxp- token (required)
  SLACK_COOKIE_D            xoxd-... cookie value (required for xoxc- tokens)
  VIGILANT_SLACK_CHANNELS   comma-separated channel IDs (e.g. C0123,C0456)
  VIGILANT_SLACK_USER_ID    your Slack user id to watch for mentions of
                            (defaults to the token's own user via auth.test)
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from typing import TypeVar

from ..engine import Config, model_key_missing
from .core import extract_pr_refs, format_reply, review_from_text
from .slack_auth import TokenSource, build_token_source
from .slack_client import AUTH_ERROR_CODES, SlackClient, SlackError

T = TypeVar("T")


def message_triggers(text: str, mention_token: str) -> list[str]:
    """Return PR refs to review if ``text`` mentions the operator, else ``[]``.

    A message only triggers when it both @-mentions the watched user (Slack
    encodes this as ``<@U012345>``) and contains at least one GitHub PR link.
    Requiring the mention keeps the monitor from reacting to every PR posted in
    a busy channel, and also stops it from looping on its own reply (which echoes
    the PR URL but never mentions the user).
    """
    if mention_token not in (text or ""):
        return []
    return extract_pr_refs(text)


def _channels_from_env(explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    raw = os.environ.get("VIGILANT_SLACK_CHANNELS", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def run_slack_watch(
    config: Config,
    channels: list[str] | None = None,
    poll_interval: int = 60,
    once: bool = False,
    reply: bool = True,
    auto_token: bool = False,
) -> int:
    """Poll Slack channel(s) and review PRs you're @-mentioned on.

    Returns a process exit code (0 on clean shutdown, 1 on config error). When
    the token comes from ``--auto-token`` (the browser session), an expired
    token is transparently re-extracted and the failed call retried.
    """
    key_problem = model_key_missing(config)
    if key_problem:
        sys.stderr.write(key_problem + "\n")
        return 1

    watch_channels = _channels_from_env(channels)
    if not watch_channels:
        sys.stderr.write(
            "No channels to watch. Pass --channel C0123 (repeatable) or set "
            "VIGILANT_SLACK_CHANNELS=C0123,C0456.\n"
        )
        return 1

    try:
        source: TokenSource = build_token_source(auto_token, probe_channel=watch_channels[0])
        token, cookie_d = source.get()
        client = SlackClient(token, cookie_d)
    except SlackError as e:
        sys.stderr.write(str(e) + "\n")
        return 1

    # A single mutable holder so a refresh can swap the client under the closures.
    state = {"client": client}

    def refresh() -> bool:
        if not source.can_refresh:
            return False
        try:
            new_token, new_cookie = source.get(force_refresh=True)
            state["client"] = SlackClient(new_token, new_cookie)
            sys.stderr.write("Slack token expired; re-extracted a fresh one.\n")
            return True
        except SlackError as e:
            sys.stderr.write(f"Slack token refresh failed: {e}\n")
            return False

    def api(fn: Callable[[SlackClient], T]) -> T:
        """Run a Slack call, refreshing the token once on an auth-expiry error."""
        try:
            return fn(state["client"])
        except SlackError as e:
            if e.code in AUTH_ERROR_CODES and refresh():
                return fn(state["client"])
            raise

    user_id = os.environ.get("VIGILANT_SLACK_USER_ID")
    try:
        if not user_id:
            user_id = str(api(lambda c: c.auth_test()).get("user_id", ""))
    except SlackError as e:
        sys.stderr.write(f"Slack auth failed: {e}\n")
        return 1
    if not user_id:
        sys.stderr.write(
            "Could not determine your Slack user id; set VIGILANT_SLACK_USER_ID.\n"
        )
        return 1

    mention_token = f"<@{user_id}>"
    # Only act on messages posted after startup; never backfill channel history.
    start_ts = f"{time.time():.6f}"
    last_ts: dict[str, str] = dict.fromkeys(watch_channels, start_ts)

    sys.stderr.write(
        f"Vigilant PR watching Slack for @{user_id} in "
        f"{', '.join(watch_channels)} (every {poll_interval}s). Ctrl-C to stop.\n"
    )

    def poll_once() -> None:
        for channel in watch_channels:
            try:
                # lambda runs immediately inside api(); loop-var capture is safe here.
                messages = api(
                    lambda c: c.conversations_history(channel, oldest=last_ts[channel])  # noqa: B023
                )
            except SlackError as e:
                sys.stderr.write(f"[{channel}] history fetch failed: {e}\n")
                continue
            # Slack returns newest-first; process oldest-first for stable ordering.
            for msg in sorted(messages, key=lambda m: str(m.get("ts", ""))):
                ts = str(msg.get("ts", ""))
                if ts and ts > last_ts[channel]:
                    last_ts[channel] = ts
                refs = message_triggers(str(msg.get("text", "")), mention_token)
                if not refs:
                    continue
                sys.stderr.write(f"[{channel}] mention + PR link at {ts}; reviewing...\n")
                outcomes = review_from_text(str(msg.get("text", "")), config)
                if reply and outcomes:
                    body = format_reply(outcomes)
                    try:
                        api(lambda c: c.chat_post_message(channel, body, thread_ts=ts))  # noqa: B023
                    except SlackError as e:
                        sys.stderr.write(f"[{channel}] could not post reply: {e}\n")

    try:
        while True:
            poll_once()
            if once:
                return 0
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        sys.stderr.write("\nStopped.\n")
        return 0
