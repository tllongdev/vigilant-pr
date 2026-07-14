# Spec: Host Provider Interface

Status: Phase 1 (GitHub) implemented on `develop`. Phase 2 (GitLab) is a follow-up.
Scope owner: Vigilant PR

## Summary

Introduce a **host provider interface** that isolates every git-host-specific
operation (fetching a PR and its diff, reading prior review state, posting a
review) behind a single, stable contract. The adversarial review engine (prompt
building, model call, finding dedup/ratchet, diff-line validation, comment
formatting, and the APPROVE/COMMENT decision) becomes fully host-agnostic and
talks only to that interface.

This mirrors the existing **model provider** pattern (`engine/providers.py`),
where a `provider/model` string selects one of several backends behind one call
site. Here, the target (a PR URL or a bare number in the current repo) selects a
host behind one interface. GitHub is the only implementation shipped in Phase 1;
GitLab (and others) become additive `HostProvider` implementations with zero
changes to the engine.

## Section 1: Context & Constraints

### What already exists (do not change)
- The engine is stdlib-only. All GitHub I/O goes through the `gh` CLI via
  `engine/util.run` (retry/backoff, exit-3 on hard failure).
- The review flow (`engine/review.py::run_review`, `run_threads_only`) is the
  single code path used by every trigger surface (CLI one-shot, `github-watch`,
  `slack-watch`, `teams-watch`). If that path is host-agnostic, all surfaces are.
- Pure, host-agnostic logic already lives as standalone functions in `review.py`:
  `parse_diff_lines`, `parse_review_json`, `format_inline_comment`,
  `format_review_body`, `decide_event`, `cap_nits`, `filter_to_diff_lines`,
  `_norm_title`, and the `Finding` / `PriorThread` dataclasses. These are reused
  by every host and are not part of the host interface.
- The `github-watch` daemon (`engine/watcher.py`) is intrinsically GitHub-shaped
  (it polls `gh search prs --review-requested=@me`). It stays GitHub-specific in
  Phase 1; a future `gitlab-watch` is a separate loop, not a change to this one.
- Comments post under the user's own token with a hidden signature marker.

### Decisions already made (do not re-litigate)
- **The engine must not name a host.** After Phase 1, `run_review` /
  `run_threads_only` contain no `gh`, no `github.com`, and no GitHub-shaped dict
  keys. Everything host-specific is reached through the interface.
- **The PR is normalized.** Hosts return a `PullRequest` dataclass with
  host-neutral field names (`head_sha`, `base`, `head`, `changed_files`,
  `is_draft`, `diff`, ...), not a raw provider payload. This is the contract every
  host must satisfy; it is what keeps GitHub's field naming out of the engine.
- **The review-submission boundary is "submit this finished review".** The engine
  hands the host a fully formatted summary body, the chosen event
  (`APPROVE` / `COMMENT`), and the list of already-formatted inline `Finding`s +
  the signature. The host's only job is to map that to its own review API. No
  finding detection, severity, or decision logic ever lives in a host.
- **Dispatch is by target.** A PR URL selects the host by its domain / URL shape;
  a bare PR number (or no argument) uses the default host (GitHub) against the
  current repo. This matches how users already invoke the tool.
- **Phase 1 is a seam, not a rewrite.** The GitHub implementation may delegate to
  the existing, tested `review.py` `gh` helper functions. The value delivered now
  is the interface + normalized `PullRequest` + host-routed engine. Physically
  relocating each `gh` helper body into the GitHub host is mechanical cleanup that
  can follow without touching the contract.

### Approaches ruled out (do not re-evaluate)
- A raw provider dict as the PR contract: rejected - it leaks GitHub field names
  (`headRefOid`, `baseRefName`, `changedFiles`) into the engine and every future
  host would have to mimic GitHub's exact JSON shape.
- Formatting review/inline comment bodies inside the host: rejected - formatting
  is host-agnostic and shared; only the transport (API mapping) is host-specific.
- Making `github-watch` generic in Phase 1: rejected as premature. Review-request
  discovery differs enough per host that a second watcher is cleaner than a
  parametrized one until there is a real second host to validate against.

### Constraints
- Stdlib-only core preserved; no new runtime dependency for the interface.
- Zero behavior change for GitHub users: for any given PR, the posted review is
  byte-identical to pre-refactor output.
