from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.auth.deps import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_AGENT,
    ROLE_DEVELOPER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_SUPERVISOR,
    UserContext,
    require_account_access,
    require_roles,
    require_roles_or_any_nav_permission,
    require_roles_or_nav_permission,
)
from backend.dependencies import DbDep
from backend.exceptions import ForbiddenError, NotFoundError
from backend.schemas.accounts import AccountCreate, AccountOut, AccountUpdate
from backend.schemas.common import MessageResponse
from backend.schemas.users import UserOut
from backend.services.account_dependencies import delete_account_dependencies
from backend.services.audit import write_audit_log
from backend.services.role_nav_permissions import seed_account_role_nav_permissions
from backend.services.user_queries import build_user_out
from backend.services.widget_features import (
    dumps_widget_features,
    parse_widget_features,
    widget_features_out,
)
from backend.utils import serialize_row

router = APIRouter(prefix="/accounts", tags=["accounts"])

_ACCOUNT_LIST_ROLES = (
    ROLE_SUPER_ADMIN,
    ROLE_ORG_ADMIN,
    ROLE_ACCOUNT_MANAGER,
    ROLE_AGENT,
    ROLE_SUPERVISOR,
    ROLE_DEVELOPER,
)
_ACCOUNT_LIST_NAV = (
    "accounts",
    "prompts",
    "account-updates",
    "tickets",
    "ingestion",
    "dashboard",
    "users",
    "agents",
    "chat",
)

_ACCOUNT_WITH_ORG_SELECT = """
SELECT a.id, a.organization_id, a.llm_config_id, a.name, a.description, a.corpus_id, a.status,
       a.kb_source_base_url, a.widget_features, a.created_at,
       o.name AS organization_name, o.code AS organization_code
FROM AIVA_accounts a
JOIN AIVA_organizations o ON o.id = a.organization_id
"""


def _to_account_out(row: dict | None) -> AccountOut:
    data = serialize_row(row) or {}
    features = parse_widget_features(data.get("widget_features"))
    data["widget_features"] = widget_features_out(features)
    return AccountOut(**data)


async def _fetch_account_by_id(db: DbDep, account_id: int) -> dict | None:
    return await db.fetch_one(
        f"{_ACCOUNT_WITH_ORG_SELECT} WHERE a.id = :id",
        {"id": account_id},
    )


def _scoped_org_filter(user: UserContext, organization_id: int | None) -> int | None:
    if user.is_super_admin:
        return organization_id
    return user.organization_id


@router.get("", response_model=list[AccountOut])
async def list_accounts(
    user: Annotated[
        UserContext,
        Depends(require_roles_or_any_nav_permission(_ACCOUNT_LIST_NAV, *_ACCOUNT_LIST_ROLES)),
    ],
    db: DbDep,
    organization_id: int | None = Query(default=None),
) -> list[AccountOut]:
    org = _scoped_org_filter(user, organization_id)
    if user.is_super_admin:
        if org is None:
            rows = await db.fetch_all(f"{_ACCOUNT_WITH_ORG_SELECT} ORDER BY a.id")
        else:
            rows = await db.fetch_all(
                f"{_ACCOUNT_WITH_ORG_SELECT} WHERE a.organization_id = :org_id ORDER BY a.id",
                {"org_id": org},
            )
    elif user.is_org_admin:
        rows = await db.fetch_all(
            f"{_ACCOUNT_WITH_ORG_SELECT} WHERE a.organization_id = :org_id ORDER BY a.id",
            {"org_id": org},
        )
    else:
        rows = await db.fetch_all(
            f"""
            {_ACCOUNT_WITH_ORG_SELECT}
            JOIN AIVA_account_users au ON au.account_id = a.id
            WHERE au.user_id = :user_id AND au.status = 'ACTIVE'
            ORDER BY a.id
            """,
            {"user_id": user.id},
        )
    return [_to_account_out(r) for r in rows]


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(
    body: AccountCreate,
    user: Annotated[
        UserContext,
        Depends(require_roles_or_nav_permission("accounts", ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN)),
    ],
    db: DbDep,
) -> AccountOut:
    if not user.is_super_admin and body.organization_id != user.organization_id:
        raise ForbiddenError("Cannot create account in another organization")

    account_id = await db.execute(
        """
        INSERT INTO AIVA_accounts (
            organization_id, llm_config_id, name, description, corpus_id, status,
            kb_source_base_url, widget_features
        ) VALUES (
            :organization_id, :llm_config_id, :name, :description, :corpus_id, :status,
            :kb_source_base_url, :widget_features
        )
        RETURNING id INTO :out_id
        """,
        {
            **body.model_dump(exclude={"widget_features"}),
            "widget_features": dumps_widget_features(body.widget_features),
        },
        return_id=True,
    )
    row = await _fetch_account_by_id(db, int(account_id or 0))
    await seed_account_role_nav_permissions(db, int(account_id or 0))
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="account",
        entity_id=int(account_id or 0),
        action_type="CREATE",
        new_value=body.model_dump(),
    )
    return _to_account_out(row)


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: int,
    user: Annotated[
        UserContext,
        Depends(require_roles_or_nav_permission("accounts", ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER, ROLE_SUPERVISOR, ROLE_DEVELOPER)),
    ],
    db: DbDep,
) -> AccountOut:
    row = await _fetch_account_by_id(db, account_id)
    if not row:
        raise NotFoundError("Account not found")
    require_account_access(account_id, user, int(row["organization_id"]))
    return _to_account_out(row)


