"""Poll-based Slack monitor (no Slack app, no admin approval) - the only Slack
surface Vigilant PR ships, on purpose: it needs zero workspace setup.

Modeled on the YTB workflow: authenticate with a token you already have
(``xoxc-`` session token + ``d`` cookie, or a ``xoxb-``/``xoxp-`` OAuth token),
then poll one or more channels. When a message (a) @-mentions you and (b)
contains a GitHub PR link, Vigilant reviews the PR as your GitHub identity and
(optionally) replies in-thread with the outcome.

Mentions are caught whether they're **top-level messages or replies inside a
thread**: each poll reads ``conversations.history`` for new top-level messages
and ``conversations.replies`` for new replies in the threads it is tracking. At
startup it seeds tracked threads from the last week of history (paginated), so
replies to already-existing threads are covered, not just brand-new ones.

Progress is **persisted to disk** (per channel-set), so a restart resumes where
it left off - it reviews anything that arrived while it was down and does not
re-open the tracking window or re-review messages it already handled.

This never installs anything into the workspace and never opens an inbound
socket - it just reads a channel you can already read.

Config comes from the environment:
  SLACK_TOKEN               xoxc-/xoxb-/xoxp- token (required unless --auto-token)
  SLACK_COOKIE_D            xoxd-... cookie value (required for xoxc- tokens)
  VIGILANT_SLACK_CHANNELS   comma-separated channel IDs (e.g. C0123,C0456)
  VIGILANT_SLACK_USER_ID    your Slack user id to watch for mentions of
                            (defaults to the token's own user via auth.test)
  VIGILANT_SLACK_STATE_DIR  where to persist watch state (default ~/.config/vigilant-pr)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from ..engine import Config, model_key_missing
from ..ui import print_banner, status
from .core import extract_pr_refs, format_reply, review_from_text
from .slack_auth import TokenSource, build_token_source
from .slack_client import AUTH_ERROR_CODES, SlackClient, SlackError

T = TypeVar("T")

# Bound the reply-polling cost: forget threads older than this, and never track
# more than this many per channel (keeping the most recent). One week comfortably
# covers "someone revived last week's PR thread to ping me" without unbounded growth.
MAX_THREAD_AGE_SECONDS = 7 * 86400
MAX_WATCHED_THREADS_PER_CHANNEL = 200
# Startup seed / downtime catch-up: how far back to look and how many pages to pull.
SEED_LOOKBACK_SECONDS = 7 * 86400
HISTORY_PAGE_LIMIT = 200
HISTORY_MAX_PAGES = 8
STATE_VERSION = 1


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


def _ts_float(ts: str) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def _state_dir() -> Path:
    override = os.environ.get("VIGILANT_SLACK_STATE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "vigilant-pr" / "slack_watch"


def _state_file(channels: list[str], user_id: str) -> Path:
    """A stable per-(user, channel-set) state file so distinct monitors don't clash."""
    key = user_id + "|" + ",".join(sorted(channels))
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return _state_dir() / f"{digest}.json"


