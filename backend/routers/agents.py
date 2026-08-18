from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.auth.deps import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_AGENT,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_SUPERVISOR,
    UserContext,
    require_account_access,
    require_roles_or_nav_permission,
)
from backend.auth.hashing import hash_password
from backend.dependencies import DbDep, EmbeddingServiceDep
from backend.exceptions import ConflictError, ForbiddenError, NotFoundError
from backend.schemas.agents import TraineeCreate
from backend.schemas.kb_queues import (
    AgentQueueAccessOut,
    AgentQueueAccessUpdate,
    AgentQueueSummaryOut,
    QueueGroupOut,
)
from backend.schemas.users import UserOut
from backend.services.account_membership import grant_account_role_access
from backend.services.agent_queue_access import (
    build_agent_queue_summaries,
    list_assigned_queue_keys,
    list_assigned_queue_keys_by_account,
    require_can_manage_agent_queues,
    set_assigned_queue_keys,
)
from backend.services.audit import write_audit_log
from backend.services.chat_queues import load_account_corpus_config
from backend.services.kb_queue_groups import list_queue_catalog
from backend.services.user_queries import build_user_out

router = APIRouter(prefix="/agents", tags=["agents"])

_AGENT_ACCESS = require_roles_or_nav_permission(
    "agents",
    ROLE_SUPER_ADMIN,
    ROLE_ORG_ADMIN,
    ROLE_ACCOUNT_MANAGER,
    ROLE_SUPERVISOR,
)


async def _resolve_agent_role_id(db: DbDep) -> int:
    row = await db.fetch_one("SELECT id FROM AIVA_roles WHERE name = :name", {"name": ROLE_AGENT})
    if not row:
        raise NotFoundError("AGENT role is not configured")
    return int(row["id"])


async def _accessible_account_ids(db: DbDep, user: UserContext) -> set[int]:
    if user.is_super_admin:
        rows = await db.fetch_all("SELECT id FROM AIVA_accounts")
        return {int(r["id"]) for r in rows}
    if user.is_org_admin:
        rows = await db.fetch_all(
            "SELECT id FROM AIVA_accounts WHERE organization_id = :org_id",
            {"org_id": user.organization_id},
        )
        return {int(r["id"]) for r in rows}
    return set(user.account_ids)


async def _require_promotable_trainee(
    db: DbDep,
    user: UserContext,
    user_id: int,
    account_id: int,
) -> dict:
    account = await db.fetch_one(
        "SELECT id, organization_id FROM AIVA_accounts WHERE id = :id",
        {"id": account_id},
    )
    if not account:
        raise NotFoundError("Account not found")

    account_org = int(account["organization_id"])
    require_account_access(account_id, user, account_org)

    row = await db.fetch_one(
        """
        SELECT u.id, u.email, u.organization_id, NVL(u.is_trainee, 0) AS is_trainee
        FROM AIVA_users u
        WHERE u.id = :id
        """,
        {"id": user_id},
    )
    if not row:
        raise NotFoundError("User not found")
    if int(row["is_trainee"]) != 1:
        raise ConflictError("User is already a full agent")

    if not user.is_super_admin and int(row["organization_id"]) != user.organization_id:
        raise ForbiddenError("Cannot promote a user in another organization")

    on_account = await db.fetch_one(
        """
        SELECT 1
        FROM AIVA_account_users
        WHERE account_id = :account_id AND user_id = :user_id AND status = 'ACTIVE'
        """,
        {"account_id": account_id, "user_id": user_id},
    )
    if not on_account:
        raise NotFoundError("Trainee is not assigned to this account")

    has_agent_role = await db.fetch_one(
        """
        SELECT 1
        FROM AIVA_user_roles ur
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE ur.user_id = :user_id AND r.name = :agent_role
        """,
        {"user_id": user_id, "agent_role": ROLE_AGENT},
    )
    if not has_agent_role:
        raise NotFoundError("User is not an agent")

    return row


