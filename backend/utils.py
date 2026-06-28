from __future__ import annotations

import inspect
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _format_scalar(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


async def normalize_db_value(value: Any) -> Any:
    if isinstance(value, (datetime, date, Decimal)):
        return _format_scalar(value)
    if hasattr(value, "read"):
        result = value.read()
        if inspect.isawaitable(result):
            result = await result
        return _format_scalar(result)
    return _format_scalar(value)


async def normalize_db_row(row: tuple[Any, ...], columns: list[str]) -> dict[str, Any]:
    return {
        name: await normalize_db_value(value)
        for name, value in zip(columns, row, strict=False)
    }


def serialize_value(value: Any) -> Any:
    if inspect.iscoroutine(value) or inspect.isawaitable(value):
        return value
    if isinstance(value, (datetime, date, Decimal)):
        return _format_scalar(value)
    if hasattr(value, "read"):
        result = value.read()
        if inspect.isawaitable(result):
            return value
        return _format_scalar(result)
    return _format_scalar(value)


def serialize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: serialize_value(v) for k, v in row.items()}


def serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [serialize_row(r) or {} for r in rows]


ALLOWED_USER_EMAIL_DOMAIN = "gochat247.com"


def normalize_allowed_email(email: str) -> str:
    """Require @{ALLOWED_USER_EMAIL_DOMAIN}; allow local-part-only input."""
    raw = (email or "").strip().lower()
    if not raw:
        raise ValueError("Email is required")
    local, sep, domain = raw.partition("@")
    if not local:
        raise ValueError("Email is required")
    if sep and domain != ALLOWED_USER_EMAIL_DOMAIN:
        raise ValueError(f"Email must use @{ALLOWED_USER_EMAIL_DOMAIN}")
    if not sep:
        return f"{local}@{ALLOWED_USER_EMAIL_DOMAIN}"
    return raw
