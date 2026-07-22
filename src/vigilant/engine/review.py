# Copyright 2026 Timothy Long / Longitudinal Intelligence Technologies (LIT)
# SPDX-License-Identifier: Apache-2.0
"""Vigilant PR review engine.

Ported from the production knowledge-substrate reviewer with behavior preserved:
two-tier Sonnet/Opus selection, adversarial JSON-output prompt, severity scheme,
thread-aware re-review, incremental diff scoping, dedup, nit-ratchet, diff-line
validation, single-call posting, retry/backoff, and a failure comment on hard
errors. The only additive change is the configurable on-behalf-of signature
(see engine.identity) and a `run_review(target, config)` public entry point that
replaces the original module's `main()`.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import GENERIC_PROFILE, MODEL_PROFILES, Config
from .errors import ReviewFailedError
from .identity import (
    build_footnote,
    build_signature,
    resolve_handle,
    signature_index,
)
from .providers import call_model, model_key_missing

# On a re-review, only these severities are posted. Nits are suppressed so re-runs
# stop drilling into style minutiae on code that already passed an earlier pass.
RERUN_KEEP_SEVERITIES = {"critical", "medium"}

SYSTEM_PROMPT = """You are an adversarial code reviewer with no team history, no familiarity with the author, and no stake in the PR being merged. You assume code is wrong until proven correct.

Output rules:
- Respond with a single JSON object, nothing before or after it.
- Schema:
  {{
    "summary": "<2-4 sentence prose summary>",
    "tally": {{"critical": int, "medium": int, "nit": int}},
    "findings": [
      {{
        "severity": "critical" | "medium" | "nit",
        "path": "<file path>",
        "line": <int>,                     # line in the new file (RIGHT side of diff)
        "title": "<one-line summary>",
        "body": "<full prose: failure mode, evidence, recommended fix>"
      }}
    ],
    "thread_responses": [
      {{
        "comment_id": <int>,
        "disposition": "acknowledged" | "re_flagged",
        "reply": "<brief response validating or contesting the human's reply>"
      }}
    ],
    "skipped": ["<things you could not verify>"]
  }}

