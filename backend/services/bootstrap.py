from __future__ import annotations

import logging

from backend.auth.deps import ROLE_SUPER_ADMIN
from backend.auth.hashing import hash_password
from backend.config import Settings, get_settings
from backend.database import Database

_log = logging.getLogger(__name__)


async def _ensure_organization(db: Database, settings: Settings) -> int:
    org = await db.fetch_one(
        "SELECT id FROM AIVA_organizations WHERE code = :code",
        {"code": settings.bootstrap_superadmin_organization_code},
    )
    if org:
        return int(org["id"])

    org_id = await db.execute(
        """
        INSERT INTO AIVA_organizations (name, code, status)
        VALUES (:name, :code, 'ACTIVE')
        RETURNING id INTO :out_id
        """,
        {
            "name": settings.bootstrap_superadmin_organization_name,
            "code": settings.bootstrap_superadmin_organization_code,
        },
        return_id=True,
    )
    _log.info(
        "Created bootstrap organization %s (%s)",
        settings.bootstrap_superadmin_organization_name,
        settings.bootstrap_superadmin_organization_code,
    )
    return int(org_id or 0)


async def _ensure_super_admin_role_id(db: Database) -> int:
    role = await db.fetch_one(
        "SELECT id FROM AIVA_roles WHERE name = :name",
        {"name": ROLE_SUPER_ADMIN},
    )
    if not role:
        raise RuntimeError(
            f"Role '{ROLE_SUPER_ADMIN}' is missing from AIVA_roles; seed roles before starting the backend"
        )
    return int(role["id"])


async def ensure_bootstrap_superadmin(db: Database, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not settings.bootstrap_superadmin_enabled:
        _log.info("Bootstrap superadmin is disabled")
        return

    organization_id = await _ensure_organization(db, settings)
    role_id = await _ensure_super_admin_role_id(db)

    existing = await db.fetch_one(
        """
        SELECT id FROM AIVA_users
        WHERE LOWER(email) = LOWER(:email)
        """,
        {"email": settings.bootstrap_superadmin_email},
    )
    if existing:
        user_id = int(existing["id"])
        await db.execute(
            """
            UPDATE AIVA_users
            SET password_hash = :password_hash,
                status = 'ACTIVE',
                first_name = :first_name,
                last_name = :last_name,
                organization_id = :organization_id
            WHERE id = :id
            """,
            {
                "id": user_id,
                "password_hash": hash_password(settings.bootstrap_superadmin_password),
                "first_name": settings.bootstrap_superadmin_first_name,
                "last_name": settings.bootstrap_superadmin_last_name,
                "organization_id": organization_id,
            },
        )
        role_assignment = await db.fetch_one(
            """
            SELECT 1 FROM AIVA_user_roles
            WHERE user_id = :user_id AND role_id = :role_id
            """,
            {"user_id": user_id, "role_id": role_id},
        )
        if not role_assignment:
            await db.execute(
                """
                INSERT INTO AIVA_user_roles (user_id, role_id, account_id)
                VALUES (:user_id, :role_id, NULL)
                """,
                {"user_id": user_id, "role_id": role_id},
            )
        _log.info("Bootstrap superadmin synced: %s", settings.bootstrap_superadmin_email)
        return

    user_id = await db.execute(
        """
        INSERT INTO AIVA_users (
            organization_id, first_name, last_name, email, password_hash, status
        ) VALUES (
            :organization_id, :first_name, :last_name, :email, :password_hash, 'ACTIVE'
        )
        RETURNING id INTO :out_id
        """,
        {
            "organization_id": organization_id,
            "first_name": settings.bootstrap_superadmin_first_name,
            "last_name": settings.bootstrap_superadmin_last_name,
            "email": settings.bootstrap_superadmin_email,
            "password_hash": hash_password(settings.bootstrap_superadmin_password),
        },
        return_id=True,
    )
    await db.execute(
        """
        INSERT INTO AIVA_user_roles (user_id, role_id, account_id)
        VALUES (:user_id, :role_id, NULL)
        """,
        {"user_id": user_id, "role_id": role_id},
    )
    _log.info("Created bootstrap superadmin: %s", settings.bootstrap_superadmin_email)
