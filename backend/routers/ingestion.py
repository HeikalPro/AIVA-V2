from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends

from backend.auth.deps import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_DEVELOPER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_SUPERVISOR,
    UserContext,
    require_account_access,
    require_roles,
)
from backend.dependencies import DbDep, EmbeddingServiceDep
from backend.exceptions import BadRequestError, ForbiddenError, NotFoundError
from backend.schemas.ingestion import (
    INGESTION_STATUSES,
    IngestionPendingCountOut,
    IngestionRequestCreate,
    IngestionRequestCreateOut,
    IngestionRequestOut,
    IngestionRequestUpdate,
    IngestionTrigger,
    JobOut,
)
from backend.services.audit import write_audit_log
from backend.services.ingestion_requests import build_stored_description, parse_stored_description
from backend.services.notifications import notify_developers_new_ingestion
from backend.utils import serialize_row

router = APIRouter(prefix="/ingestion", tags=["ingestion"])

_REQUEST_SELECT = """
    SELECT ir.*, a.name AS account_name,
           o.id AS organization_id, o.name AS organization_name
    FROM AIVA_ingestion_requests ir
    JOIN AIVA_accounts a ON a.id = ir.account_id
    JOIN AIVA_organizations o ON o.id = a.organization_id
    WHERE ir.id = :id
"""


async def _fetch_request_row(db: DbDep, request_id: int) -> dict:
    row = await db.fetch_one(_REQUEST_SELECT, {"id": request_id})
    if not row:
        raise NotFoundError("Ingestion request not found")
    return row


def _ensure_ingestion_request_access(user: UserContext, row: dict) -> None:
    if user.is_super_admin:
        return
    org_id = row.get("organization_id")
    if org_id is None:
        raise ForbiddenError("Cannot access ingestion request outside your organization")
    if int(org_id) != user.organization_id:
        raise ForbiddenError("Cannot access ingestion request outside your organization")


def _row_to_out(row: dict) -> IngestionRequestOut:
    data = serialize_row(row) or {}
    kb_desc, name, email, phone = parse_stored_description(data.get("description"))
    return IngestionRequestOut(
        id=int(data["id"]),
        account_id=int(data["account_id"]),
        account_name=row.get("account_name"),
        organization_id=int(row["organization_id"]) if row.get("organization_id") is not None else None,
        organization_name=row.get("organization_name"),
        requested_by=int(data["requested_by"]),
        requester_name=name,
        requester_email=email,
        requester_phone=phone,
        request_type=data.get("request_type"),
        status=data.get("status"),
        priority=data.get("priority"),
        description=kb_desc,
        created_at=data.get("created_at"),
    )


@router.post("/requests", response_model=IngestionRequestCreateOut, status_code=201)
async def create_ingestion_request(
    body: IngestionRequestCreate,
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER, ROLE_SUPERVISOR)),
    ],
    db: DbDep,
) -> IngestionRequestCreateOut:
    account = await db.fetch_one("SELECT organization_id FROM AIVA_accounts WHERE id = :id", {"id": body.account_id})
    if not account:
        raise NotFoundError("Account not found")
    require_account_access(body.account_id, user, int(account["organization_id"]))

    stored_description = build_stored_description(body.description, user, body.requester_phone)

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
            "priority": "MEDIUM",
            "description": stored_description,
        },
        return_id=True,
    )
    row = await db.fetch_one(
        """
        SELECT ir.*, a.name AS account_name
        FROM AIVA_ingestion_requests ir
        JOIN AIVA_accounts a ON a.id = ir.account_id
        WHERE ir.id = :id
        """,
        {"id": req_id},
    )
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="ingestion_request",
        entity_id=int(req_id or 0),
        action_type="CREATE",
        new_value=body.model_dump(),
    )
    notify_result = await notify_developers_new_ingestion(
        organization_id=int(account["organization_id"]),
        request_id=int(req_id or 0),
        request_type=body.request_type,
        description=body.description.strip(),
        account_name=row.get("account_name") if row else None,
        created_by_user_id=user.id,
    )
    out = _row_to_out(row or {})
    return IngestionRequestCreateOut(**out.model_dump(), developer_notify=notify_result)


@router.get("/requests/pending-count", response_model=IngestionPendingCountOut)
async def ingestion_pending_count(
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> IngestionPendingCountOut:
    row = await db.fetch_one(
        """
        SELECT COUNT(*) AS cnt FROM AIVA_ingestion_requests
        WHERE status = 'PENDING'
        """
    )
    cnt = int(row["cnt"]) if row and row.get("cnt") is not None else 0
    return IngestionPendingCountOut(pending_count=cnt)


@router.get("/requests", response_model=list[IngestionRequestOut])
async def list_ingestion_requests(
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER, ROLE_SUPERVISOR, ROLE_DEVELOPER)),
    ],
    db: DbDep,
) -> list[IngestionRequestOut]:
    if user.is_super_admin:
        rows = await db.fetch_all(
            """
            SELECT ir.*, a.name AS account_name,
                   o.id AS organization_id, o.name AS organization_name
            FROM AIVA_ingestion_requests ir
            JOIN AIVA_accounts a ON a.id = ir.account_id
            JOIN AIVA_organizations o ON o.id = a.organization_id
            ORDER BY ir.created_at DESC
            """
        )
    else:
        rows = await db.fetch_all(
            """
            SELECT ir.*, a.name AS account_name
            FROM AIVA_ingestion_requests ir
            JOIN AIVA_accounts a ON a.id = ir.account_id
            WHERE a.organization_id = :org_id
            ORDER BY ir.created_at DESC
            """,
            {"org_id": user.organization_id},
        )
    return [_row_to_out(r) for r in rows]


@router.get("/requests/{request_id}", response_model=IngestionRequestOut)
async def get_ingestion_request(
    request_id: int,
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_DEVELOPER)),
    ],
    db: DbDep,
) -> IngestionRequestOut:
    row = await _fetch_request_row(db, request_id)
    _ensure_ingestion_request_access(user, row)
    return _row_to_out(row)


@router.patch("/requests/{request_id}", response_model=IngestionRequestOut)
async def update_ingestion_request(
    request_id: int,
    body: IngestionRequestUpdate,
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_DEVELOPER)),
    ],
    db: DbDep,
) -> IngestionRequestOut:
    status = body.status.strip().upper()
    if status not in INGESTION_STATUSES:
        raise BadRequestError(f"status must be one of: {', '.join(INGESTION_STATUSES)}")

    old = await _fetch_request_row(db, request_id)
    _ensure_ingestion_request_access(user, old)
    await db.execute(
        "UPDATE AIVA_ingestion_requests SET status = :status WHERE id = :id",
        {"status": status, "id": request_id},
    )
    row = await _fetch_request_row(db, request_id)
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="ingestion_request",
        entity_id=request_id,
        action_type="UPDATE",
        old_value={"status": old.get("status")},
        new_value={"status": status},
    )
    return _row_to_out(row)


@router.post("/trigger", response_model=JobOut)
async def trigger_ingestion(
    body: IngestionTrigger,
    user: Annotated[UserContext, Depends(require_roles(ROLE_ACCOUNT_MANAGER, ROLE_SUPERVISOR))],
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
        Depends(require_roles(ROLE_ACCOUNT_MANAGER, ROLE_SUPERVISOR)),
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
