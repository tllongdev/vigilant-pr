"""Model-agnostic inference layer.

Vigilant PR runs against any model reachable over one of two wire protocols:

  - the Anthropic Messages API (Claude), and
  - the OpenAI-compatible /chat/completions API - which Groq, Google Gemini
    (via its OpenAI-compat endpoint), NVIDIA NIM, OpenAI, OpenRouter, Ollama,
    vLLM, LM Studio and TGI all speak.

A model is selected with a `provider/model` string (e.g. `groq/llama-3.3-70b-versatile`,
`anthropic/claude-sonnet-5`, `ollama/qwen2.5:14b`). A bare string with no
provider prefix is treated as Anthropic for backward compatibility, so bare
Anthropic ids like `claude-sonnet-5` / `claude-opus-4-8` keep working unchanged.

Everything here is stdlib-only (urllib) - no litellm, no SDKs - so the engine
stays dependency-free and the container stays small.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from .config import MODEL_PROFILES, Config
from .errors import ReviewFailedError

ANTHROPIC_VERSION = "2023-06-01"
RETRY_WAITS_SECONDS = (5, 10, 20, 45, 90, 180, 360)
DEFAULT_MAX_TOKENS = 8000

# Provider registry. `style` selects the wire protocol; `key_env` names the env
# var holding the API key (None = keyless, e.g. a local Ollama); `base` is the
# default endpoint (override any of them with VIGILANT_API_BASE); `json_mode`
# requests strict JSON output where the provider reliably supports it.
PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {"style": "anthropic", "key_env": "ANTHROPIC_API_KEY",
                  "base": "https://api.anthropic.com", "json_mode": False},
    "openai": {"style": "openai", "key_env": "OPENAI_API_KEY",
               "base": "https://api.openai.com/v1", "json_mode": True},
    "groq": {"style": "openai", "key_env": "GROQ_API_KEY",
             "base": "https://api.groq.com/openai/v1", "json_mode": True},
    "gemini": {"style": "openai", "key_env": "GEMINI_API_KEY",
               "base": "https://generativelanguage.googleapis.com/v1beta/openai", "json_mode": True},
    "nvidia_nim": {"style": "openai", "key_env": "NVIDIA_NIM_API_KEY",
                   "base": "https://integrate.api.nvidia.com/v1", "json_mode": False},
    "openrouter": {"style": "openai", "key_env": "OPENROUTER_API_KEY",
                   "base": "https://openrouter.ai/api/v1", "json_mode": False},
    "ollama": {"style": "openai", "key_env": None,
               "base": "http://localhost:11434/v1", "json_mode": False},
    # Generic OpenAI-compatible server (vLLM, LM Studio, TGI, ...). Requires
    # VIGILANT_API_BASE; key optional via VIGILANT_API_KEY.
    "openai_compatible": {"style": "openai", "key_env": "VIGILANT_API_KEY",
                          "base": None, "json_mode": False},
    "mock": {"style": "mock", "key_env": None, "base": None, "json_mode": False},
}

# Aliases for ergonomics.
_PROVIDER_ALIASES = {
    "openai-compatible": "openai_compatible",
    "nvidia": "nvidia_nim",
    "nim": "nvidia_nim",
    "google": "gemini",
    "claude": "anthropic",
}


# Recommended model string per provider, used for auto-selection and hints.
RECOMMENDED_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "openai/gpt-5.5",
    "groq": "groq/llama-3.3-70b-versatile",
    "gemini": "gemini/gemini-2.5-flash",
    "nvidia_nim": "nvidia_nim/deepseek-ai/deepseek-v3.2-exp",
    "openrouter": "openrouter/meta-llama/llama-3.3-70b-instruct",
}
# Preference order when auto-selecting a model from whatever key is present.
_AUTO_ORDER = ("anthropic", "openai", "groq", "gemini", "nvidia_nim", "openrouter")


def auto_select_model() -> str | None:
    """Pick a model string from whichever provider key is present.

    Returns the recommended `provider/model` for the first provider (in
    preference order) whose key is set, or None if no provider key is found.
    Anthropic wins when present (its bare model name is the built-in default).
    """
    for provider in _AUTO_ORDER:
        if provider_api_key(provider):
            return RECOMMENDED_MODELS[provider]
    return None


def resolve_provider(model: str) -> tuple[str, str]:
    """Split a `provider/model` string into (provider_key, model_name).

    A bare string (no slash) or an unrecognized prefix is treated as Anthropic,
    preserving the original behavior. `nvidia_nim/deepseek-ai/deepseek-v3.2-exp`
    correctly yields provider `nvidia_nim` and model `deepseek-ai/deepseek-v3.2-exp`.
    """
    if model == "mock":
        return "mock", "mock"
    if "/" not in model:
        return "anthropic", model
    prefix, rest = model.split("/", 1)
    key = _PROVIDER_ALIASES.get(prefix, prefix)
    if key in PROVIDERS:
        return key, rest
    # Unknown prefix: assume it is part of an Anthropic-style bare name.
    return "anthropic", model


def provider_api_key(provider: str) -> str | None:
    """Read the API key for `provider` from its configured env var."""
    key_env = PROVIDERS.get(provider, {}).get("key_env")
    return os.environ.get(key_env) if key_env else None


def provider_needs_key(provider: str) -> bool:
    """True if the provider requires an API key (i.e. not keyless/mock)."""
    return bool(PROVIDERS.get(provider, {}).get("key_env"))


def base_url(provider: str, config: Config) -> str | None:
    """Resolve the endpoint base URL, honoring a VIGILANT_API_BASE override."""
    if config.api_base:
        return config.api_base
    return PROVIDERS.get(provider, {}).get("base")


def missing_key_message(provider: str) -> str:
    """A helpful, actionable message when the provider's key is absent."""
    key_env = PROVIDERS.get(provider, {}).get("key_env") or "(none)"
    hints = {
        "anthropic": "Get one at https://console.anthropic.com/settings/keys",
        "groq": "Free, no card: https://console.groq.com/keys (key starts with gsk_)",
        "gemini": "Free tier: https://aistudio.google.com/apikey",
        "nvidia_nim": "Free, no card: https://build.nvidia.com (key starts with nvapi-)",
        "openai": "https://platform.openai.com/api-keys",
        "openrouter": "https://openrouter.ai/keys",
        "openai_compatible": "Set VIGILANT_API_BASE (and VIGILANT_API_KEY if required).",
    }
    hint = hints.get(provider, "")
    return f"{key_env} is not set for provider '{provider}'. {hint}".strip()