@router.get("/{account_id}/users", response_model=list[UserOut])
async def list_account_users(
    account_id: int,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("users", ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN))],
    db: DbDep,
) -> list[UserOut]:
    row = await db.fetch_one("SELECT organization_id FROM AIVA_accounts WHERE id = :id", {"id": account_id})
    if not row:
        raise NotFoundError("Account not found")
    if not user.is_super_admin and int(row["organization_id"]) != user.organization_id:
        raise ForbiddenError("Cannot view users in another organization")

    members = await db.fetch_all(
        """
        SELECT u.id
        FROM AIVA_users u
        JOIN AIVA_account_users au ON au.user_id = u.id
        WHERE au.account_id = :account_id AND au.status = 'ACTIVE'
        ORDER BY u.id
        """,
        {"account_id": account_id},
    )
    return [await build_user_out(db, int(member["id"])) for member in members]


@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: int,
    body: AccountUpdate,
    user: Annotated[
        UserContext,
        Depends(
            require_roles_or_nav_permission(
                "accounts",
                ROLE_SUPER_ADMIN,
                ROLE_ORG_ADMIN,
                ROLE_ACCOUNT_MANAGER,
                ROLE_DEVELOPER,
            )
        ),
    ],
    db: DbDep,
) -> AccountOut:
    old = await db.fetch_one("SELECT * FROM AIVA_accounts WHERE id = :id", {"id": account_id})
    if not old:
        raise NotFoundError("Account not found")
    require_account_access(account_id, user, int(old["organization_id"]))

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        row = await _fetch_account_by_id(db, account_id)
        return _to_account_out(row)

    widget_features = updates.pop("widget_features", None)
    if widget_features is not None:
        updates["widget_features"] = dumps_widget_features(widget_features)

    updates["id"] = account_id
    set_parts = [f"{k} = :{k}" for k in updates if k != "id"]
    await db.execute(
        f"UPDATE AIVA_accounts SET {', '.join(set_parts)} WHERE id = :id",
        updates,
    )
    row = await _fetch_account_by_id(db, account_id)
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="account",
        entity_id=account_id,
        action_type="UPDATE",
        old_value=serialize_row(old),
        new_value=updates,
    )
    return _to_account_out(row)


@router.delete("/{account_id}", response_model=MessageResponse)
async def delete_account(
    account_id: int,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("accounts", ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN))],
    db: DbDep,
) -> MessageResponse:
    old = await db.fetch_one("SELECT * FROM AIVA_accounts WHERE id = :id", {"id": account_id})
    if not old:
        raise NotFoundError("Account not found")
    if not user.is_super_admin and int(old["organization_id"]) != user.organization_id:
        raise ForbiddenError("Cannot delete account in another organization")

    await delete_account_dependencies(db, account_id)
    await db.execute("DELETE FROM AIVA_accounts WHERE id = :id", {"id": account_id})
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="account",
        entity_id=account_id,
        action_type="DELETE",
        old_value=serialize_row(old),
    )
    return MessageResponse(message="Account deleted")
