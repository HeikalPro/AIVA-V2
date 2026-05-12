"""Retry policy built on tenacity."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from llm_service.core.exceptions import ConnectionError, RateLimitError, TimeoutError

R = TypeVar("R")

logger = logging.getLogger(__name__)


def build_async_retrying(*, max_attempts: int = 3, base_wait: float = 1.0) -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=base_wait, min=1, max=60),
        retry=retry_if_exception_type((RateLimitError, TimeoutError, ConnectionError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


async def run_with_retry(
    fn: Callable[..., Awaitable[R]],
    /,
    *args: Any,
    max_attempts: int | None = None,
    **kwargs: Any,
) -> R:
    """Run async callable with retry policy, or a single attempt when max_attempts <= 1."""
    attempts = 3 if max_attempts is None else max(1, max_attempts)
    if attempts <= 1:
        return await fn(*args, **kwargs)
    async for attempt in build_async_retrying(max_attempts=attempts):
        with attempt:
            return await fn(*args, **kwargs)
    raise RuntimeError("retry loop exited without return")  # pragma: no cover
