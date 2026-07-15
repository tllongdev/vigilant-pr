"""Vigilant PR review engine.

Public entry points both the CLI and the watcher call, so there is a single
review code path with no behavioral divergence between surfaces.
"""

from __future__ import annotations

from .config import DEFAULT_MODEL, MODEL_PROFILES, OPUS_MODEL, SONNET_MODEL, Config, load_dotenv
from .errors import ReviewFailedError
from .hosts import GitHubHost, HostProvider, PullRequest, detect_host, resolve_host
from .identity import build_signature, resolve_handle
from .providers import (
    PROVIDERS,
    auto_select_model,
    list_models,
    model_key_missing,
    provider_api_key,
    resolve_provider,
)
from .review import run_review, run_threads_only
from .util import ensure_github_auth, github_preflight
from .watcher import run_watch

__all__ = [
    "Config",
    "DEFAULT_MODEL",
    "MODEL_PROFILES",
    "OPUS_MODEL",
    "PROVIDERS",
    "SONNET_MODEL",
    "GitHubHost",
    "HostProvider",
    "PullRequest",
    "ReviewFailedError",
    "auto_select_model",
    "build_signature",
    "detect_host",
    "ensure_github_auth",
    "github_preflight",
    "list_models",
    "load_dotenv",
    "model_key_missing",
    "provider_api_key",
    "resolve_handle",
    "resolve_host",
    "resolve_provider",
    "run_review",
    "run_threads_only",
    "run_watch",
]
