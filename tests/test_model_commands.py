"""Tests for the `vigilant model` command handlers and the shared add flow."""

from __future__ import annotations

import pytest

from vigilant import cli, onboarding, store


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGILANT_CONFIG_DIR", str(tmp_path / "cfg"))
    for var in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "VIGILANT_MODEL"):
        monkeypatch.delenv(var, raising=False)
    yield


def test_model_list_empty(capsys):
    assert cli.run_model_list() == 0
    assert "No stored model providers" in capsys.readouterr().out


def test_model_list_shows_masked_key_and_active(capsys):
    store.set_provider_key("groq", "gsk_secretvalue", model="groq/llama-3.3-70b-versatile")
    assert cli.run_model_list() == 0
    out = capsys.readouterr().out
    assert "groq" in out
    assert "gsk_...alue" in out  # masked, not the full key
    assert "gsk_secretvalue" not in out
    assert "* " in out  # active marker


def test_model_use_by_provider_id_and_full_string(capsys):
    store.set_provider_key("groq", "gsk_1", model="groq/llama-3.3-70b-versatile")
    store.set_provider_key("anthropic", "sk-ant-1", model="anthropic/claude-sonnet-5")

    assert cli.run_model_use("groq") == 0
    assert store.get_active_model() == "groq/llama-3.3-70b-versatile"

    assert cli.run_model_use("anthropic/claude-opus-4-8") == 0
    assert store.get_active_model() == "anthropic/claude-opus-4-8"


def test_model_remove(capsys):
    store.set_provider_key("groq", "gsk_1", model="groq/llama-3.3-70b-versatile")
    assert cli.run_model_remove("groq") == 0
    assert cli.run_model_remove("groq") == 1  # already gone


def test_add_provider_flow_uses_env_key_noninteractive(monkeypatch):
    # Off-TTY (pytest) with the provider key already in the environment: the flow
    # should store it without prompting. Stub verification so no network call runs.
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env_key_1234")
    monkeypatch.setattr(onboarding, "_verify_key", lambda provider: True)

    model = onboarding.add_provider_flow("groq")
    assert model == "groq/llama-3.3-70b-versatile"
    assert store.load_store()["providers"]["groq"]["api_key"] == "gsk_env_key_1234"
    assert store.get_active_model() == "groq/llama-3.3-70b-versatile"


def test_add_provider_flow_unknown_provider(capsys):
    assert onboarding.add_provider_flow("nope") is None
    assert "Unknown provider" in capsys.readouterr().err
