"""Access-token providers for Microsoft Graph."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from crm_ingestion.config import MicrosoftGraphSettings
from crm_ingestion.errors import AuthenticationError, ConfigurationError


class AuthenticationProvider(ABC):
    """Supplies bearer tokens for Graph requests."""

    @abstractmethod
    def get_access_token(self) -> str:
        """Return a valid access token."""

    def invalidate(self) -> None:
        """Forget any cached token (called once after a 401). No-op by default."""


class StaticTokenAuthProvider(AuthenticationProvider):
    """Always returns the same token. For tests and local experiments."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("token must not be empty")
        self._token = token

    def get_access_token(self) -> str:
        return self._token


class ClientCredentialsAuthProvider(AuthenticationProvider):
    """App-only tokens via MSAL's client-credentials flow.

    Credentials are checked when a token is requested, not at construction, so objects
    can be wired up before real credentials exist. MSAL caches tokens in-process."""

    def __init__(
        self,
        settings: MicrosoftGraphSettings,
        *,
        app_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._settings = settings
        self._app_factory = app_factory
        self._app: Any = None
        self._lock = threading.Lock()

    def get_access_token(self) -> str:
        app = self._get_app()
        scopes = [self._settings.graph_scope]
        try:
            result = app.acquire_token_for_client(scopes=scopes)
        except Exception as exc:  # noqa: BLE001 - msal raises assorted types; scrubbed below
            raise AuthenticationError(
                f"token request failed: {self._scrub(f'{type(exc).__name__}: {exc}')}"
            ) from None
        if not isinstance(result, dict):
            raise AuthenticationError("token request returned an unexpected response")
        token = result.get("access_token")
        if isinstance(token, str) and token:
            return token
        error = result.get("error") or "unknown_error"
        description = result.get("error_description") or "no description"
        raise AuthenticationError(f"could not acquire Graph token: {error}: {self._scrub(str(description))}")

    def invalidate(self) -> None:
        """Drop the MSAL app (and its token cache) so the next call fetches a fresh token."""
        with self._lock:
            self._app = None

    def _get_app(self) -> Any:
        with self._lock:
            if self._app is None:
                self._app = self._build_app()
            return self._app

    def _build_app(self) -> Any:
        settings = self._settings
        if not settings.is_configured:
            raise ConfigurationError(
                "Microsoft Graph credentials are not configured; set " + ", ".join(settings.missing())
            )
        factory = self._app_factory
        if factory is None:
            try:
                import msal
            except ImportError as exc:
                raise ConfigurationError(
                    "msal is not installed; run `pip install crm-document-ingestion[msal]`"
                ) from exc
            factory = msal.ConfidentialClientApplication
        authority = f"{settings.authority_host.rstrip('/')}/{settings.tenant_id}"
        try:
            return factory(
                settings.client_id,
                authority=authority,
                client_credential=settings.client_secret.get_secret_value(),
            )
        except Exception as exc:  # noqa: BLE001 - msal raises assorted types; scrubbed below
            raise AuthenticationError(
                f"could not create MSAL client: {self._scrub(f'{type(exc).__name__}: {exc}')}"
            ) from None

    def _scrub(self, text: str) -> str:
        secret = self._settings.client_secret.get_secret_value()
        return text.replace(secret, "***") if secret else text
