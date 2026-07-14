"""Host provider interface.

Isolates every git-host-specific operation (fetch a PR + diff, read prior review
state, post a review) behind one contract so the review engine stays fully
host-agnostic. This mirrors the model-provider pattern in `providers.py`: there,
a `provider/model` string selects a backend; here, the review target (a PR URL
or a bare number in the current repo) selects a host.

GitHub is the only implementation shipped today. A new host (GitLab, Bitbucket,
...) is an additive class that satisfies `HostProvider` plus one registry entry -
no change to the engine. See `docs/plans/host-provider_spec.md`.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .identity import build_signature, is_signed_comment
from .review import Finding, PriorThread, _norm_title, format_inline_comment
from .util import run

# HTML-comment marker embedded in every posted review body recording the head SHA
# the review ran against, so a later run can scope itself to the diff since the
# last review (incremental re-review).
_SHA_MARKER_RE = re.compile(r"<!--\s*ai-review-sha:\s*([0-9a-f]{7,40})\s*-->")

_FINDING_TITLE_RE = re.compile(r"\*\*[^\n*][^\n]*?\*\*\s*-\s*(.+)")
_TABLE_ROW_RE = re.compile(r"^\|[^|]*\|\s*`([^`:]+):\d+`\s*\|\s*(.+?)\s*\|\s*$")


@dataclass
class PullRequest:
    """Host-neutral view of a pull/merge request.

    Field names are deliberately host-agnostic (e.g. `head_sha`, not GitHub's
    `headRefOid`) so no host's payload shape leaks into the engine. `diff` is
    mutable so the engine can swap in an incremental diff for a re-review.
    """

    repo: str
    number: int
    title: str
    body: str
    base: str
    head: str
    head_sha: str
    changed_files: int
    is_draft: bool
    diff: str


@runtime_checkable
class HostProvider(Protocol):
    """Everything the review engine needs from a git host.

    Read side returns normalized data; write side takes an already-formatted
    review (summary body, chosen event, formatted inline findings) and maps it to
    the host's own API. No finding detection, severity, or decision logic ever
    lives in a host - that is the engine's job.
    """

    id: str

    # --- read side --------------------------------------------------------
    def parse_target(self, arg: str) -> tuple[int, str | None]: ...
    def detect_repo(self) -> str: ...
    def fetch_pr(self, repo: str, number: int) -> PullRequest: ...
    def read_guidance(self, repo: str, head_sha: str) -> str: ...
    def fetch_prior_threads(self, repo: str, number: int) -> list[PriorThread]: ...
    def last_review_sha(self, repo: str, number: int) -> str | None: ...
    def prior_finding_signatures(self, repo: str, number: int) -> set[tuple[str, str]]: ...
    def incremental_diff(self, repo: str, base_sha: str, head_sha: str) -> str | None: ...

    # --- write side -------------------------------------------------------
    def post_review(
        self,
        repo: str,
        number: int,
        head_sha: str,
        body: str,
        event: str,
        findings: list[Finding],
        sig: str,
    ) -> str: ...
    def post_thread_responses(
        self, repo: str, number: int, thread_responses: list[dict[str, Any]], sig: str
    ) -> int: ...
    def post_failure_comment(self, repo: str, number: int, reason: str, model: str) -> None: ...


class GitHubHost:
    """GitHub implementation, backed by the `gh` CLI."""

    id = "github"

    def parse_target(self, arg: str) -> tuple[int, str | None]:
        """Parse a PR number or full github.com URL into (number, repo_or_None)."""
        m = re.match(r"https?://github\.com/([^/]+/[^/]+)/pull/(\d+)", arg)
        if m:
            return int(m.group(2)), m.group(1)
        if arg.isdigit():
            return int(arg), None
        sys.stderr.write(f"Invalid PR argument: {arg}\n")
        sys.exit(1)

    def detect_repo(self) -> str:
        out = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], check=False)
        if not out.strip():
            sys.stderr.write("Could not detect repo. Pass --repo OWNER/REPO explicitly.\n")
            sys.exit(1)
        return out.strip()

    def fetch_pr(self, repo: str, number: int) -> PullRequest:
        meta_json = run([
            "gh", "pr", "view", str(number),
            "--repo", repo,
            "--json",
            "number,title,body,baseRefName,headRefName,headRefOid,files,additions,deletions,changedFiles,isDraft",
        ])
        meta: dict[str, Any] = json.loads(meta_json)
        diff = run(["gh", "pr", "diff", str(number), "--repo", repo])
        return PullRequest(
            repo=repo,
            number=number,
            title=meta.get("title", "") or "",
            body=meta.get("body") or "",
            base=meta.get("baseRefName", "") or "",
            head=meta.get("headRefName", "") or "",
            head_sha=meta.get("headRefOid", "") or "",
            changed_files=int(meta.get("changedFiles", 0) or 0),
            is_draft=bool(meta.get("isDraft")),
            diff=diff,
        )

    def read_guidance(self, repo: str, head_sha: str) -> str:
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

    def fetch_prior_threads(self, repo: str, number: int) -> list[PriorThread]:
        raw = run(
            ["gh", "api", f"repos/{repo}/pulls/{number}/comments",
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

    def last_review_sha(self, repo: str, number: int) -> str | None:
        raw = run(
            ["gh", "api", f"repos/{repo}/pulls/{number}/reviews",
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

    def prior_finding_signatures(self, repo: str, number: int) -> set[tuple[str, str]]:
        sigs: set[tuple[str, str]] = set()

        raw = run(
            ["gh", "api", f"repos/{repo}/pulls/{number}/comments",
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
            ["gh", "api", f"repos/{repo}/pulls/{number}/reviews",
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

    def incremental_diff(self, repo: str, base_sha: str, head_sha: str) -> str | None:
        if not base_sha or base_sha == head_sha:
            return None
        out = run(
            ["gh", "api", f"repos/{repo}/compare/{base_sha}...{head_sha}",
             "-H", "Accept: application/vnd.github.diff"],
            check=False,
        )
        return out if out.strip() else None

    def post_review(
        self,
        repo: str,
        number: int,
        head_sha: str,
        body: str,
        event: str,
        findings: list[Finding],
        sig: str,
    ) -> str:
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
                f"repos/{repo}/pulls/{number}/reviews",
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

    def post_thread_responses(
        self, repo: str, number: int, thread_responses: list[dict[str, Any]], sig: str
    ) -> int:
        posted = 0
        for tr in thread_responses:
            comment_id = tr.get("comment_id")
            disposition = tr.get("disposition", "acknowledged")
            reply_text = tr.get("reply", "")
            if not comment_id or not reply_text:
                continue

            marker = "Validated" if disposition == "acknowledged" else "Re-flagged"
            reply_body = f"{sig}\n\n**{marker}** - {reply_text}"

            try:
                run([
                    "gh", "api",
                    f"repos/{repo}/pulls/{number}/comments/{comment_id}/replies",
                    "--method", "POST",
                    "-f", f"body={reply_body}",
                ])
                posted += 1
            except SystemExit:
                sys.stderr.write(f"Failed to post thread response to comment {comment_id}\n")
        return posted

    def post_failure_comment(self, repo: str, number: int, reason: str, model: str) -> None:
        body = (
            "**Automated review could not complete.**\n\n"
            f"Reason: {reason}\n\n"
            "This PR did not get a review pass and still needs a human review. "
            "To retry, re-run Vigilant PR against this PR.\n\n"
            f"{build_signature(model)}"
        )
        try:
            run(
                ["gh", "pr", "comment", str(number), "--repo", repo, "--body", body],
                check=True,
            )
            sys.stderr.write(f"Posted failure comment to PR #{number}\n")
        except SystemExit:
            sys.stderr.write("Failed to post failure comment to PR (gh command failed)\n")


# Registry of host id -> constructor. Adding a host is one entry plus its class.
HOST_PROVIDERS: dict[str, type] = {
    "github": GitHubHost,
}

# Hosts recognized by URL shape but not yet implemented. Mapped to a short label
# used in the "not supported yet" message so users get a clear signal instead of
# a silent misroute to GitHub.
_UNSUPPORTED_HOSTS = {
    "gitlab": "GitLab",
    "bitbucket": "Bitbucket",
}

_GITLAB_RE = re.compile(r"gitlab\.com|/-/merge_requests/")
_BITBUCKET_RE = re.compile(r"bitbucket\.org|/pull-requests/")


def detect_host(target: str | None) -> str:
    """Map a review target to a host id.

    A gitlab.com / merge-request URL resolves to `gitlab`; a bitbucket URL to
    `bitbucket`; everything else (a github.com PR URL, a bare PR number, or no
    argument) resolves to `github`, the default host.
    """
    if target:
        if _GITLAB_RE.search(target):
            return "gitlab"
        if _BITBUCKET_RE.search(target):
            return "bitbucket"
    return "github"


def resolve_host(target: str | None = None) -> HostProvider:
    """Return the concrete `HostProvider` for a review target.

    Unknown-but-recognized hosts (e.g. GitLab today) exit cleanly with an
    actionable message rather than misrouting to GitHub or raising a traceback.
    """
    host_id = detect_host(target)
    ctor = HOST_PROVIDERS.get(host_id)
    if ctor is None:
        label = _UNSUPPORTED_HOSTS.get(host_id, host_id)
        sys.stderr.write(
            f"{label} is not supported yet - Vigilant PR currently reviews GitHub PRs only.\n"
            "Track/host support is a planned addition (see docs/plans/host-provider_spec.md).\n"
        )
        sys.exit(1)
    host: HostProvider = ctor()
    return host
