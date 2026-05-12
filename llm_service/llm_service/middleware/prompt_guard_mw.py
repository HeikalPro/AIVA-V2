"""Prompt safety / validation hooks."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from llm_service.core.exceptions import MiddlewareError
from llm_service.core.models import LLMResponse

from .base import MiddlewareContext, Next

Validator = Callable[[MiddlewareContext], Coroutine[Any, Any, None]]


class PromptGuardMiddleware:
    def __init__(self, validators: list[Validator] | None = None) -> None:
        self._validators = validators or []

    def add(self, fn: Validator) -> None:
        self._validators.append(fn)

    async def __call__(self, ctx: MiddlewareContext, call_next: Next) -> LLMResponse:
        for v in self._validators:
            try:
                await v(ctx)
            except MiddlewareError:
                raise
            except Exception as e:
                raise MiddlewareError(str(e)) from e
        return await call_next(ctx)
