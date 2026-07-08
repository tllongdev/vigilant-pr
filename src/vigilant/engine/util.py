"""Subprocess helpers shared across the engine.

Kept dependency-free (stdlib only). The `run` helper wraps `gh`/other CLI calls
with retry/backoff on transient failures, matching the behavior of the original
knowledge-substrate reviewer.
"""

from __future__ import annotations

import random
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
