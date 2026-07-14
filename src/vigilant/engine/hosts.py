"""Host provider interface.

Isolates every git-host-specific operation (fetch a PR + diff, read prior review
state, post a review) behind one contract so the review engine stays fully
host-agnostic. This mirrors the model-provider pattern in `providers.py`: there,
a `provider/model` string selects a backend; here, the review target (a PR URL
or a bare number in the current repo) selects a host.

GitHub is the only implementation shipped today. A new host (GitLab, Bitbucket,
...) is an additive class that satisfies `HostProvider` plus one registry entry -
no change to the engine. See `docs/plans/host-provider_spec.md`.

Phase 1 note: `GitHubHost` delegates to the existing, tested `gh` helpers in
`review.py`. The value here is the interface, the normalized `PullRequest`, and
routing the engine through it; relocating those helper bodies into the host is
mechanical cleanup that does not touch this contract.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from . import review


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
    def detect_repo(self) -> str: ...
    def fetch_pr(self, repo: str, number: int) -> PullRequest: ...
    def read_guidance(self, repo: str, head_sha: str) -> str: ...
    def fetch_prior_threads(self, repo: str, number: int) -> list[review.PriorThread]: ...
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
        findings: list[review.Finding],
        sig: str,
    ) -> str: ...
    def post_thread_responses(
        self, repo: str, number: int, thread_responses: list[dict[str, Any]], sig: str
    ) -> int: ...
    def post_failure_comment(self, repo: str, number: int, reason: str, model: str) -> None: ...


class GitHubHost:
    """GitHub implementation, backed by the `gh` CLI."""

    id = "github"

    def detect_repo(self) -> str:
        return review.detect_repo()

    def fetch_pr(self, repo: str, number: int) -> PullRequest:
        d = review.fetch_pr(repo, number)
        return PullRequest(
            repo=repo,
            number=number,
            title=d.get("title", "") or "",
            body=d.get("body") or "",
            base=d.get("baseRefName", "") or "",
            head=d.get("headRefName", "") or "",
            head_sha=d.get("headRefOid", "") or "",
            changed_files=int(d.get("changedFiles", 0) or 0),
            is_draft=bool(d.get("isDraft")),
            diff=d.get("diff", "") or "",
        )

    def read_guidance(self, repo: str, head_sha: str) -> str:
        return review.read_guidance(repo, head_sha)

    def fetch_prior_threads(self, repo: str, number: int) -> list[review.PriorThread]:
        return review.fetch_prior_threads(repo, number)

    def last_review_sha(self, repo: str, number: int) -> str | None:
        return review.fetch_last_bot_review_sha(repo, number)

    def prior_finding_signatures(self, repo: str, number: int) -> set[tuple[str, str]]:
        return review.fetch_prior_finding_signatures(repo, number)

    def incremental_diff(self, repo: str, base_sha: str, head_sha: str) -> str | None:
        return review.get_incremental_diff(repo, base_sha, head_sha)

    def post_review(
        self,
        repo: str,
        number: int,
        head_sha: str,
        body: str,
        event: str,
        findings: list[review.Finding],
        sig: str,
    ) -> str:
        return review.post_review(repo, number, head_sha, body, event, findings, sig)

    def post_thread_responses(
        self, repo: str, number: int, thread_responses: list[dict[str, Any]], sig: str
    ) -> int:
        return review.post_thread_responses(repo, number, thread_responses, sig)

    def post_failure_comment(self, repo: str, number: int, reason: str, model: str) -> None:
        review.post_failure_comment(repo, number, reason, model)


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
