"""Unit tests for the pure (no-I/O) functions of the Vigilant PR engine.

These call the real code directly - if the function body is deleted or broken,
the test fails. No mocks of the thing under test.
"""

from __future__ import annotations

import sys

import pytest

from vigilant.engine.config import OPUS_MODEL, SONNET_MODEL, Config
from vigilant.engine.identity import (
    SIG_MARKER,
    SIG_PREFIX_LEGACY,
    SIG_PREFIX_VIGILANT,
    build_footnote,
    build_signature,
    is_signed_comment,
    signature_index,
)
from vigilant.engine.review import (
    Finding,
    _approve_before_post,
    _norm_title,
    cap_nits,
    decide_event,
    dependency_search_dirs,
    diff_touches_dependencies,
    downgrade_unverifiable,
    filter_to_diff_lines,
    format_review_body,
    parse_diff_lines,
    parse_review_json,
)


def _finding(severity: str) -> Finding:
    return Finding(severity=severity, path="a.py", line=1, title="t", body="b")


def test_decide_event_approves_when_clean() -> None:
    assert decide_event([]) == "APPROVE"


def test_decide_event_approves_when_only_nits() -> None:
    assert decide_event([_finding("nit"), _finding("nit")]) == "APPROVE"


def test_decide_event_comments_on_medium() -> None:
    assert decide_event([_finding("nit"), _finding("medium")]) == "COMMENT"


def test_decide_event_comments_on_critical() -> None:
    assert decide_event([_finding("critical")]) == "COMMENT"


def test_decide_event_comments_when_prior_thread_reflagged() -> None:
    assert decide_event([_finding("nit")], [{"disposition": "re_flagged"}]) == "COMMENT"


def test_decide_event_approves_when_thread_acknowledged() -> None:
    assert decide_event([_finding("nit")], [{"disposition": "acknowledged"}]) == "APPROVE"


def test_user_prompt_injects_current_date_and_guards_against_hallucinated_dates() -> None:
    from vigilant.engine.review import USER_PROMPT_TEMPLATE

    assert "{today}" in USER_PROMPT_TEMPLATE
    rendered = USER_PROMPT_TEMPLATE.format(
        pr_number=1, repo="o/r", today="2026-07-11", title="t", body="b",
        base="main", head="feat", head_sha="abc", file_count=1,
        review_scope_note="", guidance="", dependency_manifests="", diff="",
        prior_threads_section="",
    )
    assert "Today's date is 2026-07-11" in rendered
    # explicit guard so the model stops flagging valid dates as future/typos
    assert "future-dated" in rendered
    assert "training cutoff" in rendered

SAMPLE_DIFF = """diff --git a/app.py b/app.py
index 111..222 100644
--- a/app.py
+++ b/app.py
@@ -1,4 +1,6 @@
 import os
+import sys
+
 def main():
-    return 1
+    return 0
diff --git a/removed.py b/removed.py
deleted file mode 100644
--- a/removed.py
+++ /dev/null
@@ -1,2 +0,0 @@
-print("gone")
-print("bye")
"""


def test_parse_diff_lines_marks_added_and_context_lines() -> None:
    valid = parse_diff_lines(SAMPLE_DIFF)
    assert "app.py" in valid
    # Added `import sys` (line 2), blank added line (3), context `def main()` (4),
    # and added `return 0` are all valid RIGHT-side targets.
    assert 2 in valid["app.py"]
    assert 3 in valid["app.py"]
    assert 4 in valid["app.py"]


def test_parse_diff_lines_excludes_deleted_file_target() -> None:
    valid = parse_diff_lines(SAMPLE_DIFF)
    # removed.py maps to /dev/null on the new side - no valid RIGHT lines.
    assert valid.get("removed.py", set()) == set()


