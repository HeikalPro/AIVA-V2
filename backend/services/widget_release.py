"""Widget installer (.exe) storage: binary on disk, metadata in AIVA_widget_release.

Latest-only — each upload replaces the previous file and metadata row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.config import get_settings
from backend.database import Database
from backend.utils import serialize_row

# Fixed on-disk name; the user-facing download name is derived from version/original name.
STORED_FILENAME = "widget-latest.exe"


def release_dir() -> Path:
    d = Path(get_settings().widget_release_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def stored_file_path() -> Path:
    return release_dir() / STORED_FILENAME


async def get_current_release(db: Database) -> dict[str, Any] | None:
    row = await db.fetch_one(
        """
        SELECT wr.id, wr.version, wr.original_filename, wr.stored_filename, wr.file_size,
               wr.content_type, wr.notes, wr.uploaded_by, wr.uploaded_at,
               u.email AS uploaded_by_email
        FROM AIVA_widget_release wr
        LEFT JOIN AIVA_users u ON u.id = wr.uploaded_by
        ORDER BY wr.id DESC
        FETCH FIRST 1 ROW ONLY
        """
    )
    return serialize_row(row) if row else None


async def replace_release(
    db: Database,
    *,
    version: str,
    notes: str | None,
    original_filename: str,
    stored_filename: str,
    file_size: int,
    content_type: str | None,
    uploaded_by: int,
) -> dict[str, Any] | None:
    """Replace the current release metadata (latest-only)."""
    await db.execute("DELETE FROM AIVA_widget_release")
    await db.execute(
        """
        INSERT INTO AIVA_widget_release (
            version, original_filename, stored_filename, file_size, content_type, notes, uploaded_by
        ) VALUES (
            :version, :original_filename, :stored_filename, :file_size, :content_type, :notes, :uploaded_by
        )
        """,
        {
            "version": version,
            "original_filename": original_filename,
            "stored_filename": stored_filename,
            "file_size": file_size,
            "content_type": content_type,
            "notes": notes,
            "uploaded_by": uploaded_by,
        },
    )
    return await get_current_release(db)


async def delete_release(db: Database) -> None:
    await db.execute("DELETE FROM AIVA_widget_release")
    path = stored_file_path()
    if path.exists():
        path.unlink()
