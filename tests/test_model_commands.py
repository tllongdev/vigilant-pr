"""Tests for the `vigilant model` command handlers and the shared add flow."""

from __future__ import annotations

import os

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


def _feed_prompts(monkeypatch, answers):
    """Stub onboarding._prompt to return successive answers."""
    it = iter(answers)
    monkeypatch.setattr(onboarding, "_prompt", lambda *a, **k: next(it))


def _sandbox_env(monkeypatch):
    """Isolate os.environ writes made by the flow so they don't leak between tests."""
    monkeypatch.setattr(onboarding.os, "environ", dict(os.environ))


def test_add_gateway_flow_static(monkeypatch):
    monkeypatch.setattr(onboarding.sys.stdin, "isatty", lambda: True)
    _sandbox_env(monkeypatch)
    _feed_prompts(monkeypatch, ["deepseek-v4-pro", "https://gw.example.com/v1", "1"])
    monkeypatch.setattr(onboarding.getpass, "getpass", lambda *a, **k: "static-tok")
    monkeypatch.setattr(onboarding, "_verify_key", lambda provider: True)

    assert onboarding.add_gateway_flow() == "gateway/deepseek-v4-pro"
    entry = store.load_store()["providers"]["gateway"]
    assert entry["api_base"] == "https://gw.example.com/v1"
    assert entry["api_key"] == "static-tok"
    assert "oauth_token_url" not in entry
    assert store.get_active_model() == "gateway/deepseek-v4-pro"


def test_add_gateway_flow_oauth(monkeypatch):
    monkeypatch.setattr(onboarding.sys.stdin, "isatty", lambda: True)
    _sandbox_env(monkeypatch)
    # model, base, auth-mode "2", token_url, client_id, scope (blank), audience (blank)
    _feed_prompts(monkeypatch, [
        "foo", "https://gw.example.com/v1", "2",
        "https://auth.example.com/token", "cid", "", "",
    ])
    monkeypatch.setattr(onboarding.getpass, "getpass", lambda *a, **k: "secret")
    monkeypatch.setattr(onboarding, "_yes", lambda *a, **k: False)  # not HTTP Basic
    monkeypatch.setattr(onboarding, "_verify_key", lambda provider: True)

    assert onboarding.add_gateway_flow() == "gateway/foo"
    entry = store.load_store()["providers"]["gateway"]
    assert entry["oauth_token_url"] == "https://auth.example.com/token"
    assert entry["oauth_client_id"] == "cid"
    assert entry["oauth_client_secret"] == "secret"
    assert "api_key" not in entry
    assert "oauth_scope" not in entry  # blank input dropped
    assert "oauth_auth_style" not in entry  # _yes returned False


def test_add_gateway_flow_incomplete_oauth_stores_nothing(monkeypatch):
    monkeypatch.setattr(onboarding.sys.stdin, "isatty", lambda: True)
    _sandbox_env(monkeypatch)
    # OAuth chosen but client id left blank -> nothing stored.
    _feed_prompts(monkeypatch, ["foo", "https://gw/v1", "2", "https://auth/token", ""])
    monkeypatch.setattr(onboarding.getpass, "getpass", lambda *a, **k: "secret")

    assert onboarding.add_gateway_flow() is None
    assert "gateway" not in store.load_store()["providers"]


def test_add_gateway_flow_noninteractive_returns_none(monkeypatch, capsys):
    monkeypatch.setattr(onboarding.sys.stdin, "isatty", lambda: False)
    assert onboarding.add_gateway_flow() is None
    assert "interactive" in capsys.readouterr().err.lower()


def test_add_provider_flow_routes_to_gateway(monkeypatch):
    # preselected "gateway" must dispatch to the gateway flow (off-TTY -> None).
    monkeypatch.setattr(onboarding.sys.stdin, "isatty", lambda: False)
    assert onboarding.add_provider_flow("gateway") is None
