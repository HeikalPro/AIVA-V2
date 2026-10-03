"""Knowledge-document import endpoints (Super Admin only)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import status as http_status
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException

from backend.doc_intel.constants import DocStatus
from backend.doc_intel.guards import AdminUser
from backend.doc_intel.runtime import InstalledRuntime
from backend.doc_intel.schemas import (
    KbDocumentListOut,
    KbDocumentOut,
    KbPreviewOut,
    KbQueuesUpdate,
    KbUploadOut,
)
from backend.exceptions import BadRequestError

router = APIRouter(prefix="/kb-documents", tags=["doc-intel"])

# Room for the multipart framing and the small form fields around the files.
_FORM_OVERHEAD_BYTES = 1024 * 1024

_UPLOAD_FORM_SCHEMA = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["account_id", "queue_keys", "files"],
                    "properties": {
                        "account_id": {"type": "integer"},
                        "queue_keys": {"type": "array", "items": {"type": "string"}},
                        "files": {"type": "array", "items": {"type": "string", "format": "binary"}},
                    },
                }
            }
        },
    }
}


@router.post(
    "",
    response_model=KbUploadOut,
    status_code=http_status.HTTP_202_ACCEPTED,
    openapi_extra=_UPLOAD_FORM_SCHEMA,
)
async def upload_kb_documents(request: Request, user: AdminUser, runtime: InstalledRuntime) -> KbUploadOut:
    """Store PDF/DOCX files and queue them for import into the selected queues (returns at once).

    The multipart body is parsed here, AFTER the Super Admin guard. Declaring the fields as
    ``Form``/``File`` parameters would make FastAPI read and spool the whole body before
    authentication, so anyone could make the server buffer uploads it then refuses.
    """
    settings = runtime.settings
    limit = settings.max_upload_bytes * settings.max_files_per_upload + _FORM_OVERHEAD_BYTES
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(
            status_code=413,  # literal: the constant's name differs across Starlette versions
            detail=f"Upload too large: at most {settings.max_files_per_upload} files of "
            f"{settings.max_upload_mb} MB each per request",
        )
    try:
        form = await request.form(max_files=settings.max_files_per_upload + 1, max_fields=100)
    except MultiPartException as ex:
        raise BadRequestError(f"Invalid upload form: {ex.message}") from None
    try:
        raw_account = form.get("account_id")
        if not isinstance(raw_account, str) or not raw_account.strip().lstrip("-").isdigit():
            raise BadRequestError("account_id is required and must be a number")
        queue_keys = [str(v) for v in form.getlist("queue_keys") if isinstance(v, str)]
        files = [f for f in form.getlist("files") if isinstance(f, StarletteUploadFile)]
        return await runtime.service.upload(user, int(raw_account), queue_keys, files)
    finally:
        await form.close()


@router.get("", response_model=KbDocumentListOut)
async def list_kb_documents(
    user: AdminUser,
    runtime: InstalledRuntime,
    account_id: int | None = Query(default=None),
    status: DocStatus | None = Query(default=None),
    batch_id: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> KbDocumentListOut:
    return await runtime.service.list_documents(
        account_id=account_id, status=status, batch_id=batch_id, limit=limit, offset=offset
    )


@router.get("/{document_id}", response_model=KbDocumentOut)
async def get_kb_document(document_id: int, user: AdminUser, runtime: InstalledRuntime) -> KbDocumentOut:
    return await runtime.service.get_document_out(document_id)


@router.get("/{document_id}/preview", response_model=KbPreviewOut)
async def preview_kb_document(
    document_id: int,
    user: AdminUser,
    runtime: InstalledRuntime,
    max_chars: int = Query(default=20000, ge=1, le=200000),
) -> KbPreviewOut:
    """Extracted text per page (truncated), for checking extraction quality."""
    return await runtime.service.preview(document_id, max_chars)


@router.post("/{document_id}/retry", response_model=KbDocumentOut, status_code=http_status.HTTP_202_ACCEPTED)
async def retry_kb_document(document_id: int, user: AdminUser, runtime: InstalledRuntime) -> KbDocumentOut:
    """Re-queue a failed document; it resumes at chunking when the extracted text is kept."""
    return await runtime.service.retry(user, document_id)


@router.post("/{document_id}/republish", response_model=KbDocumentOut, status_code=http_status.HTTP_202_ACCEPTED)
async def republish_kb_document(document_id: int, user: AdminUser, runtime: InstalledRuntime) -> KbDocumentOut:
    """Re-chunk, re-embed and re-publish from the stored extracted text (e.g. after a REINDEX)."""
    return await runtime.service.republish(user, document_id)


@router.patch("/{document_id}/queues", response_model=KbDocumentOut)
async def change_kb_document_queues(
    document_id: int, body: KbQueuesUpdate, user: AdminUser, runtime: InstalledRuntime
) -> KbDocumentOut:
    """Change the queues a document is available in (configuration only, no re-embedding)."""
    return await runtime.service.change_queues(user, document_id, body.queue_keys)


@router.delete("/{document_id}", response_model=KbDocumentOut)
async def unpublish_kb_document(document_id: int, user: AdminUser, runtime: InstalledRuntime) -> KbDocumentOut:
    """Unpublish: removed from every queue, chunks deleted; the record is kept as UNPUBLISHED."""
    return await runtime.service.unpublish(user, document_id)