async def _list_agent_user_ids(db: DbDep, account_ids: set[int]) -> list[int]:
    if not account_ids:
        return []
    placeholders = ", ".join(f":acc_{i}" for i in range(len(account_ids)))
    params = {f"acc_{i}": account_id for i, account_id in enumerate(sorted(account_ids))}
    rows = await db.fetch_all(
        f"""
        SELECT DISTINCT u.id
        FROM AIVA_users u
        JOIN AIVA_account_users au ON au.user_id = u.id AND au.status = 'ACTIVE'
        WHERE au.account_id IN ({placeholders})
          AND EXISTS (
            SELECT 1
            FROM AIVA_user_roles ur
            JOIN AIVA_roles r ON r.id = ur.role_id
            WHERE ur.user_id = u.id AND r.name = :agent_role
          )
        ORDER BY u.id
        """,
        {**params, "agent_role": ROLE_AGENT},
    )
    return [int(r["id"]) for r in rows]


@router.get("", response_model=list[UserOut])
async def list_agents(
    user: Annotated[UserContext, Depends(_AGENT_ACCESS)],
    db: DbDep,
    account_id: int | None = Query(default=None),
) -> list[UserOut]:
    accessible = await _accessible_account_ids(db, user)
    if account_id is not None:
        account = await db.fetch_one(
            "SELECT organization_id FROM AIVA_accounts WHERE id = :id",
            {"id": account_id},
        )
        if not account:
            raise NotFoundError("Account not found")
        require_account_access(account_id, user, int(account["organization_id"]))
        target_accounts = {account_id}
    else:
        target_accounts = accessible

    user_ids = await _list_agent_user_ids(db, target_accounts)
    return [await build_user_out(db, user_id) for user_id in user_ids]


@router.post("/trainees", response_model=UserOut, status_code=201)
async def create_trainee(
    body: TraineeCreate,
    user: Annotated[UserContext, Depends(_AGENT_ACCESS)],
    db: DbDep,
) -> UserOut:
    account = await db.fetch_one(
        "SELECT id, organization_id FROM AIVA_accounts WHERE id = :id",
        {"id": body.account_id},
    )
    if not account:
        raise NotFoundError("Account not found")

    account_org = int(account["organization_id"])
    require_account_access(body.account_id, user, account_org)

    if not user.is_super_admin and account_org != user.organization_id:
        raise ForbiddenError("Cannot create trainee for an account in another organization")

    existing = await db.fetch_one(
        "SELECT id FROM AIVA_users WHERE LOWER(email) = LOWER(:email)",
        {"email": body.email},
    )
    if existing:
        raise ConflictError("Email already registered")

    role_id = await _resolve_agent_role_id(db)
    user_id = await db.execute(
        """
        INSERT INTO AIVA_users (
            organization_id, first_name, last_name, email, password_hash, status, is_trainee
        ) VALUES (
            :organization_id, :first_name, :last_name, :email, :password_hash, :status, :is_trainee
        )
        RETURNING id INTO :out_id
        """,
        {
            "organization_id": account_org,
            "first_name": body.first_name,
            "last_name": body.last_name,
            "email": body.email,
            "password_hash": hash_password(body.password),
            "status": body.status,
            "is_trainee": 1,
        },
        return_id=True,
    )
    await db.execute(
        """
        INSERT INTO AIVA_user_roles (user_id, role_id, account_id)
        VALUES (:user_id, :role_id, :account_id)
        """,
        {"user_id": user_id, "role_id": role_id, "account_id": body.account_id},
    )
    await db.execute(
        """
        INSERT INTO AIVA_account_users (account_id, user_id, assigned_by, status)
        VALUES (:account_id, :user_id, :assigned_by, 'ACTIVE')
        """,
        {"account_id": body.account_id, "user_id": user_id, "assigned_by": user.id},
    )
    await grant_account_role_access(db, int(user_id), body.account_id)

    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="user",
        entity_id=int(user_id or 0),
        action_type="CREATE_TRAINEE",
        new_value={"email": body.email, "account_id": body.account_id, "role": ROLE_AGENT},
    )
    return await build_user_out(db, int(user_id))


@router.post("/{user_id}/promote", response_model=UserOut)
async def promote_trainee_to_agent(
    user_id: int,
    user: Annotated[UserContext, Depends(_AGENT_ACCESS)],
    db: DbDep,
    account_id: int = Query(..., description="Account context for the promotion"),
) -> UserOut:
    trainee = await _require_promotable_trainee(db, user, user_id, account_id)

    await db.execute(
        "UPDATE AIVA_users SET is_trainee = 0 WHERE id = :id",
        {"id": user_id},
    )
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="user",
        entity_id=user_id,
        action_type="PROMOTE_TRAINEE",
        old_value={"is_trainee": True, "email": trainee["email"]},
        new_value={"is_trainee": False, "account_id": account_id, "role": ROLE_AGENT},
    )
    return await build_user_out(db, user_id)