def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        path.chmod(0o600)
    except OSError as e:
        sys.stderr.write(f"Could not persist Slack watch state: {e}\n")


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

    if sys.stdout.isatty():
        print_banner()

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
    now_ts = f"{time.time():.6f}"

    # Restore persisted progress so a restart resumes rather than re-baselining.
    state_path = _state_file(watch_channels, user_id)
    saved = _load_state(state_path)
    saved_last: dict[str, Any] = saved.get("last_ts", {}) if saved.get("user_id") == user_id else {}
    saved_threads: dict[str, Any] = saved.get("threads", {}) if saved.get("user_id") == user_id else {}
    saved_reviewed = saved.get("reviewed", []) if saved.get("user_id") == user_id else []

    reviewed_cutoff = time.time() - MAX_THREAD_AGE_SECONDS
    reviewed: set[str] = {t for t in saved_reviewed if _ts_float(str(t)) >= reviewed_cutoff}

    # Per channel: where reviewing resumes (last_ts), the baseline before which we
    # never review, tracked thread parents, and each thread's processed-reply cursor.
    last_ts: dict[str, str] = {}
    baseline_ts: dict[str, str] = {}
    known_threads: dict[str, set[str]] = {}
    thread_cursor: dict[tuple[str, str], str] = {}
    for ch in watch_channels:
        base = str(saved_last.get(ch) or now_ts)
        last_ts[ch] = base
        baseline_ts[ch] = base
        known_threads[ch] = set(saved_threads.get(ch, {}).keys())
        for tt, cur in saved_threads.get(ch, {}).items():
            thread_cursor[(ch, str(tt))] = str(cur)

    def track_thread(channel: str, msg: dict[str, Any]) -> None:
        """Watch the thread a message belongs to for future replies."""
        ts = str(msg.get("ts", ""))
        thread_ts = str(msg.get("thread_ts", ts) or ts)
        if not thread_ts:
            return
        known_threads[channel].add(thread_ts)
        # Only replies after our baseline (or after the parent, if newer) count.
        thread_cursor.setdefault((channel, thread_ts), max(thread_ts, baseline_ts[channel]))

    def prune_threads(channel: str, now: float) -> None:
        cutoff = now - MAX_THREAD_AGE_SECONDS
        fresh = [t for t in known_threads[channel] if _ts_float(t) >= cutoff]
        fresh.sort(key=_ts_float, reverse=True)
        keep = set(fresh[:MAX_WATCHED_THREADS_PER_CHANNEL])
        for t in known_threads[channel] - keep:
            thread_cursor.pop((channel, t), None)
        known_threads[channel] = keep

    def history_since(channel: str, oldest: str) -> list[dict[str, Any]]:
        """All channel messages since ``oldest``, paginated and bounded."""
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(HISTORY_MAX_PAGES):
            # lambda runs immediately inside api(); capturing `channel`/`cursor` is safe here.
            page, cursor = api(lambda c: c.conversations_history_page(channel, oldest=oldest, cursor=cursor, limit=HISTORY_PAGE_LIMIT))  # noqa: B023
            collected.extend(page)
            if not cursor:
                break
        return collected

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

    def persist() -> None:
        cut = time.time() - MAX_THREAD_AGE_SECONDS
        threads_out = {
            ch: {tt: thread_cursor[(ch, tt)] for tt in known_threads[ch] if (ch, tt) in thread_cursor}
            for ch in watch_channels
        }
        _save_state(state_path, {
            "version": STATE_VERSION,
            "user_id": user_id,
            "last_ts": last_ts,
            "threads": threads_out,
            "reviewed": sorted(t for t in reviewed if _ts_float(t) >= cut),
        })

    # Seed tracked threads from the last week of history so replies to threads
    # that already exist at startup are caught. This only *tracks* threads; it
    # never reviews a pre-baseline message (maybe_review is gated by baseline via
    # the poll/reply cursors below, not called here).
    seed_oldest = f"{time.time() - SEED_LOOKBACK_SECONDS:.6f}"
    for channel in watch_channels:
        try:
            for msg in history_since(channel, seed_oldest):
                track_thread(channel, msg)
        except SlackError as e:
            sys.stderr.write(f"[{channel}] seed fetch failed: {e}\n")

    resumed = " (resumed)" if saved.get("user_id") == user_id else ""
    sys.stderr.write(
        f"Vigilant PR watching Slack for @{user_id} in "
        f"{', '.join(watch_channels)} (every {poll_interval}s, threads included){resumed}. "
        "Ctrl-C to stop.\n"
    )

    def poll_once() -> None:
        now = time.time()
        for channel in watch_channels:
            try:
                messages = history_since(channel, last_ts[channel])
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
                cursor = thread_cursor.get((channel, thread_ts), baseline_ts[channel])
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
        persist()

    try:
        while True:
            poll_once()
            if once:
                return 0
            now = time.strftime("%H:%M:%S")
            heartbeat = (
                f"slack-watch running | {len(watch_channels)} channel(s) | "
                f"last poll {now} | {len(reviewed)} reviewed"
            )
            with status(heartbeat):
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        sys.stderr.write("\nStopped.\n")
        return 0
