from __future__ import annotations

import os
import tempfile
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from backend.auth.deps import (
    ROLE_DEVELOPER,
    ROLE_SUPER_ADMIN,
    UserContext,
    get_current_user,
    require_roles,
)
from backend.config import get_settings
from backend.dependencies import DbDep
from backend.exceptions import BadRequestError, NotFoundError
from backend.schemas.common import MessageResponse
from backend.schemas.widget import WidgetReleaseOut
from backend.services.audit import write_audit_log
from backend.services.widget_release import (
    INSTALLER_FILENAME,
    STORED_FILENAME,
    delete_release,
    installer_info,
    installer_path,
    replace_release,
    stored_file_path,
)

router = APIRouter(prefix="/widget-release", tags=["widget-release"])

_CHUNK = 1024 * 1024  # 1 MB streaming chunk

# Roles allowed to publish/remove a release. Downloads are open to any signed-in user.
_MANAGE_ROLES = (ROLE_SUPER_ADMIN, ROLE_DEVELOPER)


@router.get("", response_model=WidgetReleaseOut | None)
async def current_release(
    _user: Annotated[UserContext, Depends(get_current_user)],
) -> WidgetReleaseOut | None:
    """Current widget installer metadata (any signed-in user). None if none available.

    Served from the fixed installer file on disk (no DB row), so metadata can't
    desync from the actual file across restarts/redeploys.
    """
    info = installer_info()
    if not info:
        return None
    return WidgetReleaseOut(id=0, uploaded_by=0, **info)


@router.post("", response_model=WidgetReleaseOut, status_code=201)
async def upload_release(
    user: Annotated[UserContext, Depends(require_roles(*_MANAGE_ROLES))],
    db: DbDep,
    file: Annotated[UploadFile, File(...)],
    version: Annotated[str, Form(...)],
    notes: Annotated[str | None, Form()] = None,
) -> WidgetReleaseOut:
    """Upload a new widget .exe (Super Admin / Developer). Replaces the previous one."""
    settings = get_settings()
    filename = (file.filename or "").strip()
    if not filename.lower().endswith(".exe"):
        raise BadRequestError("Only .exe files are allowed.")
    version = (version or "").strip()
    if not version:
        raise BadRequestError("Version is required.")

    max_bytes = settings.widget_release_max_mb * 1024 * 1024
    target = stored_file_path()

    # Stream to a temp file in the same dir (enforcing the size cap), then atomically swap.
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
    size = 0
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise BadRequestError(f"File exceeds the {settings.widget_release_max_mb} MB limit.")
                out.write(chunk)
        if size == 0:
            raise BadRequestError("The uploaded file is empty.")
        os.replace(tmp_name, target)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise
    finally:
        await file.close()

    release = await replace_release(
        db,
        version=version,
        notes=(notes or None),
        original_filename=filename,
        stored_filename=STORED_FILENAME,
        file_size=size,
        content_type=file.content_type,
        uploaded_by=user.id,
    )
    assert release is not None
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="widget_release",
        entity_id=int(release["id"]),
        action_type="UPLOAD",
        new_value={"version": version, "filename": filename, "file_size": size},
    )
    return WidgetReleaseOut(**release)


@router.get("/download")
async def download_release(
    _user: Annotated[UserContext, Depends(get_current_user)],
) -> FileResponse:
    """Download the widget installer (any signed-in user)."""
    path = installer_path()
    if not path.exists():
        raise NotFoundError("No widget installer is available yet.")
    return FileResponse(
        path=str(path),
        media_type="application/octet-stream",
        filename=INSTALLER_FILENAME,
    )


@router.delete("", response_model=MessageResponse)
async def remove_release(
    user: Annotated[UserContext, Depends(require_roles(*_MANAGE_ROLES))],
    db: DbDep,
) -> MessageResponse:
    """Remove the current widget release (Super Admin / Developer)."""
    await delete_release(db)
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="widget_release",
        entity_id=0,
        action_type="DELETE",
    )
    return MessageResponse(message="Widget release deleted")
