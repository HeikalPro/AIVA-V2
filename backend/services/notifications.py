from __future__ import annotations

import asyncio
import logging
import re
import time

from backend.auth.role_constants import (
    ROLE_DEVELOPER,
    ROLE_SUPER_ADMIN,
)
from backend.config import get_settings
from backend.dependencies import get_db
from backend.schemas.notifications import DeveloperNotifyOut
from backend.services.email import get_mail_sender
from backend.services.email.templates import build_message

_log = logging.getLogger(__name__)

# Roles that receive server-error alerts (super admins + developers only).
ERROR_ALERT_ROLES = (ROLE_SUPER_ADMIN, ROLE_DEVELOPER)


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


async def _error_alert_recipients(organization_id: int | None) -> list[str]:
    """Alert recipients: the org the error belongs to, plus the platform org.

    Errors that can't be mapped to an org (anonymous traffic, or a widget whose
    corpus matches no account) fall back to the platform org instead of querying
    unscoped — otherwise one tenant's staff would receive another tenant's traces.
    Leaving ``notify_platform_org_id`` unset keeps the old unscoped behaviour.
    """
    platform_org_id = get_settings().notify_platform_org_id
    org_ids: list[int | None] = []
    if organization_id is not None:
        org_ids.append(organization_id)
    if platform_org_id is not None and platform_org_id != organization_id:
        org_ids.append(platform_org_id)
    if not org_ids:
        org_ids.append(None)

    emails: list[str] = []
    for org_id in org_ids:
        for email in await fetch_role_emails(ERROR_ALERT_ROLES, organization_id=org_id):
            if email not in emails:
                emails.append(email)
    return emails


