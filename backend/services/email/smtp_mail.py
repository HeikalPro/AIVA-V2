from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from backend.config import Settings, get_settings
from backend.services.email.base import EmailMessage

_log = logging.getLogger(__name__)


class SmtpMailSender:
    """Send transactional mail via SMTP (e.g. Resend)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def _is_configured(self) -> bool:
        s = self._settings
        return bool(s.smtp_host and s.smtp_password and s.smtp_from_email)

    def _send_sync(self, msg: EmailMessage) -> bool:
        s = self._settings
        if not self._is_configured():
            _log.error("SMTP is not configured (SMTP_HOST, SMTP_PASSWORD, SMTP_FROM_EMAIL)")
            return False

        mime = MIMEMultipart("alternative")
        mime["Subject"] = msg.subject
        mime["From"] = f"{s.smtp_from_name} <{s.smtp_from_email}>"
        mime["To"] = ", ".join(msg.to)
        mime.attach(MIMEText(msg.text_body, "plain", "utf-8"))
        if msg.html_body:
            mime.attach(MIMEText(msg.html_body, "html", "utf-8"))

        encryption = (s.smtp_encryption or "tls").lower()
        try:
            if encryption == "ssl":
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=context) as server:
                    if s.smtp_username:
                        server.login(s.smtp_username, s.smtp_password)
                    server.sendmail(s.smtp_from_email, msg.to, mime.as_string())
            else:
                with smtplib.SMTP(s.smtp_host, s.smtp_port) as server:
                    server.ehlo()
                    if encryption == "tls":
                        context = ssl.create_default_context()
                        server.starttls(context=context)
                        server.ehlo()
                    if s.smtp_username:
                        server.login(s.smtp_username, s.smtp_password)
                    server.sendmail(s.smtp_from_email, msg.to, mime.as_string())
            _log.info("SMTP sent to %s subject=%r", msg.to, msg.subject)
            return True
        except Exception:
            _log.exception("SMTP send failed (to=%s)", msg.to)
            return False

    async def send(self, msg: EmailMessage) -> bool:
        return await asyncio.to_thread(self._send_sync, msg)


_smtp_sender: SmtpMailSender | None = None


def get_smtp_mail_sender() -> SmtpMailSender:
    global _smtp_sender
    if _smtp_sender is None:
        _smtp_sender = SmtpMailSender()
    return _smtp_sender
