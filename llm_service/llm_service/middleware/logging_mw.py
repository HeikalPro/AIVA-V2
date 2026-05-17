"""Structured logging middleware."""

from __future__ import annotations

import time

import structlog

from llm_service.core.models import LLMResponse

from .base import MiddlewareContext, Next

log = structlog.get_logger(__name__)


class LoggingMiddleware:
    async def __call__(self, ctx: MiddlewareContext, call_next: Next) -> LLMResponse:
        bound = log.bind(
            correlation_id=ctx.request.correlation_id,
            model=ctx.request.model,
        )
        bound.info("llm.request", messages_count=len(ctx.request.messages))
        t0 = time.monotonic()
        try:
            response = await call_next(ctx)
            bound.info(
                "llm.response",
                latency_ms=round((time.monotonic() - t0) * 1000, 2),
                total_tokens=response.usage.total_tokens if response.usage else None,
                finish_reason=response.finish_reason,
            )
            return response
        except Exception as e:
            bound.error("llm.error", error=str(e), error_type=type(e).__name__)
            raise