@router.get("/queue-access/bulk", response_model=AgentQueueSummaryOut)
async def bulk_agent_queue_access(
    user: Annotated[UserContext, Depends(_AGENT_ACCESS)],
    db: DbDep,
    embedding_svc: EmbeddingServiceDep,
    account_id: int = Query(..., description="Account context"),
) -> AgentQueueSummaryOut:
    account = await db.fetch_one(
        "SELECT organization_id FROM AIVA_accounts WHERE id = :id",
        {"id": account_id},
    )
    if not account:
        raise NotFoundError("Account not found")
    require_account_access(account_id, user, int(account["organization_id"]))

    user_ids = await _list_agent_user_ids(db, {account_id})
    _corpus_id, corpus_config = await load_account_corpus_config(db, embedding_svc, account_id)
    assigned_by_user = await list_assigned_queue_keys_by_account(db, account_id=account_id)
    items = build_agent_queue_summaries(
        user_ids=user_ids,
        corpus_config=corpus_config,
        assigned_by_user=assigned_by_user,
    )
    return AgentQueueSummaryOut(
        account_id=account_id,
        agents=[
            {
                "user_id": item["user_id"],
                "queues": [QueueGroupOut(**q) for q in item["queues"]],
                "is_restricted": item["is_restricted"],
            }
            for item in items
        ],
    )


@router.get("/{user_id}/queue-access", response_model=AgentQueueAccessOut)
async def get_agent_queue_access(
    user_id: int,
    user: Annotated[UserContext, Depends(_AGENT_ACCESS)],
    db: DbDep,
    embedding_svc: EmbeddingServiceDep,
    account_id: int = Query(..., description="Account context"),
) -> AgentQueueAccessOut:
    await require_can_manage_agent_queues(db, user, account_id=account_id, target_user_id=user_id)

    on_account = await db.fetch_one(
        """
        SELECT 1 FROM AIVA_account_users
        WHERE account_id = :account_id AND user_id = :user_id AND status = 'ACTIVE'
        """,
        {"account_id": account_id, "user_id": user_id},
    )
    if not on_account:
        raise NotFoundError("Agent is not assigned to this account")

    _corpus_id, corpus_config = await load_account_corpus_config(db, embedding_svc, account_id)
    catalog = list_queue_catalog(corpus_config)
    assigned = await list_assigned_queue_keys(db, account_id=account_id, user_id=user_id)
    allowed = assigned if assigned else [item["key"] for item in catalog]

    return AgentQueueAccessOut(
        account_id=account_id,
        user_id=user_id,
        available_queues=[QueueGroupOut(**item) for item in catalog],
        assigned_queues=assigned,
        allowed_queues=allowed,
    )


@router.put("/{user_id}/queue-access", response_model=AgentQueueAccessOut)
async def update_agent_queue_access(
    user_id: int,
    body: AgentQueueAccessUpdate,
    user: Annotated[UserContext, Depends(_AGENT_ACCESS)],
    db: DbDep,
    embedding_svc: EmbeddingServiceDep,
    account_id: int = Query(..., description="Account context"),
) -> AgentQueueAccessOut:
    await require_can_manage_agent_queues(db, user, account_id=account_id, target_user_id=user_id)

    on_account = await db.fetch_one(
        """
        SELECT 1 FROM AIVA_account_users
        WHERE account_id = :account_id AND user_id = :user_id AND status = 'ACTIVE'
        """,
        {"account_id": account_id, "user_id": user_id},
    )
    if not on_account:
        raise NotFoundError("Agent is not assigned to this account")

    _corpus_id, corpus_config = await load_account_corpus_config(db, embedding_svc, account_id)
    assigned = await set_assigned_queue_keys(
        db,
        account_id=account_id,
        user_id=user_id,
        queue_keys=body.queue_keys,
        corpus_config=corpus_config,
        assigned_by=user.id,
    )
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="agent_queue_access",
        entity_id=user_id,
        action_type="UPDATE",
        old_value=None,
        new_value={"account_id": account_id, "queue_keys": assigned},
    )
    catalog = list_queue_catalog(corpus_config)
    return AgentQueueAccessOut(
        account_id=account_id,
        user_id=user_id,
        available_queues=[QueueGroupOut(**item) for item in catalog],
        assigned_queues=assigned,
        allowed_queues=assigned,
    )
