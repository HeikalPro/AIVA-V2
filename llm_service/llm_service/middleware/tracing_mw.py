"""OpenTelemetry span middleware (optional)."""

from __future__ import annotations

from llm_service.core.models import LLMResponse

from .base import MiddlewareContext, Next


class TracingMiddleware:
    async def __call__(self, ctx: MiddlewareContext, call_next: Next) -> LLMResponse:
        try:
            from opentelemetry import trace
        except ImportError:
            return await call_next(ctx)
        tracer = trace.get_tracer("llm_service")
        with tracer.start_as_current_span("llm.chat") as span:
            span.set_attribute("llm.model", ctx.request.model)
            span.set_attribute("llm.correlation_id", ctx.request.correlation_id)
            return await call_next(ctx)
