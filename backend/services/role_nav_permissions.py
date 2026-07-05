"""Nav page permissions per role (sidebar / admin UI access)."""

from __future__ import annotations

from backend.auth.role_constants import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_AGENT,
    ROLE_DEVELOPER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_SUPERVISOR,
)
from backend.database import Database

# Stable keys aligned with AIVA-V2-UI NAV_ITEMS.permission
NAV_PERMISSION_CATALOG: list[dict[str, str]] = [
    {"key": "dashboard", "label": "Dashboard"},
    {"key": "organizations", "label": "Organizations"},
    {"key": "accounts", "label": "Accounts"},
    {"key": "users", "label": "Users"},
    {"key": "agents", "label": "Agents & trainees"},
    {"key": "roles", "label": "Roles & access"},
    {"key": "prompts", "label": "Prompts"},
    {"key": "llm-configs", "label": "LLM Configs"},
    {"key": "message-ratings", "label": "Message feedback"},
    {"key": "account-updates", "label": "Updates"},
    {"key": "chat", "label": "Chat"},
    {"key": "tickets", "label": "Tickets"},
    {"key": "ingestion", "label": "Ingestion"},
]

ALL_NAV_KEYS: frozenset[str] = frozenset(item["key"] for item in NAV_PERMISSION_CATALOG)

# Retired nav keys kept out of UI/auth even if old DB rows still reference them.
DEPRECATED_NAV_KEYS: frozenset[str] = frozenset({"http-logs"})


def _active_nav_keys(keys: list[str]) -> list[str]:
    return sorted(k for k in keys if k in ALL_NAV_KEYS and k not in DEPRECATED_NAV_KEYS)

# Org admins may grant extra pages to individual users, but not these sensitive areas.
ORG_ADMIN_RESTRICTED_NAV_KEYS: frozenset[str] = frozenset(
    {"organizations", "roles", "message-ratings", "llm-configs"}
)

# Mirrors AIVA-V2-UI/src/lib/roles.ts defaults (used when DB has no overrides).
DEFAULT_ROLE_NAV_PERMISSIONS: dict[str, list[str]] = {
    ROLE_SUPER_ADMIN: sorted(ALL_NAV_KEYS),
    ROLE_ORG_ADMIN: [
        "dashboard",
        "accounts",
        "users",
        "agents",
        "prompts",
        "account-updates",
        "tickets",
        "ingestion",
    ],
    ROLE_ACCOUNT_MANAGER: [
        "dashboard",
        "accounts",
        "users",
        "agents",
        "prompts",
        "account-updates",
        "tickets",
        "ingestion",
    ],
    ROLE_SUPERVISOR: ["dashboard", "agents", "account-updates", "chat", "tickets", "ingestion"],
    ROLE_AGENT: ["chat"],
    ROLE_DEVELOPER: [
        "dashboard",
        "prompts",
        "llm-configs",
        "tickets",
        "ingestion",
    ],
}


async def _table_exists(db: Database, table: str) -> bool:
    row = await db.fetch_one(
        "SELECT 1 FROM user_tables WHERE table_name = :table_name",
        {"table_name": table.upper()},
    )
    return row is not None


