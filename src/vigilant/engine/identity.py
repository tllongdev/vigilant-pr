"""Identity and signature handling for Vigilant PR.

Comments are authored by the running user's GitHub token, so they are the user's
review. To stay honest, every comment carries a signature block making clear it
is an AI-assisted first pass, naming the model that ran, and (when known) the
handle it was posted on behalf of.
"""

from __future__ import annotations

from .config import MODEL_PROFILES
from .util import run

# Stable prefix on the first line of every Vigilant PR comment/review body. Used
# both to render the signature and to detect the tool's own prior comments on a
# re-review. The legacy prefix is the one emitted by the original
# knowledge-substrate reviewer - matched too so a repo migrating from that tool
# still gets correct dedup / thread detection.
SIG_PREFIX_VIGILANT = "\U0001f916 AI-assisted PR review"
SIG_PREFIX_LEGACY = "\U0001f916 Automated AI Code Review"
SIGNATURE_PREFIXES = (SIG_PREFIX_VIGILANT, SIG_PREFIX_LEGACY)


def build_signature(model: str, handle: str | None = None) -> str:
    """Build the 3-line signature block for a comment or review body.

    When `handle` is known the block attributes the review to that user; when it
    cannot be resolved it falls back to a generic (still honest) block.
    """
    suffix = MODEL_PROFILES.get(model, {}).get("signature_suffix", "")
    if handle:
        line1 = f"> {SIG_PREFIX_VIGILANT} - commissioned and posted by @{handle}"
    else:
        line1 = f"> {SIG_PREFIX_VIGILANT} - automated first-pass"
    return (
        f"{line1}\n"
        f"> Reviewer model: {model} {suffix}\n"
        "> An automated first-pass. Not a substitute for a full human review."
    )


def is_signed_comment(body: str) -> bool:
    """True if `body` was authored by Vigilant PR (or the legacy KS reviewer)."""
    return any(prefix in body for prefix in SIGNATURE_PREFIXES)


def signature_index(body: str) -> int:
    """Index of the earliest known signature prefix in `body`, or -1 if none."""
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
