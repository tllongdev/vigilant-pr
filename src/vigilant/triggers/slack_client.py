"""Minimal, dependency-free Slack Web API client.

Deliberately avoids the Slack SDK and Socket Mode so the poll-based monitor
needs no installed Slack app (and therefore no workspace-admin approval). It
works with whatever token you already have:

  - a browser-session token (``xoxc-...``) plus the ``d`` cookie (``xoxd-...``),
    exactly like the YTB workflow - no app required, and
  - a bot/user OAuth token (``xoxb-`` / ``xoxp-``) via a Bearer header, if you
    happen to have one.

Only the few methods the monitor needs are implemented (auth.test,
conversations.history, chat.postMessage). Everything goes over stdlib urllib.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

_API_BASE = "https://slack.com/api"


class SlackError(RuntimeError):
    """Raised when the Slack API returns ``ok: false`` or the call fails.

    ``code`` carries the Slack error string (e.g. ``invalid_auth``,
    ``token_expired``) when the API responded, so callers can distinguish an
    expired token (refresh + retry) from a hard failure.
    """

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


# Slack error codes that mean "the token is no longer valid" - a signal to
# refresh (re-extract) the token and retry rather than give up.
AUTH_ERROR_CODES = frozenset(
    {"invalid_auth", "not_authed", "token_expired", "token_revoked", "account_inactive"}
)


class SlackClient:
    def __init__(self, token: str, cookie_d: str | None = None, timeout: int = 30):
        if not token:
            raise SlackError("A Slack token is required (SLACK_TOKEN).")
        self.token = token
        self.cookie_d = cookie_d
        self.timeout = timeout
        # xoxc session tokens are only valid alongside the `d` cookie.
        if token.startswith("xoxc-") and not cookie_d:
            raise SlackError(
                "An xoxc- session token also needs the 'd' cookie "
                "(set SLACK_COOKIE_D to the xoxd-... value)."
            )

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        data = {k: v for k, v in params.items() if v is not None}
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        if self.token.startswith("xoxc-"):
            data["token"] = self.token
            headers["Cookie"] = f"d={self.cookie_d}"
        else:
            headers["Authorization"] = f"Bearer {self.token}"
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(
            f"{_API_BASE}/{method}", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - normalize transport errors
            raise SlackError(f"Slack API call {method} failed: {e}") from e
        if not payload.get("ok"):
            code = payload.get("error")
            raise SlackError(f"Slack API {method} returned error: {code}", code=code)
        return payload

    def auth_test(self) -> dict[str, Any]:
        """Return identity for the token (includes ``user_id`` and ``team``)."""
        return self._call("auth.test", {})

    def conversations_history(
        self, channel: str, oldest: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return channel messages newer than ``oldest`` (newest-first)."""
        payload = self._call(
            "conversations.history",
            {"channel": channel, "oldest": oldest, "limit": limit, "inclusive": "false"},
        )
        messages = payload.get("messages", [])
        return messages if isinstance(messages, list) else []

    def conversations_history_page(
        self, channel: str, oldest: str | None = None, cursor: str | None = None, limit: int = 200
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of channel history plus the next cursor (``None`` when done).

        Used to paginate a wider startup seed / downtime catch-up than a single
        ``conversations_history`` call returns.
        """
        payload = self._call(
            "conversations.history",
            {"channel": channel, "oldest": oldest, "cursor": cursor,
             "limit": limit, "inclusive": "false"},
        )
        messages = payload.get("messages", [])
        next_cursor = payload.get("response_metadata", {}).get("next_cursor") or None
        return (messages if isinstance(messages, list) else []), next_cursor

    def conversations_replies(
        self, channel: str, ts: str, oldest: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return messages in a thread (the parent ``ts`` plus its replies).

        ``conversations.history`` only returns top-level channel messages, so
        this is how we see @-mentions posted as replies inside a thread.
        """
        payload = self._call(
            "conversations.replies",
            {"channel": channel, "ts": ts, "oldest": oldest, "limit": limit},
        )
        messages = payload.get("messages", [])
        return messages if isinstance(messages, list) else []

    def chat_post_message(
        self, channel: str, text: str, thread_ts: str | None = None
    ) -> None:
        self._call(
            "chat.postMessage",
            {"channel": channel, "text": text, "thread_ts": thread_ts},
        )