def test_filter_to_diff_lines_splits_in_and_out() -> None:
    valid = {"app.py": {2, 3, 4}}
    in_diff_f = Finding("critical", "app.py", 2, "t", "b")
    out_diff_f = Finding("nit", "app.py", 99, "t", "b")
    other_file = Finding("nit", "other.py", 2, "t", "b")
    in_diff, out_of_diff = filter_to_diff_lines([in_diff_f, out_diff_f, other_file], valid)
    assert in_diff == [in_diff_f]
    assert out_of_diff == [out_diff_f, other_file]


def test_cap_nits_keeps_criticals_and_caps_nits() -> None:
    findings = (
        [Finding("critical", "a", 1, "c", "b")]
        + [Finding("medium", "a", 2, "m", "b")]
        + [Finding("nit", "a", i, f"n{i}", "b") for i in range(10)]
    )
    kept, overflow = cap_nits(findings, cap=5)
    assert overflow == 5
    assert sum(1 for f in kept if f.severity == "nit") == 5
    assert sum(1 for f in kept if f.severity == "critical") == 1
    assert sum(1 for f in kept if f.severity == "medium") == 1


def test_norm_title_collapses_whitespace_and_case() -> None:
    assert _norm_title("  Token   refresh RACES.  ") == "token refresh races"
    assert _norm_title("Same Title") == _norm_title("same   title.")


def test_parse_review_json_plain() -> None:
    assert parse_review_json('{"summary": "ok"}') == {"summary": "ok"}


def test_parse_review_json_embedded_in_prose() -> None:
    raw = 'Here is the review:\n{"summary": "ok", "findings": []}\nDone.'
    assert parse_review_json(raw) == {"summary": "ok", "findings": []}


def test_parse_review_json_unparseable_raises() -> None:
    from vigilant.engine.review import ReviewFailedError

    with pytest.raises(ReviewFailedError):
        parse_review_json("no json here at all")


def test_parse_review_json_trailing_comma_in_object() -> None:
    raw = '{"summary": "ok", "tally": {"critical": 0, "nit": 1},}'
    assert parse_review_json(raw) == {"summary": "ok", "tally": {"critical": 0, "nit": 1}}


def test_parse_review_json_trailing_comma_before_object_close() -> None:
    # The exact shape that broke on a live PR: a trailing comma after the last
    # value inside a nested object, then the closing brace on the next line.
    raw = '{\n  "findings": [\n    {\n      "severity": "medium",\n      "body": "x",\n    }\n  ]\n}'
    assert parse_review_json(raw) == {"findings": [{"severity": "medium", "body": "x"}]}


def test_parse_review_json_trailing_comma_embedded_in_prose() -> None:
    raw = 'Here is the review:\n{"summary": "ok", "findings": [],}\nDone.'
    assert parse_review_json(raw) == {"summary": "ok", "findings": []}


def test_strip_trailing_commas_preserves_commas_inside_strings() -> None:
    from vigilant.engine.review import strip_trailing_commas

    # A comma-then-brace sequence *inside* a string value must survive.
    src = '{"body": "call foo(a,) and update both places,}"}'
    assert strip_trailing_commas(src) == src
    assert parse_review_json(src) == {"body": "call foo(a,) and update both places,}"}


def test_build_signature_is_hidden_html_comment_with_handle_and_model() -> None:
    sig = build_signature(SONNET_MODEL, handle="octocat")
    assert sig.startswith(SIG_MARKER)
    assert sig.endswith("-->")  # renders invisibly on GitHub
    assert "@octocat" in sig
    assert SONNET_MODEL in sig
    assert "(effort=medium)" in sig


def test_build_signature_without_handle_omits_handle_but_stays_detectable() -> None:
    sig = build_signature(OPUS_MODEL, handle=None)
    assert "@" not in sig
    assert OPUS_MODEL in sig
    assert sig.startswith(SIG_MARKER)
    assert is_signed_comment(sig)


