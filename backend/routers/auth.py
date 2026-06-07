from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from zoho_auth.models import ZohoUserSession

from backend.auth.deps import UserContext, get_current_user
from backend.auth.hashing import verify_password
from backend.auth.jwt import create_token_pair, decode_token
from backend.config import get_settings
from backend.dependencies import DbDep
from backend.exceptions import BadRequestError, UnauthorizedError
from backend.limiter import limiter
from backend.schemas.auth import LoginRequest, RefreshRequest, TokenResponse, UserProfile, ZohoLoginResponse
from backend.schemas.common import MessageResponse
from backend.services.zoho_auth_service import (
    get_zoho_auth_service,
    pop_oauth_state,
)
from backend.services.zoho_user_provisioning import provision_zoho_user

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


async def _record_login_attempt(
    db: DbDep,
    request: Request,
    *,
    user_id: int | None,
    success: bool,
) -> None:
    if user_id is None:
        return
    await db.execute(
        """
        INSERT INTO AIVA_login_history (user_id, ip_address, device_info, success)
        VALUES (:user_id, :ip_address, :device_info, :success)
        """,
        {
            "user_id": user_id,
            "ip_address": _client_ip(request),
            "device_info": request.headers.get("user-agent"),
            "success": 1 if success else 0,
        },
    )


async def _issue_tokens_for_user(
    db: DbDep,
    request: Request,
    user: dict,
) -> TokenResponse:
    if user.get("status") != "ACTIVE":
        raise UnauthorizedError("User account is inactive")

    await _record_login_attempt(db, request, user_id=int(user["id"]), success=True)

    tokens = create_token_pair(
        int(user["id"]),
        organization_id=int(user["organization_id"]),
        email=str(user["email"]),
    )
    await db.execute(
        "UPDATE AIVA_users SET last_login = CURRENT_TIMESTAMP WHERE id = :id",
        {"id": int(user["id"])},
    )
    return TokenResponse(**tokens)


async def _issue_tokens_for_zoho_user(
    db: DbDep,
    request: Request,
    *,
    session: ZohoUserSession,
) -> TokenResponse:
    email = session.email
    if not email:
        raise UnauthorizedError("Zoho profile did not include an email")

    user = await db.fetch_one(
        """
        SELECT id, email, organization_id, status
        FROM AIVA_users
        WHERE LOWER(email) = LOWER(:email)
        """,
        {"email": email},
    )

    if not user:
        user = await provision_zoho_user(db, session, request=request)
    elif user.get("status") != "ACTIVE":
        raise UnauthorizedError("No active AIVA account found for this Zoho user")

    return await _issue_tokens_for_user(db, request, user)


@router.post("/login", response_model=TokenResponse)
@limiter.limit(get_settings().rate_limit_login)
async def login(body: LoginRequest, request: Request, db: DbDep) -> TokenResponse:
    user = await db.fetch_one(
        """
        SELECT id, email, organization_id, status, password_hash
        FROM AIVA_users
        WHERE LOWER(email) = LOWER(:email)
        """,
        {"email": body.email},
    )

    if not user or not verify_password(body.password, str(user.get("password_hash") or "")):
        if user:
            await _record_login_attempt(db, request, user_id=int(user["id"]), success=False)
        raise UnauthorizedError("Invalid email or password")

    return await _issue_tokens_for_user(db, request, user)


@router.get("/zoho/login", response_model=None)
@limiter.limit(get_settings().rate_limit_login)
async def zoho_login(
    request: Request,
    redirect: Annotated[bool, Query()] = False,
    return_to: Annotated[str | None, Query()] = None,
):
    service = get_zoho_auth_service()
    callback_url = service.validate_return_to(return_to)
    auth_url, _state = service.start_login(return_to=callback_url)
    if redirect:
        return RedirectResponse(auth_url, status_code=302)
    return ZohoLoginResponse(auth_url=auth_url)


@router.get("/zoho/callback")
@limiter.limit(get_settings().rate_limit_login)
async def zoho_callback(
    request: Request,
    db: DbDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query(alias="error_description")] = None,
) -> RedirectResponse:
    service = get_zoho_auth_service()
    state_ok, return_to = pop_oauth_state(state)

    if error:
        detail = error_description or error
        return RedirectResponse(
            service.format_frontend_redirect({}, error=detail, return_to=return_to),
            status_code=302,
        )

    if not code:
        return RedirectResponse(
            service.format_frontend_redirect({}, error="Missing authorization code", return_to=return_to),
            status_code=302,
        )

    if not state_ok:
        return RedirectResponse(
            service.format_frontend_redirect({}, error="Invalid or expired login state", return_to=return_to),
            status_code=302,
        )

    session = await service.authenticate_code(code)
    # Mail API uses a dedicated service token (python -m zoho_auth --force-login), not web login.
    email = session.email
    if not email:
        return RedirectResponse(
            service.format_frontend_redirect(
                {}, error="Zoho profile did not include an email", return_to=return_to
            ),
            status_code=302,
        )

    try:
        tokens = await _issue_tokens_for_zoho_user(db, request, session=session)
    except (UnauthorizedError, BadRequestError) as exc:
        return RedirectResponse(
            service.format_frontend_redirect({}, error=str(exc.detail), return_to=return_to),
            status_code=302,
        )

    return RedirectResponse(
        service.format_frontend_redirect(tokens.model_dump(), return_to=return_to),
        status_code=302,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: DbDep) -> TokenResponse:
    payload = decode_token(body.refresh_token, expected_type="refresh")
    user_id = payload.get("uid")
    if user_id is None:
        raise UnauthorizedError("Invalid refresh token")

    user = await db.fetch_one(
        "SELECT id, email, organization_id, status FROM AIVA_users WHERE id = :id",
        {"id": int(user_id)},
    )
    if not user or user.get("status") != "ACTIVE":
        raise UnauthorizedError("User not found or inactive")

    tokens = create_token_pair(
        int(user["id"]),
        organization_id=int(user["organization_id"]),
        email=str(user["email"]),
    )
    return TokenResponse(**tokens)


@router.post("/logout", response_model=MessageResponse)
async def logout(
    user: Annotated[UserContext, Depends(get_current_user)],
) -> MessageResponse:
    return MessageResponse(message="Logged out")


@router.get("/me", response_model=UserProfile)
async def me(user: Annotated[UserContext, Depends(get_current_user)]) -> UserProfile:
    return UserProfile(
        id=user.id,
        email=user.email,
        organization_id=user.organization_id,
        first_name=user.first_name,
        last_name=user.last_name,
        roles=sorted(user.role_names),
    )
