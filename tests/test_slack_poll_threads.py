"""Integration-style test for slack-watch thread scanning, with a fake client."""

from __future__ import annotations

from typing import Any

import pytest

import vigilant.triggers.slack_poll as sp
from vigilant.engine import Config

USER = "U1"
MENTION = f"<@{USER}>"
PR_A = "https://github.com/o/r/pull/1"
PR_B = "https://github.com/o/r/pull/2"

# ts values chosen larger than any real epoch so they sort after start_ts.
TOP = "9999999999.000001"        # top-level message that mentions us
PARENT = "9999999999.000002"     # thread parent WITHOUT a mention
REPLY = "9999999999.000003"      # reply in that thread that mentions us


class FakeClient:
    posted: list[tuple[str, str]] = []

    def __init__(self, token: str, cookie_d: str | None = None):
        pass

    def auth_test(self) -> dict[str, Any]:
        return {"user_id": USER}

    def conversations_history(
        self, channel: str, oldest: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return [
            {"ts": TOP, "text": f"{MENTION} please review {PR_A}"},
            {"ts": PARENT, "text": "kicking off a discussion (no mention)"},
        ]

    def conversations_history_page(
        self, channel: str, oldest: str | None = None, cursor: str | None = None, limit: int = 200
    ) -> tuple[list[dict[str, Any]], str | None]:
        return self.conversations_history(channel, oldest=oldest, limit=limit), None

    def conversations_replies(
        self, channel: str, ts: str, oldest: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        if ts == PARENT:
            return [
                {"ts": PARENT, "text": "kicking off a discussion (no mention)"},
                {"ts": REPLY, "text": f"{MENTION} can you take {PR_B}"},
            ]
        return [{"ts": ts, "text": ""}]

    def chat_post_message(self, channel: str, text: str, thread_ts: str | None = None) -> None:
        FakeClient.posted.append((channel, thread_ts or ""))


def test_thread_reply_mention_is_reviewed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    FakeClient.posted = []
    reviewed_text: list[str] = []

    monkeypatch.setenv("SLACK_TOKEN", "xoxb-fake")
    monkeypatch.setenv("VIGILANT_SLACK_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sp, "SlackClient", FakeClient)
    monkeypatch.setattr(sp, "review_from_text", lambda text, config: (reviewed_text.append(text) or ["ok"]))
    monkeypatch.setattr(sp, "format_reply", lambda outcomes: "done")

    rc = sp.run_slack_watch(Config(model="mock"), channels=["C1"], once=True)
    assert rc == 0

    joined = "\n".join(reviewed_text)
    assert PR_A in joined  # top-level mention caught
    assert PR_B in joined  # thread-reply mention caught
    # One reply posted per reviewed message: top-level in its own thread, and the
    # thread reply back into the parent thread.
    assert (("C1", TOP) in FakeClient.posted) and (("C1", PARENT) in FakeClient.posted)


def test_message_without_mention_not_reviewed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    FakeClient.posted = []
    reviewed_text: list[str] = []

    class NoMention(FakeClient):
        def conversations_history(self, channel: str, oldest: str | None = None, limit: int = 50):
            return [{"ts": TOP, "text": f"look at {PR_A} (no mention)"}]

        def conversations_replies(self, channel, ts, oldest=None, limit=50):
            return [{"ts": ts, "text": ""}]

    monkeypatch.setenv("SLACK_TOKEN", "xoxb-fake")
    monkeypatch.setenv("VIGILANT_SLACK_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sp, "SlackClient", NoMention)
    monkeypatch.setattr(sp, "review_from_text", lambda text, config: (reviewed_text.append(text) or ["ok"]))
    monkeypatch.setattr(sp, "format_reply", lambda outcomes: "done")

    rc = sp.run_slack_watch(Config(model="mock"), channels=["C1"], once=True)
    assert rc == 0
    assert reviewed_text == []
    assert FakeClient.posted == []


NEWER = "9999999999.000009"


def test_restart_resumes_and_dedupes(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    FakeClient.posted = []

    class Cfg(FakeClient):
        msgs: list[dict[str, Any]] = [{"ts": TOP, "text": f"{MENTION} review {PR_A}"}]

        def conversations_history(self, channel: str, oldest: str | None = None, limit: int = 50):
            return list(type(self).msgs)

        def conversations_replies(self, channel, ts, oldest=None, limit=50):
            return [{"ts": ts, "text": ""}]

    monkeypatch.setenv("SLACK_TOKEN", "xoxb-fake")
    monkeypatch.setenv("VIGILANT_SLACK_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sp, "SlackClient", Cfg)
    monkeypatch.setattr(sp, "format_reply", lambda outcomes: "done")

    # First run reviews the top-level mention and persists state.
    run1: list[str] = []
    monkeypatch.setattr(sp, "review_from_text", lambda text, config: (run1.append(text) or ["ok"]))
    assert sp.run_slack_watch(Config(model="mock"), channels=["C1"], once=True) == 0
    assert any(PR_A in t for t in run1)

    # Restart: the same message is still in history, plus a new one. The restart
    # must NOT re-review PR_A (already handled) but SHOULD review the new PR_B.
    Cfg.msgs = [
        {"ts": TOP, "text": f"{MENTION} review {PR_A}"},
        {"ts": NEWER, "text": f"{MENTION} review {PR_B}"},
    ]
    run2: list[str] = []
    monkeypatch.setattr(sp, "review_from_text", lambda text, config: (run2.append(text) or ["ok"]))
    assert sp.run_slack_watch(Config(model="mock"), channels=["C1"], once=True) == 0
    assert not any(PR_A in t for t in run2)  # no re-review across restart
    assert any(PR_B in t for t in run2)      # new message caught after restart
