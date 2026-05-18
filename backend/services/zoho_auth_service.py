from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from threading import Lock
from zoho_auth.container import ServiceContainer
from zoho_auth.exceptions import ZohoAuthError, ZohoConfigError, ZohoPolicyError, ZohoTransportError
from zoho_auth.models import ZohoUserSession

from backend.config import Settings, get_settings
from backend.exceptions import BadRequestError, ForbiddenError, UnauthorizedError

_log = logging.getLogger(__name__)

_STATE_TTL_SECONDS = 600
_states: dict[str, float] = {}
_state_lock = Lock()


def _cleanup_expired_states(now: float) -> None:
    expired = [key for key, expiry in _states.items() if expiry <= now]
    for key in expired:
        _states.pop(key, None)


def create_oauth_state() -> str:
    state = secrets.token_urlsafe(24)
    deadline = time.time() + _STATE_TTL_SECONDS
    with _state_lock:
        _cleanup_expired_states(time.time())
        _states[state] = deadline
    return state


def consume_oauth_state(state: str | None) -> bool:
    if not state:
        return False
    now = time.time()
    with _state_lock:
        expiry = _states.pop(state, None)
        _cleanup_expired_states(now)
    return expiry is not None and now < expiry


class ZohoAuthService:
    """Web OAuth adapter over the shared ``zoho_auth`` package."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._container: ServiceContainer | None = None

    @property
    def frontend_redirect_url(self) -> str:
        return os.getenv(
            "ZOHO_FRONTEND_REDIRECT_URL",
            self._settings.zoho_frontend_redirect_url,
        ).rstrip("/")

    def _get_container(self) -> ServiceContainer:
        if self._container is None:
            try:
                self._container = ServiceContainer()
            except ZohoConfigError as exc:
                _log.error("Zoho auth is not configured: %s", exc)
                raise BadRequestError("Zoho login is not configured") from exc
        return self._container

    def start_login(self) -> tuple[str, str]:
        state = create_oauth_state()
        container = self._get_container()
        return container.auth_service.build_auth_url(state), state

    def _authenticate_code(self, code: str) -> ZohoUserSession:
        container = self._get_container()
        tokens = container.token_service.exchange_code(code)
        user_info = container.profile_service.fetch(tokens.access_token)
        session = ZohoUserSession(tokens=tokens, user_info=user_info)
        container.policy.evaluate(session)
        return session

    async def authenticate_code(self, code: str) -> ZohoUserSession:
        try:
            return await asyncio.to_thread(self._authenticate_code, code)
        except ZohoPolicyError as exc:
            raise ForbiddenError(str(exc)) from exc
        except (ZohoAuthError, ZohoTransportError) as exc:
            raise UnauthorizedError(f"Zoho login failed: {exc}") from exc

    def format_frontend_redirect(self, tokens: dict[str, str], *, error: str | None = None) -> str:
        from urllib.parse import urlencode

        params: dict[str, str] = {}
        if error:
            params["error"] = error
        else:
            params["access_token"] = tokens["access_token"]
            params["refresh_token"] = tokens["refresh_token"]
            params["token_type"] = tokens.get("token_type", "bearer")
        return f"{self.frontend_redirect_url}?{urlencode(params)}"


_zoho_auth_service: ZohoAuthService | None = None


def get_zoho_auth_service() -> ZohoAuthService:
    global _zoho_auth_service
    if _zoho_auth_service is None:
        _zoho_auth_service = ZohoAuthService()
    return _zoho_auth_service
