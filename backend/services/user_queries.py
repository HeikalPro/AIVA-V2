from backend.database import Database
from backend.exceptions import NotFoundError
from backend.schemas.users import UserOut
from backend.services.role_nav_permissions import get_user_extra_nav_permissions
from backend.utils import serialize_row


async def build_user_out(db: Database, user_id: int) -> UserOut:
    row = await db.fetch_one(
        """
        SELECT u.id, u.organization_id, u.email, u.first_name, u.last_name, u.status, u.created_at,
               o.name AS organization_name, o.code AS organization_code
        FROM AIVA_users u
        JOIN AIVA_organizations o ON o.id = u.organization_id
        WHERE u.id = :id
        """,
        {"id": user_id},
    )
    if not row:
        raise NotFoundError("User not found")
    roles = await db.fetch_all(
        """
        SELECT r.name FROM AIVA_user_roles ur
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE ur.user_id = :user_id
        """,
        {"user_id": user_id},
    )
    accounts = await db.fetch_all(
        """
        SELECT account_id FROM AIVA_account_users
        WHERE user_id = :user_id AND status = 'ACTIVE'
        """,
        {"user_id": user_id},
    )
    data = serialize_row(row) or {}
    data["roles"] = [str(r["name"]) for r in roles]
    data["account_ids"] = [int(a["account_id"]) for a in accounts]
    data["extra_nav_permissions"] = await get_user_extra_nav_permissions(db, user_id)
    return UserOut(**data)
