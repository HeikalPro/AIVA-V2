from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.config import Settings, get_settings
from backend.services.email.base import EmailMessage

_log = logging.getLogger(__name__)


class ZohoMailSender:
    """Send transactional mail via Zoho Mail API using zoho_auth stored OAuth tokens."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._container: Any | None = None
        self._account_id_cache: str | None = None

    def _get_container(self) -> Any | None:
        if self._container is not None:
            return self._container
        try:
            from zoho_auth.container import ServiceContainer

            self._container = ServiceContainer()
            return self._container
        except Exception:
            _log.warning("Zoho auth not configured; mail sending disabled")
            return None

    def _get_access_token(self) -> str | None:
        container = self._get_container()
        if container is None:
            return None
        try:
            session = container.auth_service.login_or_resume(open_browser=False)
            return session.tokens.access_token
        except Exception:
            _log.exception("Failed to obtain Zoho access token for Mail API")
            return None

    @property
    def _api_base(self) -> str:
        return self._settings.zoho_mail_api_base.rstrip("/")

    async def _resolve_account_id(self, token: str) -> str | None:
        configured = (self._settings.zoho_mail_account_id or "").strip()
        if configured:
            return configured

        if self._account_id_cache:
            return self._account_id_cache

        from_addr = self._settings.zoho_mail_from_address.strip().lower()
        if not from_addr:
            _log.error("ZOHO_MAIL_FROM_ADDRESS is not set")
            return None

        url = f"{self._api_base}/accounts"
        headers = {
            "Authorization": f"Zoho-oauthtoken {token}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code >= 400:
                    _log.error("Zoho Mail list accounts failed %s: %s", resp.status_code, resp.text)
                    if "INVALID_OAUTHSCOPE" in resp.text:
                        _log.error(
                            "Re-authorize with Mail scopes: update ZOHO_SCOPE in zoho_auth/.env "
                            "then run: python -m zoho_auth (browser login required)"
                        )
                    return None
                payload = resp.json()
        except Exception:
            _log.exception("Zoho Mail list accounts request failed")
            return None

        for account in payload.get("data") or []:
            candidates: list[str] = []
            primary = account.get("primaryEmailAddress")
            if primary:
                candidates.append(str(primary))
            mailbox = account.get("mailboxAddress")
            if mailbox:
                candidates.append(str(mailbox))
            for entry in account.get("emailAddress") or []:
                mail_id = entry.get("mailId")
                if mail_id:
                    candidates.append(str(mail_id))
            if any(c.strip().lower() == from_addr for c in candidates):
                account_id = str(account.get("accountId") or "")
                if account_id:
                    self._account_id_cache = account_id
                    return account_id

        _log.error(
            "No Zoho Mail account found for from address %s; set ZOHO_MAIL_ACCOUNT_ID",
            from_addr,
        )
        return None

    async def send(self, msg: EmailMessage) -> bool:
        if not msg.to:
            return False

        from_addr = self._settings.zoho_mail_from_address.strip()
        if not from_addr:
            _log.error("ZOHO_MAIL_FROM_ADDRESS is not configured")
            return False

        token = self._get_access_token()
        if not token:
            return False

        account_id = await self._resolve_account_id(token)
        if not account_id:
            return False

        body: dict[str, Any] = {
            "fromAddress": from_addr,
            "toAddress": ",".join(msg.to),
            "subject": msg.subject,
            "content": msg.text_body,
            "mailFormat": "html" if msg.html_body else "plaintext",
            "askReceipt": "no",
        }
        if msg.html_body:
            body["content"] = msg.html_body

        url = f"{self._api_base}/accounts/{account_id}/messages"
        headers = {
            "Authorization": f"Zoho-oauthtoken {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, headers=headers, json=body)
                if resp.status_code >= 400:
                    _log.error(
                        "Zoho Mail send failed %s: %s (to=%s)",
                        resp.status_code,
                        resp.text,
                        msg.to,
                    )
                    if "INVALID_OAUTHSCOPE" in resp.text:
                        _log.error(
                            "Re-authorize with Mail scopes: python -m zoho_auth "
                            "(after ZOHO_SCOPE includes ZohoMail.messages.CREATE)"
                        )
                    return False
                _log.info("Zoho Mail sent to %s subject=%r", msg.to, msg.subject)
                return True
        except Exception:
            _log.exception("Zoho Mail send request failed (to=%s)", msg.to)
            return False


def get_zoho_mail_sender() -> ZohoMailSender:
    return ZohoMailSender(get_settings())
