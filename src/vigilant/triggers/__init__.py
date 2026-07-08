"""Chat trigger surfaces for Vigilant PR (milestones 004-005).

These map a chat request ("review this PR") to `engine.run_review`, which posts
the review on behalf of the user who owns the running GitHub token. The shared,
dependency-free logic lives in `core`; each surface (`slack`, `teams`) is a thin
adapter and imports its own optional third-party SDK lazily so the core engine
stays stdlib-only.
"""

from .core import (
    DEFAULT_TRIGGER_EMOJIS,
    ReviewOutcome,
    extract_pr_refs,
    format_reply,
    review_from_text,
    run_review_for_ref,
    split_flags,
)

__all__ = [
    "DEFAULT_TRIGGER_EMOJIS",
    "ReviewOutcome",
    "extract_pr_refs",
    "format_reply",
    "review_from_text",
    "run_review_for_ref",
    "split_flags",
]
