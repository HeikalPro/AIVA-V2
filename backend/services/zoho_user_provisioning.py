from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import Request
from zoho_auth.models import ZohoUserSession

from backend.auth.deps import ROLE_AGENT
from backend.auth.hashing import hash_password
from backend.config import Settings, get_settings
from backend.database import Database
from backend.exceptions import BadRequestError
from backend.services.audit import write_audit_log

_log = logging.getLogger(__name__)


def _parse_zoho_names(session: ZohoUserSession) -> tuple[str | None, str | None]:
    info = session.user_info
    first_name = info.get("First_Name") or info.get("first_name")
    last_name = info.get("Last_Name") or info.get("last_name")
    if first_name or last_name:
        return first_name, last_name

    display_name = session.display_name
    if not display_name:
        return None, None

    parts = display_name.strip().split(None, 1)
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]


async def _resolve_organization_id(db: Database, settings: Settings) -> int:
    if settings.zoho_auto_register_organization_id is not None:
        org = await db.fetch_one(
            """
            SELECT id FROM AIVA_organizations
            WHERE id = :id AND status = 'ACTIVE'
            """,
            {"id": settings.zoho_auto_register_organization_id},
        )
        if not org:
            raise BadRequestError(
                "Configured Zoho auto-register organization is missing or inactive"
            )
        return int(org["id"])

    orgs = await db.fetch_all(
        """
        SELECT id FROM AIVA_organizations
        WHERE status = 'ACTIVE'
        ORDER BY id
        """
    )
    org = orgs[0] if orgs else None
    if not org:
        raise BadRequestError("No active organization available for Zoho auto-registration")
    return int(org["id"])


async def _resolve_role_id(db: Database, role_name: str) -> int:
    role = await db.fetch_one(
        "SELECT id FROM AIVA_roles WHERE name = :name",
        {"name": role_name},
    )
    if not role:
        raise BadRequestError(f"Role '{role_name}' is not configured for Zoho auto-registration")
    return int(role["id"])


async def provision_zoho_user(
    db: Database,
    session: ZohoUserSession,
    *,
    request: Request | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Create an ACTIVE AIVA user from a validated Zoho OAuth session."""
    settings = settings or get_settings()
    email = session.email
    if not email:
        raise BadRequestError("Zoho profile did not include an email")

    organization_id = await _resolve_organization_id(db, settings)
    role_name = settings.zoho_auto_register_role or ROLE_AGENT
    role_id = await _resolve_role_id(db, role_name)
    first_name, last_name = _parse_zoho_names(session)

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
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "password_hash": hash_password(secrets.token_urlsafe(32)),
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

    ip_address = None
    if request is not None:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            ip_address = forwarded.split(",")[0].strip()
        elif request.client:
            ip_address = request.client.host

    await write_audit_log(
        db,
        user_id=int(user_id or 0),
        entity_type="user",
        entity_id=int(user_id or 0),
        action_type="CREATE",
        new_value={
            "email": email,
            "organization_id": organization_id,
            "role": role_name,
            "source": "zoho_oauth",
        },
        ip_address=ip_address,
    )
    _log.info("Auto-registered Zoho user %s in organization %s with role %s", email, organization_id, role_name)

    user = await db.fetch_one(
        """
        SELECT id, email, organization_id, status
        FROM AIVA_users
        WHERE id = :id
        """,
        {"id": user_id},
    )
    if not user:
        raise BadRequestError("Failed to load newly registered Zoho user")
    return user
