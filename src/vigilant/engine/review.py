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
    build_signature,
    is_signed_comment,
    resolve_handle,
    signature_index,
)
from .providers import call_model, missing_key_message, provider_api_key, resolve_provider
from .util import run

# On a re-review, only these severities are posted. Nits are suppressed so re-runs
# stop drilling into style minutiae on code that already passed an earlier pass.
RERUN_KEEP_SEVERITIES = {"critical", "medium"}

# HTML-comment marker embedded in every posted review body recording the head SHA
# the review ran against. Lets a later run scope itself to the diff since the last
# review (incremental re-review).
_SHA_MARKER_RE = re.compile(r"<!--\s*ai-review-sha:\s*([0-9a-f]{7,40})\s*-->")


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


def detect_repo() -> str:
    """Detect owner/repo from the current git directory."""
    out = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], check=False)
    if not out.strip():
        sys.stderr.write("Could not detect repo. Pass --repo OWNER/REPO explicitly.\n")
        sys.exit(1)
    return out.strip()


def parse_pr_arg(arg: str) -> tuple[int, str | None]:
    """Parse a PR number or full URL. Returns (pr_number, repo_or_None)."""
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/pull/(\d+)", arg)
    if m:
        return int(m.group(2)), m.group(1)
    if arg.isdigit():
        return int(arg), None
    sys.stderr.write(f"Invalid PR argument: {arg}\n")
    sys.exit(1)


def fetch_pr(repo: str, pr_number: int) -> dict[str, Any]:
    """Fetch PR metadata and diff via gh."""
    meta_json = run([
        "gh", "pr", "view", str(pr_number),
        "--repo", repo,
        "--json",
        "number,title,body,baseRefName,headRefName,headRefOid,files,additions,deletions,changedFiles,isDraft",
    ])
    meta: dict[str, Any] = json.loads(meta_json)
    diff = run(["gh", "pr", "diff", str(pr_number), "--repo", repo])
    meta["diff"] = diff
    return meta


def read_guidance(repo: str, head_sha: str) -> str:
    """Read AGENTS.md, CLAUDE.md, REVIEW.md from the PR head SHA if present."""
    parts: list[str] = []
    for fname in ("AGENTS.md", "CLAUDE.md", "REVIEW.md"):
        out = run(
            ["gh", "api", f"repos/{repo}/contents/{fname}?ref={head_sha}",
             "-H", "Accept: application/vnd.github.raw", "-q", "."],
            check=False,
        )
        if out.strip() and not out.strip().startswith("{"):
            parts.append(f"### {fname}\n\n{out.strip()}")
    return "\n\n".join(parts) if parts else "(no AGENTS.md / CLAUDE.md / REVIEW.md at repo root)"


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


def fetch_prior_threads(repo: str, pr_number: int) -> list[PriorThread]:
    """Fetch prior AI review comments and their human reply threads.

    Returns only threads that have at least one human reply.
    """
    raw = run(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}/comments",
         "--paginate", "--jq", ".[]"],
        check=False,
    )
    if not raw.strip():
        return []

    comments: list[dict[str, Any]] = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            comments.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    bot_comments: dict[int, dict[str, Any]] = {}
    replies_by_parent: dict[int, list[dict[str, str]]] = {}

    for c in comments:
        cid = c.get("id", 0)
        body = c.get("body", "")
        user = c.get("user", {}).get("login", "")
        in_reply_to = c.get("in_reply_to_id")

        if is_signed_comment(body) and not in_reply_to:
            bot_comments[cid] = c
        elif in_reply_to and in_reply_to in bot_comments:
            if user != "github-actions[bot]":
                replies_by_parent.setdefault(in_reply_to, []).append({
                    "user": user,
                    "body": body,
                })

    threads: list[PriorThread] = []
    for cid, c in bot_comments.items():
        replies = replies_by_parent.get(cid, [])
        if not replies:
            continue

        body = c.get("body", "")
        severity = "nit"
        title = ""
        sev_match = re.search(
            r"\*\*(?:\U0001f534|\U0001f7e0|\U0001f7e1)\s*(Critical|Medium|Nit)\*\*\s*-\s*(.+?)(?:\n|$)",
            body,
        )
        if sev_match:
            severity = sev_match.group(1).lower()
            title = sev_match.group(2).strip()

        threads.append(PriorThread(
            comment_id=cid,
            path=c.get("path", ""),
            line=c.get("line") or c.get("original_line") or 0,
            severity=severity,
            title=title,
            bot_body=body,
            replies=replies,
        ))

    return threads


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


