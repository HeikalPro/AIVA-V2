"""Simple async circuit breaker."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypeVar

from llm_service.core.exceptions import CircuitOpenError

T = TypeVar("T")


class State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        provider: str = "unknown",
    ) -> None:
        self._threshold = failure_threshold
        self._timeout = recovery_timeout
        self._failures = 0
        self._state = State.CLOSED
        self._opened_at: float | None = None
        self._provider = provider

    @property
    def state(self) -> State:
        return self._state

    def _check_state(self) -> None:
        if self._state == State.OPEN:
            elapsed = time.monotonic() - (self._opened_at or 0)
            if elapsed >= self._timeout:
                self._state = State.HALF_OPEN
            else:
                raise CircuitOpenError(
                    "Circuit open",
                    provider=self._provider,
                )

    def _on_success(self) -> None:
        self._failures = 0
        self._state = State.CLOSED

    def _on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._state = State.OPEN
            self._opened_at = time.monotonic()

    async def call(self, coro_factory: Callable[[], Awaitable[T]]) -> T:
        self._check_state()
        try:
            result = await coro_factory()
        except Exception:
            self._on_failure()
            raise
        self._on_success()
        return result
