"""Shared, dependency-free logic for chat triggers.

A chat message like "please review https://github.com/o/r/pull/42 --opus" gets
turned into one or more `engine.run_review` calls. This module owns:
  - extracting GitHub PR references from free-form chat text (incl. Slack's
    `<url|label>` link markup),
  - lifting inline `--opus` / `--sonnet` model flags out of the text,
  - invoking the engine per reference and normalizing its int/exit-code contract
    into a structured `ReviewOutcome`,
  - formatting a concise, plain-text chat reply.

It never imports a chat SDK, so it stays testable and stdlib-only.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass

from ..engine import OPUS_MODEL, SONNET_MODEL, Config, run_review

# Default reaction emojis (Slack "reacji" names, no colons) that trigger a review
# when added to a message containing a PR link.
DEFAULT_TRIGGER_EMOJIS = ("eyes", "vigilant", "mag", "shipit")

# Full GitHub PR URL. Kept strict (numeric PR id) so we don't misfire on
# /pull/ tree paths or issue links.
_PR_URL_RE = re.compile(r"https?://github\.com/([^/\s|>]+/[^/\s|>]+)/pull/(\d+)")


@dataclass
class ReviewOutcome:
    """Result of one review attempt, shaped for a chat reply."""

    ref: str
    pr_url: str
    ok: bool
    exit_code: int
    message: str


def extract_pr_refs(text: str) -> list[str]:
    """Return unique full GitHub PR URLs found in chat text, in first-seen order.

    Handles Slack link markup (`<https://...|label>` and `<https://...>`) by
    matching the URL substring directly rather than trying to unwrap it.
    """
    seen: set[str] = set()
    refs: list[str] = []
    for m in _PR_URL_RE.finditer(text or ""):
        url = f"https://github.com/{m.group(1)}/pull/{m.group(2)}"
        if url not in seen:
            seen.add(url)
            refs.append(url)
    return refs


def split_flags(text: str) -> tuple[str, str | None]:
    """Pull `--opus` / `--sonnet` out of chat text.

    Returns (text_without_flags, model_override_or_None). Later flags win; if
    both appear, the last one seen takes effect (matches CLI leniency).
    """
    model: str | None = None
    tokens = (text or "").split()
    kept: list[str] = []
    for tok in tokens:
        low = tok.lower().strip(",.")
        if low == "--opus":
            model = OPUS_MODEL
        elif low == "--sonnet":
            model = SONNET_MODEL
        else:
            kept.append(tok)
    return " ".join(kept), model


def run_review_for_ref(ref: str, config: Config) -> ReviewOutcome:
    """Run one review and normalize the engine's exit-code contract.

    `engine.run_review` returns 0/1/2 and may `sys.exit(3)` (via util.run) on a
    GitHub error; we catch that so a chat handler never dies mid-request.
    """
    pr_url = ref
    try:
        code = run_review(ref, config)
    except SystemExit as e:  # util.run exits 3 on a hard GitHub/CLI failure
        code = int(e.code) if isinstance(e.code, int) else 3
    except Exception as e:  # noqa: BLE001 - a chat handler must never crash
        return ReviewOutcome(
            ref=ref, pr_url=pr_url, ok=False, exit_code=1,
            message=f"Could not review {pr_url}: {e}",
        )

    ok = code == 0
    if ok:
        message = f"Review posted to {pr_url} (as your GitHub identity)."
    else:
        reason = {
            1: "configuration error (check ANTHROPIC_API_KEY / GitHub auth)",
            2: "the model call failed after retries",
            3: "a GitHub API/permissions error (check repo access + token scopes)",
        }.get(code, f"exit code {code}")
        message = f"Review of {pr_url} did not complete - {reason}."
    return ReviewOutcome(ref=ref, pr_url=pr_url, ok=ok, exit_code=code, message=message)


def review_from_text(text: str, config: Config) -> list[ReviewOutcome]:
    """Extract PR links + model flags from `text` and review each one.

    Returns one ReviewOutcome per PR link found (empty list if none). The model
    override from inline flags is layered onto a copy of `config` so the caller's
    config is not mutated.
    """
    body, model_override = split_flags(text)
    refs = extract_pr_refs(body)
    if not refs:
        return []
    effective = dataclasses.replace(config, model=model_override) if model_override else config
    return [run_review_for_ref(ref, effective) for ref in refs]


def format_reply(outcomes: list[ReviewOutcome]) -> str:
    """Join per-PR outcome messages into a single plain-text chat reply."""
    if not outcomes:
        return (
            "I did not find a GitHub PR link to review. "
            "Include a full URL like https://github.com/owner/repo/pull/123."
        )
    return "\n".join(o.message for o in outcomes)