def test_is_signed_comment_matches_hidden_marker_and_legacy() -> None:
    assert is_signed_comment(build_signature(SONNET_MODEL, handle="octocat"))
    assert is_signed_comment("Findings: ...\n\n<!-- vigilant-pr-review: x -->")
    # legacy visible signatures still detected for backward-compatible re-review
    assert is_signed_comment(f"> {SIG_PREFIX_VIGILANT} - commissioned...")
    assert is_signed_comment(f"> {SIG_PREFIX_LEGACY} - context-agnostic...")
    assert not is_signed_comment("a normal human comment")


def test_signature_index_finds_earliest_prefix() -> None:
    body = f"prefix text\n> {SIG_PREFIX_VIGILANT} here"
    assert signature_index(body) == body.find(SIG_PREFIX_VIGILANT)
    assert signature_index("no signature") == -1


# --- footnote (visible attribution) ------------------------------------------

def test_build_footnote_is_visible_and_names_model_and_handle() -> None:
    fn = build_footnote("claude-sonnet-5", "tllongdev")
    assert fn.startswith("---")  # visible rule, not an HTML comment
    assert "AI-assisted review" in fn
    assert "claude-sonnet-5" in fn
    assert "@tllongdev" in fn
    assert "<sub>" in fn


def test_build_footnote_omits_handle_when_absent() -> None:
    fn = build_footnote("groq/llama-3.3-70b-versatile", None)
    assert "posted by" not in fn
    assert "groq/llama-3.3-70b-versatile" in fn


# --- review body: list, not table --------------------------------------------

def test_format_review_body_uses_list_not_table() -> None:
    review = {"summary": "Looks good overall.", "skipped": []}
    findings = [Finding("critical", "auth.py", 42, "Token expiry not checked", "body")]
    body = format_review_body(review, findings, "SIG", "abc123")
    assert "| Severity |" not in body  # old table header gone
    assert "| --- |" not in body
    assert "`auth.py:42`" in body
    assert "Token expiry not checked" in body
    assert "SIG" in body  # hidden marker still present


def test_format_review_body_empty_findings_message() -> None:
    body = format_review_body({"summary": "clean", "skipped": []}, [], "SIG")
    assert "No new issues found" in body


def test_format_review_body_does_not_embed_footnote() -> None:
    # The footnote is appended by run_review (so it lands after overflow notes),
    # not by format_review_body itself.
    body = format_review_body({"summary": "s", "skipped": []}, [], "SIG")
    assert "AI-assisted review via" not in body


# --- config: attribution / approval env parsing ------------------------------

def test_config_defaults_attribution_on_approval_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIGILANT_ATTRIBUTION", raising=False)
    monkeypatch.delenv("VIGILANT_REQUIRE_APPROVAL", raising=False)
    cfg = Config.from_env()
    assert cfg.attribution is True
    assert cfg.require_approval is False


def test_config_parses_attribution_and_approval_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIGILANT_ATTRIBUTION", "0")
    monkeypatch.setenv("VIGILANT_REQUIRE_APPROVAL", "yes")
    cfg = Config.from_env()
    assert cfg.attribution is False
    assert cfg.require_approval is True


# --- approval gate ------------------------------------------------------------

def test_approve_before_post_refuses_without_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    ok = _approve_before_post("o/r", 1, "me", "m", "COMMENT", "body", [], "SIG", [])
    assert ok is False
    assert "Approval required" in capsys.readouterr().err


def test_approve_before_post_accepts_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert _approve_before_post("o/r", 1, "me", "m", "COMMENT", "b", [], "SIG", []) is True