async def _send_alert(
    *,
    subject: str,
    preheader: str,
    eyebrow: str,
    content: dict,
    organization_id: int | None,
) -> DeveloperNotifyOut:
    """Resolve recipients, send one alert email, and report what happened. Never raises."""
    try:
        recipients = await _error_alert_recipients(organization_id)
    except Exception:
        _log.exception("Failed to resolve alert recipients")
        return DeveloperNotifyOut(status="failed", message="Could not resolve recipients.")

    if not recipients:
        _log.warning("No admin/developer recipients for alert %r (org=%s)", subject, organization_id)
        return DeveloperNotifyOut(
            status="no_recipients",
            message="No active admins or developers to email.",
        )

    msg = build_message(to=recipients, subject=subject, preheader=preheader, eyebrow=eyebrow, **content)
    try:
        ok = await get_mail_sender().send(msg)
    except Exception:
        _log.exception("Failed to send alert email %r", subject)
        ok = False

    if ok:
        _log.info("Sent alert %r to %s", subject, ", ".join(recipients))
        return DeveloperNotifyOut(
            status="sent",
            message=f"Error alert sent to: {', '.join(recipients)}",
            recipients=recipients,
        )
    return DeveloperNotifyOut(status="failed", message="Error alert email was not sent.", recipients=recipients)


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

    content = {
        "title": f"New support ticket #{ticket_id}",
        "intro": (
            "A new support ticket has been raised in AIVA and is awaiting review by "
            "the development team. The details are summarised below."
        ),
        "details": [
            ("Ticket ID", f"#{ticket_id}"),
            ("Subject", subject),
            ("Account", account or "—"),
            ("Created by", creator),
        ],
        "block_label": "Description" if desc_preview else None,
        "block_text": desc_preview or None,
        "cta_label": "Open ticket in AIVA",
        "cta_url": link,
    }

    msg = build_message(
        to=recipients,
        subject=f"[AIVA] New ticket #{ticket_id}: {subject}",
        preheader=f"Ticket #{ticket_id}: {subject}",
        eyebrow="Support",
        **content,
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

    content = {
        "title": f"New ingestion request #{request_id}",
        "intro": (
            "A new knowledge-base ingestion request has been submitted in AIVA and "
            "is awaiting processing. The details are summarised below."
        ),
        "details": [
            ("Request ID", f"#{request_id}"),
            ("Type", request_type or "—"),
            ("Account", account_name or "—"),
            ("Requested by", creator),
        ],
        "block_label": "Knowledge-base description" if desc_preview else None,
        "block_text": desc_preview or None,
        "cta_label": "Open ingestion in AIVA",
        "cta_url": link,
    }

    msg = build_message(
        to=recipients,
        subject=f"[AIVA] New ingestion request #{request_id}",
        preheader=f"Ingestion request #{request_id} awaiting processing",
        eyebrow="Knowledge base",
        **content,
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

    link = _frontend_link("/logs")
    where = f"{http_method or ''} {path or route}".strip()
    trace_preview = (stack_trace or "").strip()
    if len(trace_preview) > 4000:
        trace_preview = trace_preview[:4000] + "\n... (truncated)"

    content = {
        "title": "Unhandled server error",
        "intro": (
            "An unhandled error was recorded on the AIVA server. Please review the "
            "details below and investigate at your earliest convenience."
        ),
        "details": [
            ("Type", exception_type),
            ("Message", exception_message or "—"),
            ("Where", where or "—"),
            ("Status", str(status_code or "—")),
            ("Request ID", request_id or "—"),
            ("User", user_email or "anonymous"),
        ],
        "block_label": "Stack trace" if trace_preview else None,
        "block_text": trace_preview or None,
        "cta_label": "Open error logs in AIVA",
        "cta_url": link,
    }

    return await _send_alert(
        subject=f"[AIVA] Error: {exception_type} at {route}",
        preheader=f"{exception_type} at {route}",
        eyebrow="System alert",
        content=content,
        organization_id=organization_id,
    )


_URL_HOST_RE = re.compile(r"https?://([^/\s'\"]+)")
_STATUS_RE = re.compile(r"\b([45]\d{2})\b")


def _widget_error_label(error_message: str | None) -> str:
    """Short, stable label for a widget failure — used in the subject and as the
    throttle key, so an embeddings 503 and an LLM 401 stay separate buckets."""
    text = (error_message or "").strip()
    if not text:
        return "Widget failure"
    status = _STATUS_RE.search(text)
    host = _URL_HOST_RE.search(text)
    if status and host:
        return f"{status.group(1)} from {host.group(1)}"
    if host:
        return f"Failure calling {host.group(1)}"
    if status:
        return f"HTTP {status.group(1)}"
    return text.splitlines()[0][:60]


async def notify_widget_failure(
    *,
    corpus_id: str | None,
    account_id: int | None,
    account_name: str | None,
    organization_id: int | None,
    query_text: str | None,
    error_message: str | None,
    stage: str = "Knowledge base retrieval",
    force: bool = False,
) -> DeveloperNotifyOut:
    """Email admins + developers when a customer-facing widget turn fails.

    These are degraded turns rather than crashes — the visitor asked something and
    got no answer — so they have their own switch (``notify_widget_errors_enabled``)
    and can be muted without losing server-error alerts.
    """
    settings = get_settings()
    if not force and not settings.notify_widget_errors_enabled:
        return DeveloperNotifyOut(
            status="disabled",
            message="Widget failure notifications are turned off in server settings.",
        )

    label = _widget_error_label(error_message)
    route = f"widget:{corpus_id or 'unknown'}"
    if not force and _error_alert_throttled(label, route, settings.notify_errors_throttle_seconds):
        _log.info(
            "Throttled widget alert for %s on %s (within %ss window)",
            label,
            route,
            settings.notify_errors_throttle_seconds,
        )
        return DeveloperNotifyOut(status="disabled", message="Throttled duplicate widget alert.")

    question = (query_text or "").strip()
    if len(question) > 500:
        question = question[:500] + "..."
    detail = (error_message or "").strip().splitlines()
    detail_line = detail[0][:300] if detail else "—"

    account_label = account_name or (f"#{account_id}" if account_id is not None else "—")
    content = {
        "title": "Widget chat failure",
        "intro": (
            "A chat turn failed in the AIVA widget, so the visitor did not get an "
            "answer. The details are summarised below."
        ),
        "details": [
            ("Stage", stage),
            ("Account", account_label),
            ("Corpus", corpus_id or "—"),
            ("Error", detail_line),
        ],
        "block_label": "Customer question" if question else None,
        "block_text": question or None,
        "cta_label": "Open error logs in AIVA",
        "cta_url": _frontend_link("/logs"),
    }

    return await _send_alert(
        subject=f"[AIVA] Widget failure: {label}",
        preheader=f"{stage} failed for {account_label}",
        eyebrow="Widget alert",
        content=content,
        organization_id=organization_id,
    )


# Strong references to fire-and-forget alert tasks so they aren't GC'd mid-flight.
_alert_tasks: set[asyncio.Task] = set()


def _spawn_alert(coro) -> None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:  # No running loop (shouldn't happen inside a request).
        coro.close()
        return
    _alert_tasks.add(task)
    task.add_done_callback(_alert_tasks.discard)


def schedule_error_alert(**kwargs) -> None:
    """Send the admin/developer error alert without delaying the caller's response."""
    _spawn_alert(notify_error_admins_developers(**kwargs))


def schedule_widget_failure_alert(**kwargs) -> None:
    """Send the widget-failure alert without delaying the widget's logging ack."""
    _spawn_alert(notify_widget_failure(**kwargs))
