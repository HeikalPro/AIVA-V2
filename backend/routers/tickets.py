from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query

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
from backend.dependencies import DbDep
from backend.exceptions import ForbiddenError, NotFoundError
from backend.schemas.common import MessageResponse
from backend.schemas.notifications import DeveloperNotifyOut
from backend.schemas.tickets import (
    TicketCreate,
    TicketCreateOut,
    TicketOpenCountOut,
    TicketOut,
    TicketUpdate,
)
from backend.services.audit import write_audit_log
from backend.services.notifications import notify_developers_new_ticket
from backend.services.zoho_bridge import get_zoho_bridge
from backend.utils import serialize_row

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.get("/open-count", response_model=TicketOpenCountOut)
async def ticket_open_count(
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> TicketOpenCountOut:
    row = await db.fetch_one(
        """
        SELECT COUNT(*) AS cnt FROM AIVA_tickets
        WHERE status IN ('OPEN', 'IN_PROGRESS')
        """
    )
    cnt = int(row["cnt"]) if row and row.get("cnt") is not None else 0
    return TicketOpenCountOut(open_count=cnt)


async def _maybe_sync_zoho(ticket_row: dict) -> None:
    bridge = get_zoho_bridge()
    await bridge.push_ticket(serialize_row(ticket_row) or {})


@router.get("", response_model=list[TicketOut])
async def list_tickets(
    user: Annotated[
        UserContext,
        Depends(
            require_roles(
                ROLE_SUPER_ADMIN,
                ROLE_ORG_ADMIN,
                ROLE_ACCOUNT_MANAGER,
                ROLE_SUPERVISOR,
                ROLE_DEVELOPER,
            )
        ),
    ],
    db: DbDep,
    organization_id: int | None = Query(default=None),
    account_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
) -> list[TicketOut]:
    params: dict = {}
    clauses: list[str] = []

    if user.is_super_admin:
        if organization_id:
            clauses.append("organization_id = :organization_id")
            params["organization_id"] = organization_id
    else:
        clauses.append("organization_id = :organization_id")
        params["organization_id"] = user.organization_id

    if account_id:
        clauses.append("account_id = :account_id")
        params["account_id"] = account_id

    if status:
        clauses.append("status = :status")
        params["status"] = status

    if user.has_role(ROLE_DEVELOPER) and not user.is_super_admin:
        clauses.append("(assigned_to = :assigned_to OR assigned_to IS NULL)")
        params["assigned_to"] = user.id

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await db.fetch_all(
        f"SELECT * FROM AIVA_tickets {where} ORDER BY created_at DESC",
        params,
    )
    return [TicketOut(**serialize_row(r) or {}) for r in rows]


@router.post("", response_model=TicketCreateOut, status_code=201)
async def create_ticket(
    body: TicketCreate,
    background: BackgroundTasks,
    user: Annotated[
        UserContext,
        Depends(
            require_roles(
                ROLE_SUPER_ADMIN,
                ROLE_ORG_ADMIN,
                ROLE_ACCOUNT_MANAGER,
                ROLE_SUPERVISOR,
                ROLE_DEVELOPER,
            )
        ),
    ],
    db: DbDep,
) -> TicketCreateOut:
    if body.organization_id != user.organization_id:
        raise ForbiddenError("Cannot create ticket in another organization")
    if body.account_id:
        account = await db.fetch_one("SELECT organization_id FROM AIVA_accounts WHERE id = :id", {"id": body.account_id})
        if account:
            require_account_access(body.account_id, user, int(account["organization_id"]))

    ticket_id = await db.execute(
        """
        INSERT INTO AIVA_tickets (
            organization_id, account_id, created_by, ticket_type,
            priority, status, subject, description
        ) VALUES (
            :organization_id, :account_id, :created_by, :ticket_type,
            'MEDIUM', 'OPEN', :subject, :description
        )
        RETURNING id INTO :out_id
        """,
        {
            "organization_id": body.organization_id,
            "account_id": body.account_id,
            "created_by": user.id,
            "ticket_type": body.ticket_type,
            "subject": body.subject,
            "description": body.description,
        },
        return_id=True,
    )
    row = await db.fetch_one("SELECT * FROM AIVA_tickets WHERE id = :id", {"id": ticket_id})
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="ticket",
        entity_id=int(ticket_id or 0),
        action_type="CREATE",
        new_value={"subject": body.subject, "ticket_type": body.ticket_type},
    )
    notify_result = DeveloperNotifyOut(
        status="failed",
        message="Ticket was not created.",
    )
    if row:
        background.add_task(_maybe_sync_zoho, row)
        notify_result = await notify_developers_new_ticket(
            organization_id=body.organization_id,
            ticket_id=int(ticket_id or 0),
            subject=body.subject,
            description=body.description,
            account_id=body.account_id,
            created_by_user_id=user.id,
        )
    data = serialize_row(row) or {}
    return TicketCreateOut(**data, developer_notify=notify_result)


