"""Tests for the managed credential store."""

from __future__ import annotations

import os
import sys

import pytest

from vigilant import store


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGILANT_CONFIG_DIR", str(tmp_path / "cfg"))
    # Make sure no real provider keys/model leak in from the environment.
    for var in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "VIGILANT_MODEL"):
        monkeypatch.delenv(var, raising=False)
    yield


def test_load_store_missing_returns_empty():
    data = store.load_store()
    assert data == {"active_model": None, "providers": {}}


def test_set_provider_key_round_trips_and_sets_active():
    store.set_provider_key("groq", "gsk_secret", model="groq/llama-3.3-70b-versatile")
    data = store.load_store()
    assert data["providers"]["groq"]["api_key"] == "gsk_secret"
    assert data["active_model"] == "groq/llama-3.3-70b-versatile"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_saved_file_is_0600():
    store.set_provider_key("groq", "gsk_secret", model="groq/llama-3.3-70b-versatile")
    mode = store.credentials_path().stat().st_mode & 0o777
    assert mode == 0o600


def test_apply_store_to_env_fills_missing():
    store.set_provider_key("groq", "gsk_secret", model="groq/llama-3.3-70b-versatile")
    store.apply_store_to_env()
    assert os.environ["GROQ_API_KEY"] == "gsk_secret"
    assert os.environ["VIGILANT_MODEL"] == "groq/llama-3.3-70b-versatile"


def test_apply_store_to_env_does_not_override_real_env(monkeypatch):
    store.set_provider_key("groq", "gsk_stored", model="groq/llama-3.3-70b-versatile")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real")
    monkeypatch.setenv("VIGILANT_MODEL", "anthropic/claude-sonnet-5")
    store.apply_store_to_env()
    assert os.environ["GROQ_API_KEY"] == "gsk_real"
    assert os.environ["VIGILANT_MODEL"] == "anthropic/claude-sonnet-5"


def test_set_gateway_config_static_round_trips():
    store.set_gateway_config(
        "gateway/deepseek-v4-pro",
        {"api_base": "https://gw.example.com/v1", "api_key": "static-tok"},
    )
    data = store.load_store()
    entry = data["providers"]["gateway"]
    assert entry["model"] == "gateway/deepseek-v4-pro"
    assert entry["api_base"] == "https://gw.example.com/v1"
    assert entry["api_key"] == "static-tok"
    assert data["active_model"] == "gateway/deepseek-v4-pro"


def test_set_gateway_config_drops_empty_fields():
    store.set_gateway_config(
        "gateway/foo",
        {"api_base": "https://gw/v1", "api_key": "t", "oauth_scope": ""},
    )
    entry = store.load_store()["providers"]["gateway"]
    assert "oauth_scope" not in entry


def test_apply_store_to_env_fills_gateway_static(monkeypatch):
    for var in store.GATEWAY_ENV_FIELDS.values():
        monkeypatch.delenv(var, raising=False)
    store.set_gateway_config(
        "gateway/foo",
        {"api_base": "https://gw.example.com/v1", "api_key": "static-tok"},
    )
    store.apply_store_to_env()
    assert os.environ["VIGILANT_API_BASE"] == "https://gw.example.com/v1"
    assert os.environ["VIGILANT_API_KEY"] == "static-tok"
    assert os.environ["VIGILANT_MODEL"] == "gateway/foo"


def test_apply_store_to_env_fills_gateway_oauth(monkeypatch):
    for var in store.GATEWAY_ENV_FIELDS.values():
        monkeypatch.delenv(var, raising=False)
    store.set_gateway_config(
        "gateway/foo",
        {
            "api_base": "https://gw.example.com/v1",
            "oauth_token_url": "https://auth.example.com/token",
            "oauth_client_id": "cid",
            "oauth_client_secret": "secret",
            "oauth_scope": "models:read",
            "oauth_auth_style": "basic",
        },
    )
    store.apply_store_to_env()
    assert os.environ["VIGILANT_OAUTH_TOKEN_URL"] == "https://auth.example.com/token"
    assert os.environ["VIGILANT_OAUTH_CLIENT_ID"] == "cid"
    assert os.environ["VIGILANT_OAUTH_CLIENT_SECRET"] == "secret"
    assert os.environ["VIGILANT_OAUTH_SCOPE"] == "models:read"
    assert os.environ["VIGILANT_OAUTH_AUTH_STYLE"] == "basic"


def test_apply_store_to_env_gateway_does_not_override_real_env(monkeypatch):
    for var in store.GATEWAY_ENV_FIELDS.values():
        monkeypatch.delenv(var, raising=False)
    store.set_gateway_config(
        "gateway/foo", {"api_base": "https://stored/v1", "api_key": "stored-tok"}
    )
    monkeypatch.setenv("VIGILANT_API_BASE", "https://real/v1")
    store.apply_store_to_env()
    assert os.environ["VIGILANT_API_BASE"] == "https://real/v1"
    # A field not set in the real env is still filled from the store.
    assert os.environ["VIGILANT_API_KEY"] == "stored-tok"


def test_remove_provider_repoints_active():
    store.set_provider_key("groq", "gsk_1", model="groq/llama-3.3-70b-versatile")
    store.set_provider_key("anthropic", "sk-ant-1", model="anthropic/claude-sonnet-5")
    store.set_active_model("groq/llama-3.3-70b-versatile")

    assert store.remove_provider("groq") is True
    data = store.load_store()
    assert "groq" not in data["providers"]
    # active repointed to the remaining provider's model
    assert data["active_model"] == "anthropic/claude-sonnet-5"


def test_remove_last_provider_clears_active():
    store.set_provider_key("groq", "gsk_1", model="groq/llama-3.3-70b-versatile")
    assert store.remove_provider("groq") is True
    assert store.get_active_model() is None


def test_remove_missing_provider_is_false():
    assert store.remove_provider("groq") is False


def test_mask_key():
    assert store.mask_key("gsk_abcdef1234") == "gsk_...1234"
    assert store.mask_key("short") == "****"
