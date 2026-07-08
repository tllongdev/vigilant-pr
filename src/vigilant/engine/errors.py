"""Shared engine exceptions.

Kept in its own module so both `providers` and `review` can raise/catch the same
type without an import cycle.
"""

from __future__ import annotations


class ReviewFailedError(Exception):
    """Raised when the review cannot be completed (API errors, parse failures).

    `exit_code` mirrors the process contract: 2 = model/inference error.
    """

    def __init__(self, reason: str, exit_code: int = 2):
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code
