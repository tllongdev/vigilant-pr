# Copyright 2026 Timothy Long / LongIntel
# SPDX-License-Identifier: Apache-2.0
"""Managed credential store for Vigilant PR (stdlib only).

Lets a user save one or more provider API keys and switch between them without
hand-editing a `.env`. Keys live in a `0600` JSON file under the user's config
dir (default `~/.config/vigilant-pr/credentials.json`), the same plaintext-at-rest
posture as the `gh` and `aws` CLIs.

At startup the CLI calls `apply_store_to_env()`, which fills in any provider key
env var and `VIGILANT_MODEL` that are *not already set*. Combined with the
existing `load_dotenv()`, the resolution order is:

    real environment variable  >  .env  >  credential store

so an explicit env var or `.env` value always wins over the store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .engine.providers import PROVIDERS, resolve_provider


def config_dir() -> Path:
    """Directory holding Vigilant PR's managed config.

    Honors `VIGILANT_CONFIG_DIR`, then `XDG_CONFIG_HOME`, else `~/.config`.
    """
    override = os.environ.get("VIGILANT_CONFIG_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "vigilant-pr"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def load_store() -> dict[str, Any]:
    """Read the credential store, returning an empty structure if absent/corrupt."""
    path = credentials_path()
    if not path.exists():
        return {"active_model": None, "providers": {}}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"active_model": None, "providers": {}}
    if not isinstance(data, dict):
        return {"active_model": None, "providers": {}}
    data.setdefault("active_model", None)
    providers = data.get("providers")
    if not isinstance(providers, dict):
        data["providers"] = {}
    return data


def save_store(data: dict[str, Any]) -> None:
    """Write the store atomically with locked-down permissions (dir 0700, file 0600)."""
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass  # best-effort on platforms without POSIX perms
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def set_provider_key(
    provider: str, api_key: str, model: str | None = None, make_active: bool = True
) -> None:
    """Store `api_key` for `provider` (optionally its default model) and save.

    When `make_active` and a model is known, this model becomes the active one.
    """
    store = load_store()
    providers = store.setdefault("providers", {})
    entry = providers.setdefault(provider, {})
    entry["api_key"] = api_key
    if model:
        entry["model"] = model
    chosen = model or entry.get("model")
    if make_active and chosen:
        store["active_model"] = chosen
    save_store(store)


def remove_provider(provider: str) -> bool:
    """Delete a stored provider key. Returns True if something was removed.

    If the active model belonged to the removed provider, it is repointed to any
    other stored provider's model, or cleared if none remain.
    """
    store = load_store()
    providers = store.get("providers", {})
    if provider not in providers:
        return False
    providers.pop(provider)
    active = store.get("active_model")
    if active and resolve_provider(active)[0] == provider:
        fallback = next(
            (info.get("model") for info in providers.values() if info.get("model")), None
        )
        if fallback:
            store["active_model"] = fallback
        else:
            store["active_model"] = None
    save_store(store)
    return True


def set_active_model(model: str) -> None:
    store = load_store()
    store["active_model"] = model
    save_store(store)


def get_active_model() -> str | None:
    return load_store().get("active_model")


def list_stored() -> dict[str, dict[str, Any]]:
    """Return the stored providers mapping (provider -> {api_key, model})."""
    providers = load_store().get("providers", {})
    return providers if isinstance(providers, dict) else {}


def apply_store_to_env() -> None:
    """Fill provider key env vars and VIGILANT_MODEL from the store if unset.

    Only sets values that are missing, so real env vars and `.env` always win.
    """
    store = load_store()
    for provider, info in store.get("providers", {}).items():
        if not isinstance(info, dict):
            continue
        key_env = PROVIDERS.get(provider, {}).get("key_env")
        api_key = info.get("api_key")
        if key_env and api_key and key_env not in os.environ:
            os.environ[key_env] = api_key
    active = store.get("active_model")
    if active and "VIGILANT_MODEL" not in os.environ:
        os.environ["VIGILANT_MODEL"] = active


def mask_key(api_key: str) -> str:
    """Mask a secret for display, keeping only enough to recognize it."""
    if len(api_key) <= 8:
        return "****"
    return f"{api_key[:4]}...{api_key[-4:]}"
