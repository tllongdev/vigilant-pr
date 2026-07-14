"""Unit tests for the model-agnostic provider layer.

Pure logic (provider resolution, key mapping, payload building, mock output) is
tested directly; no network calls are made.
"""

from __future__ import annotations

import json

import pytest

from vigilant.engine import providers
from vigilant.engine.config import Config


def test_resolve_provider_bare_name_is_anthropic() -> None:
    assert providers.resolve_provider("claude-sonnet-4-6") == ("anthropic", "claude-sonnet-4-6")


def test_resolve_provider_prefixed() -> None:
    assert providers.resolve_provider("groq/llama-3.3-70b-versatile") == (
        "groq", "llama-3.3-70b-versatile",
    )


def test_resolve_provider_nvidia_keeps_nested_model_path() -> None:
    # nvidia_nim model names themselves contain a slash - only the first splits.
    assert providers.resolve_provider("nvidia_nim/deepseek-ai/deepseek-v3.2-exp") == (
        "nvidia_nim", "deepseek-ai/deepseek-v3.2-exp",
    )


def test_resolve_provider_alias() -> None:
    assert providers.resolve_provider("google/gemini-2.5-flash")[0] == "gemini"
    assert providers.resolve_provider("openai-compatible/foo")[0] == "openai_compatible"


def test_resolve_grok_alias_maps_to_xai() -> None:
    # "grok" (xAI's model) is an alias for the xai provider; distinct from "groq".
    assert providers.resolve_provider("grok/grok-4.5") == ("xai", "grok-4.5")
    assert providers.resolve_provider("xai/grok-4.5") == ("xai", "grok-4.5")
    assert providers.PROVIDERS["xai"]["key_env"] == "XAI_API_KEY"


def test_resolve_provider_mock() -> None:
    assert providers.resolve_provider("mock") == ("mock", "mock")


def test_resolve_provider_unknown_prefix_falls_back_to_anthropic() -> None:
    # Unknown prefix is treated as a bare Anthropic name (no crash).
    assert providers.resolve_provider("weird-model-name")[0] == "anthropic"


def test_provider_api_key_reads_correct_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert providers.provider_api_key("groq") == "gsk_test"
    assert providers.provider_api_key("anthropic") is None


def test_provider_needs_key() -> None:
    assert providers.provider_needs_key("groq") is True
    assert providers.provider_needs_key("ollama") is False
    assert providers.provider_needs_key("mock") is False


def _clear_all_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for spec in providers.PROVIDERS.values():
        key_env = spec.get("key_env")
        if key_env:
            monkeypatch.delenv(key_env, raising=False)


def test_auto_select_model_prefers_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    assert providers.auto_select_model() == providers.RECOMMENDED_MODELS["anthropic"]


def test_auto_select_model_falls_back_to_present_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    assert providers.auto_select_model() == providers.RECOMMENDED_MODELS["groq"]


def test_auto_select_model_none_when_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_keys(monkeypatch)
    assert providers.auto_select_model() is None


def test_model_key_missing_none_for_mock() -> None:
    assert providers.model_key_missing(Config(model="mock")) is None


def test_model_key_missing_none_for_keyless_ollama() -> None:
    assert providers.model_key_missing(Config(model="ollama/qwen2.5:14b")) is None


def test_model_key_missing_none_when_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    assert providers.model_key_missing(Config(model="groq/llama-3.3-70b-versatile")) is None


def test_model_key_missing_message_when_key_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_keys(monkeypatch)
    msg = providers.model_key_missing(Config(model="groq/llama-3.3-70b-versatile"))
    assert msg is not None
    assert "GROQ_API_KEY" in msg


def test_base_url_override_wins() -> None:
    cfg = Config(api_base="http://localhost:1234/v1")
    assert providers.base_url("ollama", cfg) == "http://localhost:1234/v1"


def test_base_url_default() -> None:
    assert providers.base_url("groq", Config()) == "https://api.groq.com/openai/v1"


def test_missing_key_message_names_env_var() -> None:
    msg = providers.missing_key_message("groq")
    assert "GROQ_API_KEY" in msg
    assert "groq" in msg


def test_build_openai_payload_json_mode() -> None:
    p = providers.build_openai_payload("sys", "usr", "m", True, 0.2, 8000)
    assert p["model"] == "m"
    assert p["messages"][0] == {"role": "system", "content": "sys"}
    assert p["messages"][1] == {"role": "user", "content": "usr"}
    assert p["temperature"] == 0.2
    assert p["max_tokens"] == 8000
    assert p["response_format"] == {"type": "json_object"}


def test_build_openai_payload_no_json_mode_omits_response_format() -> None:
    p = providers.build_openai_payload("s", "u", "m", False, 0.5, 1000)
    assert "response_format" not in p


def test_mock_returns_valid_review_json() -> None:
    out = providers.call_mock("sys", "usr")
    data = json.loads(out)
    assert data["findings"] == []
    assert "tally" in data


def test_call_model_mock_path() -> None:
    cfg = Config(model="mock")
    out = providers.call_model("sys", "usr", cfg)
    assert json.loads(out)["tally"] == {"critical": 0, "medium": 0, "nit": 0}


def test_call_model_missing_key_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    cfg = Config(model="groq/llama-3.3-70b-versatile")
    with pytest.raises(providers.ReviewFailedError) as exc:
        providers.call_model("sys", "usr", cfg)
    assert exc.value.exit_code == 1


def test_call_model_dispatches_to_openai_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_openai(system, user, api_key, model, base, json_mode, temperature,
                    max_tokens, provider_label="model", retry_waits=()):  # type: ignore[no-untyped-def]
        captured.update(
            api_key=api_key, model=model, base=base, json_mode=json_mode, label=provider_label
        )
        return "{}"

    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    monkeypatch.setattr(providers, "call_openai_compatible", fake_openai)
    cfg = Config(model="groq/llama-3.3-70b-versatile")
    assert providers.call_model("sys", "usr", cfg) == "{}"
    assert captured["model"] == "llama-3.3-70b-versatile"
    assert captured["base"] == "https://api.groq.com/openai/v1"
    assert captured["api_key"] == "gsk_x"
    assert captured["json_mode"] is True
    assert captured["label"] == "groq"


def test_call_model_anthropic_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_anthropic(system, user, api_key, model, retry_waits=()):  # type: ignore[no-untyped-def]
        captured.update(api_key=api_key, model=model)
        return "{}"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setattr(providers, "call_anthropic", fake_anthropic)
    cfg = Config(model="claude-sonnet-4-6")
    assert providers.call_model("sys", "usr", cfg) == "{}"
    assert captured["model"] == "claude-sonnet-4-6"
    assert captured["api_key"] == "sk-ant-x"