Review checklist (apply to the diff; skip categories that don't apply):
- Correctness: off-by-one, wrong boolean logic, mutable defaults, shadowed names, missing return, type coercion bugs.
- Concurrency / state: races, unsafe shared mutable state, missing locks, broken transactions, retry idempotency.
- Edge cases: empty input, single element, null/None, very large input, Unicode, timezones, DST, integer overflow.
- Error handling: swallowed exceptions, bare except, errors that leak PII or secrets, missing rollback, retries on non-idempotent ops.
- Security: SQL/shell/template/prompt injection, authn/authz bypass, IDOR, missing tenant scoping, secrets in logs, weak randomness, missing rate limiting, unsafe deserialization.
- Data integrity: non-backward-compatible migrations, schema changes without backfill, missing NOT NULL on required fields, lossy type changes.
- Resource hygiene: unclosed handles, leaked DB connections, unbounded memory growth, N+1 queries.
- API contract: breaking changes to public endpoints, response shape drift, removed fields.
- Tests: missing tests for the bug or behavior the PR claims to fix; tests that mock the thing under test; happy-path-only.
- Repo conventions: violations of AGENTS.md / CLAUDE.md (nits unless about safety/correctness).

Severity (only for issues INTRODUCED by this PR - never flag pre-existing issues):
- "critical": would break behavior in production, leak data, corrupt state, or block rollback. Must fix before merge.
- "medium": real issue that should be fixed before merge but won't cause immediate production failure (e.g., missing error handling, race conditions under specific conditions, hardcoded values that should be configurable).
- "nit": style, naming, minor improvement. Cap at 5 inline; mention overflow in summary. Not blocking.

Verifiability (hard rule): you see ONLY the unified diff, the repo guidance files, and any dependency manifests provided below - not the whole repository. A finding must never be 'critical' or 'medium' if it rests on something you cannot see (a dependency's installed version, a call site not in the diff, config or a manifest not provided). Before flagging a dependency or version-support issue, check the provided dependency manifests: if they show the dependency/version is present and supported, do not flag it; if no manifest is provided and you cannot otherwise confirm it from the diff, classify it as 'nit' (or omit it) and record what you could not verify in "skipped". Never assume a dependency is missing, unpinned, or too old just because the diff does not show a version bump.

Prior thread evaluation (only when prior threads are provided):
- You may receive threads from your own prior review where a human has replied.
- For each thread, evaluate the human's response against the current diff:
  - "acknowledged": The concern was fixed in the new code, or the human gave a sound technical reason to keep it as-is. Reply briefly confirming.
  - "re_flagged": The concern was NOT fixed and the human's response does not adequately justify keeping the issue. Reply explaining why the concern still stands.
- Do NOT re-post findings that were already raised in a prior thread. Only post NEW findings for NEW issues in the diff.
- If there are no prior threads, omit "thread_responses" or return an empty array.

Skip:
- Anything the repo's CI already enforces (ruff, mypy, gofmt, go vet, formatting).
- Generated files, *.lock, .venv/, node_modules/, vendored/.
- Pure docs/typo PRs: a single COMMENT review with a one-line body, no inline noise.

Cite a concrete `path:line` and explain the failure mode in every finding. No vague "consider" without a named bug or risk.

Approval is decided mechanically from your findings, not by you: the review is submitted as an APPROVE when there are zero 'critical' and zero 'medium' findings (nit-only or clean), and as a COMMENT otherwise. The goal is to approve unless something genuinely blocks merge, so be honest and precise about severity - do not inflate a nit to 'medium' to avoid approving, and do not downgrade a real blocking bug to 'nit' just to approve. Classify each issue on its actual merit."""


THREADS_ONLY_SYSTEM_PROMPT = """You are validating human responses to your prior code review comments. You have no team history and no stake in the PR.

Output rules:
- Respond with a single JSON object, nothing before or after it.
- Schema:
  {{
    "thread_responses": [
      {{
        "comment_id": <int>,
        "disposition": "acknowledged" | "re_flagged",
        "reply": "<brief response validating or contesting the human's reply>"
      }}
    ]
  }}

Evaluation rules:
- "acknowledged": The human gave a sound technical justification for keeping the code as-is, or stated the issue was fixed (and you have no reason to doubt it based on the context). Reply briefly confirming.
- "re_flagged": The human dismissed a valid concern without adequate justification. Reply explaining why the concern still stands. Be specific.
- Evaluate each thread independently based on its technical merit.
- Keep replies concise - one to two sentences."""


THREADS_ONLY_USER_TEMPLATE = """Validate the human responses to your prior review comments on PR #{pr_number} in {repo}.

PR title: {title}
Head branch: {head}

{thread_context}

Output the validation JSON now. Nothing before or after."""


USER_PROMPT_TEMPLATE = """Review pull request #{pr_number} in {repo}.

Today's date is {today}. Treat this as the ground-truth current date. Do NOT flag any date as "future-dated", "a typo", or suspicious by comparing it to your training cutoff - you do not know the current date except from this line. Only flag a date if the diff itself contains evidence it is wrong (e.g. it contradicts another date in the same file).

PR title: {title}

PR body:
{body}

Base branch: {base}
Head branch: {head}
Head SHA: {head_sha}
Files changed: {file_count}
{review_scope_note}
Repo guidance files (if present, read carefully):
{guidance}

Dependency manifests (declared dependencies/versions at the PR head - use these to verify any import or version-support claim; do NOT assume a dependency is missing or too old just because the diff shows no bump):
{dependency_manifests}

Unified diff:
```diff
{diff}
```
{prior_threads_section}
Output the review JSON now. Nothing before or after."""


@dataclass
class Finding:
    severity: str
    path: str
    line: int
    title: str
    body: str

    @property
    def severity_marker(self) -> str:
        return {
            "critical": "\U0001f534 Critical",
            "medium": "\U0001f7e0 Medium",
            "nit": "\U0001f7e1 Nit",
        }.get(self.severity, self.severity.title())


@dataclass
class PriorThread:
    """A prior bot review comment and any human replies."""

    comment_id: int
    path: str
    line: int
    severity: str
    title: str
    bot_body: str
    replies: list[dict[str, str]]


def format_thread_context(threads: list[PriorThread]) -> str:
    """Format prior threads into readable context for the review prompt."""
    if not threads:
        return ""

    by_path: dict[str, list[PriorThread]] = {}
    for t in threads:
        by_path.setdefault(t.path, []).append(t)

    parts: list[str] = []
    for path in sorted(by_path):
        parts.append(f"### {path}")
        for t in by_path[path]:
            parts.append(f"\n**[Thread {t.comment_id}]** ({t.severity}) {t.title}")
            sig_start = signature_index(t.bot_body)
            sig_end = t.bot_body.find("\n\n", sig_start) if sig_start >= 0 else -1
            finding_body = t.bot_body[sig_end:].strip() if sig_end > 0 else t.bot_body
            parts.append(f"Bot finding: {finding_body[:500]}")
            for r in t.replies:
                parts.append(f"Reply from @{r['user']}: {r['body'][:300]}")

    return "\n".join(parts)


def _norm_title(title: str) -> str:
    """Normalize a finding title for dedup: collapse whitespace, drop trailing
    period, lowercase."""
    return re.sub(r"\s+", " ", title).strip().rstrip(".").lower()


# Phrases where the model itself admits it could not verify a claim. A finding
# whose body rests on an admitted unknown must not land as a blocking severity -
# it is capped to a non-blocking nit so it surfaces as "worth verifying" rather
# than a false alarm. Motivated by a Critical raised on a dependency the diff did
# not show (the repo actually pinned a supported version).
_UNVERIFIABLE_MARKERS = re.compile(
    r"could ?n[o']t (?:confirm|verify|tell|determine|check)"
    r"|can(?:not|'t|no?t) (?:confirm|verify|tell|determine|be verified|be confirmed)"
    r"|unable to (?:confirm|verify|determine|tell)"
    r"|no way to (?:verify|confirm|tell|know)"
    r"|(?:not|isn't|is not|aren't|are not|wasn't|was not|no visible|not shown|not present|"
    r"not included|not available) (?:\w+\s+){0,3}(?:in|within) (?:the |this )?"
    r"(?:diff|pr|changeset|patch|requirements|manifest)"
    r"|not (?:in|part of) (?:the |this )?diff"
    r"|outside (?:the|this) (?:diff|pr|patch|changeset)"
    r"|without (?:seeing|access to|visibility into|the full)",
    re.IGNORECASE,
)

# Diff signals that dependency manifests are worth fetching so version/import
# claims can be checked against real declared versions instead of guessed.
_ADDED_IMPORT_RE = re.compile(
    r"^\+\s*(?:import\s|from\s+\S+\s+import\s|const\s+[\w{}, ]+=\s*require\(|"
    r"require\s|require\(|use\s+\w|#include\s|using\s+\w)"
)
_MANIFEST_PATH_RE = re.compile(
    r"(?:^|/)(?:requirements[^/]*\.txt|pyproject\.toml|setup\.cfg|setup\.py|Pipfile|"
    r"package\.json|go\.mod|Gemfile|Cargo\.toml|composer\.json|"
    r"build\.gradle(?:\.kts)?|pom\.xml)$"
)


def downgrade_unverifiable(findings: list[Finding]) -> tuple[list[Finding], int]:
    """Cap self-admitted unverifiable critical/medium findings to nit.

    The reviewer only sees the diff (plus guidance and any fetched manifests), so
    a finding whose own reasoning admits it could not verify the claim is not
    allowed to block a merge. It is downgraded to a non-blocking nit and
    annotated, turning a confident false alarm into an honest "worth checking"
    note. Returns the (possibly rewritten) findings and how many were downgraded.
    """
    downgraded = 0
    result: list[Finding] = []
    for f in findings:
        if f.severity in ("critical", "medium") and _UNVERIFIABLE_MARKERS.search(f.body):
            note = (
                "\n\n_Severity capped to nit by Vigilant PR: this finding's own reasoning "
                "could not verify the claim from the diff, so it is not treated as blocking. "
                "Confirm manually if it matters._"
            )
            result.append(
                Finding(
                    severity="nit",
                    path=f.path,
                    line=f.line,
                    title=f.title,
                    body=f.body + note,
                )
            )
            downgraded += 1
        else:
            result.append(f)
    return result, downgraded


def dependency_search_dirs(diff_text: str) -> tuple[str, ...]:
    """Directories to search for dependency manifests: the directory of each
    changed file, plus the repo root. Ordered, de-duplicated. This lets a manifest
    that lives beside the code (monorepo/service subdir) be found, not just root."""
    dirs: list[str] = []
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            path = raw[len("+++ b/"):].strip()
            if not path or path == "/dev/null":
                continue
            directory = path.rsplit("/", 1)[0] if "/" in path else ""
            if directory and directory not in dirs:
                dirs.append(directory)
    dirs.append("")  # repo root is always searched
    return tuple(dict.fromkeys(dirs))


def diff_touches_dependencies(diff_text: str) -> bool:
    """Whether the diff adds an import/require/use line or edits a dependency
    manifest - the signal to fetch manifests so version/import claims are checked
    against real declared versions instead of guessed."""
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/") or raw.startswith("--- a/"):
            if _MANIFEST_PATH_RE.search(raw[6:].strip()):
                return True
            continue
        if raw.startswith("+") and not raw.startswith("+++") and _ADDED_IMPORT_RE.match(raw):
            return True
    return False


def parse_diff_lines(diff_text: str) -> dict[str, set[int]]:
    """Build {path: set of new-file line numbers GitHub will accept comments on}."""
    valid: dict[str, set[int]] = {}
    current_path: str | None = None
    new_line: int | None = None
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            current_path = raw[len("+++ b/"):].strip()
            valid.setdefault(current_path, set())
            new_line = None
            continue
        if raw.startswith("+++ /dev/null"):
            current_path = None
            new_line = None
            continue
        m = hunk_re.match(raw)
        if m:
            new_line = int(m.group(1))
            continue
        if current_path is None or new_line is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            valid[current_path].add(new_line)
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            pass
        elif raw.startswith(" "):
            valid[current_path].add(new_line)
            new_line += 1
    return valid


def strip_trailing_commas(s: str) -> str:
    """Remove trailing commas before } or ] - a common LLM JSON quirk that stdlib
    json rejects. String-aware, so commas inside string values are left untouched."""
    out: list[str] = []
    in_str = False
    esc = False
    n = len(s)
    for i, ch in enumerate(s):
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            continue
        if ch == ",":
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n and s[j] in "}]":
                continue
        out.append(ch)
    return "".join(out)


def parse_review_json(text: str) -> dict[str, Any]:
    """Extract the JSON object from the model's response, tolerant of stray prose,
    code fences, and trailing commas."""
    candidates: list[str] = [text.strip()]
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    last_err: json.JSONDecodeError | None = None
    for cand in candidates:
        for variant in (cand, strip_trailing_commas(cand)):
            try:
                parsed: dict[str, Any] = json.loads(variant)
                return parsed
            except json.JSONDecodeError as e:
                last_err = e
    if last_err is None:
        sys.stderr.write("Model did not return parseable JSON. Raw output:\n")
        sys.stderr.write(text)
        raise ReviewFailedError("Model response was not parseable JSON")
    sys.stderr.write(f"JSON parse failed: {last_err}\nRaw output:\n{text}\n")
    raise ReviewFailedError(f"Model response JSON parse failed: {last_err}") from last_err


def format_inline_comment(f: Finding, sig: str) -> str:
    return (
        f"{sig}\n\n"
        f"**{f.severity_marker}** - {f.title}\n\n"
        f"{f.body}\n\n"
        f"File: `{f.path}:{f.line}`"
    )


def format_review_body(
    review: dict[str, Any], findings: list[Finding], sig: str, head_sha: str = ""
) -> str:
    # Derive the tally from the findings actually being posted (after dedup,
    # ratchet, and nit-capping) rather than the model's self-reported counts.
    critical = sum(1 for f in findings if f.severity == "critical")
    medium = sum(1 for f in findings if f.severity == "medium")
    nit = sum(1 for f in findings if f.severity == "nit")
    summary = review.get("summary", "").strip() or "(no summary)"
    skipped = review.get("skipped", []) or []

    # A compact, human-reading list (not a table): each finding is also an inline
    # comment on its line, so this is just a scannable at-a-glance recap.
    if findings:
        findings_list = "\n".join(
            f"{f.severity_marker} `{f.path}:{f.line}` - {f.title}" for f in findings
        )
    else:
        findings_list = "No new issues found in this diff."

    skipped_section = ""
    if skipped:
        skipped_section = "\n\n## What I did not check\n\n" + "\n".join(f"- {s}" for s in skipped)

    tally_parts = []
    if critical:
        tally_parts.append(f"\U0001f534 {critical} Critical")
    if medium:
        tally_parts.append(f"\U0001f7e0 {medium} Medium")
    if nit:
        tally_parts.append(f"\U0001f7e1 {nit} Nit")
    tally_str = ", ".join(tally_parts) if tally_parts else "No findings"

    sha_marker = f"\n\n<!-- ai-review-sha: {head_sha} -->" if head_sha else ""

    return (
        f"{sig}\n\n"
        f"**Findings:** {tally_str}\n\n"
        f"{summary}\n\n"
        f"{findings_list}"
        f"{skipped_section}"
        f"{sha_marker}"
    )


def decide_event(
    findings: list[Finding], thread_responses: list[dict[str, Any]] | None = None
) -> str:
    """Choose the GitHub review event from the findings.

    Policy: the goal is to get the PR approved unless something actually blocks
    merge. APPROVE when there are no blocking findings (critical or medium) -
    so nit-only or clean reviews approve with their comments attached. Anything
    blocking, or a prior concern re-flagged as still unresolved, stays a COMMENT
    (surface it without approving, and without hard-blocking via REQUEST_CHANGES).
    """
    blocking = any(f.severity in ("critical", "medium") for f in findings)
    reflagged = any(
        (tr.get("disposition") == "re_flagged") for tr in (thread_responses or [])
    )
    return "COMMENT" if (blocking or reflagged) else "APPROVE"


def cap_nits(findings: list[Finding], cap: int = 5) -> tuple[list[Finding], int]:
    """Keep the top `cap` nits, count the rest."""
    critical = [f for f in findings if f.severity == "critical"]
    medium = [f for f in findings if f.severity == "medium"]
    nits = [f for f in findings if f.severity == "nit"]
    kept_nits = nits[:cap]
    overflow = max(0, len(nits) - cap)
    return critical + medium + kept_nits, overflow


def filter_to_diff_lines(
    findings: list[Finding], valid_lines: dict[str, set[int]]
) -> tuple[list[Finding], list[Finding]]:
    """Split findings into (in-diff, out-of-diff)."""
    in_diff: list[Finding] = []
    out_of_diff: list[Finding] = []
    for f in findings:
        if f.line in valid_lines.get(f.path, set()):
            in_diff.append(f)
        else:
            out_of_diff.append(f)
    return in_diff, out_of_diff


def run_threads_only(target: str, config: Config) -> int:
    """Lightweight mode: validate human replies to prior AI comments only."""
    from .hosts import resolve_host

    key_problem = model_key_missing(config)
    if key_problem:
        sys.stderr.write(key_problem + "\n")
        return 1

    host = resolve_host(target)
    pr_number, url_repo = host.parse_target(target)
    repo = config.repo or url_repo or host.detect_repo()
    handle = resolve_handle(config.handle)
    sig = build_signature(config.model, handle)

    sys.stderr.write(f"Fetching PR #{pr_number} in {repo}...\n")
    pr = host.fetch_pr(repo, pr_number)

    sys.stderr.write("Threads-only mode: fetching prior AI review threads...\n")
    prior_threads = host.fetch_prior_threads(repo, pr_number)
    if not prior_threads:
        sys.stderr.write("No prior threads with human replies found. Nothing to validate.\n")
        return 0

    thread_context = format_thread_context(prior_threads)
    sys.stderr.write(f"Found {len(prior_threads)} thread(s) with human replies\n")

    user_prompt = THREADS_ONLY_USER_TEMPLATE.format(
        pr_number=pr_number,
        repo=repo,
        title=pr.title,
        head=pr.head,
        thread_context=thread_context,
    )

    sys.stderr.write(f"Calling model (threads-only) {config.model}...\n")
    try:
        raw = call_model(THREADS_ONLY_SYSTEM_PROMPT, user_prompt, config)
        result = parse_review_json(raw)
    except ReviewFailedError as e:
        sys.stderr.write(f"Thread validation failed: {e.reason}\n")
        return e.exit_code

    thread_responses = result.get("thread_responses", [])
    if not thread_responses:
        sys.stderr.write("Model returned no thread responses.\n")
        return 0

    if config.dry_run:
        print("=" * 80)
        print(f"THREAD RESPONSES ({len(thread_responses)}) - threads-only mode:")
        print("=" * 80)
        for tr in thread_responses:
            disp = tr.get("disposition", "?")
            cid = tr.get("comment_id", "?")
            reply = tr.get("reply", "")
            print(f"  [{disp}] comment {cid}: {reply}")
        print()
        sys.stderr.write(f"\nDry run complete. {len(thread_responses)} thread responses.\n")
        return 0

    sys.stderr.write(f"Posting {len(thread_responses)} thread response(s)...\n")
    posted = host.post_thread_responses(repo, pr_number, thread_responses, sig)
    sys.stderr.write(f"Posted {posted}/{len(thread_responses)} thread responses\n")
    return 0


def _print_review_preview(
    model: str,
    event: str,
    body: str,
    findings: list[Finding],
    sig: str,
    thread_responses: list[dict[str, Any]],
) -> None:
    """Print the full review (summary body + inline comments + thread replies).

    Shared by dry-run and the approval gate so the user sees exactly what would
    post before deciding.
    """
    print("=" * 80)
    print(f"REVIEW BODY ({event}) - reviewed by {model}:")
    print("=" * 80)
    print(body)
    print()
    for i, f in enumerate(findings, 1):
        print("=" * 80)
        print(f"INLINE COMMENT {i}/{len(findings)} - {f.path}:{f.line}")
        print("=" * 80)
        print(format_inline_comment(f, sig))
        print()
    if thread_responses:
        print("=" * 80)
        print(f"THREAD RESPONSES ({len(thread_responses)}):")
        print("=" * 80)
        for tr in thread_responses:
            disp = tr.get("disposition", "?")
            cid = tr.get("comment_id", "?")
            reply = tr.get("reply", "")
            print(f"  [{disp}] comment {cid}: {reply}")
        print()


def _approve_before_post(
    repo: str,
    pr_number: int,
    handle: str | None,
    model: str,
    event: str,
    body: str,
    findings: list[Finding],
    sig: str,
    thread_responses: list[dict[str, Any]],
) -> bool:
    """Preview the review and ask the user to approve posting it.

    Returns True to post, False to skip. When there's no interactive terminal
    (piped/CI), approval can't be collected, so it refuses to post (safe default)
    and tells the user how to proceed.
    """
    if not sys.stdin.isatty():
        sys.stderr.write(
            "Approval required (--approve / VIGILANT_REQUIRE_APPROVAL) but no interactive "
            "terminal to confirm; not posting. Run in a terminal to approve, or disable with "
            "--no-approve / VIGILANT_REQUIRE_APPROVAL=0 to auto-post.\n"
        )
        return False

    _print_review_preview(model, event, body, findings, sig, thread_responses)
    as_who = f"@{handle}" if handle else "you"
    tally = f"{len(findings)} inline comment(s), {len(thread_responses)} thread reply(ies)"
    try:
        answer = input(
            f"\nPost this {event} review on {repo}#{pr_number} as {as_who}? "
            f"[{tally}]  (y/N): "
        ).strip().lower()
    except EOFError:
        answer = ""
    if answer in ("y", "yes"):
        return True
    sys.stderr.write("Skipped: review not posted.\n")
    return False


def run_review(target: str, config: Config) -> int:
    """Run a full PR review and post it on behalf of the configured user.

    Returns a process-style exit code: 0 success, 1 config error, 2 Anthropic
    error, 3 GitHub error (the latter raised via util.run -> sys.exit).
    """
    from .hosts import resolve_host

    model = config.model
    profile = MODEL_PROFILES.get(model, GENERIC_PROFILE)

    key_problem = model_key_missing(config)
    if key_problem:
        sys.stderr.write(key_problem + "\n")
        return 1

    host = resolve_host(target)
    pr_number, url_repo = host.parse_target(target)
    repo = config.repo or url_repo or host.detect_repo()
    handle = resolve_handle(config.handle)
    sig = build_signature(model, handle)

    sys.stderr.write(f"Fetching PR #{pr_number} in {repo}...\n")
    pr = host.fetch_pr(repo, pr_number)

    if pr.is_draft:
        sys.stderr.write("PR is a draft. Continuing anyway (explicit invocation).\n")

    sys.stderr.write("Reading repo guidance files...\n")
    guidance = host.read_guidance(repo, pr.head_sha)

    # Re-review state: what SHA we last reviewed, and every finding already raised.
    head_sha = pr.head_sha
    last_reviewed_sha = host.last_review_sha(repo, pr_number)
    prior_sigs = host.prior_finding_signatures(repo, pr_number)
    is_rerun = bool(last_reviewed_sha) or bool(prior_sigs)

    review_scope_note = ""
    if last_reviewed_sha and last_reviewed_sha != head_sha:
        incr = host.incremental_diff(repo, last_reviewed_sha, head_sha)
        if incr:
            pr.diff = incr
            review_scope_note = (
                "\nNOTE: This is an INCREMENTAL diff containing only changes since the last "
                f"review (commit {last_reviewed_sha[:7]}). Review only these changes; do not "
                "re-raise issues on code outside this diff.\n"
            )
            sys.stderr.write(f"Incremental review: diff since {last_reviewed_sha[:7]}\n")

    # When the diff adds imports or edits a manifest, fetch the declared
    # dependency manifests so the model can verify version/import claims instead
    # of guessing (guards against false "dependency missing/too old" findings).
    dependency_manifests = "(none fetched - the diff does not appear to touch dependencies)"
    if diff_touches_dependencies(pr.diff):
        sys.stderr.write("Diff touches imports/dependencies; fetching dependency manifests...\n")
        fetched = host.read_dependency_manifests(repo, head_sha, dependency_search_dirs(pr.diff))
        if fetched.strip():
            dependency_manifests = fetched
        else:
            dependency_manifests = (
                "(diff touches dependencies, but no known manifest file was found at the PR head)"
            )

    sys.stderr.write("Fetching prior AI review threads...\n")
    prior_threads = host.fetch_prior_threads(repo, pr_number)
    thread_context = format_thread_context(prior_threads)
    if prior_threads:
        sys.stderr.write(f"Found {len(prior_threads)} prior thread(s) with human replies\n")
        prior_threads_section = (
            "\nPrior AI review threads with human responses "
            "(evaluate each and include in thread_responses):\n"
            f"{thread_context}\n"
        )
    else:
        prior_threads_section = ""

    user_prompt = USER_PROMPT_TEMPLATE.format(
        pr_number=pr_number,
        repo=repo,
        today=datetime.now(UTC).date().isoformat(),
        title=pr.title,
        body=pr.body or "(empty)",
        base=pr.base,
        head=pr.head,
        head_sha=pr.head_sha,
        file_count=pr.changed_files,
        review_scope_note=review_scope_note,
        guidance=guidance,
        dependency_manifests=dependency_manifests,
        diff=pr.diff,
        prior_threads_section=prior_threads_section,
    )

    sys.stderr.write(
        f"Calling model [{profile['tier_label']}] {model} {profile['signature_suffix']}...\n"
    )

    try:
        raw = call_model(SYSTEM_PROMPT, user_prompt, config)
        review = parse_review_json(raw)
    except ReviewFailedError as e:
        if not config.dry_run:
            host.post_failure_comment(repo, pr_number, e.reason, model)
        return e.exit_code

    raw_findings = [
        Finding(
            severity=f.get("severity", "nit"),
            path=f.get("path", ""),
            line=int(f.get("line", 1)),
            title=f.get("title", "").strip(),
            body=f.get("body", "").strip(),
        )
        for f in review.get("findings", [])
        if f.get("path") and f.get("line")
    ]

    # Verifiability guardrail: a finding whose own reasoning admits it couldn't be
    # verified from the diff must not block a merge. Cap those to nit.
    raw_findings, downgraded = downgrade_unverifiable(raw_findings)
    if downgraded:
        sys.stderr.write(
            f"Capped {downgraded} unverifiable finding(s) to nit "
            "(their own reasoning could not confirm the claim from the diff).\n"
        )

    # Dedup: never repost a finding already raised on this PR (by path+title).
    if prior_sigs:
        deduped = [
            f for f in raw_findings
            if (f.path, _norm_title(f.title)) not in prior_sigs
        ]
        suppressed = len(raw_findings) - len(deduped)
        if suppressed:
            sys.stderr.write(f"Suppressed {suppressed} finding(s) already raised on this PR.\n")
        raw_findings = deduped

    # Nit ratchet: on a re-review, only escalate genuinely new findings at the
    # kept severities (incremental scoping already restricts to changed lines).
    if is_rerun:
        before = len(raw_findings)
        raw_findings = [f for f in raw_findings if f.severity in RERUN_KEEP_SEVERITIES]
        ratcheted = before - len(raw_findings)
        if ratcheted:
            kept = "/".join(sorted(RERUN_KEEP_SEVERITIES))
            sys.stderr.write(
                f"Re-review ratchet: suppressed {ratcheted} finding(s) below [{kept}].\n"
            )

    valid_lines = parse_diff_lines(pr.diff)
    in_diff_findings, dropped_findings = filter_to_diff_lines(raw_findings, valid_lines)
    if dropped_findings:
        sys.stderr.write(
            f"Dropping {len(dropped_findings)} finding(s) whose line is outside the diff:\n"
        )
        for f in dropped_findings:
            sys.stderr.write(f"  - {f.path}:{f.line} ({f.severity_marker}) {f.title}\n")

    findings, overflow = cap_nits(in_diff_findings)
    body = format_review_body(review, findings, sig, head_sha)
    if overflow:
        body += f"\n\n*Plus {overflow} additional nits not posted inline.*"
    if dropped_findings:
        body += (
            f"\n\n*Plus {len(dropped_findings)} finding(s) on lines outside the diff "
            f"(would have been rejected as inline comments by the GitHub API):*\n\n"
            + "\n".join(
                f"- **{f.severity_marker}** `{f.path}:{f.line}` - {f.title}"
                for f in dropped_findings
            )
        )

    if config.attribution:
        body += "\n\n" + build_footnote(model, handle)

    thread_responses = review.get("thread_responses", [])
    event = decide_event(findings + dropped_findings, thread_responses)

    # On a re-review with nothing new to say, stay silent rather than posting a
    # "No findings" review on every push.
    if is_rerun and not findings and not dropped_findings and not thread_responses:
        sys.stderr.write(
            "Re-review: no new findings, demotions, or thread responses - skipping post.\n"
        )
        if config.dry_run:
            print("Re-review with nothing new; would skip posting.")
        return 0

    if config.dry_run:
        _print_review_preview(model, event, body, findings, sig, thread_responses)
        sys.stderr.write(
            f"\nDry run complete. {len(findings)} findings, "
            f"{len(thread_responses)} thread responses ({event}) from {model}.\n"
        )
        return 0

    if config.require_approval and not _approve_before_post(
        repo, pr_number, handle, model, event, body, findings, sig, thread_responses
    ):
        return 0

    sys.stderr.write(
        f"Posting {event} review with {len(findings)} inline comments (model: {model})...\n"
    )
    url = host.post_review(repo, pr_number, head_sha, body, event, findings, sig)
    sys.stderr.write(f"Posted: {url}\n")

    if thread_responses:
        sys.stderr.write(f"Posting {len(thread_responses)} thread response(s)...\n")
        posted = host.post_thread_responses(repo, pr_number, thread_responses, sig)
        sys.stderr.write(f"Posted {posted}/{len(thread_responses)} thread responses\n")

    return 0
