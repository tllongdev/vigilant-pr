"""Tests for the .env upsert and provider catalog used by `vigilant init`."""

from __future__ import annotations

from pathlib import Path

from vigilant.onboarding import PROVIDER_CATALOG, upsert_env_file


def test_upsert_creates_file_when_absent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    upsert_env_file(env, {"VIGILANT_MODEL": "groq/llama-3.3-70b-versatile"})
    assert env.read_text() == "VIGILANT_MODEL=groq/llama-3.3-70b-versatile\n"


def test_upsert_replaces_existing_assignment(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("VIGILANT_MODEL=old\nGH_TOKEN=abc\n")
    upsert_env_file(env, {"VIGILANT_MODEL": "new"})
    text = env.read_text()
    assert "VIGILANT_MODEL=new" in text
    assert "VIGILANT_MODEL=old" not in text
    assert "GH_TOKEN=abc" in text  # untouched


def test_upsert_uncomments_template_line(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# GROQ_API_KEY=\n# other comment\n")
    upsert_env_file(env, {"GROQ_API_KEY": "gsk_x"})
    text = env.read_text()
    assert "GROQ_API_KEY=gsk_x" in text
    assert "# GROQ_API_KEY=" not in text
    assert "# other comment" in text


def test_upsert_appends_unseen_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\n")
    upsert_env_file(env, {"NEWKEY": "v"})
    text = env.read_text()
    assert "EXISTING=1" in text
    assert "NEWKEY=v" in text


def test_upsert_handles_export_prefix(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("export VIGILANT_MODEL=old\n")
    upsert_env_file(env, {"VIGILANT_MODEL": "new"})
    text = env.read_text()
    assert "VIGILANT_MODEL=new" in text
    assert "old" not in text


def test_provider_catalog_free_options_first() -> None:
    # The onboarding menu should lead with free, no-card providers.
    assert PROVIDER_CATALOG[0].free
    assert PROVIDER_CATALOG[0].key == "groq"
    keys = {pc.key for pc in PROVIDER_CATALOG}
    assert {"groq", "gemini", "nvidia_nim", "anthropic"} <= keys
