"""Role access + usage report (JSON data and PDF export)."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from backend.auth.deps import UserContext
from backend.database import Database
from backend.exceptions import ForbiddenError
from backend.services.role_nav_permissions import (
    DEPRECATED_NAV_KEYS,
    NAV_PERMISSION_CATALOG,
    list_roles_with_nav_permissions,
)

_NAV_LABELS = {item["key"]: item["label"] for item in NAV_PERMISSION_CATALOG}

_ROLE_DISPLAY = {
    "SUPER_ADMIN": "Super Admin",
    "ORGANIZATION_ADMIN": "Organization Admin",
    "ACCOUNT_MANAGER": "Account Manager",
    "SUPERVISOR": "Supervisor",
    "DEVELOPER": "Developer",
    "AGENT": "Agent",
}


def _display_role(name: str) -> str:
    return _ROLE_DISPLAY.get(name, name)


def _page_label(key: str) -> str:
    return _NAV_LABELS.get(key, key)


def _resolve_org_id(user: UserContext, organization_id: int | None) -> int | None:
    if user.is_super_admin:
        return organization_id
    if organization_id is not None and organization_id != user.organization_id:
        raise ForbiddenError("Cannot view report for another organization")
    return user.organization_id


def _org_sql_filter(column: str, org_id: int | None) -> tuple[str, dict[str, Any]]:
    if org_id is None:
        return "", {}
    return f" AND {column} = :org_id", {"org_id": org_id}


async def _org_name(db: Database, org_id: int | None) -> str:
    if org_id is None:
        return "All organizations"
    row = await db.fetch_one("SELECT name FROM AIVA_organizations WHERE id = :id", {"id": org_id})
    return str(row["name"]) if row else f"Organization #{org_id}"


async def build_role_report(db: Database, user: UserContext, organization_id: int | None = None) -> dict:
    org_id = _resolve_org_id(user, organization_id)
    org_name = await _org_name(db, org_id)
    roles = await list_roles_with_nav_permissions(db)

    u_filter, u_params = _org_sql_filter("u.organization_id", org_id)
    t_filter, t_params = _org_sql_filter("t.organization_id", org_id)
    user_on_org = f" AND u.organization_id = :org_id" if org_id is not None else ""

    user_counts = await db.fetch_all(
        f"""
        SELECT r.name AS role_name, COUNT(DISTINCT u.id) AS user_count
        FROM AIVA_roles r
        LEFT JOIN AIVA_user_roles ur ON ur.role_id = r.id AND ur.account_id IS NULL
        LEFT JOIN AIVA_users u ON u.id = ur.user_id AND u.status = 'ACTIVE'{user_on_org}
        GROUP BY r.name
        ORDER BY r.name
        """,
        u_params,
    )
    user_count_by_role = {str(r["role_name"]): int(r["user_count"] or 0) for r in user_counts}

    chat_rows = await db.fetch_all(
        f"""
        SELECT r.name AS role_name, COUNT(DISTINCT cs.id) AS cnt
        FROM AIVA_chat_sessions cs
        JOIN AIVA_users u ON u.id = cs.user_id
        JOIN AIVA_user_roles ur ON ur.user_id = u.id AND ur.account_id IS NULL
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE 1=1 {u_filter}
        GROUP BY r.name
        """,
        u_params,
    )
    chat_by_role = {str(r["role_name"]): int(r["cnt"] or 0) for r in chat_rows}

    msg_rows = await db.fetch_all(
        f"""
        SELECT r.name AS role_name, COUNT(cm.id) AS cnt
        FROM AIVA_chat_messages cm
        JOIN AIVA_chat_sessions cs ON cs.id = cm.session_id
        JOIN AIVA_users u ON u.id = cs.user_id
        JOIN AIVA_user_roles ur ON ur.user_id = u.id AND ur.account_id IS NULL
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE 1=1 {u_filter}
        GROUP BY r.name
        """,
        u_params,
    )
    messages_by_role = {str(r["role_name"]): int(r["cnt"] or 0) for r in msg_rows}

    ai_rows = await db.fetch_all(
        f"""
        SELECT r.name AS role_name, COUNT(ar.id) AS cnt
        FROM AIVA_ai_requests ar
        JOIN AIVA_chat_sessions cs ON cs.id = ar.session_id
        JOIN AIVA_users u ON u.id = cs.user_id
        JOIN AIVA_user_roles ur ON ur.user_id = u.id AND ur.account_id IS NULL
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE 1=1 {u_filter}
        GROUP BY r.name
        """,
        u_params,
    )
    ai_by_role = {str(r["role_name"]): int(r["cnt"] or 0) for r in ai_rows}

    ticket_rows = await db.fetch_all(
        f"""
        SELECT r.name AS role_name, COUNT(t.id) AS cnt
        FROM AIVA_tickets t
        JOIN AIVA_users u ON u.id = t.created_by
        JOIN AIVA_user_roles ur ON ur.user_id = u.id AND ur.account_id IS NULL
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE 1=1 {t_filter}
        GROUP BY r.name
        """,
        t_params,
    )
    tickets_by_role = {str(r["role_name"]): int(r["cnt"] or 0) for r in ticket_rows}

    extra_rows = await db.fetch_all(
        f"""
        SELECT u.email, r.name AS role_name, unp.nav_key
        FROM AIVA_user_nav_permissions unp
        JOIN AIVA_users u ON u.id = unp.user_id
        JOIN AIVA_user_roles ur ON ur.user_id = u.id AND ur.account_id IS NULL
        JOIN AIVA_roles r ON r.id = ur.role_id
        WHERE 1=1 {u_filter}
        ORDER BY r.name, u.email, unp.nav_key
        """,
        u_params,
    )
    extras_by_user: dict[tuple[str, str], list[str]] = {}
    for row in extra_rows:
        nav_key = str(row["nav_key"])
        if nav_key in DEPRECATED_NAV_KEYS:
            continue
        key = (str(row["email"]), str(row["role_name"]))
        extras_by_user.setdefault(key, []).append(nav_key)

    role_rows = []
    for role in roles:
        name = str(role["name"])
        role_rows.append(
            {
                "role_name": name,
                "display_name": _display_role(name),
                "user_count": user_count_by_role.get(name, 0),
                "nav_permissions": [
                    {"key": k, "label": _page_label(k)} for k in role["nav_permissions"]
                ],
                "usage": {
                    "chat_sessions": chat_by_role.get(name, 0),
                    "chat_messages": messages_by_role.get(name, 0),
                    "ai_requests": ai_by_role.get(name, 0),
                    "tickets_created": tickets_by_role.get(name, 0),
                },
            }
        )

    individual_extras = [
        {
            "email": email,
            "role_name": role_name,
            "display_role": _display_role(role_name),
            "extra_pages": [{"key": k, "label": _page_label(k)} for k in pages],
        }
        for (email, role_name), pages in sorted(extras_by_user.items())
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": user.email,
        "organization_id": org_id,
        "organization_name": org_name,
        "roles": role_rows,
        "individual_extras": individual_extras,
    }


def build_role_report_pdf(report: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        title="GoChat247 Role Report",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Heading1"],
        fontSize=16,
        textColor=colors.HexColor("#004080"),
        spaceAfter=8,
    )
    heading_style = ParagraphStyle(
        "SectionHeading",
        parent=styles["Heading2"],
        fontSize=12,
        textColor=colors.HexColor("#004080"),
        spaceBefore=14,
        spaceAfter=6,
    )
    body_style = styles["Normal"]

    story: list[Any] = []
    story.append(Paragraph("GoChat247 — Roles Report", title_style))
    story.append(
        Paragraph(
            f"<b>Organization:</b> {report['organization_name']}<br/>"
            f"<b>Generated:</b> {report['generated_at'][:19].replace('T', ' ')} UTC<br/>"
            f"<b>Generated by:</b> {report['generated_by']}",
            body_style,
        )
    )
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("1. Page access by role", heading_style))
    access_data = [["Role", "Users", "Pages"]]
    for role in report["roles"]:
        pages = ", ".join(p["label"] for p in role["nav_permissions"]) or "—"
        access_data.append(
            [role["display_name"], str(role["user_count"]), pages]
        )
    access_table = Table(access_data, colWidths=[3.2 * cm, 1.5 * cm, 12.3 * cm])
    access_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#004080")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]
        )
    )
    story.append(access_table)

    story.append(Paragraph("2. Usage by role", heading_style))
    usage_data = [
        ["Role", "Users", "Chat sessions", "Messages", "AI requests", "Tickets"]
    ]
    for role in report["roles"]:
        u = role["usage"]
        usage_data.append(
            [
                role["display_name"],
                str(role["user_count"]),
                str(u["chat_sessions"]),
                str(u["chat_messages"]),
                str(u["ai_requests"]),
                str(u["tickets_created"]),
            ]
        )
    usage_table = Table(
        usage_data,
        colWidths=[3.2 * cm, 1.5 * cm, 2.5 * cm, 2.2 * cm, 2.5 * cm, 2.0 * cm],
    )
    usage_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#004080")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ALIGN", (1, 1), (-1, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]
        )
    )
    story.append(usage_table)

    story.append(Paragraph("3. Individual extra page access", heading_style))
    if report["individual_extras"]:
        extra_data = [["Email", "Role", "Extra pages"]]
        for row in report["individual_extras"]:
            extra_data.append(
                [
                    row["email"],
                    row["display_role"],
                    ", ".join(p["label"] for p in row["extra_pages"]),
                ]
            )
        extra_table = Table(extra_data, colWidths=[5.5 * cm, 3.0 * cm, 8.5 * cm])
        extra_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#004080")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ]
            )
        )
        story.append(extra_table)
    else:
        story.append(Paragraph("No users with individual extra page grants.", body_style))

    doc.build(story)
    return buffer.getvalue()