@router.get("/{ticket_id}", response_model=TicketOut)
async def get_ticket(
    ticket_id: int,
    user: Annotated[
        UserContext,
        Depends(
            require_roles(
                ROLE_SUPER_ADMIN,
                ROLE_ORG_ADMIN,
                ROLE_ACCOUNT_MANAGER,
                ROLE_SUPERVISOR,
                ROLE_DEVELOPER,
            )
        ),
    ],
    db: DbDep,
) -> TicketOut:
    row = await db.fetch_one("SELECT * FROM AIVA_tickets WHERE id = :id", {"id": ticket_id})
    if not row:
        raise NotFoundError("Ticket not found")
    if not user.is_super_admin and int(row["organization_id"]) != user.organization_id:
        raise ForbiddenError("Cannot view ticket in another organization")
    return TicketOut(**serialize_row(row) or {})


@router.patch("/{ticket_id}", response_model=TicketOut)
async def update_ticket(
    ticket_id: int,
    body: TicketUpdate,
    user: Annotated[
        UserContext,
        Depends(
            require_roles(
                ROLE_SUPER_ADMIN,
                ROLE_ORG_ADMIN,
                ROLE_ACCOUNT_MANAGER,
                ROLE_SUPERVISOR,
                ROLE_DEVELOPER,
            )
        ),
    ],
    db: DbDep,
) -> TicketOut:
    old = await db.fetch_one("SELECT * FROM AIVA_tickets WHERE id = :id", {"id": ticket_id})
    if not old:
        raise NotFoundError("Ticket not found")
    if not user.is_super_admin and int(old["organization_id"]) != user.organization_id:
        raise ForbiddenError("Cannot update ticket in another organization")

    updates = body.model_dump(exclude_unset=True)
    if updates:
        updates["id"] = ticket_id
        set_parts = [f"{k} = :{k}" for k in updates if k != "id"]
        await db.execute(
            f"UPDATE AIVA_tickets SET {', '.join(set_parts)} WHERE id = :id",
            updates,
        )
    row = await db.fetch_one("SELECT * FROM AIVA_tickets WHERE id = :id", {"id": ticket_id})
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="ticket",
        entity_id=ticket_id,
        action_type="UPDATE",
        old_value=serialize_row(old),
        new_value=updates,
    )
    return TicketOut(**serialize_row(row) or {})


@router.delete("/{ticket_id}", response_model=MessageResponse)
async def delete_ticket(
    ticket_id: int,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN))],
    db: DbDep,
) -> MessageResponse:
    old = await db.fetch_one("SELECT * FROM AIVA_tickets WHERE id = :id", {"id": ticket_id})
    if not old:
        raise NotFoundError("Ticket not found")
    if not user.is_super_admin and int(old["organization_id"]) != user.organization_id:
        raise ForbiddenError("Cannot delete ticket in another organization")
    await db.execute("DELETE FROM AIVA_tickets WHERE id = :id", {"id": ticket_id})
    return MessageResponse(message="Ticket deleted")
