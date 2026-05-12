"""Middleware protocol and context."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from llm_service.core.models import LLMRequest, LLMResponse

Next = Callable[["MiddlewareContext"], Awaitable[LLMResponse]]


@dataclass
class MiddlewareContext:
    request: LLMRequest
    response: LLMResponse | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: BaseException | None = None


class Middleware(Protocol):
    async def __call__(self, ctx: MiddlewareContext, call_next: Next) -> LLMResponse: ...
