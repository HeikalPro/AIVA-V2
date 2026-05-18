from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends

from backend.auth.deps import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_DEVELOPER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    UserContext,
    require_account_access,
    require_roles,
)
from backend.dependencies import DbDep, EmbeddingServiceDep
from backend.exceptions import BadRequestError, ForbiddenError, NotFoundError
from backend.schemas.ingestion import IngestionRequestCreate, IngestionRequestOut, IngestionTrigger, JobOut
from backend.services.audit import write_audit_log
from backend.utils import serialize_row

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.post("/requests", response_model=IngestionRequestOut, status_code=201)
async def create_ingestion_request(
    body: IngestionRequestCreate,
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)),
    ],
    db: DbDep,
) -> IngestionRequestOut:
    account = await db.fetch_one("SELECT organization_id FROM AIVA_accounts WHERE id = :id", {"id": body.account_id})
    if not account:
        raise NotFoundError("Account not found")
    require_account_access(body.account_id, user, int(account["organization_id"]))

    req_id = await db.execute(
        """
        INSERT INTO AIVA_ingestion_requests (
            account_id, requested_by, request_type, status, priority, description
        ) VALUES (
            :account_id, :requested_by, :request_type, 'PENDING', :priority, :description
        )
        RETURNING id INTO :out_id
        """,
        {
            "account_id": body.account_id,
            "requested_by": user.id,
            "request_type": body.request_type,
            "priority": body.priority,
            "description": body.description,
        },
        return_id=True,
    )
    row = await db.fetch_one("SELECT * FROM AIVA_ingestion_requests WHERE id = :id", {"id": req_id})
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="ingestion_request",
        entity_id=int(req_id or 0),
        action_type="CREATE",
        new_value=body.model_dump(),
    )
    return IngestionRequestOut(**serialize_row(row) or {})


@router.get("/requests", response_model=list[IngestionRequestOut])
async def list_ingestion_requests(
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER, ROLE_DEVELOPER)),
    ],
    db: DbDep,
) -> list[IngestionRequestOut]:
    if user.is_super_admin:
        rows = await db.fetch_all("SELECT * FROM AIVA_ingestion_requests ORDER BY created_at DESC")
    elif user.has_role(ROLE_DEVELOPER):
        rows = await db.fetch_all(
            "SELECT * FROM AIVA_ingestion_requests WHERE status IN ('PENDING', 'IN_PROGRESS') ORDER BY created_at DESC"
        )
    else:
        rows = await db.fetch_all(
            """
            SELECT ir.* FROM AIVA_ingestion_requests ir
            JOIN AIVA_accounts a ON a.id = ir.account_id
            WHERE a.organization_id = :org_id
            ORDER BY ir.created_at DESC
            """,
            {"org_id": user.organization_id},
        )
    return [IngestionRequestOut(**serialize_row(r) or {}) for r in rows]


@router.post("/trigger", response_model=JobOut)
async def trigger_ingestion(
    body: IngestionTrigger,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_DEVELOPER))],
    embedding_svc: EmbeddingServiceDep,
) -> JobOut:
    if not body.lines and not body.records:
        raise BadRequestError("Provide lines or records")

    def _run():
        if body.reindex:
            return embedding_svc.reindex(
                body.corpus_id,
                lines=body.lines,
                records=body.records,
            )
        return embedding_svc.ingest(
            body.corpus_id,
            lines=body.lines,
            records=body.records,
        )

    result = await asyncio.to_thread(_run)
    return JobOut(job_id=result["job_id"], mode=result.get("mode"))


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job_status(
    job_id: str,
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_DEVELOPER, ROLE_ACCOUNT_MANAGER)),
    ],
    embedding_svc: EmbeddingServiceDep,
) -> JobOut:
    try:
        job = await asyncio.to_thread(embedding_svc.get_job, job_id)
    except LookupError as ex:
        raise NotFoundError("Job not found") from ex
    except ValueError as ex:
        raise BadRequestError(str(ex)) from ex

    return JobOut(
        job_id=job_id,
        status=job.get("status"),
        error_msg=job.get("error_msg"),
    )
