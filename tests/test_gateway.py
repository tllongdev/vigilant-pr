# Copyright 2026 Timothy Long / Longitudinal Intelligence Technologies (LIT)
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the `gateway` provider and its OAuth2 client-credentials auth.

The token exchange is stdlib urllib, so the network layer is faked by
monkeypatching urllib.request.urlopen - no real requests are made.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import pytest

from vigilant.engine import providers
from vigilant.engine.config import Config

_GATEWAY_ENV = (
    "VIGILANT_API_KEY",
    "VIGILANT_OAUTH_TOKEN_URL",
    "VIGILANT_OAUTH_CLIENT_ID",
    "VIGILANT_OAUTH_CLIENT_SECRET",
    "VIGILANT_OAUTH_SCOPE",
    "VIGILANT_OAUTH_AUDIENCE",
    "VIGILANT_OAUTH_AUTH_STYLE",
)


def _clear_gateway_env(mp: pytest.MonkeyPatch) -> None:
    for key in _GATEWAY_ENV:
        mp.delenv(key, raising=False)
    providers._OAUTH_TOKEN_CACHE.clear()


class _FakeResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _fake_urlopen(responses: list[dict[str, Any]], calls: list[Any]):  # type: ignore[no-untyped-def]
    """Return a urlopen stub that yields each payload in turn and records requests."""

    def _open(req: Any, timeout: float | None = None) -> _FakeResp:
        calls.append(req)
        payload = responses[min(len(calls) - 1, len(responses) - 1)]
        return _FakeResp(payload)

    return _open


# --- resolution --------------------------------------------------------------

def test_resolve_gateway_provider() -> None:
    assert providers.resolve_provider("gateway/deepseek-v4-pro") == ("gateway", "deepseek-v4-pro")


def test_gateway_is_openai_style() -> None:
    assert providers.PROVIDERS["gateway"]["style"] == "openai"


# --- gateway_auth_configured (no network) ------------------------------------

def test_gateway_auth_configured_static_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_API_KEY", "static-tok")
    assert providers.gateway_auth_configured() is True


