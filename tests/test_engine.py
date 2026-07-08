"""Unit tests for the pure (no-I/O) functions of the Vigilant PR engine.

These call the real code directly - if the function body is deleted or broken,
the test fails. No mocks of the thing under test.
"""

from __future__ import annotations

import pytest

from vigilant.engine.config import OPUS_MODEL, SONNET_MODEL
from vigilant.engine.identity import (
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
    filter_to_diff_lines,
    parse_diff_lines,
    parse_pr_arg,
    parse_review_json,
)

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


def test_build_signature_on_behalf_includes_handle_and_model() -> None:
    sig = build_signature(SONNET_MODEL, handle="octocat")
    assert "@octocat" in sig
    assert SONNET_MODEL in sig
    assert sig.startswith("> " + SIG_PREFIX_VIGILANT)
    assert "(effort=medium)" in sig


def test_build_signature_without_handle_is_generic_but_honest() -> None:
    sig = build_signature(OPUS_MODEL, handle=None)
    assert "@" not in sig
    assert OPUS_MODEL in sig
    assert "automated first-pass" in sig.lower()


def test_is_signed_comment_matches_new_and_legacy() -> None:
    assert is_signed_comment(f"> {SIG_PREFIX_VIGILANT} - commissioned...")
    assert is_signed_comment(f"> {SIG_PREFIX_LEGACY} - context-agnostic...")
    assert not is_signed_comment("a normal human comment")


def test_signature_index_finds_earliest_prefix() -> None:
    body = f"prefix text\n> {SIG_PREFIX_VIGILANT} here"
    assert signature_index(body) == body.find(SIG_PREFIX_VIGILANT)
    assert signature_index("no signature") == -1