def _profile(model_name: str) -> dict[str, Any]:
    from .config import GENERIC_PROFILE

    return MODEL_PROFILES.get(model_name, GENERIC_PROFILE)


def call_anthropic(
    system: str,
    user: str,
    api_key: str,
    model: str,
    retry_waits: tuple[int, ...] = RETRY_WAITS_SECONDS,
) -> str:
    """Call the Anthropic Messages API with the model's reasoning profile.

    Retries on 429 and 5xx using the wait schedule. Honors `retry-after` when
    supplied. 4xx errors other than 429 are not retried. Returns concatenated
    text content blocks.
    """
    profile = _profile(model)
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": 16000,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if profile.get("thinking"):
        payload["thinking"] = profile["thinking"]
    if profile.get("output_config"):
        payload["output_config"] = profile["output_config"]
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    url = "https://api.anthropic.com/v1/messages"
    data = _post_with_retry(url, payload, headers, retry_waits, "Anthropic")
    text_blocks = [
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ]
    return "".join(text_blocks).strip()


def build_openai_payload(
    system: str,
    user: str,
    model: str,
    json_mode: bool,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    """Build an OpenAI-compatible /chat/completions request body (pure, testable)."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def call_openai_compatible(
    system: str,
    user: str,
    api_key: str | None,
    model: str,
    base: str,
    json_mode: bool,
    temperature: float,
    max_tokens: int,
    provider_label: str = "model",
    retry_waits: tuple[int, ...] = RETRY_WAITS_SECONDS,
) -> str:
    """Call any OpenAI-compatible /chat/completions endpoint. Returns the message content."""
    payload = build_openai_payload(system, user, model, json_mode, temperature, max_tokens)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = base.rstrip("/") + "/chat/completions"
    data = _post_with_retry(url, payload, headers, retry_waits, provider_label)
    try:
        choices = data.get("choices", [])
        content = choices[0]["message"]["content"] if choices else ""
    except (KeyError, IndexError, TypeError) as e:
        raise ReviewFailedError(f"{provider_label}: unexpected response shape: {e}") from e
    return (content or "").strip()


def call_mock(system: str, user: str) -> str:
    """Scripted, keyless output so the whole pipeline can be exercised for free."""
    return json.dumps({
        "summary": "Mock review: no model was called. Set a real VIGILANT_MODEL "
                   "(e.g. groq/llama-3.3-70b-versatile) and provider key to review for real.",
        "tally": {"critical": 0, "medium": 0, "nit": 0},
        "findings": [],
        "thread_responses": [],
        "skipped": ["Everything - this is mock output with no model."],
    })


def call_model(system: str, user: str, config: Config) -> str:
    """Dispatch to the right provider based on `config.model`.

    Resolves the provider, key, and base URL, then calls the matching wire
    protocol. Raises ReviewFailedError (exit 2) on inference failure.
    """
    provider, model_name = resolve_provider(config.model)
    style = PROVIDERS.get(provider, {}).get("style", "anthropic")

    if style == "mock":
        return call_mock(system, user)

    api_key = provider_api_key(provider)
    if style == "anthropic":
        if not api_key:
            raise ReviewFailedError(missing_key_message(provider), exit_code=1)
        return call_anthropic(system, user, api_key, model_name)

    base = base_url(provider, config)
    if not base:
        raise ReviewFailedError(
            f"No API base for provider '{provider}'. Set VIGILANT_API_BASE.", exit_code=1
        )
    if provider_needs_key(provider) and not api_key:
        raise ReviewFailedError(missing_key_message(provider), exit_code=1)
    json_mode = bool(PROVIDERS.get(provider, {}).get("json_mode"))
    return call_openai_compatible(
        system, user, api_key, model_name, base, json_mode,
        config.temperature, config.max_tokens, provider_label=provider,
    )


def _post_with_retry(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    retry_waits: tuple[int, ...],
    label: str,
) -> dict[str, Any]:
    """POST JSON with retry/backoff on 429 and 5xx; raise ReviewFailedError otherwise."""
    max_attempts = 1 + len(retry_waits)
    body = json.dumps(payload).encode("utf-8")
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                parsed: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
                return parsed
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {e.code}: {err_body[:400]}"
            retryable = e.code == 429 or 500 <= e.code < 600
            if not retryable or attempt == max_attempts:
                sys.stderr.write(f"{label} API error {last_error}\n")
                raise ReviewFailedError(
                    f"{label} API error after {attempt} attempt(s): HTTP {e.code}"
                ) from e
            retry_after = e.headers.get("retry-after") if e.headers else None
            wait = float(retry_after) if retry_after and retry_after.isdigit() \
                else retry_waits[attempt - 1] + random.uniform(0, 2)
            sys.stderr.write(
                f"{label} returned {e.code} (attempt {attempt}/{max_attempts}); "
                f"retrying in {wait:.1f}s...\n"
            )
            time.sleep(wait)
        except urllib.error.URLError as e:
            last_error = f"connection error: {e}"
            if attempt == max_attempts:
                sys.stderr.write(f"{label} API {last_error}\n")
                raise ReviewFailedError(
                    f"{label} API connection error after {attempt} attempt(s): {e}"
                ) from e
            wait = retry_waits[attempt - 1] + random.uniform(0, 2)
            sys.stderr.write(
                f"{label} connection error (attempt {attempt}/{max_attempts}); "
                f"retrying in {wait:.1f}s: {e}\n"
            )
            time.sleep(wait)

    raise ReviewFailedError(f"{label} API exhausted {max_attempts} attempts. Last: {last_error}")


def list_models(provider: str, config: Config) -> list[str]:
    """Best-effort: ask the provider which models the credentials can reach.

    Returns model ids, or an empty list if the provider has no list endpoint,
    the key is missing, or the call fails. Never raises.
    """
    style = PROVIDERS.get(provider, {}).get("style")
    api_key = provider_api_key(provider)
    try:
        if style == "anthropic":
            if not api_key:
                return []
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        if style == "openai":
            base = base_url(provider, config)
            if not base or (provider_needs_key(provider) and not api_key):
                return []
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            req = urllib.request.Request(base.rstrip("/") + "/models", headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError):
        return []
    return []
