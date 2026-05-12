"""Prometheus metric definitions (optional dependency)."""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

T = TypeVar("T")


class _MetricLike(Protocol):
    def labels(self, *args: Any, **kwargs: Any) -> _MetricLike: ...

    def inc(self, *args: Any, **kwargs: Any) -> None: ...

    def observe(self, *args: Any, **kwargs: Any) -> None: ...


class _NoopMetric:
    def labels(self, *args: Any, **kwargs: Any) -> _NoopMetric:
        return self

    def inc(self, *args: Any, **kwargs: Any) -> None:
        return None

    def observe(self, *args: Any, **kwargs: Any) -> None:
        return None


try:
    from prometheus_client import Counter as _Counter
    from prometheus_client import Gauge as _Gauge
    from prometheus_client import Histogram as _Histogram

    LLM_REQUESTS = _Counter(
        "llm_requests_total",
        "Total LLM requests",
        ["provider", "model", "status"],
    )
    LLM_LATENCY = _Histogram(
        "llm_latency_seconds",
        "LLM request latency",
        ["provider", "model"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    )
    LLM_TOKENS = _Counter(
        "llm_tokens_total",
        "Total tokens used",
        ["provider", "model", "type"],
    )
    LLM_ERRORS = _Counter(
        "llm_errors_total",
        "Total LLM errors",
        ["provider", "error_type"],
    )
    CIRCUIT_STATE = _Gauge(
        "llm_circuit_breaker_state",
        "Circuit breaker state (0=closed,1=half_open,2=open)",
        ["provider"],
    )
except ImportError:  # pragma: no cover
    LLM_REQUESTS = _NoopMetric()
    LLM_LATENCY = _NoopMetric()
    LLM_TOKENS = _NoopMetric()
    LLM_ERRORS = _NoopMetric()
    CIRCUIT_STATE = _NoopMetric()