def fetch_last_bot_review_sha(repo: str, pr_number: int) -> str | None:
    """Return the head SHA the most recent bot review ran against."""
    raw = run(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}/reviews",
         "--paginate", "--jq", ".[]"],
        check=False,
    )
    last_sha: str | None = None
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rv = json.loads(line)
        except json.JSONDecodeError:
            continue
        body = rv.get("body", "") or ""
        if not is_signed_comment(body):
            continue
        m = _SHA_MARKER_RE.search(body)
        if m:
            last_sha = m.group(1)  # reviews returned in chronological order
    return last_sha


def _norm_title(title: str) -> str:
    """Normalize a finding title for dedup: collapse whitespace, drop trailing
    period, lowercase."""
    return re.sub(r"\s+", " ", title).strip().rstrip(".").lower()


_FINDING_TITLE_RE = re.compile(r"\*\*[^\n*][^\n]*?\*\*\s*-\s*(.+)")
_TABLE_ROW_RE = re.compile(r"^\|[^|]*\|\s*`([^`:]+):\d+`\s*\|\s*(.+?)\s*\|\s*$")


def fetch_prior_finding_signatures(repo: str, pr_number: int) -> set[tuple[str, str]]:
    """Collect (path, normalized-title) signatures of every finding already posted."""
    sigs: set[tuple[str, str]] = set()

    raw = run(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}/comments",
         "--paginate", "--jq", ".[]"],
        check=False,
    )
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        body = c.get("body", "") or ""
        if not is_signed_comment(body) or c.get("in_reply_to_id"):
            continue
        path = c.get("path", "")
        m = _FINDING_TITLE_RE.search(body)
        if path and m:
            sigs.add((path, _norm_title(m.group(1))))

    raw_reviews = run(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}/reviews",
         "--paginate", "--jq", ".[]"],
        check=False,
    )
    for line in raw_reviews.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rv = json.loads(line)
        except json.JSONDecodeError:
            continue
        body = rv.get("body", "") or ""
        if not is_signed_comment(body):
            continue
        for row in body.splitlines():
            rm = _TABLE_ROW_RE.match(row.strip())
            if rm:
                sigs.add((rm.group(1).strip(), _norm_title(rm.group(2))))

    return sigs


def get_incremental_diff(repo: str, base_sha: str, head_sha: str) -> str | None:
    """Return the unified diff between two SHAs via the compare API, or None."""
    if not base_sha or base_sha == head_sha:
        return None
    out = run(
        ["gh", "api", f"repos/{repo}/compare/{base_sha}...{head_sha}",
         "-H", "Accept: application/vnd.github.diff"],
        check=False,
    )
    return out if out.strip() else None


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


def parse_review_json(text: str) -> dict[str, Any]:
    """Extract the JSON object from the model's response, tolerant of stray prose."""
    try:
        parsed: dict[str, Any] = json.loads(text)
        return parsed
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        sys.stderr.write("Model did not return parseable JSON. Raw output:\n")
        sys.stderr.write(text)
        raise ReviewFailedError("Model response was not parseable JSON")
    try:
        parsed = json.loads(m.group(0))
        return parsed
    except json.JSONDecodeError as e:
        sys.stderr.write(f"JSON parse failed: {e}\nRaw output:\n{text}\n")
        raise ReviewFailedError(f"Model response JSON parse failed: {e}") from e


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

    table_rows = []
    for f in findings:
        table_rows.append(f"| {f.severity_marker} | `{f.path}:{f.line}` | {f.title} |")
    table = "\n".join(table_rows) if table_rows else "| (none) | | |"

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
        f"| Severity | File:Line | Issue |\n"
        f"| --- | --- | --- |\n"
        f"{table}"
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


