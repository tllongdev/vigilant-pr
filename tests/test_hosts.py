"""Unit tests for the host provider interface.

Covers target->host detection, provider resolution (including the clean exit for
a recognized-but-unsupported host), and the GitHub payload -> normalized
PullRequest mapping. These call the real code directly.
"""

from __future__ import annotations

import json

import pytest

from vigilant.engine import hosts
from vigilant.engine.hosts import (
    HOST_PROVIDERS,
    GitHubHost,
    HostProvider,
    PullRequest,
    detect_host,
    resolve_host,
)


def test_detect_host_defaults_to_github() -> None:
    assert detect_host(None) == "github"
    assert detect_host("42") == "github"
    assert detect_host("https://github.com/acme/widget/pull/7") == "github"


def test_detect_host_recognizes_gitlab() -> None:
    assert detect_host("https://gitlab.com/acme/widget/-/merge_requests/7") == "gitlab"


def test_detect_host_recognizes_bitbucket() -> None:
    assert detect_host("https://bitbucket.org/acme/widget/pull-requests/7") == "bitbucket"


def test_resolve_host_returns_github_for_default_and_github_url() -> None:
    assert isinstance(resolve_host(None), GitHubHost)
    assert isinstance(resolve_host("https://github.com/acme/widget/pull/7"), GitHubHost)


def test_github_host_satisfies_protocol() -> None:
    # runtime_checkable Protocol: the concrete host is structurally a HostProvider.
    assert isinstance(GitHubHost(), HostProvider)


def test_resolve_host_exits_cleanly_for_unsupported_host() -> None:
    with pytest.raises(SystemExit) as exc:
        resolve_host("https://gitlab.com/acme/widget/-/merge_requests/7")
    assert exc.value.code == 1


def _fake_gh(meta: dict[str, object], diff: str) -> object:
    """Stand-in for `gh`: returns the metadata JSON for `pr view` and the diff
    text for `pr diff`, dispatched on the subcommand."""

    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> str:
        if "diff" in cmd:
            return diff
        return json.dumps(meta)

    return fake_run


def test_github_host_normalizes_pr_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    meta = {
        "number": 7,
        "title": "Fix bug",
        "body": "does the thing",
        "baseRefName": "main",
        "headRefName": "feature",
        "headRefOid": "deadbeef",
        "changedFiles": 3,
        "isDraft": True,
    }
    monkeypatch.setattr(hosts, "run", _fake_gh(meta, "diff --git a/x b/x"))

    pr = GitHubHost().fetch_pr("acme/widget", 7)

    assert isinstance(pr, PullRequest)
    assert pr.repo == "acme/widget"
    assert pr.number == 7
    assert pr.title == "Fix bug"
    assert pr.body == "does the thing"
    assert pr.base == "main"
    assert pr.head == "feature"
    assert pr.head_sha == "deadbeef"
    assert pr.changed_files == 3
    assert pr.is_draft is True
    assert pr.diff == "diff --git a/x b/x"


def test_github_host_normalizes_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hosts, "run", _fake_gh({"number": 1}, ""))
    pr = GitHubHost().fetch_pr("acme/widget", 1)
    assert pr.title == ""
    assert pr.body == ""
    assert pr.changed_files == 0
    assert pr.is_draft is False
    assert pr.diff == ""


def test_registry_maps_github() -> None:
    assert HOST_PROVIDERS["github"] is GitHubHost


@pytest.mark.parametrize(
    "arg,expected",
    [
        ("123", (123, None)),
        ("https://github.com/Org/Repo/pull/42", (42, "Org/Repo")),
    ],
)
def test_github_parse_target_valid(arg: str, expected: tuple[int, str | None]) -> None:
    assert GitHubHost().parse_target(arg) == expected


def test_github_parse_target_invalid_exits() -> None:
    with pytest.raises(SystemExit):
        GitHubHost().parse_target("not-a-pr")