def test_gateway_auth_configured_oauth_triple(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_OAUTH_TOKEN_URL", "https://auth.example.com/token")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_SECRET", "secret")
    assert providers.gateway_auth_configured() is True


def test_gateway_auth_not_configured_when_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_OAUTH_TOKEN_URL", "https://auth.example.com/token")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_ID", "cid")  # secret missing
    assert providers.gateway_auth_configured() is False


def test_gateway_auth_not_configured_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    assert providers.gateway_auth_configured() is False


# --- model_key_missing -------------------------------------------------------

def test_model_key_missing_none_for_gateway_static(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_API_KEY", "static-tok")
    assert providers.model_key_missing(Config(model="gateway/foo")) is None


def test_model_key_missing_none_for_gateway_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_OAUTH_TOKEN_URL", "https://auth.example.com/token")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_SECRET", "secret")
    assert providers.model_key_missing(Config(model="gateway/foo")) is None


def test_model_key_missing_message_for_unconfigured_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    msg = providers.model_key_missing(Config(model="gateway/foo"))
    assert msg is not None
    assert "VIGILANT_OAUTH_TOKEN_URL" in msg


# --- get_gateway_bearer: static fallback -------------------------------------

def test_get_gateway_bearer_static(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_API_KEY", "static-tok")
    assert providers.get_gateway_bearer(Config(model="gateway/foo")) == "static-tok"


def test_get_gateway_bearer_none_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    assert providers.get_gateway_bearer(Config(model="gateway/foo")) is None


def test_get_gateway_bearer_oauth_partial_falls_back_to_static(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_OAUTH_TOKEN_URL", "https://auth.example.com/token")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_ID", "cid")  # no secret
    monkeypatch.setenv("VIGILANT_API_KEY", "static-tok")
    assert providers.get_gateway_bearer(Config(model="gateway/foo")) == "static-tok"


# --- get_gateway_bearer: OAuth2 client-credentials ---------------------------

def test_get_gateway_bearer_oauth_fetch_and_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_OAUTH_TOKEN_URL", "https://auth.example.com/token")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("VIGILANT_OAUTH_SCOPE", "models:read")

    calls: list[Any] = []
    responses = [
        {"access_token": "tok-1", "expires_in": 3600},
        {"access_token": "tok-2", "expires_in": 3600},
    ]
    monkeypatch.setattr(providers.urllib.request, "urlopen", _fake_urlopen(responses, calls))

    cfg = Config(model="gateway/foo")
    # First call mints a token.
    assert providers.get_gateway_bearer(cfg) == "tok-1"
    assert len(calls) == 1
    # Body carries the grant type, credentials, and scope.
    body = urllib.parse.parse_qs(calls[0].data.decode("utf-8"))
    assert body["grant_type"] == ["client_credentials"]
    assert body["client_id"] == ["cid"]
    assert body["client_secret"] == ["secret"]
    assert body["scope"] == ["models:read"]
    # Second call returns the cached token (no new request).
    assert providers.get_gateway_bearer(cfg) == "tok-1"
    assert len(calls) == 1


def test_get_gateway_bearer_refreshes_when_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_OAUTH_TOKEN_URL", "https://auth.example.com/token")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_SECRET", "secret")

    calls: list[Any] = []
    responses = [
        {"access_token": "tok-1", "expires_in": 3600},
        {"access_token": "tok-2", "expires_in": 3600},
    ]
    monkeypatch.setattr(providers.urllib.request, "urlopen", _fake_urlopen(responses, calls))

    cfg = Config(model="gateway/foo")
    assert providers.get_gateway_bearer(cfg) == "tok-1"
    # Force the cached token to look expired; next call must refetch.
    key = ("https://auth.example.com/token", "cid")
    tok, _expiry = providers._OAUTH_TOKEN_CACHE[key]
    providers._OAUTH_TOKEN_CACHE[key] = (tok, 0.0)
    assert providers.get_gateway_bearer(cfg) == "tok-2"
    assert len(calls) == 2


def test_get_gateway_bearer_basic_auth_style(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_OAUTH_TOKEN_URL", "https://auth.example.com/token")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("VIGILANT_OAUTH_AUTH_STYLE", "basic")

    calls: list[Any] = []
    monkeypatch.setattr(
        providers.urllib.request,
        "urlopen",
        _fake_urlopen([{"access_token": "tok", "expires_in": 60}], calls),
    )

    assert providers.get_gateway_bearer(Config(model="gateway/foo")) == "tok"
    auth = calls[0].get_header("Authorization")
    assert auth is not None
    assert auth.startswith("Basic ")
    # Body must not carry the secret when using Basic auth.
    body = urllib.parse.parse_qs(calls[0].data.decode("utf-8"))
    assert "client_secret" not in body


def test_get_gateway_bearer_raises_on_missing_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_OAUTH_TOKEN_URL", "https://auth.example.com/token")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("VIGILANT_OAUTH_CLIENT_SECRET", "secret")

    calls: list[Any] = []
    monkeypatch.setattr(
        providers.urllib.request, "urlopen", _fake_urlopen([{"error": "nope"}], calls)
    )
    with pytest.raises(providers.ReviewFailedError):
        providers.get_gateway_bearer(Config(model="gateway/foo"))


# --- call_model dispatch -----------------------------------------------------

def _fake_openai(captured: dict[str, Any]):  # type: ignore[no-untyped-def]
    def _fn(system, user, api_key, model, base, json_mode, temperature,  # type: ignore[no-untyped-def]
            max_tokens, provider_label="model", retry_waits=()):
        captured.update(api_key=api_key, model=model, base=base, label=provider_label)
        return "{}"

    return _fn


def test_call_model_gateway_static_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_API_KEY", "static-tok")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(providers, "call_openai_compatible", _fake_openai(captured))

    cfg = Config(model="gateway/deepseek-v4-pro", api_base="https://gw.example.com/v1")
    assert providers.call_model("sys", "usr", cfg) == "{}"
    assert captured["api_key"] == "static-tok"
    assert captured["model"] == "deepseek-v4-pro"
    assert captured["base"] == "https://gw.example.com/v1"
    assert captured["label"] == "gateway"


def test_call_model_gateway_oauth_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setattr(providers, "get_gateway_bearer", lambda config: "oauth-tok")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(providers, "call_openai_compatible", _fake_openai(captured))

    cfg = Config(model="gateway/foo", api_base="https://gw.example.com/v1")
    assert providers.call_model("sys", "usr", cfg) == "{}"
    assert captured["api_key"] == "oauth-tok"


def test_call_model_gateway_no_base_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("VIGILANT_API_KEY", "static-tok")
    cfg = Config(model="gateway/foo")  # no api_base
    with pytest.raises(providers.ReviewFailedError) as exc:
        providers.call_model("sys", "usr", cfg)
    assert exc.value.exit_code == 1


def test_call_model_gateway_unconfigured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    cfg = Config(model="gateway/foo", api_base="https://gw.example.com/v1")
    with pytest.raises(providers.ReviewFailedError) as exc:
        providers.call_model("sys", "usr", cfg)
    assert exc.value.exit_code == 1