def test_approve_before_post_rejects_no(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert _approve_before_post("o/r", 1, "me", "m", "COMMENT", "b", [], "SIG", []) is False
    assert "not posted" in capsys.readouterr().err


# --- verifiability guardrail: cap self-admitted-unverifiable findings to nit ---


def _f(severity: str, body: str) -> Finding:
    return Finding(severity=severity, path="a.py", line=3, title="t", body=body)


def test_downgrade_unverifiable_caps_critical_that_admits_it_cannot_confirm() -> None:
    findings = [_f("critical", "This will crash. I could not confirm the sentry-sdk version.")]
    out, n = downgrade_unverifiable(findings)
    assert n == 1
    assert out[0].severity == "nit"
    assert "capped to nit" in out[0].body.lower()


def test_downgrade_unverifiable_caps_medium_not_visible_in_diff() -> None:
    findings = [_f("medium", "The caller is not visible in the diff, so this may break.")]
    out, n = downgrade_unverifiable(findings)
    assert n == 1
    assert out[0].severity == "nit"


def test_downgrade_unverifiable_leaves_verified_critical_alone() -> None:
    # A concrete, self-contained critical with no hedge must stay critical.
    findings = [_f("critical", "Off-by-one on line 3 drops the last record. Use <= not <.")]
    out, n = downgrade_unverifiable(findings)
    assert n == 0
    assert out[0].severity == "critical"
    assert out[0].body == findings[0].body  # unchanged, no note appended


def test_downgrade_unverifiable_never_touches_nits() -> None:
    findings = [_f("nit", "I cannot verify this style choice but it reads oddly.")]
    out, n = downgrade_unverifiable(findings)
    assert n == 0
    assert out[0].severity == "nit"


# --- dependency-touch detection: when to fetch manifests ----------------------


def test_diff_touches_dependencies_python_import() -> None:
    diff = "+++ b/app.py\n@@ -1 +1,2 @@\n import os\n+import requests\n"
    assert diff_touches_dependencies(diff) is True


def test_diff_touches_dependencies_manifest_edit() -> None:
    diff = "+++ b/requirements.txt\n@@ -1 +1,2 @@\n flask\n+sentry-sdk==2.1.0\n"
    assert diff_touches_dependencies(diff) is True


def test_diff_touches_dependencies_go_and_js() -> None:
    go = '+++ b/main.go\n@@ -1 +1,2 @@\n+import "fmt"\n'
    js = "+++ b/a.ts\n@@ -1 +1,2 @@\n+import { x } from 'y'\n"
    req = "+++ b/app.js\n@@ -1 +1,2 @@\n+const z = require('z')\n"
    assert diff_touches_dependencies(go) is True
    assert diff_touches_dependencies(js) is True
    assert diff_touches_dependencies(req) is True


def test_diff_touches_dependencies_false_for_plain_code() -> None:
    # No added import lines and no manifest edit -> no manifest fetch.
    diff = "+++ b/app.py\n@@ -1 +1,2 @@\n def f():\n+    return 1\n"
    assert diff_touches_dependencies(diff) is False


def test_diff_touches_dependencies_ignores_removed_import() -> None:
    # A removed import (context/deletion) is not a reason to fetch manifests.
    diff = "+++ b/app.py\n@@ -1,2 +1 @@\n-import requests\n def f():\n"
    assert diff_touches_dependencies(diff) is False


def test_dependency_search_dirs_collects_changed_dirs_and_root() -> None:
    diff = (
        "+++ b/worker/worker.py\n@@ -1 +1 @@\n+import x\n"
        "+++ b/services/api/main.go\n@@ -1 +1 @@\n+import y\n"
    )
    dirs = dependency_search_dirs(diff)
    assert dirs == ("worker", "services/api", "")


def test_dependency_search_dirs_root_only_for_root_file() -> None:
    diff = "+++ b/app.py\n@@ -1 +1 @@\n+import x\n"
    assert dependency_search_dirs(diff) == ("",)


def test_dependency_search_dirs_dedups_and_skips_dev_null() -> None:
    diff = (
        "+++ b/pkg/a.py\n@@ -1 +1 @@\n+import x\n"
        "+++ b/pkg/b.py\n@@ -1 +1 @@\n+import y\n"
        "+++ /dev/null\n"
    )
    assert dependency_search_dirs(diff) == ("pkg", "")
