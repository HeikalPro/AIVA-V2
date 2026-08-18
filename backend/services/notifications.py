from __future__ import annotations

import html
import logging
import time

from backend.auth.role_constants import (
    ROLE_DEVELOPER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
)
from backend.config import get_settings
from backend.dependencies import get_db
from backend.schemas.notifications import DeveloperNotifyOut
from backend.services.email.base import EmailMessage
from backend.services.email import get_mail_sender

_log = logging.getLogger(__name__)

# Roles that receive server-error alerts.
ERROR_ALERT_ROLES = (ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_DEVELOPER)


async def fetch_role_emails(
    roles: list[str] | tuple[str, ...],
    *,
    organization_id: int | None = None,
    exclude_user_id: int | None = None,
) -> list[str]:
    """Active users' emails for the given role name(s), optionally scoped to an org."""
    if not roles:
        return []
    db = get_db()
    role_binds = {f"role{i}": name for i, name in enumerate(roles)}
    role_placeholders = ", ".join(f":{k}" for k in role_binds)
    where = [
        "u.status = 'ACTIVE'",
        f"r.name IN ({role_placeholders})",
    ]
    binds: dict[str, object] = dict(role_binds)
    if organization_id is not None:
        where.append("u.organization_id = :org_id")
        binds["org_id"] = organization_id
    rows = await db.fetch_all(
        f"""
        SELECT DISTINCT u.id, u.email
        FROM AIVA_users u
        JOIN AIVA_user_roles ur ON ur.user_id = u.id
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE {" AND ".join(where)}
        """,
        binds,
    )
    emails: list[str] = []
    for row in rows:
        if exclude_user_id is not None and int(row["id"]) == exclude_user_id:
            continue
        email = str(row.get("email") or "").strip()
        if email and email not in emails:
            emails.append(email)
    return emails


async def fetch_developer_emails(
    organization_id: int,
    *,
    exclude_user_id: int | None = None,
) -> list[str]:
    return await fetch_role_emails(
        [ROLE_DEVELOPER],
        organization_id=organization_id,
        exclude_user_id=exclude_user_id,
    )


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
        ok = await get_mail_sender().send(msg)
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
            "Email was not sent. Configure SMTP_HOST, SMTP_PASSWORD, and SMTP_FROM_EMAIL "
            "in backend/.env (or use Zoho Mail as fallback)."
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
        ok = await get_mail_sender().send(msg)
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
            "Email was not sent. Configure SMTP_HOST, SMTP_PASSWORD, and SMTP_FROM_EMAIL "
            "in backend/.env (or use Zoho Mail as fallback)."
        ),
        recipients=recipients,
    )


# Last-sent monotonic timestamp per (exception_type, route) — throttles error alerts
# so a crash loop can't flood inboxes.
_error_alert_last_sent: dict[tuple[str, str], float] = {}


def _error_alert_throttled(exception_type: str, route: str, window_seconds: int) -> bool:
    if window_seconds <= 0:
        return False
    key = (exception_type, route)
    now = time.monotonic()
    last = _error_alert_last_sent.get(key)
    if last is not None and (now - last) < window_seconds:
        return True
    _error_alert_last_sent[key] = now
    return False


async def notify_error_admins_developers(
    *,
    exception_type: str,
    exception_message: str | None,
    stack_trace: str | None,
    http_method: str | None,
    path: str | None,
    route_template: str | None,
    status_code: int | None,
    request_id: str | None,
    user_email: str | None,
    organization_id: int | None,
    force: bool = False,
) -> DeveloperNotifyOut:
    """Email admins + developers about an unhandled server error. Never raises.

    ``force=True`` (used by the "send test alert" button) bypasses both the
    ``notify_errors_enabled`` switch and the duplicate throttle so the mail is
    always attempted.
    """
    settings = get_settings()
    if not force and not settings.notify_errors_enabled:
        return DeveloperNotifyOut(
            status="disabled",
            message="Error email notifications are turned off in server settings.",
        )

    route = route_template or path or "-"
    if not force and _error_alert_throttled(exception_type, route, settings.notify_errors_throttle_seconds):
        _log.info(
            "Throttled error alert for %s at %s (within %ss window)",
            exception_type,
            route,
            settings.notify_errors_throttle_seconds,
        )
        return DeveloperNotifyOut(status="disabled", message="Throttled duplicate error alert.")

    try:
        recipients = await fetch_role_emails(ERROR_ALERT_ROLES, organization_id=organization_id)
    except Exception:
        _log.exception("Failed to resolve error-alert recipients")
        return DeveloperNotifyOut(status="failed", message="Could not resolve recipients.")

    if not recipients:
        _log.warning(
            "No admin/developer recipients for error alert (%s); org=%s",
            exception_type,
            organization_id,
        )
        return DeveloperNotifyOut(
            status="no_recipients",
            message="No active admins or developers to email.",
        )

    link = _frontend_link("/logs")
    where = f"{http_method or ''} {path or route}".strip()
    trace_preview = (stack_trace or "").strip()
    if len(trace_preview) > 4000:
        trace_preview = trace_preview[:4000] + "\n... (truncated)"

    text = (
        f"An unhandled error occurred in AIVA.\n\n"
        f"Type: {exception_type}\n"
        f"Message: {exception_message or '—'}\n"
        f"Where: {where or '—'}\n"
        f"Status: {status_code or '—'}\n"
        f"Request ID: {request_id or '—'}\n"
        f"User: {user_email or 'anonymous'}\n"
    )
    if trace_preview:
        text += f"\nStack trace:\n{trace_preview}\n"
    text += f"\nOpen error logs: {link}\n"

    html_body = (
        f"<p>An <strong>unhandled error</strong> occurred in AIVA.</p>"
        f"<ul>"
        f"<li><strong>Type:</strong> {html.escape(exception_type)}</li>"
        f"<li><strong>Message:</strong> {html.escape(exception_message or '—')}</li>"
        f"<li><strong>Where:</strong> {html.escape(where or '—')}</li>"
        f"<li><strong>Status:</strong> {status_code or '—'}</li>"
        f"<li><strong>Request ID:</strong> {html.escape(request_id or '—')}</li>"
        f"<li><strong>User:</strong> {html.escape(user_email or 'anonymous')}</li>"
        f"</ul>"
    )
    if trace_preview:
        safe = html.escape(trace_preview).replace("\n", "<br>")
        html_body += f"<p><strong>Stack trace:</strong></p><pre>{safe}</pre>"
    html_body += f'<p><a href="{html.escape(link)}">Open error logs in AIVA</a></p>'

    msg = EmailMessage(
        to=recipients,
        subject=f"[AIVA] Error: {exception_type} at {route}",
        text_body=text,
        html_body=html_body,
    )
    try:
        ok = await get_mail_sender().send(msg)
    except Exception:
        _log.exception("Failed to send error alert email")
        ok = False

    if ok:
        _log.info("Sent error alert to %s", ", ".join(recipients))
        return DeveloperNotifyOut(
            status="sent",
            message=f"Error alert sent to: {', '.join(recipients)}",
            recipients=recipients,
        )
    return DeveloperNotifyOut(status="failed", message="Error alert email was not sent.", recipients=recipients)
