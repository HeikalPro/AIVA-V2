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


# User-facing name for the fixed installer download.
INSTALLER_FILENAME = "Aiva-Setup.exe"


def installer_path() -> Path:
    """Path to the fixed installer to serve.

    Uses settings.widget_installer_path when set (durable, survives redeploys);
    otherwise falls back to the legacy uploaded file at stored_file_path().
    """
    configured = (get_settings().widget_installer_path or "").strip()
    return Path(configured) if configured else stored_file_path()


def installer_info() -> dict[str, Any] | None:
    """Metadata for the fixed installer, read straight from disk. None if missing."""
    path = installer_path()
    if not path.exists():
        return None
    return {
        "version": get_settings().widget_version,
        "file_size": path.stat().st_size,
        "original_filename": INSTALLER_FILENAME,
        "content_type": "application/octet-stream",
    }


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
