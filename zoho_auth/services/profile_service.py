"""User-profile lookups against Zoho's user-info endpoint.

Kept separate so future profile-related calls (organisations, roles, group
memberships) can extend this service without growing the auth orchestrator.
"""

from __future__ import annotations

from ..config import ZohoConfig
from ..exceptions import ZohoTransportError
from ..http import HttpClient
from ..logging import Logger
from .base import Service


class ProfileService(Service):
    """Reads the authenticated user's profile data from Zoho."""

    def __init__(self, config: ZohoConfig, http: HttpClient, logger: Logger) -> None:
        super().__init__(config, logger)
        self._http = http

    def fetch(self, access_token: str) -> dict:
        response = self._http.get(
            self._config.user_info_url,
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )
        if not response.ok:
            raise ZohoTransportError(
                f"Fetching user info failed (HTTP {response.status_code}): {response.text}"
            )
        return response.json()
