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
    require_roles,
    require_roles_or_nav_permission,
)
from backend.dependencies import DbDep
from backend.exceptions import ForbiddenError, NotFoundError
from backend.schemas.account_updates import (
    AccountUpdateCreate,
    AccountUpdateOut,
    AccountUpdateUpdate,
)
from backend.schemas.common import MessageResponse
from backend.services.audit import write_audit_log
from backend.utils import serialize_row

router = APIRouter(prefix="/account-updates", tags=["account-updates"])

_MANAGE_ROLES = (
    ROLE_SUPER_ADMIN,
    ROLE_ORG_ADMIN,
    ROLE_ACCOUNT_MANAGER,
    ROLE_SUPERVISOR,
)

_READ_ROLES = (
    ROLE_SUPER_ADMIN,
    ROLE_ORG_ADMIN,
    ROLE_ACCOUNT_MANAGER,
    ROLE_SUPERVISOR,
    ROLE_AGENT,
)

_SELECT = """
SELECT u.id, u.account_id, u.title, u.body, u.is_active, u.created_by,
       u.created_at, u.updated_at,
       a.name AS account_name, a.organization_id,
       o.name AS organization_name
FROM AIVA_account_updates u
JOIN AIVA_accounts a ON a.id = u.account_id
JOIN AIVA_organizations o ON o.id = a.organization_id
"""


def _to_out(row: dict | None) -> AccountUpdateOut:
    data = serialize_row(row) or {}
    data["is_active"] = bool(int(data.get("is_active") or 0))
    return AccountUpdateOut(**data)


async def _fetch_update(db: DbDep, update_id: int) -> dict | None:
    return await db.fetch_one(f"{_SELECT} WHERE u.id = :id", {"id": update_id})


@router.get("/active", response_model=list[AccountUpdateOut])
async def list_active_account_updates(
    user: Annotated[UserContext, Depends(require_roles(*_READ_ROLES))],
    db: DbDep,
    account_id: int | None = Query(default=None),
) -> list[AccountUpdateOut]:
    """Active updates for accounts the user can access (widget / agent consumption)."""
    params: dict = {}
    clauses = ["u.is_active = 1"]

    if account_id is not None:
        account = await db.fetch_one(
            "SELECT organization_id FROM AIVA_accounts WHERE id = :id",
            {"id": account_id},
        )
        if not account:
            raise NotFoundError("Account not found")
        require_account_access(account_id, user, int(account["organization_id"]))
        clauses.append("u.account_id = :account_id")
        params["account_id"] = account_id
    elif not user.is_super_admin:
        if user.is_org_admin:
            clauses.append("a.organization_id = :organization_id")
            params["organization_id"] = user.organization_id
        else:
            accessible = user.account_ids
            if not accessible:
                return []
            placeholders = ", ".join(f":acc_{i}" for i in range(len(accessible)))
            clauses.append(f"u.account_id IN ({placeholders})")
            for i, aid in enumerate(sorted(accessible)):
                params[f"acc_{i}"] = aid

    where = f"WHERE {' AND '.join(clauses)}"
    rows = await db.fetch_all(
        f"{_SELECT} {where} ORDER BY u.created_at DESC",
        params,
    )
    return [_to_out(r) for r in rows]


