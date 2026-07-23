"""Unit tests for the host provider interface.

Covers target->host detection, provider resolution (including the clean exit for
a recognized-but-unsupported host), and the GitHub payload -> normalized
PullRequest mapping. These call the real code directly.
"""

from __future__ import annotations

import base64
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


def _contents_envelope(body: str) -> str:
    """A GitHub contents-API JSON envelope for a file, base64-encoding `body` -
    matching what `gh api repos/.../contents/<path>` returns for a real file."""
    encoded = base64.b64encode(body.encode()).decode()
    return json.dumps({"type": "file", "encoding": "base64", "content": encoded})


def _fake_tree_and_contents(tree_paths: list[str], contents: dict[str, str]) -> object:
    """Stand-in for `gh`: the recursive-tree call returns `tree_paths` (one per
    line); a contents call returns a JSON envelope for a mapped file, else a 404
    JSON error body (a dict with no file payload)."""

    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> str:
        api = next((c for c in cmd if c.startswith("repos/")), "")
        if "/git/trees/" in api:
            return "\n".join(tree_paths)
        for path, body in contents.items():
            if f"/contents/{path}?" in api:
                return _contents_envelope(body)
        return '{"message": "Not Found", "status": "404"}'

    return fake_run


def test_read_guidance_decodes_present_files(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: guidance must actually reach the model. The old raw+`-q .` combo
    # silently returned nothing for every existing file; the JSON-envelope path
    # decodes real content and skips 404s.
    contents = {"AGENTS.md": "# Repo rules\n\nUse tabs.\n"}

    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> str:
        api = next((c for c in cmd if c.startswith("repos/")), "")
        for path, body in contents.items():
            if f"/contents/{path}?" in api:
                return _contents_envelope(body)
        return '{"message": "Not Found", "status": "404"}'

    monkeypatch.setattr(hosts, "run", fake_run)
    out = GitHubHost().read_guidance("acme/widget", "sha1")
    assert "### AGENTS.md" in out
    assert "Use tabs." in out


def test_read_guidance_empty_when_no_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hosts, "run", lambda *a, **k: '{"message": "Not Found", "status": "404"}')
    out = GitHubHost().read_guidance("acme/widget", "sha1")
    assert "no AGENTS.md" in out


def test_read_dependency_manifests_fetches_present_declaration_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only files discovered in the tree (and in a wanted dir) are fetched; a
    # lockfile in the tree is ignored, and a 404 body would be filtered out.
    tree = ["requirements.txt", "pyproject.toml", "poetry.lock", "README.md"]
    contents = {
        "requirements.txt": "flask==3.0\nsentry-sdk==2.1.0\n",
        "pyproject.toml": "[project]\nname = 'x'\n",
    }
    monkeypatch.setattr(hosts, "run", _fake_tree_and_contents(tree, contents))
    out = GitHubHost().read_dependency_manifests("acme/widget", "sha1")

    assert "### requirements.txt" in out
    assert "sentry-sdk==2.1.0" in out
    assert "### pyproject.toml" in out
    assert "poetry.lock" not in out  # lockfiles are never included
    assert "package.json" not in out  # absent file is skipped


def test_read_dependency_manifests_finds_manifest_in_changed_file_subdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whisper-service case: the manifest lives beside the code in a subdir,
    # not at the repo root. Searching the changed file's directory finds it.
    tree = ["worker/requirements.txt", "worker/worker.py", "README.md"]
    contents = {"worker/requirements.txt": "sentry-sdk==2.1.0\ntorch==2.2\n"}
    monkeypatch.setattr(hosts, "run", _fake_tree_and_contents(tree, contents))

    out = GitHubHost().read_dependency_manifests("acme/widget", "sha1", ("worker", ""))
    assert "### worker/requirements.txt" in out
    assert "torch==2.2" in out


def test_read_dependency_manifests_ignores_manifest_outside_wanted_dirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A manifest in an unrelated dir we didn't touch is not fetched.
    tree = ["other/requirements.txt", "worker/worker.py"]
    contents = {"other/requirements.txt": "unrelated==1.0\n"}
    monkeypatch.setattr(hosts, "run", _fake_tree_and_contents(tree, contents))

    out = GitHubHost().read_dependency_manifests("acme/widget", "sha1", ("worker", ""))
    assert out == ""


def test_read_dependency_manifests_truncates_large_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big = "dep==1.0\n" * 2000  # well over the per-file char cap
    monkeypatch.setattr(
        hosts, "run", _fake_tree_and_contents(["requirements.txt"], {"requirements.txt": big})
    )
    out = GitHubHost().read_dependency_manifests("acme/widget", "sha1")

    assert "... (truncated)" in out
    assert len(out) < len(big)


def test_read_dependency_manifests_empty_when_none_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tree lists no manifests -> nothing fetched, empty result.
    monkeypatch.setattr(hosts, "run", _fake_tree_and_contents(["src/app.py", "README.md"], {}))
    assert GitHubHost().read_dependency_manifests("acme/widget", "sha1") == ""


def test_read_dependency_manifests_falls_back_when_tree_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the tree call returns nothing (truncated/permission), fall back to
    # probing known manifest names directly in the wanted dirs.
    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> str:
        api = next((c for c in cmd if c.startswith("repos/")), "")
        if "/git/trees/" in api:
            return ""  # tree unavailable
        if "/contents/requirements.txt?" in api:
            return _contents_envelope("flask==3.0\n")
        return '{"message": "Not Found", "status": "404"}'

    monkeypatch.setattr(hosts, "run", fake_run)
    out = GitHubHost().read_dependency_manifests("acme/widget", "sha1")
    assert "### requirements.txt" in out


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
