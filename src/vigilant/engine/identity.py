# Copyright 2026 Timothy Long / Longitudinal Intelligence Technologies (LIT)
# SPDX-License-Identifier: Apache-2.0
"""Identity and signature handling for Vigilant PR.

Comments are authored by the running user's GitHub token, so they read as the
user's own review. Each body carries a *hidden* HTML-comment marker (invisible
on GitHub) rather than a visible disclaimer, so the tool can still recognize its
own prior comments for dedup / re-review while the review presents as the user's.
"""

from __future__ import annotations

from .config import MODEL_PROFILES
from .util import run

# Hidden HTML-comment marker embedded at the top of every Vigilant PR body. It
# renders invisibly on GitHub, so the review reads as the user's own, but is
# still detectable for dedup and re-review thread matching.
SIG_MARKER = "<!-- vigilant-pr-review"

# Legacy VISIBLE signature prefixes still matched for detection so re-reviews of
# PRs that already carry an older visible signature - Vigilant's own earlier
# format, or the original knowledge-substrate reviewer - keep correct dedup /
# thread detection. New reviews never emit these.
SIG_PREFIX_VIGILANT = "\U0001f916 AI-assisted PR review"
SIG_PREFIX_LEGACY = "\U0001f916 Automated AI Code Review"
SIGNATURE_PREFIXES = (SIG_MARKER, SIG_PREFIX_VIGILANT, SIG_PREFIX_LEGACY)


def build_signature(model: str, handle: str | None = None) -> str:
    """Build the hidden marker line prepended to a comment or review body.

    Renders invisibly on GitHub (an HTML comment), so the review presents as the
    user's own. Carries the model (and, when known, the handle it was posted on
    behalf of) as a machine-readable audit trail and dedup key.
    """
    suffix = MODEL_PROFILES.get(model, {}).get("signature_suffix", "")
    model_str = f"{model} {suffix}".strip()
    who = f"; by=@{handle}" if handle else ""
    return f"{SIG_MARKER}: AI-assisted first-pass; model={model_str}{who} -->"


REPO_URL = "https://github.com/tllongdev/vigilant-pr"


def build_footnote(model: str, handle: str | None = None) -> str:
    """Build the short, VISIBLE attribution footnote for a posted review.

    Unlike the hidden marker, this renders on GitHub: a thin rule plus a small,
    grey `<sub>` line disclosing the review was AI-assisted, which model produced
    it, and who it was posted on behalf of. Kept quiet so it reads as a signature,
    not a banner. Controlled by Config.attribution (on by default).
    """
    who = f" \u00b7 posted by @{handle}" if handle else ""
    return (
        "---\n"
        f"<sub>\U0001f6e1\ufe0f AI-assisted review via [Vigilant PR]({REPO_URL}) "
        f"\u00b7 {model}{who}</sub>"
    )


def is_signed_comment(body: str) -> bool:
    """True if `body` was authored by Vigilant PR (or a legacy signed reviewer)."""
    return any(prefix in body for prefix in SIGNATURE_PREFIXES)


def signature_index(body: str) -> int:
    """Index of the earliest known signature marker/prefix in `body`, or -1."""
    hits = [body.find(prefix) for prefix in SIGNATURE_PREFIXES]
    hits = [i for i in hits if i >= 0]
    return min(hits) if hits else -1


def resolve_handle(explicit: str | None = None) -> str | None:
    """Resolve the GitHub handle to attribute the review to.

    Uses the explicit override when given, else the authenticated `gh` user.
    Returns None (and the caller falls back to a generic block) if it cannot be
    determined - never fails the review over a missing handle.
    """
    if explicit:
        return explicit
    out = run(["gh", "api", "user", "-q", ".login"], check=False)
    handle = out.strip()
    return handle or None
