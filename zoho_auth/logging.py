"""Logging abstractions.

The package never calls ``print`` directly inside services. Instead, services
receive a ``Logger`` and call structured methods on it. This means the team can
replace ``ConsoleLogger`` with anything that satisfies the ``Logger`` protocol
(JSON logger, file logger, test spy, etc.) without touching service code.
"""

from __future__ import annotations

import sys
from typing import Protocol, runtime_checkable


@runtime_checkable
class Logger(Protocol):
    """Structural interface every logger must satisfy."""

    def info(self, message: str) -> None: ...

    def step(self, index: int, total: int, message: str) -> None: ...

    def progress(self, message: str) -> None: ...

    def clear_progress(self) -> None: ...

    def error(self, message: str) -> None: ...


class NullLogger:
    """Drop-in silent logger. Useful in tests and in non-interactive scripts."""

    def info(self, message: str) -> None:
        return

    def step(self, index: int, total: int, message: str) -> None:
        return

    def progress(self, message: str) -> None:
        return

    def clear_progress(self) -> None:
        return

    def error(self, message: str) -> None:
        return


class ConsoleLogger:
    """Default human-friendly logger that writes to stdout/stderr.

    The numbered ``step`` output and the carriage-return based ``progress``
    output give the terminal flow used by ``ZohoLoginApp``.
    """

    _PROGRESS_WIDTH = 88

    def __init__(self, *, indent: str = "       ") -> None:
        self._indent = indent
        self._has_progress = False

    def info(self, message: str) -> None:
        self._end_progress_line_if_any()
        print(f"{self._indent}{message}", flush=True)

    def step(self, index: int, total: int, message: str) -> None:
        self._end_progress_line_if_any()
        print(f"\n[{index}/{total}] {message}", flush=True)

    def progress(self, message: str) -> None:
        sys.stdout.write(f"\r{self._indent}{message}")
        sys.stdout.flush()
        self._has_progress = True

    def clear_progress(self) -> None:
        if not self._has_progress:
            return
        sys.stdout.write("\r" + " " * self._PROGRESS_WIDTH + "\r")
        sys.stdout.flush()
        self._has_progress = False

    def error(self, message: str) -> None:
        self._end_progress_line_if_any()
        print(f"[ERROR] {message}", file=sys.stderr, flush=True)

    def _end_progress_line_if_any(self) -> None:
        if self._has_progress:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._has_progress = False
