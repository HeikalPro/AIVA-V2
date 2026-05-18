from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.auth.jwt import decode_token
from backend.database import Database
from backend.dependencies import get_db
from backend.exceptions import ForbiddenError, UnauthorizedError

_bearer = HTTPBearer(auto_error=False)

ROLE_SUPER_ADMIN = "SUPER_ADMIN"
ROLE_ORG_ADMIN = "ORGANIZATION_ADMIN"
ROLE_ACCOUNT_MANAGER = "ACCOUNT_MANAGER"
ROLE_SUPERVISOR = "SUPERVISOR"
ROLE_AGENT = "AGENT"
ROLE_DEVELOPER = "DEVELOPER"

ROLE_HIERARCHY = {
    ROLE_SUPER_ADMIN: 100,
    ROLE_ORG_ADMIN: 80,
    ROLE_ACCOUNT_MANAGER: 60,
    ROLE_SUPERVISOR: 50,
    ROLE_DEVELOPER: 40,
    ROLE_AGENT: 10,
}


@dataclass
class RoleAssignment:
    role_id: int
    role_name: str
    account_id: int | None = None


@dataclass
class UserContext:
    id: int
    email: str
    organization_id: int
    first_name: str | None
    last_name: str | None
    status: str
    roles: list[RoleAssignment] = field(default_factory=list)
    membership_account_ids: set[int] = field(default_factory=set)

    @property
    def role_names(self) -> set[str]:
        return {r.role_name for r in self.roles}

    @property
    def is_super_admin(self) -> bool:
        return ROLE_SUPER_ADMIN in self.role_names

    @property
    def is_org_admin(self) -> bool:
        return ROLE_ORG_ADMIN in self.role_names

    @property
    def account_ids(self) -> set[int]:
        ids: set[int] = set(self.membership_account_ids)
        for r in self.roles:
            if r.account_id is not None:
                ids.add(r.account_id)
        return ids

    def has_role(self, *names: str) -> bool:
        return bool(self.role_names.intersection(names))

    def can_access_account(self, account_id: int, organization_id: int | None = None) -> bool:
        if self.is_super_admin:
            return True
        if organization_id is not None and self.organization_id != organization_id:
            return False
        if self.is_org_admin:
            return True
        return account_id in self.account_ids


async def _load_user_context(db: Database, user_id: int) -> UserContext:
    user = await db.fetch_one(
        """
        SELECT id, email, organization_id, first_name, last_name, status
        FROM AIVA_users
        WHERE id = :user_id
        """,
        {"user_id": user_id},
    )
    if not user or user.get("status") != "ACTIVE":
        raise UnauthorizedError("User not found or inactive")

    role_rows = await db.fetch_all(
        """
        SELECT ur.role_id, r.name AS role_name, ur.account_id
        FROM AIVA_user_roles ur
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE ur.user_id = :user_id
        """,
        {"user_id": user_id},
    )
    roles = [
        RoleAssignment(
            role_id=int(row["role_id"]),
            role_name=str(row["role_name"]),
            account_id=int(row["account_id"]) if row.get("account_id") is not None else None,
        )
        for row in role_rows
    ]
    membership_rows = await db.fetch_all(
        """
        SELECT account_id FROM AIVA_account_users
        WHERE user_id = :user_id AND status = 'ACTIVE'
        """,
        {"user_id": user_id},
    )
    membership_account_ids = {int(row["account_id"]) for row in membership_rows}
    return UserContext(
        id=int(user["id"]),
        email=str(user["email"]),
        organization_id=int(user["organization_id"]),
        first_name=user.get("first_name"),
        last_name=user.get("last_name"),
        status=str(user["status"]),
        roles=roles,
        membership_account_ids=membership_account_ids,
    )


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Database, Depends(get_db)],
) -> UserContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError("Missing bearer token")

    payload = decode_token(credentials.credentials, expected_type="access")
    user_id = payload.get("uid")
    if user_id is None:
        raise UnauthorizedError("Invalid token payload")

    user = await _load_user_context(db, int(user_id))
    request.state.user = user
    return user


def require_roles(*allowed: str) -> Callable:
    async def _checker(user: Annotated[UserContext, Depends(get_current_user)]) -> UserContext:
        if user.is_super_admin:
            return user
        if not user.has_role(*allowed):
            raise ForbiddenError(f"Requires one of roles: {', '.join(allowed)}")
        return user

    return _checker


def require_account_access(account_id: int, user: UserContext, org_id: int | None = None) -> None:
    if not user.can_access_account(account_id, org_id):
        raise ForbiddenError("No access to this account")
