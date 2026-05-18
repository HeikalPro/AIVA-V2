from __future__ import annotations

from backend.database import Database


async def organization_delete_blockers(db: Database, org_id: int) -> list[str]:
    blockers: list[str] = []

    user_row = await db.fetch_one(
        "SELECT COUNT(*) AS cnt FROM AIVA_users WHERE organization_id = :org_id",
        {"org_id": org_id},
    )
    user_count = int(user_row["cnt"]) if user_row else 0
    if user_count:
        blockers.append(f"{user_count} user(s)")

    account_row = await db.fetch_one(
        "SELECT COUNT(*) AS cnt FROM AIVA_accounts WHERE organization_id = :org_id",
        {"org_id": org_id},
    )
    account_count = int(account_row["cnt"]) if account_row else 0
    if account_count:
        blockers.append(f"{account_count} account(s)")

    ticket_row = await db.fetch_one(
        "SELECT COUNT(*) AS cnt FROM AIVA_tickets WHERE organization_id = :org_id",
        {"org_id": org_id},
    )
    ticket_count = int(ticket_row["cnt"]) if ticket_row else 0
    if ticket_count:
        blockers.append(f"{ticket_count} ticket(s)")

    return blockers
