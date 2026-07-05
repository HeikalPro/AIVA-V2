from typing import Annotated

import oracledb
from fastapi import APIRouter, Depends, Query

from backend.auth.deps import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    UserContext,
    require_account_access,
    require_roles,
    require_roles_or_nav_permission,
)
from backend.auth.hashing import hash_password
from backend.database import Database
from backend.dependencies import DbDep
from backend.exceptions import ConflictError, ForbiddenError, NotFoundError
from backend.schemas.common import MessageResponse
from backend.schemas.users import (
    AccountUserAssign,
    UserCreate,
    UserNavPermissionsUpdate,
    UserOut,
    UserRoleAssign,
    UserUpdate,
)
from backend.services.account_membership import (
    clear_account_access_outside_organization,
    get_user_and_account,
    grant_account_role_access,
    prepare_user_for_account_assignment,
    remove_account_membership,
    upsert_account_membership,
)
from backend.services.audit import write_audit_log
from backend.services.organization_dependencies import delete_user_dependencies
from backend.services.user_queries import build_user_out
from backend.services.role_nav_permissions import (
    _resolve_role_nav_permissions,
    set_user_extra_nav_permissions,
)
from backend.utils import serialize_row

router = APIRouter(prefix="/users", tags=["users"])


async def _user_out(db: Database, user_id: int) -> UserOut:
    return await build_user_out(db, user_id)


async def _replace_user_role(
    db: Database,
    user_id: int,
    role_id: int,
    *,
    conn: oracledb.AsyncConnection | None = None,
) -> None:
    role_row = await db.fetch_one("SELECT id, name FROM AIVA_roles WHERE id = :id", {"id": role_id}, conn=conn)
    if not role_row:
        raise NotFoundError("Role not found")

    await db.execute("DELETE FROM AIVA_user_roles WHERE user_id = :user_id", {"user_id": user_id}, conn=conn)
    await db.execute(
        """
        INSERT INTO AIVA_user_roles (user_id, role_id, account_id)
        VALUES (:user_id, :role_id, NULL)
        """,
        {"user_id": user_id, "role_id": role_id},
        conn=conn,
    )


@router.get("", response_model=list[UserOut])
async def list_users(
    user: Annotated[
        UserContext,
        Depends(require_roles_or_nav_permission("users", ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)),
    ],
    db: DbDep,
    organization_id: int | None = Query(default=None),
) -> list[UserOut]:
    if user.is_super_admin:
        if organization_id:
            rows = await db.fetch_all(
                "SELECT id FROM AIVA_users WHERE organization_id = :org_id ORDER BY id",
                {"org_id": organization_id},
            )
        else:
            rows = await db.fetch_all("SELECT id FROM AIVA_users ORDER BY id")
    else:
        rows = await db.fetch_all(
            "SELECT id FROM AIVA_users WHERE organization_id = :org_id ORDER BY id",
            {"org_id": user.organization_id},
        )
    return [await _user_out(db, int(r["id"])) for r in rows]


