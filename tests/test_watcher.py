"""Unit tests for the watcher (milestone 003).

The polling loop and GitHub calls are I/O; these tests target the pure decision
logic (allow/deny, search-JSON parsing, seen-cache) by injecting fixtures rather
than mocking the function under test.
"""

from __future__ import annotations

import json

import pytest

from vigilant.engine import watcher
from vigilant.engine.config import Config


def _cfg(**kw: object) -> Config:
    return Config.from_env(**kw)  # type: ignore[arg-type]


# --- repo_allowed ---------------------------------------------------------


def test_repo_allowed_default_allows_any_wellformed_repo() -> None:
    assert watcher.repo_allowed("acme/widget", _cfg()) is True


def test_repo_allowed_rejects_malformed() -> None:
    assert watcher.repo_allowed("noslash", _cfg()) is False
    assert watcher.repo_allowed("", _cfg()) is False


def test_repo_deny_beats_everything() -> None:
    cfg = _cfg()
    cfg.repo_allow = ["acme/widget"]
    cfg.repo_deny = ["acme/widget"]
    assert watcher.repo_allowed("acme/widget", cfg) is False


def test_org_deny_blocks_all_repos_in_org() -> None:
    cfg = _cfg()
    cfg.org_deny = ["evil"]
    assert watcher.repo_allowed("evil/anything", cfg) is False
    assert watcher.repo_allowed("good/anything", cfg) is True


def test_repo_allow_list_is_exclusive() -> None:
    cfg = _cfg()
    cfg.repo_allow = ["acme/widget"]
    assert watcher.repo_allowed("acme/widget", cfg) is True
    assert watcher.repo_allowed("acme/other", cfg) is False


def test_org_allow_list_is_exclusive() -> None:
    cfg = _cfg()
    cfg.org_allow = ["acme"]
    assert watcher.repo_allowed("acme/widget", cfg) is True
    assert watcher.repo_allowed("other/widget", cfg) is False


# --- find_review_requests -------------------------------------------------

SEARCH_JSON = json.dumps(
    [
        {"number": 12, "isDraft": False, "title": "Fix bug",
         "repository": {"name": "widget", "nameWithOwner": "acme/widget"}},
        {"number": 13, "isDraft": True, "title": "WIP",
         "repository": {"name": "widget", "nameWithOwner": "acme/widget"}},
        {"number": 14, "isDraft": False, "title": "Blocked",
         "repository": {"name": "thing", "nameWithOwner": "evil/thing"}},
    ]
)


def test_find_review_requests_filters_drafts_and_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher, "run", lambda *a, **k: SEARCH_JSON)
    cfg = _cfg()
    cfg.org_deny = ["evil"]
    result = watcher.find_review_requests(cfg)
    assert result == [("acme/widget", 12, "Fix bug")]


def test_find_review_requests_keeps_drafts_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher, "run", lambda *a, **k: SEARCH_JSON)
    cfg = _cfg()
    cfg.skip_drafts = False
    cfg.org_deny = ["evil"]
    numbers = sorted(n for _, n, _ in watcher.find_review_requests(cfg))
    assert numbers == [12, 13]


def test_find_review_requests_empty_on_no_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher, "run", lambda *a, **k: "")
    assert watcher.find_review_requests(_cfg()) == []


def test_find_review_requests_empty_on_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher, "run", lambda *a, **k: "not json")
    assert watcher.find_review_requests(_cfg()) == []


# --- seen-cache + already_reviewed ---------------------------------------


def test_seen_cache_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "seen.json"
    key = watcher._seen_key("acme/widget", 12, "abc123")
    assert watcher._load_seen(path) == set()
    watcher._record_seen(key, path)
    assert key in watcher._load_seen(path)


def test_load_seen_tolerates_corrupt_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "seen.json"
    path.write_text("{ not valid json")
    assert watcher._load_seen(path) == set()


def test_already_reviewed_true_when_marker_matches_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(watcher, "fetch_last_bot_review_sha", lambda repo, num: "deadbeef")
    assert watcher.already_reviewed("acme/widget", 12, "deadbeef", tmp_path / "seen.json") is True


def test_already_reviewed_true_from_seen_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(watcher, "fetch_last_bot_review_sha", lambda repo, num: None)
    path = tmp_path / "seen.json"
    watcher._record_seen(watcher._seen_key("acme/widget", 12, "cafe"), path)
    assert watcher.already_reviewed("acme/widget", 12, "cafe", path) is True


def test_already_reviewed_false_for_new_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(watcher, "fetch_last_bot_review_sha", lambda repo, num: "oldsha")
    assert watcher.already_reviewed("acme/widget", 12, "newsha", tmp_path / "seen.json") is False
