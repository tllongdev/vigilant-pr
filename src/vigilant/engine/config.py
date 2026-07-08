"""Model profiles and runtime configuration for Vigilant PR.

This module has no internal dependencies so both `identity` and `review` can
import from it without cycles.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SONNET_MODEL = "claude-sonnet-4-6"
OPUS_MODEL = "claude-opus-4-7"
DEFAULT_MODEL = SONNET_MODEL

# Per-model reasoning configuration. Sonnet runs without extended thinking at
# medium effort - fast and cheap, good enough for every PR. Opus uses adaptive
# thinking at high effort - slower and pricier but spends real reasoning on
# hard-to-spot bugs. Both are valid Messages API parameter combinations.
MODEL_PROFILES: dict[str, dict[str, Any]] = {
    SONNET_MODEL: {
        "thinking": None,
        "output_config": {"effort": "medium"},
        "signature_suffix": "(effort=medium)",
        "tier_label": "Sonnet (auto)",
    },
    OPUS_MODEL: {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "signature_suffix": "(adaptive thinking, effort=high)",
        "tier_label": "Opus (escalation)",
    },
}


@dataclass
class Config:
    """Runtime configuration for a review run or the watcher.

    Only `model`, `dry_run`, `repo`, `handle`, and `anthropic_api_key` are used
    by the one-shot review path (milestone 001/002). The remaining fields are
    consumed by the watcher (milestone 003) and are present here so the config
    surface is stable.
    """

    # One-shot review
    model: str = DEFAULT_MODEL
    dry_run: bool = False
    repo: str | None = None
    # GitHub handle the review is posted "on behalf of". Resolved from the
    # authenticated token when None (see identity.resolve_handle).
    handle: str | None = None
    anthropic_api_key: str | None = None

    # Watcher (milestone 003)
    poll_interval: int = 120
    daily_cap: int = 50
    org_allow: list[str] = field(default_factory=list)
    org_deny: list[str] = field(default_factory=list)
    repo_allow: list[str] = field(default_factory=list)
    repo_deny: list[str] = field(default_factory=list)
    skip_drafts: bool = True

    @classmethod
    def from_env(cls, **overrides: Any) -> Config:
        """Build a Config from environment variables, then apply explicit overrides.

        Recognized env vars:
          ANTHROPIC_API_KEY         - required for actual review calls
          VIGILANT_MODEL            - model name
          VIGILANT_POLL_INTERVAL    - watcher poll seconds
          VIGILANT_DAILY_CAP        - watcher per-day review cap
          VIGILANT_ORG_ALLOW/DENY   - comma-separated org lists
          VIGILANT_REPO_ALLOW/DENY  - comma-separated owner/repo lists
        """
        cfg = cls()
        cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        if os.environ.get("VIGILANT_MODEL"):
            cfg.model = os.environ["VIGILANT_MODEL"]
        if os.environ.get("VIGILANT_POLL_INTERVAL"):
            cfg.poll_interval = int(os.environ["VIGILANT_POLL_INTERVAL"])
        if os.environ.get("VIGILANT_DAILY_CAP"):
            cfg.daily_cap = int(os.environ["VIGILANT_DAILY_CAP"])
        cfg.org_allow = _split_csv(os.environ.get("VIGILANT_ORG_ALLOW"))
        cfg.org_deny = _split_csv(os.environ.get("VIGILANT_ORG_DENY"))
        cfg.repo_allow = _split_csv(os.environ.get("VIGILANT_REPO_ALLOW"))
        cfg.repo_deny = _split_csv(os.environ.get("VIGILANT_REPO_DENY"))
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    @classmethod
    def from_file(cls, path: str | Path, **overrides: Any) -> Config:
        """Layer a `[vigilant]` TOML table over env config, then apply overrides."""
        cfg = cls.from_env()
        data = tomllib.loads(Path(path).read_text())
        table = data.get("vigilant", data)
        for key, value in table.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
