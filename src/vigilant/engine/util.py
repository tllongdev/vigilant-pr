# Copyright 2026 Timothy Long / Longitudinal Intelligence Technologies (LIT)
# SPDX-License-Identifier: Apache-2.0
"""Subprocess helpers shared across the engine.

Kept dependency-free (stdlib only). The `run` helper wraps `gh`/other CLI calls
with retry/backoff on transient failures, matching the behavior of the original
knowledge-substrate reviewer.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import time

# Substrings that indicate a transient failure worth retrying (5xx, gateway
# timeouts, connection resets). Non-transient failures (auth, 4xx, "no such PR")
# are not retried - no amount of patience makes a wrong PR number right.
_GH_TRANSIENT_PATTERNS = (
    "HTTP 5",
    "Gateway Time",
    "timeout",
    "connection reset",
    "connection refused",
    "TLS handshake",
    "i/o timeout",
)


def github_preflight() -> str | None:
    """Check that GitHub access is usable, returning a friendly message if not.

    Returns None when good to go, or an actionable error string when the `gh`
    CLI is missing/unauthenticated and no GH_TOKEN is set. Cheap to call at
    startup so a new user gets a clear reason instead of a raw exit-3 later.
    """
    has_token = bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    gh_path = shutil.which("gh")
    if gh_path is None:
        if has_token:
            return None  # `gh` reads GH_TOKEN from the environment; fine without login
        return (
            "GitHub CLI 'gh' not found and no GH_TOKEN set.\n"
            "  Install gh: https://cli.github.com  then run: gh auth login\n"
            "  or set GH_TOKEN to a token with Pull requests: read/write."
        )
    if has_token:
        return None  # token present; trust it rather than probing keyring auth
    result = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return (
            "GitHub CLI is installed but not authenticated.\n"
            "  Run: gh auth login   (or set GH_TOKEN with Pull requests: read/write)."
        )
    return None


def ensure_github_auth(interactive: bool = True) -> bool:
    """Make GitHub access usable, running `gh auth login` for the user if needed.

    Returns True when access is ready afterwards. When `gh` is authenticated or a
    GH_TOKEN is present, this is a no-op that returns True. When `gh` is installed
    but unauthenticated and we're interactive on a TTY, it launches the normal
    `gh auth login` browser/device flow (inheriting stdio) so the user just
    follows the prompts. When `gh` is missing, it prints the install URL and
    returns False (we can't reliably auto-install it).
    """
    problem = github_preflight()
    if problem is None:
        return True
    if shutil.which("gh") is None:
        sys.stderr.write(problem + "\n")
        return False
    if not (interactive and sys.stdin.isatty()):
        sys.stderr.write(problem + "\n")
        return False
    sys.stdout.write("\nConnecting your GitHub account (a browser window will open)...\n")
    subprocess.run(["gh", "auth", "login"], check=False)  # interactive; inherits stdio
    ok = github_preflight() is None
    if not ok:
        sys.stderr.write("GitHub is still not authenticated. Re-run and complete `gh auth login`.\n")
    return ok


def run(
    cmd: list[str],
    check: bool = True,
    input_text: str | None = None,
    retries: int = 4,
) -> str:
    """Run a subprocess and return stdout.

    Retries `retries` times on transient-looking failures. On a non-transient
    failure with `check=True`, writes stderr and exits with code 3 (GitHub/CLI
    error), matching the original engine's contract.
    """
    last_stderr = ""
    result = None
    for attempt in range(retries + 1):
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            input=input_text,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
        last_stderr = result.stderr
        transient = any(pat.lower() in last_stderr.lower() for pat in _GH_TRANSIENT_PATTERNS)
        if not check or not transient or attempt == retries:
            break
        wait = (2**attempt) + random.uniform(0, 1)
        sys.stderr.write(
            f"Transient subprocess failure on attempt {attempt + 1}/{retries + 1} "
            f"(retrying in {wait:.1f}s):\n$ {' '.join(cmd)}\n{last_stderr.rstrip()}\n"
        )
        time.sleep(wait)

    if check and (result is None or result.returncode != 0):
        sys.stderr.write(f"$ {' '.join(cmd)}\n")
        sys.stderr.write(last_stderr)
        sys.exit(3)
    return result.stdout if result is not None else ""
