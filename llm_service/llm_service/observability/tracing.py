"""OpenTelemetry bootstrap (optional dependency)."""

from __future__ import annotations

from typing import Any


def configure_tracing(endpoint: str = "http://localhost:4317") -> Any:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:  # pragma: no cover
        from llm_service.core.exceptions import ImportExtraError

        raise ImportExtraError(
            "Install observability extras: pip install 'llm-service[observability]'",
        ) from e

    provider = TracerProvider(resource=Resource.create({"service.name": "llm-service"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    return trace.get_tracer("llm_service")
