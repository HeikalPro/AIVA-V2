"""Build a `DownloadedFile` from a file on local disk (tests, CLI, backfills)."""

from __future__ import annotations

import mimetypes
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..connectors.sharepoint.models import DownloadedFile, SourceMetadata

__all__ = ["downloaded_file_from_path", "guess_mime_type"]

# Types some platforms' mimetypes tables lack (e.g. Windows without Office installed).
_FALLBACK_MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".msg": "application/vnd.ms-outlook",
    ".eml": "message/rfc822",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def guess_mime_type(filename: str) -> str | None:
    """MIME type from the file extension, or None when unknown."""
    guessed, _ = mimetypes.guess_type(filename, strict=False)
    if guessed:
        return guessed
    return _FALLBACK_MIME_TYPES.get(Path(filename).suffix.lower())


def _created_timestamp(st: os.stat_result) -> float | None:
    birth: float | None = getattr(st, "st_birthtime", None)
    if birth is not None:
        return birth
    if os.name == "nt":  # on Windows st_ctime is the creation time
        return st.st_ctime
    return None


def downloaded_file_from_path(
    path: str | os.PathLike[str],
    *,
    clock: Callable[[], datetime] | None = None,
    mime_type: str | None = None,
) -> DownloadedFile:
    """Read `path` into a `DownloadedFile` with `source_system="local"` metadata.

    `mime_type` overrides the extension-based guess."""
    p = Path(path).resolve()
    content = p.read_bytes()
    st = p.stat()
    mime = mime_type or guess_mime_type(p.name)
    created = _created_timestamp(st)
    now = (clock or _utc_now)()
    metadata = SourceMetadata(
        source_system="local",
        filename=p.name,
        mime_type=mime,
        size=len(content),
        source_uri=p.as_uri(),
        created_at=datetime.fromtimestamp(created, UTC) if created is not None else None,
        modified_at=datetime.fromtimestamp(st.st_mtime, UTC),
        retrieved_at=now,
    )
    return DownloadedFile(content=content, filename=p.name, mime_type=mime, metadata=metadata)
