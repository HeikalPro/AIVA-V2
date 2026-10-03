"""SharePoint / OneDrive -> CRM endpoints (Phase 2, migration V002).

Super Admin: create / edit / delete sources and their credentials, "Sync now", file retry,
and the CRM entities (business data that may hold personal data). Super Admin + Developer:
view sources (identifiers shown, the secret never), runs and files, and run the connection
diagnostics. Every route answers 503 until migration V002 is installed.

The source bodies (they carry the client secret) are parsed here, AFTER the role guard, and
a validation error never echoes the submitted values: FastAPI's default 422 would return
the very secret it was sent.
"""
from __future__ import annotations

from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi import status as http_status
from pydantic import BaseModel, ValidationError

from backend.doc_intel.constants import EntityStatus, FileState, FileStatus
from backend.doc_intel.guards import AdminUser, MonitorUser
from backend.doc_intel.runtime import CrmRuntime
from backend.doc_intel.schemas import (
    ConnectionTestOut,
    CrmEntityListOut,
    CrmEntityOut,
    SourceCreate,
    SourceFileListOut,
    SourceFileOut,
    SourceOut,
    SourceUpdate,
    SyncRunListOut,
    SyncRunOut,
)

router = APIRouter(tags=["doc-intel"])

M = TypeVar("M", bound=BaseModel)


def _json_body(model: type[BaseModel]) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        }
    }


async def _read_body(request: Request, model: type[M]) -> M:
    """Validate the JSON body as ``model``; 422 errors carry type, location and message only."""
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(
            status_code=422,
            detail=[{"type": "json_invalid", "loc": ["body"], "msg": "The request body must be valid JSON"}],
        ) from None
    try:
        return model.model_validate(data)
    except ValidationError as ex:
        errors = ex.errors(include_url=False, include_context=False, include_input=False)
        raise HTTPException(
            status_code=422,
            detail=[{"type": e.get("type"), "loc": ["body", *e.get("loc", ())], "msg": e.get("msg")} for e in errors],
        ) from None


# ---- sources ----------------------------------------------------------------------------------------


@router.get("/sources", response_model=list[SourceOut])
async def list_sources(user: MonitorUser, runtime: CrmRuntime) -> list[SourceOut]:
    """Every source that is not deleted (the client secret is never included)."""
    return await runtime.sync_service.list_sources(user)


@router.post(
    "/sources",
    response_model=SourceOut,
    status_code=http_status.HTTP_201_CREATED,
    openapi_extra=_json_body(SourceCreate),
)
async def create_source(request: Request, user: AdminUser, runtime: CrmRuntime) -> SourceOut:
    """Add a SharePoint / OneDrive folder; the Tenant ID, Client ID and secret are stored encrypted."""
    body = await _read_body(request, SourceCreate)
    return await runtime.sync_service.create_source(user, body)


@router.get("/sources/{source_id}", response_model=SourceOut)
async def get_source(source_id: int, user: MonitorUser, runtime: CrmRuntime) -> SourceOut:
    return await runtime.sync_service.get_source_out(source_id, user)


@router.patch("/sources/{source_id}", response_model=SourceOut, openapi_extra=_json_body(SourceUpdate))
async def update_source(source_id: int, request: Request, user: AdminUser, runtime: CrmRuntime) -> SourceOut:
    """Change the fields sent; omit ``client_secret`` to keep the stored one."""
    body = await _read_body(request, SourceUpdate)
    return await runtime.sync_service.update_source(user, source_id, body)


@router.delete("/sources/{source_id}", status_code=http_status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_source(source_id: int, user: AdminUser, runtime: CrmRuntime) -> Response:
    """Soft delete: syncing stops and the stored secret is wiped; runs, files and entities are kept."""
    await runtime.sync_service.delete_source(user, source_id)
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post("/sources/{source_id}/test", response_model=ConnectionTestOut)
async def run_connection_test(source_id: int, user: MonitorUser, runtime: CrmRuntime) -> ConnectionTestOut:
    """Connection diagnostics, step by step (credentials, sign-in, site, library, folder, listing). Read-only."""
    return await runtime.sync_service.test_connection(source_id)


@router.post("/sources/{source_id}/sync", response_model=SyncRunOut, status_code=http_status.HTTP_202_ACCEPTED)
async def sync_source_now(source_id: int, user: AdminUser, runtime: CrmRuntime) -> SyncRunOut:
    """"Sync now": queue a run (409 while one is already queued or running)."""
    return await runtime.sync_service.sync_now(user, source_id)


@router.get("/sources/{source_id}/runs", response_model=SyncRunListOut)
async def list_source_runs(
    source_id: int,
    user: MonitorUser,
    runtime: CrmRuntime,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SyncRunListOut:
    """Sync runs of a source, newest first."""
    return await runtime.sync_service.list_runs(source_id, limit=limit, offset=offset)


@router.get("/sources/{source_id}/files", response_model=SourceFileListOut)
async def list_source_files(
    source_id: int,
    user: MonitorUser,
    runtime: CrmRuntime,
    state: FileState | None = Query(default=None),
    status: FileStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SourceFileListOut:
    """Tracked files with their five stages, most recently changed first."""
    return await runtime.sync_service.list_files(source_id, state=state, status=status, limit=limit, offset=offset)


@router.post("/source-files/{file_id}/retry", response_model=SourceFileOut, status_code=http_status.HTTP_202_ACCEPTED)
async def retry_source_file(file_id: int, user: AdminUser, runtime: CrmRuntime) -> SourceFileOut:
    """Reset a failed file; it is processed by the sync queued for its source."""
    return await runtime.sync_service.retry_file(user, file_id)


# ---- CRM store (Super Admin only: business data) ----------------------------------------------------


@router.get("/crm/entities", response_model=CrmEntityListOut)
async def list_crm_entities(
    user: AdminUser,
    runtime: CrmRuntime,
    source_id: int | None = Query(default=None),
    entity_type: str | None = Query(default=None, max_length=64),
    status: EntityStatus | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CrmEntityListOut:
    """Entities of the internal CRM store, newest first; ``q`` searches the display value and match key."""
    return await runtime.sync_service.list_entities(
        source_id=source_id, entity_type=entity_type, status=status, q=q, limit=limit, offset=offset
    )


@router.get("/crm/entities/{entity_id}", response_model=CrmEntityOut)
async def get_crm_entity(entity_id: int, user: AdminUser, runtime: CrmRuntime) -> CrmEntityOut:
    """One entity with its fields, provenance (page, block, source text) and validation issues."""
    return await runtime.sync_service.get_entity(entity_id)
