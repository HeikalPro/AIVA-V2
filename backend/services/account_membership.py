from __future__ import annotations

from backend.auth.deps import ROLE_ORG_ADMIN, ROLE_SUPER_ADMIN
from backend.database import Database
from backend.exceptions import ForbiddenError, NotFoundError

ORG_WIDE_ROLES = {ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN}


async def get_user_and_account(db: Database, user_id: int, account_id: int) -> tuple[dict, dict]:
    user = await db.fetch_one(
        "SELECT id, organization_id FROM AIVA_users WHERE id = :id",
        {"id": user_id},
    )
    if not user:
        raise NotFoundError("User not found")

    account = await db.fetch_one(
        "SELECT id, organization_id FROM AIVA_accounts WHERE id = :id",
        {"id": account_id},
    )
    if not account:
        raise NotFoundError("Account not found")

    return user, account


async def align_user_organization(db: Database, user_id: int, organization_id: int) -> None:
    await db.execute(
        "UPDATE AIVA_users SET organization_id = :organization_id WHERE id = :id",
        {"id": user_id, "organization_id": organization_id},
    )


async def clear_account_access_outside_organization(
    db: Database,
    user_id: int,
    organization_id: int,
) -> None:
    """Deactivate memberships and scoped roles for accounts outside the user's organization."""
    foreign_accounts = await db.fetch_all(
        """
        SELECT au.account_id
        FROM AIVA_account_users au
        JOIN AIVA_accounts a ON a.id = au.account_id
        WHERE au.user_id = :user_id
          AND au.status = 'ACTIVE'
          AND a.organization_id != :organization_id
        """,
        {"user_id": user_id, "organization_id": organization_id},
    )
    for row in foreign_accounts:
        account_id = int(row["account_id"])
        await remove_account_membership(db, user_id, account_id)


async def user_has_org_wide_role(db: Database, user_id: int) -> bool:
    row = await db.fetch_one(
        """
        SELECT 1
        FROM AIVA_user_roles ur
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE ur.user_id = :user_id AND r.name IN (:super_admin, :org_admin)
        """,
        {
            "user_id": user_id,
            "super_admin": ROLE_SUPER_ADMIN,
            "org_admin": ROLE_ORG_ADMIN,
        },
    )
    return row is not None


async def prepare_user_for_account_assignment(
    db: Database,
    user_id: int,
    account_id: int,
    *,
    allow_org_move: bool,
) -> tuple[dict, dict]:
    user, account = await get_user_and_account(db, user_id, account_id)
    user_org = int(user["organization_id"])
    account_org = int(account["organization_id"])
    if user_org == account_org:
        return user, account

    if await user_has_org_wide_role(db, user_id):
        return user, account

    if allow_org_move:
        await align_user_organization(db, user_id, account_org)
        user["organization_id"] = account_org
        return user, account

    raise ForbiddenError("User must belong to the same organization as the account")


async def upsert_account_membership(
    db: Database,
    *,
    user_id: int,
    account_id: int,
    assigned_by: int,
    status: str = "ACTIVE",
) -> None:
    existing = await db.fetch_one(
        """
        SELECT status FROM AIVA_account_users
        WHERE account_id = :account_id AND user_id = :user_id
        """,
        {"account_id": account_id, "user_id": user_id},
    )
    if existing:
        await db.execute(
            """
            UPDATE AIVA_account_users
            SET status = :status, assigned_by = :assigned_by
            WHERE account_id = :account_id AND user_id = :user_id
            """,
            {
                "account_id": account_id,
                "user_id": user_id,
                "assigned_by": assigned_by,
                "status": status,
            },
        )
        return

    await db.execute(
        """
        INSERT INTO AIVA_account_users (account_id, user_id, assigned_by, status)
        VALUES (:account_id, :user_id, :assigned_by, :status)
        """,
        {
            "account_id": account_id,
            "user_id": user_id,
            "assigned_by": assigned_by,
            "status": status,
        },
    )


async def grant_account_role_access(db: Database, user_id: int, account_id: int) -> None:
    role_rows = await db.fetch_all(
        """
        SELECT ur.role_id, r.name AS role_name
        FROM AIVA_user_roles ur
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE ur.user_id = :user_id
        """,
        {"user_id": user_id},
    )
    scoped_role_ids = {
        int(row["role_id"])
        for row in role_rows
        if str(row["role_name"]) not in ORG_WIDE_ROLES
    }
    if not scoped_role_ids:
        return

    for role_id in scoped_role_ids:
        existing = await db.fetch_one(
            """
            SELECT 1 FROM AIVA_user_roles
            WHERE user_id = :user_id AND role_id = :role_id AND account_id = :account_id
            """,
            {"user_id": user_id, "role_id": role_id, "account_id": account_id},
        )
        if existing:
            continue
        await db.execute(
            """
            INSERT INTO AIVA_user_roles (user_id, role_id, account_id)
            VALUES (:user_id, :role_id, :account_id)
            """,
            {"user_id": user_id, "role_id": role_id, "account_id": account_id},
        )


async def revoke_account_role_access(db: Database, user_id: int, account_id: int) -> None:
    await db.execute(
        """
        DELETE FROM AIVA_user_roles
        WHERE user_id = :user_id AND account_id = :account_id
        """,
        {"user_id": user_id, "account_id": account_id},
    )


async def remove_account_membership(db: Database, user_id: int, account_id: int) -> None:
    membership = await db.fetch_one(
        """
        SELECT status FROM AIVA_account_users
        WHERE account_id = :account_id AND user_id = :user_id
        """,
        {"account_id": account_id, "user_id": user_id},
    )
    if not membership or str(membership.get("status")) != "ACTIVE":
        raise NotFoundError("User is not assigned to this account")

    await db.execute(
        """
        UPDATE AIVA_account_users
        SET status = 'INACTIVE'
        WHERE account_id = :account_id AND user_id = :user_id
        """,
        {"account_id": account_id, "user_id": user_id},
    )
    await revoke_account_role_access(db, user_id, account_id)
