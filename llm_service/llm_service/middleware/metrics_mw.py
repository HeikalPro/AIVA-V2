"""Prometheus metrics middleware."""

from __future__ import annotations

import time

from llm_service.core.models import LLMResponse
from llm_service.observability.metrics import LLM_ERRORS, LLM_LATENCY, LLM_REQUESTS, LLM_TOKENS

from .base import MiddlewareContext, Next


class MetricsMiddleware:
    async def __call__(self, ctx: MiddlewareContext, call_next: Next) -> LLMResponse:
        provider = str(ctx.metadata.get("provider", "unknown"))
        model = ctx.request.model
        t0 = time.perf_counter()
        try:
            resp = await call_next(ctx)
            status = "success"
            LLM_REQUESTS.labels(provider=provider, model=model, status=status).inc()
            LLM_LATENCY.labels(provider=provider, model=model).observe(time.perf_counter() - t0)
            if resp.usage:
                LLM_TOKENS.labels(provider=provider, model=model, type="prompt").inc(
                    resp.usage.prompt_tokens,
                )
                LLM_TOKENS.labels(provider=provider, model=model, type="completion").inc(
                    resp.usage.completion_tokens,
                )
            return resp
        except Exception as e:
            LLM_REQUESTS.labels(provider=provider, model=model, status="error").inc()
            LLM_ERRORS.labels(provider=provider, error_type=type(e).__name__).inc()
            raise
