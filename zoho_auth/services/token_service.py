"""Token exchange, refresh, and revocation.

Isolated from the rest of the auth flow so the team can extend it (caching,
rotation, encrypted-at-rest storage) without touching ``AuthService``.
"""

from __future__ import annotations

from ..config import ZohoConfig
from ..exceptions import ZohoAuthError, ZohoTransportError
from ..http import HttpClient
from ..logging import Logger
from ..models import TokenBundle
from .base import Service


class TokenService(Service):
    """Handles every interaction with Zoho's token endpoint."""

    def __init__(self, config: ZohoConfig, http: HttpClient, logger: Logger) -> None:
        super().__init__(config, logger)
        self._http = http

    def exchange_code(self, code: str) -> TokenBundle:
        """Exchange a one-time authorization code for an access/refresh token pair."""

        response = self._http.post(
            self._config.token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "redirect_uri": self._config.redirect_uri,
                "code": code,
            },
        )
        return self._parse(response, action="exchange authorization code")

    def refresh(self, refresh_token: str) -> TokenBundle:
        """Get a fresh access token using a previously stored refresh token."""

        response = self._http.post(
            self._config.token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "refresh_token": refresh_token,
            },
        )
        return self._parse(response, action="refresh access token")

    def revoke(self, token: str) -> None:
        """Best-effort revocation. Logs but never raises on failure."""

        revoke_url = self._config.token_url.rstrip("/") + "/revoke"
        try:
            response = self._http.post(revoke_url, data={"token": token})
        except ZohoTransportError as exc:
            self._logger.error(f"Revoke request failed: {exc}")
            return
        if not response.ok:
            self._logger.error(
                f"Revoke returned HTTP {response.status_code}: {response.text}"
            )

    def _parse(self, response, *, action: str) -> TokenBundle:
        if not response.ok:
            raise ZohoTransportError(
                f"Failed to {action} (HTTP {response.status_code}): {response.text}"
            )
        payload = response.json()
        if "error" in payload:
            raise ZohoAuthError(
                f"Zoho rejected request to {action}: {payload['error']}"
            )
        if "access_token" not in payload:
            raise ZohoAuthError(
                f"Token response missing access_token while trying to {action}: {payload}"
            )
        return TokenBundle.from_payload(payload)
