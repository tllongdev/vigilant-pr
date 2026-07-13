"""Poll-based Slack monitor (no Slack app, no admin approval) - the only Slack
surface Vigilant PR ships, on purpose: it needs zero workspace setup.

Modeled on the YTB workflow: authenticate with a token you already have
(``xoxc-`` session token + ``d`` cookie, or a ``xoxb-``/``xoxp-`` OAuth token),
then poll one or more channels. When a message (a) @-mentions you and (b)
contains a GitHub PR link, Vigilant reviews the PR as your GitHub identity and
(optionally) replies in-thread with the outcome.

Mentions are caught whether they're **top-level messages or replies inside a
thread**: each poll reads ``conversations.history`` for new top-level messages
and ``conversations.replies`` for new replies in the threads it is tracking
(seeded from recent history at startup, and grown as new threads appear). Only
activity after startup is acted on; nothing is backfilled.

This never installs anything into the workspace and never opens an inbound
socket - it just reads a channel you can already read.

Config comes from the environment:
  SLACK_TOKEN               xoxc-/xoxb-/xoxp- token (required unless --auto-token)
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
from typing import Any, TypeVar

from ..engine import Config, model_key_missing
from .core import extract_pr_refs, format_reply, review_from_text
from .slack_auth import TokenSource, build_token_source
from .slack_client import AUTH_ERROR_CODES, SlackClient, SlackError

T = TypeVar("T")

# Bound the reply-polling cost: forget threads older than this, and never track
# more than this many per channel (keeping the most recent).
MAX_THREAD_AGE_SECONDS = 2 * 86400
MAX_WATCHED_THREADS_PER_CHANNEL = 50


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
    # Per-channel set of thread parent ts we scan for new replies, plus the
    # newest reply ts already handled in each. A ts we've already reviewed is
    # remembered so a message can't be actioned twice across the two paths.
    known_threads: dict[str, set[str]] = {ch: set() for ch in watch_channels}
    thread_cursor: dict[tuple[str, str], str] = {}
    reviewed: set[str] = set()

    def track_thread(channel: str, msg: dict[str, Any]) -> None:
        """Watch the thread a message belongs to for future replies."""
        ts = str(msg.get("ts", ""))
        thread_ts = str(msg.get("thread_ts", ts) or ts)
        if not thread_ts:
            return
        if thread_ts not in known_threads[channel]:
            known_threads[channel].add(thread_ts)
            # Only replies after startup (or after the parent, if newer) count.
            thread_cursor[(channel, thread_ts)] = max(thread_ts, start_ts)

    def prune_threads(channel: str, now: float) -> None:
        cutoff = now - MAX_THREAD_AGE_SECONDS
        fresh = [t for t in known_threads[channel] if _ts_float(t) >= cutoff]
        fresh.sort(key=_ts_float, reverse=True)
        keep = set(fresh[:MAX_WATCHED_THREADS_PER_CHANNEL])
        for t in known_threads[channel] - keep:
            thread_cursor.pop((channel, t), None)
        known_threads[channel] = keep

    def maybe_review(channel: str, text: str, root_ts: str, msg_ts: str) -> None:
        """Review + reply if the message mentions us and links a PR (once per ts)."""
        if msg_ts in reviewed or not message_triggers(text, mention_token):
            return
        reviewed.add(msg_ts)
        sys.stderr.write(f"[{channel}] mention + PR link at {msg_ts}; reviewing...\n")
        outcomes = review_from_text(text, config)
        if reply and outcomes:
            body = format_reply(outcomes)
            try:
                api(lambda c: c.chat_post_message(channel, body, thread_ts=root_ts))  # noqa: B023
            except SlackError as e:
                sys.stderr.write(f"[{channel}] could not post reply: {e}\n")

    # Seed tracked threads from recent history so replies to threads that already
    # exist at startup are still caught (without reviewing any pre-startup message).
    for channel in watch_channels:
        try:
            for msg in api(lambda c: c.conversations_history(channel, limit=50)):  # noqa: B023
                track_thread(channel, msg)
        except SlackError as e:
            sys.stderr.write(f"[{channel}] seed fetch failed: {e}\n")

    sys.stderr.write(
        f"Vigilant PR watching Slack for @{user_id} in "
        f"{', '.join(watch_channels)} (every {poll_interval}s, threads included). "
        "Ctrl-C to stop.\n"
    )

    def poll_once() -> None:
        now = time.time()
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
                track_thread(channel, msg)
                root = str(msg.get("thread_ts", ts) or ts)
                maybe_review(channel, str(msg.get("text", "")), root_ts=root, msg_ts=ts)

            prune_threads(channel, now)
            for thread_ts in sorted(known_threads[channel]):
                cursor = thread_cursor.get((channel, thread_ts), start_ts)
                try:
                    replies = api(
                        lambda c: c.conversations_replies(channel, thread_ts, oldest=cursor)  # noqa: B023
                    )
                except SlackError as e:
                    sys.stderr.write(f"[{channel}] thread {thread_ts} fetch failed: {e}\n")
                    continue
                for r in sorted(replies, key=lambda m: str(m.get("ts", ""))):
                    r_ts = str(r.get("ts", ""))
                    if not r_ts or r_ts <= cursor:
                        continue
                    thread_cursor[(channel, thread_ts)] = r_ts
                    if r_ts == thread_ts:  # the parent; handled by the history path
                        continue
                    maybe_review(channel, str(r.get("text", "")), root_ts=thread_ts, msg_ts=r_ts)

    try:
        while True:
            poll_once()
            if once:
                return 0
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        sys.stderr.write("\nStopped.\n")
        return 0


def _ts_float(ts: str) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0
