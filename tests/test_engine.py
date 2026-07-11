"""Unit tests for the pure (no-I/O) functions of the Vigilant PR engine.

These call the real code directly - if the function body is deleted or broken,
the test fails. No mocks of the thing under test.
"""

from __future__ import annotations

import pytest

from vigilant.engine.config import OPUS_MODEL, SONNET_MODEL
from vigilant.engine.identity import (
    SIG_MARKER,
    SIG_PREFIX_LEGACY,
    SIG_PREFIX_VIGILANT,
    build_signature,
    is_signed_comment,
    signature_index,
)
from vigilant.engine.review import (
    Finding,
    _norm_title,
    cap_nits,
    decide_event,
    filter_to_diff_lines,
    parse_diff_lines,
    parse_pr_arg,
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
        review_scope_note="", guidance="", diff="", prior_threads_section="",
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


@pytest.mark.parametrize(
    "arg,expected",
    [
        ("123", (123, None)),
        ("https://github.com/Org/Repo/pull/42", (42, "Org/Repo")),
    ],
)
def test_parse_pr_arg_valid(arg: str, expected: tuple[int, str | None]) -> None:
    assert parse_pr_arg(arg) == expected


def test_parse_pr_arg_invalid_exits() -> None:
    with pytest.raises(SystemExit):
        parse_pr_arg("not-a-pr")


def test_parse_review_json_plain() -> None:
    assert parse_review_json('{"summary": "ok"}') == {"summary": "ok"}


def test_parse_review_json_embedded_in_prose() -> None:
    raw = 'Here is the review:\n{"summary": "ok", "findings": []}\nDone.'
    assert parse_review_json(raw) == {"summary": "ok", "findings": []}


def test_parse_review_json_unparseable_raises() -> None:
    from vigilant.engine.review import ReviewFailedError

    with pytest.raises(ReviewFailedError):
        parse_review_json("no json here at all")


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
