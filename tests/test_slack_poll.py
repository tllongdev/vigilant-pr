"""Tests for the poll-based Slack monitor (no network)."""

from __future__ import annotations

import pytest

from vigilant.engine import Config
from vigilant.triggers.slack_client import SlackClient, SlackError
from vigilant.triggers.slack_poll import message_triggers, run_slack_watch

MENTION = "<@U123>"
PR = "https://github.com/o/r/pull/42"


def test_message_triggers_requires_mention() -> None:
    assert message_triggers(f"please review {PR}", MENTION) == []


def test_message_triggers_requires_pr_link() -> None:
    assert message_triggers(f"hey {MENTION} thoughts?", MENTION) == []


def test_message_triggers_returns_refs_when_both_present() -> None:
    assert message_triggers(f"{MENTION} review {PR} please", MENTION) == [PR]


def test_message_triggers_handles_slack_link_markup() -> None:
    text = f"{MENTION} can you look at <{PR}|PR 42>"
    assert message_triggers(text, MENTION) == [PR]


def test_reply_message_does_not_retrigger() -> None:
    # Our own reply echoes the PR URL but never mentions the user -> no loop.
    reply = f"Review posted to {PR} (as your GitHub identity)."
    assert message_triggers(reply, MENTION) == []


def test_slack_client_xoxc_requires_cookie() -> None:
    with pytest.raises(SlackError):
        SlackClient("xoxc-abc", cookie_d=None)


def test_slack_client_bearer_token_ok() -> None:
    client = SlackClient("xoxb-abc")
    assert client.token == "xoxb-abc"


def test_slack_client_empty_token_raises() -> None:
    with pytest.raises(SlackError):
        SlackClient("")


def test_run_slack_watch_blocks_on_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    rc = run_slack_watch(Config(model="anthropic/claude-sonnet-5"), channels=["C1"], once=True)
    assert rc == 1


def test_run_slack_watch_requires_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIGILANT_SLACK_CHANNELS", raising=False)
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-abc")
    rc = run_slack_watch(Config(model="mock"), channels=None, once=True)
    assert rc == 1


def test_run_slack_watch_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_TOKEN", raising=False)
    rc = run_slack_watch(Config(model="mock"), channels=["C1"], once=True)
    assert rc == 1
