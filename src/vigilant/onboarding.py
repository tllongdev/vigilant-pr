# Copyright 2026 Timothy Long / LongIntel
# SPDX-License-Identifier: Apache-2.0
"""Interactive first-run setup (`vigilant init`).

Goal: get a brand-new user from "just installed" to "reviewing PRs" in under a
minute, without reading the README. It walks through GitHub access, picks a model
provider (highlighting the free, no-credit-card options), validates the key, and
writes everything to a local `.env` that every command auto-loads.

Kept dependency-free. All the interactive I/O funnels through small helpers so the
pure logic (`.env` upsert, provider catalog) stays unit-testable.
"""

from __future__ import annotations

import getpass
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .engine import (
    Config,
    ensure_github_auth,
    list_models,
    provider_api_key,
)
from .store import set_active_model, set_provider_key
from .ui import print_banner


@dataclass(frozen=True)
class ProviderChoice:
    key: str          # provider id in the registry
    label: str        # human label
    key_env: str      # env var holding the API key
    model: str        # recommended VIGILANT_MODEL value
    url: str          # where to get a key
    free: bool        # free / no-credit-card tier


# Ordered for onboarding: free options first so a new user can start at $0.
PROVIDER_CATALOG: tuple[ProviderChoice, ...] = (
    ProviderChoice("groq", "Groq (free, no card - fast Llama/Qwen)", "GROQ_API_KEY",
                   "groq/llama-3.3-70b-versatile", "https://console.groq.com/keys", True),
    ProviderChoice("gemini", "Google Gemini (free tier)", "GEMINI_API_KEY",
                   "gemini/gemini-2.5-flash", "https://aistudio.google.com/apikey", True),
    ProviderChoice("nvidia_nim", "NVIDIA NIM (free, no card - DeepSeek etc.)",
                   "NVIDIA_NIM_API_KEY", "nvidia_nim/deepseek-ai/deepseek-v3.2-exp",
                   "https://build.nvidia.com", True),
    ProviderChoice("anthropic", "Anthropic Claude (best quality, paid)", "ANTHROPIC_API_KEY",
                   "anthropic/claude-sonnet-5", "https://console.anthropic.com/settings/keys",
                   False),
    ProviderChoice("openai", "OpenAI GPT (paid)", "OPENAI_API_KEY",
                   "openai/gpt-5.5", "https://platform.openai.com/api-keys", False),
    ProviderChoice("xai", "xAI Grok (paid)", "XAI_API_KEY",
                   "xai/grok-4.5", "https://console.x.ai", False),
    ProviderChoice("openrouter", "OpenRouter (many models, paid)", "OPENROUTER_API_KEY",
                   "openrouter/meta-llama/llama-3.3-70b-instruct", "https://openrouter.ai/keys",
                   False),
)


def upsert_env_file(path: str | Path, updates: dict[str, str]) -> None:
    """Write ``updates`` into a ``.env`` file, preserving existing content.

    An existing assignment for a key (including a commented ``# KEY=`` template
    line, as in `.env.example`) is replaced in place; unseen keys are appended.
    Comments and ordering are otherwise left untouched.
    """
    p = Path(path)
    lines = p.read_text().splitlines() if p.exists() else []
    applied: set[str] = set()
    out: list[str] = []
    for raw in lines:
        stripped = raw.lstrip()
        bare = stripped[1:].lstrip() if stripped.startswith("#") else stripped
        matched = None
        for key in updates:
            if key not in applied and (bare.startswith(f"{key}=") or bare.startswith(f"export {key}=")):
                matched = key
                break
        if matched is not None:
            out.append(f"{matched}={updates[matched]}")
            applied.add(matched)
        else:
            out.append(raw)
    remaining = [k for k in updates if k not in applied]
    if remaining and out and out[-1].strip():
        out.append("")
    out.extend(f"{k}={updates[k]}" for k in remaining)
    p.write_text("\n".join(out) + "\n")


