"""Vigilant PR - a portable, workflow-agnostic AI PR reviewer that posts on your behalf."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vigilant-pr")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
