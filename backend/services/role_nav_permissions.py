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
        "prompts",
        "account-updates",
        "tickets",
        "ingestion",
    ],
    ROLE_ACCOUNT_MANAGER: [
        "dashboard",
        "accounts",
        "users",
        "prompts",
        "account-updates",
        "tickets",
        "ingestion",
    ],
    ROLE_SUPERVISOR: ["dashboard", "account-updates", "chat", "tickets", "ingestion"],
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
    await ensure_user_nav_permissions_schema(db)


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
) -> list[str]:
    if is_super_admin:
        return _active_nav_keys(sorted(ALL_NAV_KEYS))

    role_perms = set(
        _active_nav_keys(
            await _resolve_role_nav_permissions(db, role_ids=role_ids, role_names=role_names)
        )
    )
    extra_perms = set(_active_nav_keys(await get_user_extra_nav_permissions(db, user_id)))
    return sorted(role_perms | extra_perms)
