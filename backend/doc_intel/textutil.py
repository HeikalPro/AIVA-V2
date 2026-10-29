"""Small helpers shared across the document intelligence modules."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    """Naive UTC timestamp, the storage convention for every doc-intel column."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(value: Any) -> str | None:
    """Render a stored (naive UTC) timestamp as ISO-8601 with a ``Z`` suffix.

    ``Database.fetch_*`` already turns datetimes into naive ISO strings; both forms
    are accepted so callers never have to care which one they hold.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat() + "Z"
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z") or "+" in text[10:]:
        return text
    return text.replace(" ", "T") + "Z"


def truncate_utf8(text: str | None, max_bytes: int) -> str | None:
    """Cut ``text`` so its UTF-8 encoding fits a VARCHAR2(max_bytes) column.

    Arabic text is 2 bytes per character, so truncating by characters (as the
    older ``[:4000]`` idiom does) can overflow the column and fail the very write
    that records the error.
    """
    if text is None:
        return None
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    cut = raw[: max(0, max_bytes - 3)]
    return cut.decode("utf-8", errors="ignore") + "..."
