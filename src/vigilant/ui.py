"""Terminal presentation for Vigilant PR (stdlib only).

Two pieces:
  - `banner()` - a branded ANSI wordmark shown at the top of interactive commands
    and the watchers.
  - `status()` - a TTY-aware live status line with an animated spinner for
    long-running watchers.

Everything degrades gracefully: with `NO_COLOR` set, a dumb terminal, or a
non-TTY stream (pipes, Docker logs, CI), color and animation are dropped so the
output stays clean and greppable. No third-party dependencies.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from types import TracebackType
from typing import IO

# Brand gradient endpoints: #4da3ff (blue) -> #a98bff (violet).
_BRAND_START = (77, 163, 255)
_BRAND_END = (169, 139, 255)

_WORDMARK = r"""
 __     ___ ____ ___ _        _    _   _ _____   ____  ____
 \ \   / /_ _/ ___|_ _| |     / \  | \ | |_   _| |  _ \|  _ \
  \ \ / / | | |  _ | || |    / _ \ |  \| | | |   | |_) | |_) |
   \ V /  | | |_| || || |___/ ___ \| |\  | | |   |  __/|  _ <
    \_/  |___\____|___|_____/_/   \_\_| \_| |_|   |_|   |_| \_\
"""

_TAGLINE = "adversarial PR review, posted as you"

_SPINNER_FRAMES = ("-", "\\", "|", "/")


def use_color(stream: IO[str] | None = None) -> bool:
    """Whether to emit ANSI color/animation to `stream` (default stdout)."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _rgb(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def banner(stream: IO[str] | None = None) -> str:
    """Return the branded banner, colorized when the stream supports it."""
    lines = _WORDMARK.strip("\n").splitlines()
    if not use_color(stream):
        return "\n".join(lines) + f"\n{_TAGLINE}\n"
    reset = "\033[0m"
    n = max(len(lines) - 1, 1)
    colored = []
    for i, line in enumerate(lines):
        r, g, b = _lerp(_BRAND_START, _BRAND_END, i / n)
        colored.append(f"{_rgb(r, g, b)}{line}{reset}")
    tag = f"\033[2m{_TAGLINE}{reset}"  # dim
    return "\n".join(colored) + f"\n{tag}\n"


def print_banner(stream: IO[str] | None = None) -> None:
    stream = stream or sys.stdout
    stream.write(banner(stream))
    stream.flush()


class Status:
    """A live, animated status line for long-running commands.

    On a TTY it animates a spinner and rewrites a single line in place; call
    `update()` to change the message and `log()` to print a permanent line above
    the spinner (for discrete events like "reviewed PR #123"). Off a TTY it is
    quiet: `update()` does nothing (so logs are not spammed each poll) and
    `log()` prints a plain line, keeping Docker/CI output clean.
    """

    def __init__(self, message: str = "", stream: IO[str] | None = None) -> None:
        self._message = message
        self._stream = stream or sys.stderr
        self._animate = use_color(self._stream)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Status:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def start(self) -> None:
        # Only animate on a TTY. Off a TTY this is a no-op so steady-state polling
        # never spams logs; discrete events still go through log().
        if self._animate:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()

    def _spin(self) -> None:
        i = 0
        while not self._stop.is_set():
            with self._lock:
                frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
                self._stream.write(f"\r\033[K{frame} {self._message}")
                self._stream.flush()
            i += 1
            time.sleep(0.12)

    def _clear_line(self) -> None:
        self._stream.write("\r\033[K")
        self._stream.flush()

    def update(self, message: str) -> None:
        with self._lock:
            self._message = message
        # Off a TTY we deliberately stay silent here; steady-state polling should
        # not produce a line every interval.

    def log(self, text: str) -> None:
        """Print a permanent line (a real event), keeping the spinner intact."""
        with self._lock:
            if self._animate:
                self._clear_line()
            self._stream.write(text + "\n")
            self._stream.flush()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._animate:
            self._clear_line()


def status(message: str = "", stream: IO[str] | None = None) -> Status:
    """Convenience factory mirroring a context-manager usage."""
    return Status(message, stream)