def post_review(
    repo: str,
    pr_number: int,
    head_sha: str,
    body: str,
    event: str,
    findings: list[Finding],
    sig: str,
) -> str:
    """Post the review with all inline comments in a single API call."""
    inline = []
    for f in findings:
        inline.append({
            "path": f.path,
            "line": f.line,
            "side": "RIGHT",
            "body": format_inline_comment(f, sig),
        })
    def _submit(review_event: str) -> str:
        payload = {
            "commit_id": head_sha,
            "body": body,
            "event": review_event,
            "comments": inline,
        }
        out = run([
            "gh", "api",
            f"repos/{repo}/pulls/{pr_number}/reviews",
            "--method", "POST",
            "--input", "-",
        ], input_text=json.dumps(payload))
        parsed = json.loads(out)
        return str(parsed.get("html_url", ""))

    try:
        return _submit(event)
    except SystemExit:
        # GitHub rejects APPROVE on your own PR (and a few other cases). Rather
        # than lose the whole review, fall back to a plain COMMENT so the
        # findings still post.
        if event == "APPROVE":
            sys.stderr.write("APPROVE rejected by GitHub; falling back to COMMENT.\n")
            return _submit("COMMENT")
        raise


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


def post_thread_responses(
    repo: str,
    pr_number: int,
    thread_responses: list[dict[str, Any]],
    sig: str,
) -> int:
    """Post reply comments to prior AI review threads based on model validation."""
    posted = 0
    for tr in thread_responses:
        comment_id = tr.get("comment_id")
        disposition = tr.get("disposition", "acknowledged")
        reply_text = tr.get("reply", "")
        if not comment_id or not reply_text:
            continue

        marker = "Validated" if disposition == "acknowledged" else "Re-flagged"
        body = f"{sig}\n\n**{marker}** - {reply_text}"

        try:
            run([
                "gh", "api",
                f"repos/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
                "--method", "POST",
                "-f", f"body={body}",
            ])
            posted += 1
        except SystemExit:
            sys.stderr.write(f"Failed to post thread response to comment {comment_id}\n")
    return posted


def post_failure_comment(repo: str, pr_number: int, reason: str, model: str) -> None:
    """Post a PR comment explaining that the automated review could not complete.

    Unlike a review, this stays visible (it warns that no review ran and the PR
    still needs a human), but carries only the hidden marker - no bot framing.
    """
    body = (
        "**Automated review could not complete.**\n\n"
        f"Reason: {reason}\n\n"
        "This PR did not get a review pass and still needs a human review. "
        "To retry, re-run Vigilant PR against this PR.\n\n"
        f"{build_signature(model)}"
    )
    try:
        run(
            ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", body],
            check=True,
        )
        sys.stderr.write(f"Posted failure comment to PR #{pr_number}\n")
    except SystemExit:
        sys.stderr.write("Failed to post failure comment to PR (gh command failed)\n")