async def ensure_role_nav_permissions_schema(db: Database) -> None:
    created = not await _table_exists(db, "AIVA_ROLE_NAV_PERMISSIONS")
    if created:
        await db.execute(
            """
            CREATE TABLE AIVA_role_nav_permissions (
                role_id   NUMBER NOT NULL,
                nav_key   VARCHAR2(64) NOT NULL,
                PRIMARY KEY (role_id, nav_key),
                CONSTRAINT fk_aiva_role_nav_role
                    FOREIGN KEY (role_id) REFERENCES AIVA_roles(id) ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            "CREATE INDEX idx_aiva_role_nav_role ON AIVA_role_nav_permissions (role_id)"
        )

    await _seed_default_role_nav_permissions(db)
    await _ensure_agents_nav_permission(db)
    await ensure_account_role_nav_permissions_schema(db)
    await ensure_user_nav_permissions_schema(db)


async def ensure_account_role_nav_permissions_schema(db: Database) -> None:
    if not await _table_exists(db, "AIVA_ACCOUNT_ROLE_NAV_PERMISSIONS"):
        await db.execute(
            """
            CREATE TABLE AIVA_account_role_nav_permissions (
                account_id NUMBER NOT NULL,
                role_id    NUMBER NOT NULL,
                nav_key    VARCHAR2(64) NOT NULL,
                PRIMARY KEY (account_id, role_id, nav_key),
                CONSTRAINT fk_aiva_acct_role_nav_account
                    FOREIGN KEY (account_id) REFERENCES AIVA_accounts(id) ON DELETE CASCADE,
                CONSTRAINT fk_aiva_acct_role_nav_role
                    FOREIGN KEY (role_id) REFERENCES AIVA_roles(id) ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            "CREATE INDEX idx_aiva_acct_role_nav_account ON AIVA_account_role_nav_permissions (account_id)"
        )

    await _seed_account_role_nav_permissions_for_existing_accounts(db)


async def ensure_user_nav_permissions_schema(db: Database) -> None:
    if await _table_exists(db, "AIVA_USER_NAV_PERMISSIONS"):
        return

    await db.execute(
        """
        CREATE TABLE AIVA_user_nav_permissions (
            user_id   NUMBER NOT NULL,
            nav_key   VARCHAR2(64) NOT NULL,
            PRIMARY KEY (user_id, nav_key),
            CONSTRAINT fk_aiva_user_nav_user
                FOREIGN KEY (user_id) REFERENCES AIVA_users(id) ON DELETE CASCADE
        )
        """
    )
    await db.execute(
        "CREATE INDEX idx_aiva_user_nav_user ON AIVA_user_nav_permissions (user_id)"
    )


async def _seed_default_role_nav_permissions(db: Database) -> None:
    role_rows = await db.fetch_all("SELECT id, name FROM AIVA_roles")
    for row in role_rows:
        role_name = str(row["name"])
        role_id = int(row["id"])
        existing = await db.fetch_one(
            "SELECT 1 FROM AIVA_role_nav_permissions WHERE role_id = :role_id AND ROWNUM = 1",
            {"role_id": role_id},
        )
        if existing:
            continue
        keys = DEFAULT_ROLE_NAV_PERMISSIONS.get(role_name, [])
        for nav_key in keys:
            await db.execute(
                """
                INSERT INTO AIVA_role_nav_permissions (role_id, nav_key)
                VALUES (:role_id, :nav_key)
                """,
                {"role_id": role_id, "nav_key": nav_key},
            )


_AGENTS_NAV_ROLES = frozenset(
    {ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER, ROLE_SUPERVISOR}
)


async def _ensure_agents_nav_permission(db: Database) -> None:
    """Grant the agents nav key to supervisor-facing roles when missing (existing DBs)."""
    if "agents" not in ALL_NAV_KEYS:
        return
    role_rows = await db.fetch_all("SELECT id, name FROM AIVA_roles")
    for row in role_rows:
        role_name = str(row["name"])
        if role_name not in _AGENTS_NAV_ROLES:
            continue
        role_id = int(row["id"])
        existing = await db.fetch_one(
            """
            SELECT 1 FROM AIVA_role_nav_permissions
            WHERE role_id = :role_id AND nav_key = 'agents'
            """,
            {"role_id": role_id},
        )
        if existing:
            continue
        await db.execute(
            """
            INSERT INTO AIVA_role_nav_permissions (role_id, nav_key)
            VALUES (:role_id, 'agents')
            """,
            {"role_id": role_id},
        )


async def seed_account_role_nav_permissions(db: Database, account_id: int) -> None:
    """Initialize per-account role page access from code defaults."""
    existing = await db.fetch_one(
        """
        SELECT 1 FROM AIVA_account_role_nav_permissions
        WHERE account_id = :account_id AND ROWNUM = 1
        """,
        {"account_id": account_id},
    )
    if existing:
        return

    role_rows = await db.fetch_all("SELECT id, name FROM AIVA_roles")
    for row in role_rows:
        role_id = int(row["id"])
        role_name = str(row["name"])
        keys = DEFAULT_ROLE_NAV_PERMISSIONS.get(role_name, [])
        for nav_key in keys:
            await db.execute(
                """
                INSERT INTO AIVA_account_role_nav_permissions (account_id, role_id, nav_key)
                VALUES (:account_id, :role_id, :nav_key)
                """,
                {"account_id": account_id, "role_id": role_id, "nav_key": nav_key},
            )


async def _seed_account_role_nav_permissions_for_existing_accounts(db: Database) -> None:
    accounts = await db.fetch_all("SELECT id FROM AIVA_accounts ORDER BY id")
    for row in accounts:
        await seed_account_role_nav_permissions(db, int(row["id"]))


async def _account_role_nav_keys(
    db: Database,
    *,
    account_id: int,
    role_id: int,
    role_name: str,
) -> list[str]:
    rows = await db.fetch_all(
        """
        SELECT nav_key FROM AIVA_account_role_nav_permissions
        WHERE account_id = :account_id AND role_id = :role_id
        ORDER BY nav_key
        """,
        {"account_id": account_id, "role_id": role_id},
    )
    if rows:
        return _active_nav_keys([str(r["nav_key"]) for r in rows])
    return _active_nav_keys(list(DEFAULT_ROLE_NAV_PERMISSIONS.get(role_name, [])))


async def list_roles_with_nav_permissions_for_account(db: Database, account_id: int) -> list[dict]:
    role_rows = await db.fetch_all("SELECT id, name FROM AIVA_roles ORDER BY id")
    out: list[dict] = []
    for row in role_rows:
        role_id = int(row["id"])
        role_name = str(row["name"])
        nav_permissions = await _account_role_nav_keys(
            db,
            account_id=account_id,
            role_id=role_id,
            role_name=role_name,
        )
        out.append(
            {
                "id": role_id,
                "name": role_name,
                "nav_permissions": nav_permissions,
            }
        )
    return out


async def set_account_role_nav_permissions(
    db: Database,
    account_id: int,
    role_id: int,
    nav_keys: list[str],
) -> list[str]:
    role_row = await db.fetch_one(
        "SELECT id, name FROM AIVA_roles WHERE id = :id",
        {"id": role_id},
    )
    if not role_row:
        raise ValueError(f"Role {role_id} not found")

    role_name = str(role_row["name"])
    if role_name == ROLE_SUPER_ADMIN:
        nav_keys = sorted(ALL_NAV_KEYS)

    valid = {k for k in nav_keys if k in ALL_NAV_KEYS}
    async with db.connection() as conn:
        await db.execute(
            """
            DELETE FROM AIVA_account_role_nav_permissions
            WHERE account_id = :account_id AND role_id = :role_id
            """,
            {"account_id": account_id, "role_id": role_id},
            conn=conn,
        )
        for nav_key in sorted(valid):
            await db.execute(
                """
                INSERT INTO AIVA_account_role_nav_permissions (account_id, role_id, nav_key)
                VALUES (:account_id, :role_id, :nav_key)
                """,
                {"account_id": account_id, "role_id": role_id, "nav_key": nav_key},
                conn=conn,
            )
    return sorted(valid)


async def _user_role_assignments(db: Database, user_id: int) -> list[dict]:
    return await db.fetch_all(
        """
        SELECT ur.role_id, r.name AS role_name, ur.account_id
        FROM AIVA_user_roles ur
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE ur.user_id = :user_id
        """,
        {"user_id": user_id},
    )


async def _effective_account_ids_for_nav(
    db: Database,
    *,
    user_id: int,
    organization_id: int,
    is_org_admin: bool,
    membership_account_ids: set[int],
) -> set[int]:
    if is_org_admin:
        rows = await db.fetch_all(
            "SELECT id FROM AIVA_accounts WHERE organization_id = :org_id",
            {"org_id": organization_id},
        )
        return {int(r["id"]) for r in rows}

    account_ids = set(membership_account_ids)
    role_rows = await _user_role_assignments(db, user_id)
    for row in role_rows:
        account_id = row.get("account_id")
        if account_id is not None:
            account_ids.add(int(account_id))
    return account_ids


async def _resolve_account_scoped_role_nav_permissions(
    db: Database,
    *,
    user_id: int,
    organization_id: int,
    is_org_admin: bool,
    membership_account_ids: set[int],
    role_ids: list[int],
    role_names: set[str],
) -> list[str]:
    account_ids = await _effective_account_ids_for_nav(
        db,
        user_id=user_id,
        organization_id=organization_id,
        is_org_admin=is_org_admin,
        membership_account_ids=membership_account_ids,
    )
    if not account_ids:
        return await _resolve_role_nav_permissions(
            db,
            role_ids=role_ids,
            role_names=role_names,
        )

    role_rows = await _user_role_assignments(db, user_id)
    merged: set[str] = set()
    for account_id in account_ids:
        applicable = [
            row
            for row in role_rows
            if row.get("account_id") is None or int(row["account_id"]) == account_id
        ]
        if not applicable:
            continue
        for row in applicable:
            role_id = int(row["role_id"])
            role_name = str(row["role_name"])
            merged.update(
                await _account_role_nav_keys(
                    db,
                    account_id=account_id,
                    role_id=role_id,
                    role_name=role_name,
                )
            )
    if merged:
        return sorted(merged)

    return await _resolve_role_nav_permissions(
        db,
        role_ids=role_ids,
        role_names=role_names,
    )


async def list_roles_with_nav_permissions(db: Database) -> list[dict]:
    role_rows = await db.fetch_all("SELECT id, name FROM AIVA_roles ORDER BY id")
    perm_rows = await db.fetch_all(
        "SELECT role_id, nav_key FROM AIVA_role_nav_permissions ORDER BY role_id, nav_key"
    )
    perms_by_role: dict[int, list[str]] = {}
    for row in perm_rows:
        rid = int(row["role_id"])
        perms_by_role.setdefault(rid, []).append(str(row["nav_key"]))

    out: list[dict] = []
    for row in role_rows:
        role_id = int(row["id"])
        role_name = str(row["name"])
        nav_permissions = perms_by_role.get(role_id)
        if nav_permissions is None:
            nav_permissions = list(DEFAULT_ROLE_NAV_PERMISSIONS.get(role_name, []))
        out.append(
            {
                "id": role_id,
                "name": role_name,
                "nav_permissions": _active_nav_keys(nav_permissions),
            }
        )
    return out


async def get_role_nav_permissions(db: Database, role_id: int) -> list[str]:
    role_row = await db.fetch_one(
        "SELECT id, name FROM AIVA_roles WHERE id = :id",
        {"id": role_id},
    )
    if not role_row:
        return []

    rows = await db.fetch_all(
        """
        SELECT nav_key FROM AIVA_role_nav_permissions
        WHERE role_id = :role_id
        ORDER BY nav_key
        """,
        {"role_id": role_id},
    )
    if rows:
        return _active_nav_keys([str(r["nav_key"]) for r in rows])
    return _active_nav_keys(list(DEFAULT_ROLE_NAV_PERMISSIONS.get(str(role_row["name"]), [])))


async def set_role_nav_permissions(db: Database, role_id: int, nav_keys: list[str]) -> list[str]:
    role_row = await db.fetch_one(
        "SELECT id, name FROM AIVA_roles WHERE id = :id",
        {"id": role_id},
    )
    if not role_row:
        raise ValueError(f"Role {role_id} not found")

    role_name = str(role_row["name"])
    if role_name == ROLE_SUPER_ADMIN:
        # Super Admin always has full access; keep DB row in sync for display.
        nav_keys = sorted(ALL_NAV_KEYS)

    valid = {k for k in nav_keys if k in ALL_NAV_KEYS}
    async with db.connection() as conn:
        await db.execute(
            "DELETE FROM AIVA_role_nav_permissions WHERE role_id = :role_id",
            {"role_id": role_id},
            conn=conn,
        )
        for nav_key in sorted(valid):
            await db.execute(
                """
                INSERT INTO AIVA_role_nav_permissions (role_id, nav_key)
                VALUES (:role_id, :nav_key)
                """,
                {"role_id": role_id, "nav_key": nav_key},
                conn=conn,
            )
    return sorted(valid)


async def _resolve_role_nav_permissions(
    db: Database,
    *,
    role_ids: list[int],
    role_names: set[str],
) -> list[str]:
    if not role_ids:
        merged: set[str] = set()
        for name in role_names:
            merged.update(DEFAULT_ROLE_NAV_PERMISSIONS.get(name, []))
        return sorted(merged)

    placeholders = ", ".join(f":rid{i}" for i in range(len(role_ids)))
    params = {f"rid{i}": rid for i, rid in enumerate(role_ids)}
    rows = await db.fetch_all(
        f"""
        SELECT DISTINCT nav_key
        FROM AIVA_role_nav_permissions
        WHERE role_id IN ({placeholders})
        ORDER BY nav_key
        """,
        params,
    )
    if rows:
        return [str(r["nav_key"]) for r in rows]

    merged = set()
    for name in role_names:
        merged.update(DEFAULT_ROLE_NAV_PERMISSIONS.get(name, []))
    return sorted(merged)


async def get_user_extra_nav_permissions(db: Database, user_id: int) -> list[str]:
    rows = await db.fetch_all(
        """
        SELECT nav_key FROM AIVA_user_nav_permissions
        WHERE user_id = :user_id
        ORDER BY nav_key
        """,
        {"user_id": user_id},
    )
    return _active_nav_keys([str(r["nav_key"]) for r in rows])


def _filter_grantable_nav_keys(nav_keys: list[str], *, allow_restricted: bool) -> list[str]:
    valid = {k for k in nav_keys if k in ALL_NAV_KEYS}
    if allow_restricted:
        return sorted(valid)
    return sorted(valid - ORG_ADMIN_RESTRICTED_NAV_KEYS)


async def set_user_extra_nav_permissions(
    db: Database,
    user_id: int,
    nav_keys: list[str],
    *,
    allow_restricted: bool,
) -> list[str]:
    valid = _filter_grantable_nav_keys(nav_keys, allow_restricted=allow_restricted)
    async with db.connection() as conn:
        await db.execute(
            "DELETE FROM AIVA_user_nav_permissions WHERE user_id = :user_id",
            {"user_id": user_id},
            conn=conn,
        )
        for nav_key in valid:
            await db.execute(
                """
                INSERT INTO AIVA_user_nav_permissions (user_id, nav_key)
                VALUES (:user_id, :nav_key)
                """,
                {"user_id": user_id, "nav_key": nav_key},
                conn=conn,
            )
    return valid


async def resolve_user_nav_permissions(
    db: Database,
    *,
    user_id: int,
    role_ids: list[int],
    role_names: set[str],
    is_super_admin: bool,
    organization_id: int = 0,
    membership_account_ids: set[int] | None = None,
    is_org_admin: bool = False,
) -> list[str]:
    if is_super_admin:
        return _active_nav_keys(sorted(ALL_NAV_KEYS))

    role_perms = set(
        _active_nav_keys(
            await _resolve_account_scoped_role_nav_permissions(
                db,
                user_id=user_id,
                organization_id=organization_id,
                is_org_admin=is_org_admin,
                membership_account_ids=membership_account_ids or set(),
                role_ids=role_ids,
                role_names=role_names,
            )
        )
    )
    extra_perms = set(_active_nav_keys(await get_user_extra_nav_permissions(db, user_id)))
    return sorted(role_perms | extra_perms)
