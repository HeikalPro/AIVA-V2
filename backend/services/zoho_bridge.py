from __future__ import annotations

import logging
from typing import Any

from backend.config import Settings, get_settings

_log = logging.getLogger(__name__)


class ZohoBridge:
    """Sync AIVA tickets to Zoho Desk using stored OAuth tokens from zoho_auth."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._container: Any | None = None

    def _get_container(self) -> Any | None:
        if self._container is not None:
            return self._container
        if not self._settings.zoho_ticket_sync_enabled:
            return None
        try:
            from zoho_auth.container import ServiceContainer

            self._container = ServiceContainer()
            return self._container
        except Exception:
            _log.warning("Zoho auth not configured; ticket sync disabled")
            return None

    def _get_access_token(self) -> str | None:
        container = self._get_container()
        if container is None:
            return None
        try:
            session = container.auth_service.login_or_resume(open_browser=False)
            return session.tokens.access_token
        except Exception:
            _log.exception("Failed to obtain Zoho access token")
            return None

    async def push_ticket(self, ticket: dict[str, Any]) -> dict[str, Any] | None:
        """Create a ticket in Zoho Desk (best-effort, non-blocking)."""
        if not self._settings.zoho_ticket_sync_enabled:
            return None

        token = self._get_access_token()
        if not token:
            return None

        container = self._get_container()
        if container is None:
            return None

        payload = {
            "subject": ticket.get("subject") or f"AIVA Ticket #{ticket.get('id')}",
            "description": ticket.get("description") or "",
            "priority": (ticket.get("priority") or "Medium").title(),
            "status": "Open",
        }

        url = f"{self._settings.zoho_desk_api_base.rstrip('/')}/tickets"
        headers = {
            "Authorization": f"Zoho-oauthtoken {token}",
            "Content-Type": "application/json",
        }

        try:
            import asyncio

            import requests

            def _post() -> dict[str, Any] | None:
                resp = requests.post(url, headers=headers, json=payload, timeout=30)
                if resp.status_code >= 400:
                    _log.error("Zoho Desk API error %s: %s", resp.status_code, resp.text)
                    return None
                return resp.json() if resp.text else {"status": "synced"}

            return await asyncio.to_thread(_post)
        except Exception:
            _log.exception("Failed to push ticket %s to Zoho", ticket.get("id"))
            return None


def get_zoho_bridge() -> ZohoBridge:
    return ZohoBridge(get_settings())