- Existing public imports and test seams keep working
  (`review.parse_pr_arg`, `review.Finding`, `watcher.find_review_requests`,
  `watcher.already_reviewed`, `watcher.fetch_last_bot_review_sha`, etc.).

## Section 2: Requirements

### 2.1 The `PullRequest` contract
A host-neutral dataclass carrying everything the engine needs about a PR:
`repo`, `number`, `title`, `body`, `base`, `head`, `head_sha`, `changed_files`,
`is_draft`, and `diff` (mutable, so the engine can swap in an incremental diff).
Every host constructs this from its own API payload.

### 2.2 The `HostProvider` interface
A single object exposing the operations the engine needs. Read side:
- `detect_repo()` - infer `owner/repo` (or host equivalent) for the current dir.
- `fetch_pr(repo, number) -> PullRequest` - metadata + full diff, normalized.
- `read_guidance(repo, head_sha) -> str` - AGENTS.md / CLAUDE.md / REVIEW.md at
  head, concatenated (empty-marker string when none).
- `fetch_prior_threads(repo, number) -> list[PriorThread]` - prior signed review
  comments that have human replies.
- `last_review_sha(repo, number) -> str | None` - head SHA the last signed review
  ran against (for incremental re-review).
- `prior_finding_signatures(repo, number) -> set[tuple[str, str]]` -
  `(path, normalized-title)` of every finding already posted (dedup).
- `incremental_diff(repo, base_sha, head_sha) -> str | None` - diff between two
  SHAs, or None when not applicable.

Write side:
- `post_review(repo, number, head_sha, body, event, findings, sig) -> str` -
  submit the finished review (summary body + inline comments) in one call;
  return the review URL. Handles host-specific fallbacks (e.g. GitHub rejecting
  `APPROVE` on your own PR degrades to `COMMENT`).
- `post_thread_responses(repo, number, thread_responses, sig) -> int` - post
  replies to prior review threads; return count posted.
- `post_failure_comment(repo, number, reason, model)` - leave a visible "review
  could not complete" comment carrying only the hidden marker.

The interface is defined as a `typing.Protocol` (structural) so a new host is a
class that satisfies it, with no base-class import required.

### 2.3 Dispatch
- `detect_host(target) -> str` maps a target to a host id: a `gitlab.com` /
  `/-/merge_requests/` URL shape resolves to `gitlab`; everything else (a
  `github.com` PR URL, a bare number, or no argument) resolves to `github`.
- `resolve_host(target) -> HostProvider` returns the concrete provider. An
  unsupported-but-recognized host (Phase 1: GitLab) exits cleanly with an
  actionable "not supported yet" message rather than a traceback.
- A `HOST_PROVIDERS` registry maps id -> constructor, so adding a host is one
  registry entry plus the class.

### 2.4 Engine routing
`run_review` and `run_threads_only` obtain `host = resolve_host(target)` once and
call only `host.*` for I/O, operating on the normalized `PullRequest`. No `gh`
invocation and no GitHub-shaped key access remains in either function.

### 2.5 Acceptance criteria (encode as tests)
1. `detect_host` returns `github` for a github.com PR URL, a bare number, and
   `None`; returns `gitlab` for a gitlab.com merge-request URL.
2. `resolve_host(None)` and a github URL yield a `GitHubHost`.
3. `resolve_host` for a GitLab target exits with a clear unsupported message
   (non-zero), not a traceback.
4. `GitHubHost.fetch_pr` maps a representative `gh pr view` JSON payload to a
   `PullRequest` with correct host-neutral fields (esp. `head_sha` from
   `headRefOid`, `is_draft` from `isDraft`, `changed_files` from `changedFiles`).
5. The existing engine and watcher tests still pass unchanged (behavior parity).

### 2.6 Parallelizable / follow-up work
- Phase 2: a `GitLabHost` implementation (via `glab` or the REST API) validated
  against a real merge request; register it and flip `detect_host` for GitLab
  from "unsupported" to that class. No engine change required.
- Cleanup: relocate each `gh` helper body from `review.py` into `GitHubHost`
  (the interface and call sites do not change).
- A future `gitlab-watch` review-request daemon, parallel to `github-watch`.

## Out of scope
- Any change to finding detection, severity, the system prompt, or the
  APPROVE/COMMENT decision.
- Making `github-watch` host-generic in Phase 1.
- Shipping a GitLab (or Bitbucket) implementation in Phase 1 - the interface is
  landed and GitHub routes through it; the second host follows once there is a
  real target to validate against.
