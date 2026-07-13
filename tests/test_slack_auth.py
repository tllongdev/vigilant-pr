"""Tests for Slack token source selection (no network / no browser)."""

from __future__ import annotations

import pytest

from vigilant.triggers.slack_auth import (
    BrowserTokenSource,
    EnvTokenSource,
    _select_team_token,
    build_token_source,
)
from vigilant.triggers.slack_client import SlackError


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SLACK_TOKEN", "SLACK_COOKIE_D", "VIGILANT_SLACK_AUTO_TOKEN", "VIGILANT_SLACK_TEAM"):
        monkeypatch.delenv(var, raising=False)


def test_env_source_returns_token_and_never_refreshes() -> None:
    src = EnvTokenSource("xoxb-abc", None)
    assert src.get() == ("xoxb-abc", None)
    assert src.can_refresh is False


def test_build_prefers_static_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-abc")
    src = build_token_source(auto=True)  # static token wins even if auto asked
    assert isinstance(src, EnvTokenSource)
    assert src.get()[0] == "xoxb-abc"


def test_build_uses_browser_when_auto_and_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    src = build_token_source(auto=True)
    assert isinstance(src, BrowserTokenSource)
    assert src.can_refresh is True


def test_build_uses_browser_when_env_auto_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("VIGILANT_SLACK_AUTO_TOKEN", "1")
    assert isinstance(build_token_source(auto=False), BrowserTokenSource)


def test_build_errors_without_token_or_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    with pytest.raises(SlackError):
        build_token_source(auto=False)


def test_select_team_uses_hint() -> None:
    extracted = {
        "cookie_d": "xoxd-c",
        "teams": {"T1": {"name": "A", "token": "xoxc-1"}, "T2": {"name": "B", "token": "xoxc-2"}},
    }
    assert _select_team_token(extracted, team_hint="T2", probe_channel=None) == ("xoxc-2", "xoxd-c")


def test_select_team_single_team_no_hint() -> None:
    extracted = {"cookie_d": "xoxd-c", "teams": {"T1": {"name": "A", "token": "xoxc-1"}}}
    assert _select_team_token(extracted, team_hint=None, probe_channel=None) == ("xoxc-1", "xoxd-c")


def test_select_team_ambiguous_without_probe_raises() -> None:
    extracted = {
        "cookie_d": "xoxd-c",
        "teams": {"T1": {"name": "A", "token": "xoxc-1"}, "T2": {"name": "B", "token": "xoxc-2"}},
    }
    with pytest.raises(SlackError):
        _select_team_token(extracted, team_hint=None, probe_channel=None)