@router.post("", response_model=UserOut, status_code=201)
async def create_user(
    body: UserCreate,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN))],
    db: DbDep,
) -> UserOut:
    if not user.is_super_admin and body.organization_id != user.organization_id:
        raise ForbiddenError("Cannot create user in another organization")

    existing = await db.fetch_one(
        "SELECT id FROM AIVA_users WHERE LOWER(email) = LOWER(:email)",
        {"email": body.email},
    )
    if existing:
        raise ConflictError("Email already registered")

    if body.account_id:
        account = await db.fetch_one(
            "SELECT organization_id FROM AIVA_accounts WHERE id = :id",
            {"id": body.account_id},
        )
        if not account:
            raise NotFoundError("Account not found")
        if int(account["organization_id"]) != body.organization_id:
            raise ForbiddenError("Account must belong to the same organization as the user")

    user_id = await db.execute(
        """
        INSERT INTO AIVA_users (
            organization_id, first_name, last_name, email, password_hash, status
        ) VALUES (
            :organization_id, :first_name, :last_name, :email, :password_hash, :status
        )
        RETURNING id INTO :out_id
        """,
        {
            "organization_id": body.organization_id,
            "first_name": body.first_name,
            "last_name": body.last_name,
            "email": body.email,
            "password_hash": hash_password(body.password),
            "status": body.status,
        },
        return_id=True,
    )
    await db.execute(
        """
        INSERT INTO AIVA_user_roles (user_id, role_id, account_id)
        VALUES (:user_id, :role_id, :account_id)
        """,
        {"user_id": user_id, "role_id": body.role_id, "account_id": body.account_id},
    )
    if body.account_id:
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
        action_type="CREATE",
        new_value={"email": body.email, "role_id": body.role_id},
    )
    return await _user_out(db, int(user_id))


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: int,
    current: Annotated[
        UserContext,
        Depends(require_roles_or_nav_permission("users", ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)),
    ],
    db: DbDep,
) -> UserOut:
    row = await db.fetch_one("SELECT * FROM AIVA_users WHERE id = :id", {"id": user_id})
    if not row:
        raise NotFoundError("User not found")
    if not current.is_super_admin and int(row["organization_id"]) != current.organization_id:
        raise ForbiddenError("Cannot view user in another organization")
    return await _user_out(db, user_id)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    body: UserUpdate,
    current: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN))],
    db: DbDep,
) -> UserOut:
    old = await db.fetch_one("SELECT * FROM AIVA_users WHERE id = :id", {"id": user_id})
    if not old:
        raise NotFoundError("User not found")
    if not current.is_super_admin and int(old["organization_id"]) != current.organization_id:
        raise ForbiddenError("Cannot update user in another organization")

    updates = body.model_dump(exclude_unset=True)
    updates.pop("role_id", None)

    new_org_id = updates.get("organization_id")
    if new_org_id is not None:
        if not current.is_super_admin:
            raise ForbiddenError("Only super admin can change user organization")
        org_row = await db.fetch_one(
            "SELECT id FROM AIVA_organizations WHERE id = :id",
            {"id": new_org_id},
        )
        if not org_row:
            raise NotFoundError("Organization not found")
        if int(new_org_id) != int(old["organization_id"]):
            await clear_account_access_outside_organization(db, user_id, int(new_org_id))

    if "password" in updates:
        updates["password_hash"] = hash_password(updates.pop("password"))

    audit_new_value = dict(updates)
    if updates:
        updates["id"] = user_id
        set_parts = [f"{k} = :{k}" for k in updates if k != "id"]
        await db.execute(
            f"UPDATE AIVA_users SET {', '.join(set_parts)} WHERE id = :id",
            updates,
        )
    await write_audit_log(
        db,
        user_id=current.id,
        entity_type="user",
        entity_id=user_id,
        action_type="UPDATE",
        old_value=serialize_row(old),
        new_value=audit_new_value,
    )
    return await _user_out(db, user_id)


@router.put("/{user_id}/role", response_model=UserOut)
async def set_user_role(
    user_id: int,
    body: UserRoleAssign,
    current: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> UserOut:
    target = await db.fetch_one("SELECT id FROM AIVA_users WHERE id = :id", {"id": user_id})
    if not target:
        raise NotFoundError("User not found")
    if user_id == current.id:
        role_row = await db.fetch_one("SELECT name FROM AIVA_roles WHERE id = :id", {"id": body.role_id})
        if not role_row or role_row.get("name") != ROLE_SUPER_ADMIN:
            raise ForbiddenError("Cannot remove your own super admin role")

    async with db.connection() as conn:
        await _replace_user_role(db, user_id, body.role_id, conn=conn)

    await write_audit_log(
        db,
        user_id=current.id,
        entity_type="user",
        entity_id=user_id,
        action_type="UPDATE_ROLE",
        new_value={"role_id": body.role_id},
    )
    return await _user_out(db, user_id)


@router.put("/{user_id}/nav-permissions", response_model=UserOut)
async def set_user_nav_permissions(
    user_id: int,
    body: UserNavPermissionsUpdate,
    current: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN))],
    db: DbDep,
) -> UserOut:
    target = await db.fetch_one(
        "SELECT id, organization_id FROM AIVA_users WHERE id = :id",
        {"id": user_id},
    )
    if not target:
        raise NotFoundError("User not found")
    if not current.is_super_admin and int(target["organization_id"]) != current.organization_id:
        raise ForbiddenError("Cannot change page access for users in another organization")
    if ROLE_SUPER_ADMIN in {
        str(r["name"])
        for r in await db.fetch_all(
            """
            SELECT r.name FROM AIVA_user_roles ur
            JOIN AIVA_roles r ON r.id = ur.role_id
            WHERE ur.user_id = :user_id
            """,
            {"user_id": user_id},
        )
    }:
        raise ForbiddenError("Super Admin page access cannot be customized")

    role_rows = await db.fetch_all(
        """
        SELECT ur.role_id, r.name AS role_name
        FROM AIVA_user_roles ur
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE ur.user_id = :user_id
        """,
        {"user_id": user_id},
    )
    role_ids = list({int(r["role_id"]) for r in role_rows})
    role_names = {str(r["role_name"]) for r in role_rows}
    role_perms = set(
        await _resolve_role_nav_permissions(db, role_ids=role_ids, role_names=role_names)
    )
    extra_only = [k for k in body.extra_nav_permissions if k not in role_perms]

    saved = await set_user_extra_nav_permissions(
        db,
        user_id,
        extra_only,
        allow_restricted=current.is_super_admin,
    )
    await write_audit_log(
        db,
        user_id=current.id,
        entity_type="user",
        entity_id=user_id,
        action_type="UPDATE_NAV_PERMISSIONS",
        new_value={"extra_nav_permissions": saved},
    )
    return await _user_out(db, user_id)


