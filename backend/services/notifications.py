from __future__ import annotations

import html
import logging

from backend.auth.deps import ROLE_DEVELOPER
from backend.config import get_settings
from backend.dependencies import get_db
from backend.schemas.tickets import DeveloperNotifyOut
from backend.schemas.notifications import DeveloperNotifyOut
from backend.services.email.base import EmailMessage
from backend.services.email.zoho_mail import get_zoho_mail_sender

_log = logging.getLogger(__name__)


async def fetch_developer_emails(
    organization_id: int,
    *,
    exclude_user_id: int | None = None,
) -> list[str]:
    db = get_db()
    rows = await db.fetch_all(
        """
        SELECT DISTINCT u.id, u.email
        FROM AIVA_users u
        JOIN AIVA_user_roles ur ON ur.user_id = u.id
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE u.organization_id = :org_id
          AND u.status = 'ACTIVE'
          AND r.name = :role
        """,
        {"org_id": organization_id, "role": ROLE_DEVELOPER},
    )
    emails: list[str] = []
    for row in rows:
        if exclude_user_id is not None and int(row["id"]) == exclude_user_id:
            continue
        email = str(row.get("email") or "").strip()
        if email and email not in emails:
            emails.append(email)
    return emails


async def _creator_display_name(user_id: int) -> str:
    db = get_db()
    row = await db.fetch_one(
        "SELECT first_name, last_name, email FROM AIVA_users WHERE id = :id",
        {"id": user_id},
    )
    if not row:
        return "AIVA user"
    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    return name or str(row.get("email") or "AIVA user")


async def _account_name(account_id: int | None) -> str | None:
    if account_id is None:
        return None
    db = get_db()
    row = await db.fetch_one("SELECT name FROM AIVA_accounts WHERE id = :id", {"id": account_id})
    return str(row["name"]) if row and row.get("name") else None


def _frontend_link(path: str) -> str:
    base = get_settings().frontend_url.rstrip("/")
    return f"{base}{path}"


async def notify_developers_new_ticket(
    *,
    organization_id: int,
    ticket_id: int,
    subject: str,
    description: str | None,
    account_id: int | None,
    created_by_user_id: int,
) -> DeveloperNotifyOut:
    settings = get_settings()
    if not settings.notify_developers_enabled:
        return DeveloperNotifyOut(
            status="disabled",
            message="Developer email notifications are turned off in server settings.",
        )

    exclude_id = created_by_user_id if settings.notify_skip_creator else None
    recipients = await fetch_developer_emails(organization_id, exclude_user_id=exclude_id)
    if not recipients:
        _log.warning(
            "No developer recipients for ticket #%s (org %s); assign DEVELOPER role in same org",
            ticket_id,
            organization_id,
        )
        return DeveloperNotifyOut(
            status="no_recipients",
            message="No active developers in this organization to email.",
        )
    _log.info("Notifying developers %s for ticket #%s", recipients, ticket_id)

    creator = await _creator_display_name(created_by_user_id)
    account = await _account_name(account_id)
    link = _frontend_link("/tickets")
    desc_preview = (description or "").strip()
    if len(desc_preview) > 500:
        desc_preview = desc_preview[:500] + "..."

    text = (
        f"A new support ticket was created in AIVA.\n\n"
        f"Ticket ID: {ticket_id}\n"
        f"Subject: {subject}\n"
        f"Account: {account or '—'}\n"
        f"Created by: {creator}\n"
    )
    if desc_preview:
        text += f"\nDescription:\n{desc_preview}\n"
    text += f"\nOpen AIVA: {link}\n"

    html_body = (
        f"<p>A new support ticket was created in <strong>AIVA</strong>.</p>"
        f"<ul>"
        f"<li><strong>Ticket ID:</strong> {ticket_id}</li>"
        f"<li><strong>Subject:</strong> {html.escape(subject)}</li>"
        f"<li><strong>Account:</strong> {html.escape(account or '—')}</li>"
        f"<li><strong>Created by:</strong> {html.escape(creator)}</li>"
        f"</ul>"
    )
    if desc_preview:
        safe = html.escape(desc_preview).replace("\n", "<br>")
        html_body += f"<p><strong>Description:</strong><br>{safe}</p>"
    html_body += f'<p><a href="{html.escape(link)}">Open tickets in AIVA</a></p>'

    msg = EmailMessage(
        to=recipients,
        subject=f"[AIVA] New ticket #{ticket_id}: {subject}",
        text_body=text,
        html_body=html_body,
    )
    try:
        ok = await get_zoho_mail_sender().send(msg)
    except Exception:
        _log.exception("Failed to notify developers about ticket #%s", ticket_id)
        ok = False

    if ok:
        joined = ", ".join(recipients)
        return DeveloperNotifyOut(
            status="sent",
            message=f"Email sent to developer(s): {joined}",
            recipients=recipients,
        )
    return DeveloperNotifyOut(
        status="failed",
        message=(
            "Email was not sent. Configure ZOHO_MAIL_FROM_ADDRESS to a real Zoho mailbox, "
            "then authorize the server once: cd AIVA-V2 && python -m zoho_auth --force-login "
            "(accept Mail permissions). Web sign-in does not update the mail sender token."
        ),
        recipients=recipients,
    )


