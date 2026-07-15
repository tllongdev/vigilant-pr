"""Chat trigger surfaces for Vigilant PR.

These map a review request ("review this PR") to `engine.run_review`, which posts
the review on behalf of the user who owns the running GitHub token. The shared,
dependency-free logic lives in `core`; the Slack monitor (`slack_poll`) and Teams
webhook (`teams`) are thin, stdlib-only adapters on top of it.
"""

from .core import (
    ReviewOutcome,
    extract_pr_refs,
    format_reply,
    review_from_text,
    run_review_for_ref,
    split_flags,
)

__all__ = [
    "ReviewOutcome",
    "extract_pr_refs",
    "format_reply",
    "review_from_text",
    "run_review_for_ref",
    "split_flags",
]