@router.delete("/{user_id}", response_model=MessageResponse)
async def delete_user(
    user_id: int,
    current: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN))],
    db: DbDep,
) -> MessageResponse:
    if user_id == current.id:
        raise ForbiddenError("You cannot delete your own account")
    old = await db.fetch_one("SELECT * FROM AIVA_users WHERE id = :id", {"id": user_id})
    if not old:
        raise NotFoundError("User not found")
    if not current.is_super_admin and int(old["organization_id"]) != current.organization_id:
        raise ForbiddenError("Cannot delete user in another organization")
    await delete_user_dependencies(db, user_id)
    await db.execute("DELETE FROM AIVA_users WHERE id = :id", {"id": user_id})
    await write_audit_log(
        db,
        user_id=current.id,
        entity_type="user",
        entity_id=user_id,
        action_type="DELETE",
        old_value=serialize_row(old),
    )
    return MessageResponse(message="User deleted")


@router.post("/{user_id}/roles", response_model=UserOut)
async def assign_role(
    user_id: int,
    body: UserRoleAssign,
    current: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN))],
    db: DbDep,
) -> UserOut:
    row = await db.fetch_one("SELECT organization_id FROM AIVA_users WHERE id = :id", {"id": user_id})
    if not row:
        raise NotFoundError("User not found")
    if not current.is_super_admin and int(row["organization_id"]) != current.organization_id:
        raise ForbiddenError("Cannot assign role in another organization")

    await db.execute(
        """
        INSERT INTO AIVA_user_roles (user_id, role_id, account_id)
        VALUES (:user_id, :role_id, :account_id)
        """,
        {"user_id": user_id, "role_id": body.role_id, "account_id": body.account_id},
    )
    return await _user_out(db, user_id)


@router.post("/{user_id}/accounts", response_model=UserOut)
async def assign_account(
    user_id: int,
    body: AccountUserAssign,
    current: Annotated[
        UserContext,
        Depends(require_roles_or_nav_permission("users", ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)),
    ],
    db: DbDep,
) -> UserOut:
    user_row, account = await prepare_user_for_account_assignment(
        db,
        user_id,
        body.account_id,
        allow_org_move=current.is_super_admin,
    )
    if not current.is_super_admin and int(user_row["organization_id"]) != current.organization_id:
        raise ForbiddenError("Cannot assign account in another organization")
    require_account_access(body.account_id, current, int(account["organization_id"]))

    await upsert_account_membership(
        db,
        user_id=user_id,
        account_id=body.account_id,
        assigned_by=current.id,
        status=body.status,
    )
    await grant_account_role_access(db, user_id, body.account_id)
    await write_audit_log(
        db,
        user_id=current.id,
        entity_type="account_user",
        entity_id=user_id,
        action_type="ASSIGN",
        new_value={"account_id": body.account_id, "user_id": user_id},
    )
    return await _user_out(db, user_id)


@router.delete("/{user_id}/accounts/{account_id}", response_model=UserOut)
async def unassign_account(
    user_id: int,
    account_id: int,
    current: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN))],
    db: DbDep,
) -> UserOut:
    user_row, account = await get_user_and_account(db, user_id, account_id)
    if not current.is_super_admin:
        if int(user_row["organization_id"]) != current.organization_id:
            raise ForbiddenError("Cannot remove account assignment in another organization")
        if int(account["organization_id"]) != current.organization_id:
            raise ForbiddenError("Cannot remove account assignment in another organization")
    require_account_access(account_id, current, int(account["organization_id"]))

    await remove_account_membership(db, user_id, account_id)
    await write_audit_log(
        db,
        user_id=current.id,
        entity_type="account_user",
        entity_id=user_id,
        action_type="UNASSIGN",
        old_value={"account_id": account_id, "user_id": user_id},
    )
    return await _user_out(db, user_id)
