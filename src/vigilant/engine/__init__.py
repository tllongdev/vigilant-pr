"""Vigilant PR review engine.

Public entry points both the CLI and the watcher call, so there is a single
review code path with no behavioral divergence between surfaces.
"""

from __future__ import annotations

from .config import DEFAULT_MODEL, MODEL_PROFILES, OPUS_MODEL, SONNET_MODEL, Config
from .identity import build_signature, resolve_handle
from .review import ReviewFailedError, run_review, run_threads_only
from .watcher import run_watch

__all__ = [
    "Config",
    "DEFAULT_MODEL",
    "MODEL_PROFILES",
    "OPUS_MODEL",
    "SONNET_MODEL",
    "ReviewFailedError",
    "build_signature",
    "resolve_handle",
    "run_review",
    "run_threads_only",
    "run_watch",
]