def run_threads_only(target: str, config: Config) -> int:
    """Lightweight mode: validate human replies to prior AI comments only."""
    provider, _ = resolve_provider(config.model)
    if provider not in ("mock", "ollama") and not provider_api_key(provider):
        sys.stderr.write(missing_key_message(provider) + "\n")
        return 1

    pr_number, url_repo = parse_pr_arg(target)
    repo = config.repo or url_repo or detect_repo()
    handle = resolve_handle(config.handle)
    sig = build_signature(config.model, handle)

    sys.stderr.write(f"Fetching PR #{pr_number} in {repo}...\n")
    pr = fetch_pr(repo, pr_number)

    sys.stderr.write("Threads-only mode: fetching prior AI review threads...\n")
    prior_threads = fetch_prior_threads(repo, pr_number)
    if not prior_threads:
        sys.stderr.write("No prior threads with human replies found. Nothing to validate.\n")
        return 0

    thread_context = format_thread_context(prior_threads)
    sys.stderr.write(f"Found {len(prior_threads)} thread(s) with human replies\n")

    user_prompt = THREADS_ONLY_USER_TEMPLATE.format(
        pr_number=pr_number,
        repo=repo,
        title=pr.get("title", ""),
        head=pr.get("headRefName", ""),
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
    posted = post_thread_responses(repo, pr_number, thread_responses, sig)
    sys.stderr.write(f"Posted {posted}/{len(thread_responses)} thread responses\n")
    return 0


def run_review(target: str, config: Config) -> int:
    """Run a full PR review and post it on behalf of the configured user.

    Returns a process-style exit code: 0 success, 1 config error, 2 Anthropic
    error, 3 GitHub error (the latter raised via util.run -> sys.exit).
    """
    model = config.model
    profile = MODEL_PROFILES.get(model, GENERIC_PROFILE)

    provider, _ = resolve_provider(model)
    if provider not in ("mock", "ollama") and not provider_api_key(provider):
        sys.stderr.write(missing_key_message(provider) + "\n")
        return 1

    pr_number, url_repo = parse_pr_arg(target)
    repo = config.repo or url_repo or detect_repo()
    handle = resolve_handle(config.handle)
    sig = build_signature(model, handle)

    sys.stderr.write(f"Fetching PR #{pr_number} in {repo}...\n")
    pr = fetch_pr(repo, pr_number)

    if pr.get("isDraft"):
        sys.stderr.write("PR is a draft. Continuing anyway (explicit invocation).\n")

    sys.stderr.write("Reading repo guidance files...\n")
    guidance = read_guidance(repo, pr["headRefOid"])

    # Re-review state: what SHA we last reviewed, and every finding already raised.
    head_sha = pr["headRefOid"]
    last_reviewed_sha = fetch_last_bot_review_sha(repo, pr_number)
    prior_sigs = fetch_prior_finding_signatures(repo, pr_number)
    is_rerun = bool(last_reviewed_sha) or bool(prior_sigs)

    review_scope_note = ""
    if last_reviewed_sha and last_reviewed_sha != head_sha:
        incr = get_incremental_diff(repo, last_reviewed_sha, head_sha)
        if incr:
            pr["diff"] = incr
            review_scope_note = (
                "\nNOTE: This is an INCREMENTAL diff containing only changes since the last "
                f"review (commit {last_reviewed_sha[:7]}). Review only these changes; do not "
                "re-raise issues on code outside this diff.\n"
            )
            sys.stderr.write(f"Incremental review: diff since {last_reviewed_sha[:7]}\n")

    sys.stderr.write("Fetching prior AI review threads...\n")
    prior_threads = fetch_prior_threads(repo, pr_number)
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
        title=pr.get("title", ""),
        body=pr.get("body", "") or "(empty)",
        base=pr.get("baseRefName", ""),
        head=pr.get("headRefName", ""),
        head_sha=pr.get("headRefOid", ""),
        file_count=pr.get("changedFiles", 0),
        review_scope_note=review_scope_note,
        guidance=guidance,
        diff=pr["diff"],
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
            post_failure_comment(repo, pr_number, e.reason, model)
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

    valid_lines = parse_diff_lines(pr["diff"])
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
        sys.stderr.write(
            f"\nDry run complete. {len(findings)} findings, "
            f"{len(thread_responses)} thread responses ({event}) from {model}.\n"
        )
        return 0

    sys.stderr.write(
        f"Posting {event} review with {len(findings)} inline comments (model: {model})...\n"
    )
    url = post_review(repo, pr_number, pr["headRefOid"], body, event, findings, sig)
    sys.stderr.write(f"Posted: {url}\n")

    if thread_responses:
        sys.stderr.write(f"Posting {len(thread_responses)} thread response(s)...\n")
        posted = post_thread_responses(repo, pr_number, thread_responses, sig)
        sys.stderr.write(f"Posted {posted}/{len(thread_responses)} thread responses\n")

    return 0
