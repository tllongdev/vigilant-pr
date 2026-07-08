"""Unit tests for the onboarding-friction fixes: `.env` loading and the
GitHub preflight check. Both call the real code directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vigilant.engine import util
from vigilant.engine.config import load_dotenv


def test_load_dotenv_sets_missing_and_respects_real_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "GROQ_API_KEY=gsk_from_file",
                'VIGILANT_MODEL="groq/llama-3.3-70b-versatile"',
                "export GH_TOKEN='ghp_exported'",
                "ANTHROPIC_API_KEY=should_not_win",
                "MALFORMED_NO_EQUALS",
            ]
        )
    )
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("VIGILANT_MODEL", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real_env_wins")

    assert load_dotenv(env) is True

    import os

    assert os.environ["GROQ_API_KEY"] == "gsk_from_file"
    assert os.environ["VIGILANT_MODEL"] == "groq/llama-3.3-70b-versatile"
    assert os.environ["GH_TOKEN"] == "ghp_exported"
    # real env var is never overwritten by the file
    assert os.environ["ANTHROPIC_API_KEY"] == "real_env_wins"


def test_load_dotenv_missing_file_returns_false(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path / "nope.env") is False


def test_github_preflight_ok_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    monkeypatch.setattr(util.shutil, "which", lambda _: None)
    assert util.github_preflight() is None


def test_github_preflight_missing_gh_and_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(util.shutil, "which", lambda _: None)
    msg = util.github_preflight()
    assert msg is not None
    assert "gh auth login" in msg


def test_github_preflight_gh_present_not_authed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(util.shutil, "which", lambda _: "/usr/bin/gh")

    class _Result:
        returncode = 1

    monkeypatch.setattr(util.subprocess, "run", lambda *a, **k: _Result())
    msg = util.github_preflight()
    assert msg is not None
    assert "not authenticated" in msg


def test_github_preflight_gh_present_authed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(util.shutil, "which", lambda _: "/usr/bin/gh")

    class _Result:
        returncode = 0

    monkeypatch.setattr(util.subprocess, "run", lambda *a, **k: _Result())
    assert util.github_preflight() is None