@router.get("", response_model=list[AccountUpdateOut])
async def list_account_updates(
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("account-updates", *_MANAGE_ROLES))],
    db: DbDep,
    organization_id: int | None = Query(default=None),
    account_id: int | None = Query(default=None),
    active_only: bool | None = Query(default=None),
) -> list[AccountUpdateOut]:
    params: dict = {}
    clauses: list[str] = []

    if user.is_super_admin:
        if organization_id:
            clauses.append("a.organization_id = :organization_id")
            params["organization_id"] = organization_id
    else:
        clauses.append("a.organization_id = :organization_id")
        params["organization_id"] = user.organization_id

    if account_id:
        account = await db.fetch_one(
            "SELECT organization_id FROM AIVA_accounts WHERE id = :id",
            {"id": account_id},
        )
        if account:
            require_account_access(account_id, user, int(account["organization_id"]))
        clauses.append("u.account_id = :account_id")
        params["account_id"] = account_id
    elif not user.is_super_admin and not user.is_org_admin:
        accessible = user.account_ids
        if not accessible:
            return []
        placeholders = ", ".join(f":acc_{i}" for i in range(len(accessible)))
        clauses.append(f"u.account_id IN ({placeholders})")
        for i, aid in enumerate(sorted(accessible)):
            params[f"acc_{i}"] = aid

    if active_only is True:
        clauses.append("u.is_active = 1")
    elif active_only is False:
        clauses.append("u.is_active = 0")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await db.fetch_all(
        f"{_SELECT} {where} ORDER BY u.created_at DESC",
        params,
    )
    return [_to_out(r) for r in rows]


@router.post("", response_model=AccountUpdateOut, status_code=201)
async def create_account_update(
    body: AccountUpdateCreate,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("account-updates", *_MANAGE_ROLES))],
    db: DbDep,
) -> AccountUpdateOut:
    account = await db.fetch_one(
        "SELECT organization_id FROM AIVA_accounts WHERE id = :id",
        {"id": body.account_id},
    )
    if not account:
        raise NotFoundError("Account not found")
    require_account_access(body.account_id, user, int(account["organization_id"]))

    update_id = await db.execute(
        """
        INSERT INTO AIVA_account_updates (
            account_id, title, body, is_active, created_by
        ) VALUES (
            :account_id, :title, :body, :is_active, :created_by
        )
        RETURNING id INTO :out_id
        """,
        {
            "account_id": body.account_id,
            "title": body.title,
            "body": body.body,
            "is_active": 1 if body.is_active else 0,
            "created_by": user.id,
        },
        return_id=True,
    )
    row = await _fetch_update(db, int(update_id or 0))
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="account_update",
        entity_id=int(update_id or 0),
        action_type="CREATE",
        new_value={"account_id": body.account_id, "title": body.title},
    )
    return _to_out(row)


@router.patch("/{update_id}", response_model=AccountUpdateOut)
async def update_account_update(
    update_id: int,
    body: AccountUpdateUpdate,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("account-updates", *_MANAGE_ROLES))],
    db: DbDep,
) -> AccountUpdateOut:
    old = await _fetch_update(db, update_id)
    if not old:
        raise NotFoundError("Account update not found")
    if not user.is_super_admin and int(old["organization_id"]) != user.organization_id:
        raise ForbiddenError("Cannot update account update in another organization")
    require_account_access(int(old["account_id"]), user, int(old["organization_id"]))

    updates = body.model_dump(exclude_unset=True)
    if "is_active" in updates:
        updates["is_active"] = 1 if updates["is_active"] else 0
    if updates:
        updates["id"] = update_id
        set_parts = [f"{k} = :{k}" for k in updates if k != "id"]
        set_parts.append("updated_at = SYSTIMESTAMP")
        await db.execute(
            f"UPDATE AIVA_account_updates SET {', '.join(set_parts)} WHERE id = :id",
            updates,
        )
    row = await _fetch_update(db, update_id)
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="account_update",
        entity_id=update_id,
        action_type="UPDATE",
        old_value=serialize_row(old),
        new_value=body.model_dump(exclude_unset=True),
    )
    return _to_out(row)


@router.delete("/{update_id}", response_model=MessageResponse)
async def delete_account_update(
    update_id: int,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("account-updates", *_MANAGE_ROLES))],
    db: DbDep,
) -> MessageResponse:
    old = await _fetch_update(db, update_id)
    if not old:
        raise NotFoundError("Account update not found")
    if not user.is_super_admin and int(old["organization_id"]) != user.organization_id:
        raise ForbiddenError("Cannot delete account update in another organization")
    require_account_access(int(old["account_id"]), user, int(old["organization_id"]))
    await db.execute("DELETE FROM AIVA_account_updates WHERE id = :id", {"id": update_id})
    return MessageResponse(message="Account update deleted")
