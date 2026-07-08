"""Unit tests for the dependency-free chat trigger core (milestones 004-005).

These exercise the real parsing/formatting logic. The engine call in
`review_from_text` is exercised via a monkeypatched `run_review` so we test the
outcome-normalization and reply-formatting without hitting GitHub/Anthropic.
"""

from __future__ import annotations

import pytest

from vigilant.engine.config import OPUS_MODEL, SONNET_MODEL, Config
from vigilant.triggers import core


def test_extract_pr_refs_plain_url() -> None:
    text = "please review https://github.com/acme/widget/pull/42 thanks"
    assert core.extract_pr_refs(text) == ["https://github.com/acme/widget/pull/42"]


def test_extract_pr_refs_slack_link_markup() -> None:
    text = "<https://github.com/acme/widget/pull/7|acme/widget#7> when you can"
    assert core.extract_pr_refs(text) == ["https://github.com/acme/widget/pull/7"]


def test_extract_pr_refs_dedup_preserves_order() -> None:
    text = (
        "https://github.com/a/b/pull/2 and https://github.com/a/b/pull/1 "
        "and again https://github.com/a/b/pull/2"
    )
    assert core.extract_pr_refs(text) == [
        "https://github.com/a/b/pull/2",
        "https://github.com/a/b/pull/1",
    ]


def test_extract_pr_refs_ignores_non_pr_links() -> None:
    text = "see https://github.com/acme/widget/issues/9 and https://github.com/acme/widget"
    assert core.extract_pr_refs(text) == []


def test_split_flags_opus() -> None:
    body, model = core.split_flags("review https://github.com/a/b/pull/3 --opus")
    assert model == OPUS_MODEL
    assert "--opus" not in body
    assert "pull/3" in body


def test_split_flags_last_wins() -> None:
    _, model = core.split_flags("--sonnet then --opus")
    assert model == OPUS_MODEL
    _, model2 = core.split_flags("--opus then --sonnet")
    assert model2 == SONNET_MODEL


def test_split_flags_none() -> None:
    body, model = core.split_flags("just a plain message")
    assert model is None
    assert body == "just a plain message"


def test_format_reply_empty_asks_for_link() -> None:
    msg = core.format_reply([])
    assert "did not find" in msg.lower()


def test_review_from_text_no_link_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "run_review", lambda ref, cfg: 0)
    assert core.review_from_text("no links here", Config()) == []


def test_review_from_text_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake(ref: str, cfg: Config) -> int:
        calls.append((ref, cfg.model))
        return 0

    monkeypatch.setattr(core, "run_review", fake)
    out = core.review_from_text(
        "review https://github.com/a/b/pull/5 --opus", Config()
    )
    assert len(out) == 1
    assert out[0].ok is True
    assert out[0].pr_url == "https://github.com/a/b/pull/5"
    assert calls == [("https://github.com/a/b/pull/5", OPUS_MODEL)]


def test_review_from_text_does_not_mutate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "run_review", lambda ref, cfg: 0)
    cfg = Config(model=SONNET_MODEL)
    core.review_from_text("https://github.com/a/b/pull/5 --opus", cfg)
    assert cfg.model == SONNET_MODEL  # override applied to a copy, not the original


def test_run_review_for_ref_maps_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "run_review", lambda ref, cfg: 3)
    out = core.run_review_for_ref("https://github.com/a/b/pull/9", Config())
    assert out.ok is False
    assert out.exit_code == 3
    assert "GitHub" in out.message


def test_run_review_for_ref_handles_systemexit(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(ref: str, cfg: Config) -> int:
        raise SystemExit(3)

    monkeypatch.setattr(core, "run_review", boom)
    out = core.run_review_for_ref("https://github.com/a/b/pull/9", Config())
    assert out.ok is False
    assert out.exit_code == 3


def test_run_review_for_ref_handles_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(ref: str, cfg: Config) -> int:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(core, "run_review", boom)
    out = core.run_review_for_ref("https://github.com/a/b/pull/9", Config())
    assert out.ok is False
    assert "kaboom" in out.message


def test_teams_signature_verification_roundtrip() -> None:
    import base64
    import hashlib
    import hmac

    from vigilant.triggers import teams

    secret = base64.b64encode(b"super-secret-key").decode()
    body = b'{"text": "review this"}'
    sig = base64.b64encode(
        hmac.new(base64.b64decode(secret), body, hashlib.sha256).digest()
    ).decode()
    assert teams._verify_signature(secret, body, f"HMAC {sig}") is True
    assert teams._verify_signature(secret, body, "HMAC wrong") is False
    assert teams._verify_signature(secret, body, None) is False
    assert teams._verify_signature(secret, b"tampered", f"HMAC {sig}") is False
