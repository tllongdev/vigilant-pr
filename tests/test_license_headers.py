# Copyright 2026 Timothy Long / Longitudinal Intelligence Technologies (LIT)
# SPDX-License-Identifier: Apache-2.0
"""Guard against license-header drift.

Every shipped source file under `src/vigilant` must begin with the exact
Apache-2.0 copyright + SPDX header. This has silently regressed twice (headers
stripped or the entity name abbreviated during reinstall/testing churn), so this
test fails the build the moment it happens again - before a `git add` can sweep
the regression into a commit.
"""

from __future__ import annotations

from pathlib import Path

EXPECTED_HEADER = (
    "# Copyright 2026 Timothy Long / Longitudinal Intelligence Technologies (LIT)",
    "# SPDX-License-Identifier: Apache-2.0",
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "vigilant"


def _source_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_source_tree_is_non_empty() -> None:
    # Sanity check: if the glob finds nothing (e.g. layout moved), the header
    # check below would vacuously pass, so assert we actually scanned files.
    assert _source_files(), f"no source files found under {_SRC}"


def test_every_source_file_has_exact_license_header() -> None:
    offenders: list[str] = []
    for path in _source_files():
        first_two = path.read_text(encoding="utf-8").splitlines()[:2]
        if tuple(first_two) != EXPECTED_HEADER:
            rel = path.relative_to(_SRC.parents[1])
            offenders.append(f"{rel}: got {first_two!r}")
    assert not offenders, "License header missing or altered in:\n" + "\n".join(offenders)