async def notify_developers_new_ingestion(
    *,
    organization_id: int,
    request_id: int,
    request_type: str | None,
    description: str | None,
    account_name: str | None,
    created_by_user_id: int,
) -> DeveloperNotifyOut:
    settings = get_settings()
    if not settings.notify_developers_enabled:
        return DeveloperNotifyOut(
            status="disabled",
            message="Developer email notifications are turned off in server settings.",
        )

    exclude_id = created_by_user_id if settings.notify_skip_creator else None
    recipients = await fetch_developer_emails(organization_id, exclude_user_id=exclude_id)
    if not recipients:
        _log.warning(
            "No developer recipients for ingestion #%s (org %s); assign DEVELOPER role in same org",
            request_id,
            organization_id,
        )
        return DeveloperNotifyOut(
            status="no_recipients",
            message="No active developers in this organization to email.",
        )
    _log.info("Notifying developers %s for ingestion #%s", recipients, request_id)

    creator = await _creator_display_name(created_by_user_id)
    link = _frontend_link("/ingestion")
    desc_preview = (description or "").strip()
    if len(desc_preview) > 500:
        desc_preview = desc_preview[:500] + "..."

    text = (
        f"A new knowledge-base ingestion request was created in AIVA.\n\n"
        f"Request ID: {request_id}\n"
        f"Type: {request_type or '—'}\n"
        f"Account: {account_name or '—'}\n"
        f"Requested by: {creator}\n"
    )
    if desc_preview:
        text += f"\nKB description:\n{desc_preview}\n"
    text += f"\nOpen AIVA: {link}\n"

    html_body = (
        f"<p>A new <strong>ingestion request</strong> was created in AIVA.</p>"
        f"<ul>"
        f"<li><strong>Request ID:</strong> {request_id}</li>"
        f"<li><strong>Type:</strong> {html.escape(request_type or '—')}</li>"
        f"<li><strong>Account:</strong> {html.escape(account_name or '—')}</li>"
        f"<li><strong>Requested by:</strong> {html.escape(creator)}</li>"
        f"</ul>"
    )
    if desc_preview:
        safe = html.escape(desc_preview).replace("\n", "<br>")
        html_body += f"<p><strong>KB description:</strong><br>{safe}</p>"
    html_body += f'<p><a href="{html.escape(link)}">Open ingestion in AIVA</a></p>'

    msg = EmailMessage(
        to=recipients,
        subject=f"[AIVA] New ingestion request #{request_id}",
        text_body=text,
        html_body=html_body,
    )
    try:
        ok = await get_zoho_mail_sender().send(msg)
    except Exception:
        _log.exception("Failed to notify developers about ingestion #%s", request_id)
        ok = False

    if ok:
        joined = ", ".join(recipients)
        return DeveloperNotifyOut(
            status="sent",
            message=f"Email sent to developer(s): {joined}",
            recipients=recipients,
        )
    return DeveloperNotifyOut(
        status="failed",
        message=(
            "Email was not sent. Configure ZOHO_MAIL_FROM_ADDRESS to a real Zoho mailbox, "
            "then authorize the server once: cd AIVA-V2 && python -m zoho_auth --force-login "
            "(accept Mail permissions). Web sign-in does not update the mail sender token."
        ),
        recipients=recipients,
    )