def _prompt(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except EOFError:
        return default or ""
    return answer or (default or "")


def _yes(question: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    ans = _prompt(f"{question} ({d})").lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def _choose_provider() -> ProviderChoice | None:
    print("\nPick a model provider (free options first):\n")
    for i, pc in enumerate(PROVIDER_CATALOG, start=1):
        have = " [key already set]" if provider_api_key(pc.key) else ""
        tag = "FREE" if pc.free else "paid"
        print(f"  {i}. {pc.label}  ({tag}){have}")
    print(f"  {len(PROVIDER_CATALOG) + 1}. Local model via Ollama (no key)")
    choice = _prompt("\nEnter a number", "1")
    try:
        idx = int(choice)
    except ValueError:
        print("Not a number; defaulting to 1.")
        idx = 1
    if idx == len(PROVIDER_CATALOG) + 1:
        return None  # sentinel: Ollama / local
    if not 1 <= idx <= len(PROVIDER_CATALOG):
        idx = 1
    return PROVIDER_CATALOG[idx - 1]


def _verify_key(provider: str) -> bool:
    """Best-effort live check that the key works. Never fatal."""
    try:
        models = list_models(provider, Config.from_env())
        return bool(models)
    except Exception:  # noqa: BLE001 - verification is optional
        return False


def _catalog_lookup(provider_id: str) -> ProviderChoice | None:
    for pc in PROVIDER_CATALOG:
        if pc.key == provider_id:
            return pc
    return None


def add_provider_flow(preselected: str | None = None) -> str | None:
    """Choose a provider, capture its key, store it, and make it the active model.

    Writes to the managed credential store (not `.env`). Returns the now-active
    model string, or None if nothing was stored. Shared by `vigilant init` and
    `vigilant model add`. `preselected` may be a catalog provider id or "ollama"
    for the keyless local path.
    """
    if preselected in (None, ""):
        if not sys.stdin.isatty():
            sys.stderr.write("Interactive selection needs a terminal; pass a provider name.\n")
            return None
        pc = _choose_provider()  # None sentinel => local/Ollama
    elif preselected == "ollama":
        pc = None
    else:
        pc = _catalog_lookup(preselected)
        if pc is None:
            sys.stderr.write(
                f"Unknown provider '{preselected}'. Choose one of: "
                + ", ".join(p.key for p in PROVIDER_CATALOG)
                + ", ollama.\n"
            )
            return None

    if pc is None:  # local Ollama - no key
        default_local = "ollama/qwen2.5:14b"
        model = _prompt("Ollama model", default_local) if sys.stdin.isatty() else default_local
        set_active_model(model)
        print(f"Active model set to {model} (local Ollama - no key needed).")
        print("Make sure Ollama is running: https://ollama.com  (`ollama serve`).")
        return model

    print(f"\nGet a key here: {pc.url}")
    existing = provider_api_key(pc.key)
    if sys.stdin.isatty():
        if existing and _yes(f"{pc.key} is already set in your environment - use it?"):
            key = existing
        else:
            key = getpass.getpass(f"Paste your {pc.key} (input hidden): ").strip()
    else:
        key = existing or ""
    if not key:
        print("No key provided; nothing stored.")
        return None
    os.environ[pc.key] = key  # so verification below can see it
    print("Verifying the key...")
    print("  Key works." if _verify_key(pc.key) else "  Could not auto-verify (saving anyway).")
    set_provider_key(pc.key, key, model=pc.model, make_active=True)
    print(f"Stored {pc.key} and set active model to {pc.model}.")
    return pc.model


def run_init(env_path: str = ".env") -> int:
    """Guided setup: connect GitHub, store a model key, optional Slack. Automated."""
    if not sys.stdin.isatty():
        sys.stderr.write(
            "`vigilant init` is interactive; run it in a terminal, or set "
            "VIGILANT_MODEL + a provider key + GH_TOKEN yourself (see README).\n"
        )
        return 1

    print_banner()
    print("Setup connects your GitHub account and stores your model key - no files to edit.\n")

    # 1) GitHub access (reviews post as you) - runs `gh auth login` if needed.
    if ensure_github_auth(interactive=True):
        print("GitHub access: OK (reviews will post as your identity).")
    else:
        if not _yes("GitHub is not connected yet. Continue setup anyway?", default=False):
            return 1

    # 2) Model provider -> managed credential store.
    add_provider_flow(None)

    # 3) Optional Slack monitoring (channels/tokens still live in .env).
    updates: dict[str, str] = {}
    if _yes("\nSet up Slack monitoring (review PRs you're @-mentioned on)?", default=False):
        channel = _prompt("Slack channel ID to watch (e.g. C0123ABCD)")
        if channel:
            updates["VIGILANT_SLACK_CHANNELS"] = channel
        if _yes("Auto-read + refresh the Slack token from your Chrome session?", default=True):
            updates["VIGILANT_SLACK_AUTO_TOKEN"] = "1"
            print("  (needs: pip install 'vigilant-pr[slack-refresh]' && "
                  "python -m playwright install chromium)")
        else:
            token = getpass.getpass("Paste SLACK_TOKEN (xoxc-/xoxb-, hidden): ").strip()
            if token:
                updates["SLACK_TOKEN"] = token
                if token.startswith("xoxc-"):
                    cookie = getpass.getpass("Paste the d cookie (xoxd-..., hidden): ").strip()
                    if cookie:
                        updates["SLACK_COOKIE_D"] = cookie
    if updates:
        upsert_env_file(env_path, updates)
        print(f"Wrote Slack settings to {env_path}.")

    print("\nYou're ready:")
    print("  vigilant review https://github.com/owner/repo/pull/123")
    print("  vigilant github-watch          # auto-review PRs you're requested on")
    print("  vigilant model list            # see or switch your stored models")
    if "VIGILANT_SLACK_CHANNELS" in updates:
        auto = " --auto-token" if updates.get("VIGILANT_SLACK_AUTO_TOKEN") else ""
        print(f"  vigilant slack-watch{auto}      # review PRs you're @-mentioned on in Slack")
    return 0
