"""Tests for the stdlib banner + status line."""

from __future__ import annotations

import io

from vigilant import ui


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_use_color_off_for_non_tty():
    assert ui.use_color(io.StringIO()) is False


def test_use_color_respects_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert ui.use_color(_FakeTTY()) is False


def test_use_color_off_for_dumb_terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert ui.use_color(_FakeTTY()) is False


def test_use_color_on_for_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    assert ui.use_color(_FakeTTY()) is True


def test_banner_plain_off_tty():
    text = ui.banner(io.StringIO())
    assert "\033[" not in text  # no ANSI escapes
    assert "adversarial PR review" in text


def test_banner_colored_on_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    text = ui.banner(_FakeTTY())
    assert "\033[38;2;" in text  # truecolor escape


def test_status_noop_off_tty():
    stream = io.StringIO()
    st = ui.status("working...", stream=stream)
    st.start()
    st.update("still working")  # silent off-TTY
    st.log("reviewed PR #123")  # a real event: prints
    st.stop()
    out = stream.getvalue()
    assert "reviewed PR #123" in out
    assert "still working" not in out
    assert "working..." not in out  # start() prints nothing off-TTY
