"""Structured logging with optional secret redaction."""

from __future__ import annotations

import logging
import re
from typing import Any

import structlog

_REDACT_KEYS = re.compile(r"(api_key|authorization|password|secret|token)", re.I)


def _redact_secrets(
    _logger: logging.Logger,
    _method: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in event_dict.items():
        if isinstance(k, str) and _REDACT_KEYS.search(k):
            out[k] = "***REDACTED***"
        else:
            out[k] = v
    return out


def configure_logging(*, level: str = "INFO", fmt: str = "json") -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO))
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_secrets,
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO),
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
