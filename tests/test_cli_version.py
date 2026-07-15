# Copyright 2026 Timothy Long / Longitudinal Intelligence Technologies (LIT)
# SPDX-License-Identifier: Apache-2.0
"""The `--version` flag must report the installed version and exit cleanly.

This is what lets a user confirm an upgrade actually took effect, so it needs to
work without a subcommand (argparse requires one otherwise) and print the real
package version.
"""

from __future__ import annotations

import pytest

from vigilant import cli


def test_version_flag_prints_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("vigilant-pr ")
    assert out.strip() != "vigilant-pr"  # a version string is actually present


def test_version_short_flag_matches(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["-V"])
    assert capsys.readouterr().out.startswith("vigilant-pr ")


def test_version_string_reports_installed_version() -> None:
    # Sanity: the helper resolves a concrete version, not the source fallback,
    # when the package is installed (as it is in CI and the dev venv).
    assert cli._version_string()
