from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from backend.config import Settings, get_settings
from backend.exceptions import UnauthorizedError


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(
    subject: str,
    claims: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> str:
    s = settings or get_settings()
    expire = _now() + timedelta(minutes=s.jwt_access_token_expire_minutes)
    payload = {
        "sub": subject,
        "type": "access",
        "exp": expire,
        "iat": _now(),
        **claims,
    }
    return jwt.encode(payload, s.jwt_secret_key, algorithm=s.jwt_algorithm)


def create_refresh_token(
    subject: str,
    claims: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> str:
    s = settings or get_settings()
    expire = _now() + timedelta(days=s.jwt_refresh_token_expire_days)
    payload = {
        "sub": subject,
        "type": "refresh",
        "exp": expire,
        "iat": _now(),
        **claims,
    }
    return jwt.encode(payload, s.jwt_secret_key, algorithm=s.jwt_algorithm)


def create_token_pair(
    user_id: int,
    *,
    organization_id: int,
    email: str,
    settings: Settings | None = None,
) -> dict[str, str]:
    claims = {"uid": user_id, "org_id": organization_id, "email": email}
    return {
        "access_token": create_access_token(str(user_id), claims, settings=settings),
        "refresh_token": create_refresh_token(str(user_id), claims, settings=settings),
        "token_type": "bearer",
    }


def decode_token(token: str, *, expected_type: str | None = None) -> dict[str, Any]:
    s = get_settings()
    try:
        payload = jwt.decode(
            token,
            s.jwt_secret_key,
            algorithms=[s.jwt_algorithm],
        )
    except jwt.PyJWTError as ex:
        raise UnauthorizedError("Invalid or expired token") from ex

    if expected_type and payload.get("type") != expected_type:
        raise UnauthorizedError("Invalid token type")
    return payload
