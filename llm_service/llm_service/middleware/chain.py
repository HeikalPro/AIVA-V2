"""Composable middleware pipeline."""

from __future__ import annotations

from llm_service.core.models import LLMResponse

from .base import Middleware, MiddlewareContext, Next


class MiddlewareChain:
    def __init__(self, middlewares: list[Middleware], endpoint: Next) -> None:
        self._handler = self._wrap(middlewares, endpoint)

    def _wrap(self, middlewares: list[Middleware], handler: Next) -> Next:
        for mw in reversed(middlewares):
            inner = handler

            async def handler(ctx: MiddlewareContext, mw: Middleware = mw, inner: Next = inner) -> LLMResponse:
                return await mw(ctx, inner)

        return handler

    async def execute(self, ctx: MiddlewareContext) -> LLMResponse:
        return await self._handler(ctx)

    @staticmethod
    def debug_order(middlewares: list[Middleware]) -> list[str]:
        return [type(m).__name__ for m in middlewares]
