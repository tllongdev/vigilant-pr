"""GitHub review-request watcher.

Polls for open PRs where the running user has been requested as a reviewer and
auto-reviews them on the user's behalf, using only the user's own token (no
GitHub App). Idempotent (never reviews the same head SHA twice), bounded (poll
interval + per-day cap), and resilient (a failure on one PR never crashes the
loop). Deployable as `docker run -d ... watch`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .providers import model_key_missing
from .review import fetch_last_bot_review_sha, run_review
from .util import run


def _seen_path(override: str | Path | None = None) -> Path:
    """Location of the local seen-cache (secondary idempotency guard)."""
    if override:
        return Path(override)
    env = os.environ.get("VIGILANT_SEEN_PATH")
    if env:
        return Path(env)
    return Path.home() / ".vigilant" / "seen.json"


def _seen_key(repo: str, pr_number: int, head_sha: str) -> str:
    return f"{repo}#{pr_number}@{head_sha}"


def _load_seen(path: str | Path | None = None) -> set[str]:
    p = _seen_path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    return set(data) if isinstance(data, list) else set()


def _record_seen(key: str, path: str | Path | None = None) -> None:
    p = _seen_path(path)
    seen = _load_seen(path)
    seen.add(key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(seen)))
    except OSError as e:
        sys.stderr.write(f"Warning: could not persist seen-cache to {p}: {e}\n")


def repo_allowed(repo: str, config: Config) -> bool:
    """Apply the config org/repo allow and deny lists.

    Deny always wins. If an allow list is non-empty, the repo (or its org) must
    appear in it. Empty allow lists mean "allow everything not denied".
    """
    if not repo or "/" not in repo:
        return False
    org = repo.split("/", 1)[0]
    if repo in config.repo_deny or org in config.org_deny:
        return False
    if config.repo_allow and repo not in config.repo_allow:
        return False
    if config.org_allow and org not in config.org_allow:
        return False
    return True


def find_review_requests(config: Config) -> list[tuple[str, int, str]]:
    """Return (repo, pr_number, title) for open PRs where the user is a
    requested reviewer, after applying draft and allow/deny filters."""
    out = run(
        ["gh", "search", "prs", "--review-requested=@me", "--state=open",
         "--limit", "50", "--json", "number,repository,isDraft,title"],
        check=False,
    )
    if not out.strip():
        return []
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return []

    results: list[tuple[str, int, str]] = []
    for it in items:
        repo_obj = it.get("repository", {}) or {}
        repo = repo_obj.get("nameWithOwner") or repo_obj.get("name") or ""
        number = it.get("number")
        if not repo or not number:
            continue
        if config.skip_drafts and it.get("isDraft"):
            continue
        if not repo_allowed(repo, config):
            continue
        results.append((repo, int(number), it.get("title", "")))
    return results


def _head_sha(repo: str, pr_number: int) -> str | None:
    out = run(
        ["gh", "pr", "view", str(pr_number), "--repo", repo,
         "--json", "headRefOid", "-q", ".headRefOid"],
        check=False,
    )
    return out.strip() or None


def already_reviewed(
    repo: str, pr_number: int, head_sha: str, seen_path: str | Path | None = None
) -> bool:
    """True if this exact head SHA was already reviewed.

    Primary signal is the on-PR marker (the engine's own dedup source of truth);
    the local seen-cache is a secondary belt-and-suspenders guard so a poll that
    races the GitHub API does not double-review.
    """
    last = fetch_last_bot_review_sha(repo, pr_number)
    if last and last == head_sha:
        return True
    return _seen_key(repo, pr_number, head_sha) in _load_seen(seen_path)


def run_watch(config: Config, once: bool = False, seen_path: str | Path | None = None) -> int:
    """Poll for review requests and auto-review them on the user's behalf.

    `once=True` runs a single pass and returns (useful for cron or testing).
    """
    key_problem = model_key_missing(config)
    if key_problem:
        sys.stderr.write(key_problem + "\n")
        return 1

    day = datetime.now(UTC).date()
    reviewed_today = 0
    sys.stderr.write(
        f"Vigilant PR watcher started (poll={config.poll_interval}s, "
        f"daily_cap={config.daily_cap}, model={config.model}).\n"
    )

    while True:
        today = datetime.now(UTC).date()
        if today != day:
            day = today
            reviewed_today = 0
            sys.stderr.write("New UTC day - daily review counter reset.\n")

        try:
            requests = find_review_requests(config)
        except SystemExit:
            requests = []
            sys.stderr.write("Failed to fetch review requests this cycle; will retry.\n")

        for repo, number, title in requests:
            if reviewed_today >= config.daily_cap:
                sys.stderr.write(
                    f"Daily cap ({config.daily_cap}) reached; skipping remaining PRs.\n"
                )
                break
            try:
                head = _head_sha(repo, number)
                if not head:
                    sys.stderr.write(f"Skipping {repo}#{number}: cannot read head SHA.\n")
                    continue
                if already_reviewed(repo, number, head, seen_path):
                    continue
                sys.stderr.write(f"Reviewing {repo}#{number}: {title}\n")
                per_repo = dataclasses.replace(config, repo=repo)
                rc = run_review(str(number), per_repo)
                if rc == 0:
                    _record_seen(_seen_key(repo, number, head), seen_path)
                    reviewed_today += 1
                else:
                    sys.stderr.write(f"Review of {repo}#{number} returned {rc}; not marking seen.\n")
            except SystemExit as e:
                sys.stderr.write(f"Review of {repo}#{number} failed (exit {e.code}); continuing.\n")
            except Exception as e:  # noqa: BLE001 - the loop must survive any single-PR error
                sys.stderr.write(f"Review of {repo}#{number} errored ({e}); continuing.\n")

        if once:
            return 0
        time.sleep(config.poll_interval)
