from collections import defaultdict
from typing import Annotated

from fastapi import APIRouter, Depends

from backend.auth.deps import ROLE_SUPER_ADMIN, UserContext, require_roles
from backend.dependencies import DbDep
from backend.exceptions import ConflictError, NotFoundError
from backend.schemas.organizations import (
    OrganizationCreate,
    OrganizationDeleteResult,
    OrganizationDeleteSummary,
    OrganizationOut,
    OrganizationUpdate,
)
from backend.services.audit import write_audit_log
from backend.services.organization_dependencies import (
    delete_organization_cascade,
    organization_delete_summary,
)
from backend.utils import serialize_row

router = APIRouter(prefix="/organizations", tags=["organizations"])


async def _accounts_by_organization(db: DbDep) -> dict[int, list[str]]:
    rows = await db.fetch_all(
        """
        SELECT organization_id, name
        FROM AIVA_accounts
        ORDER BY organization_id, name
        """
    )
    grouped: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        grouped[int(row["organization_id"])].append(str(row["name"]))
    return grouped


def _to_organization_out(org_row: dict, account_names: list[str]) -> OrganizationOut:
    data = serialize_row(org_row) or {}
    return OrganizationOut(
        **data,
        account_count=len(account_names),
        account_names=account_names,
    )


@router.get("", response_model=list[OrganizationOut])
async def list_organizations(
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> list[OrganizationOut]:
    org_rows = await db.fetch_all("SELECT * FROM AIVA_organizations ORDER BY id")
    accounts_by_org = await _accounts_by_organization(db)
    return [
        _to_organization_out(org, accounts_by_org.get(int(org["id"]), []))
        for org in org_rows
    ]


@router.post("", response_model=OrganizationOut, status_code=201)
async def create_organization(
    body: OrganizationCreate,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> OrganizationOut:
    existing = await db.fetch_one(
        "SELECT id FROM AIVA_organizations WHERE code = :code",
        {"code": body.code},
    )
    if existing:
        raise ConflictError("Organization code already exists")

    org_id = await db.execute(
        """
        INSERT INTO AIVA_organizations (name, code, status)
        VALUES (:name, :code, :status)
        RETURNING id INTO :out_id
        """,
        {"name": body.name, "code": body.code, "status": body.status},
        return_id=True,
    )
    row = await db.fetch_one("SELECT * FROM AIVA_organizations WHERE id = :id", {"id": org_id})
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="organization",
        entity_id=int(org_id or 0),
        action_type="CREATE",
        new_value=body.model_dump(),
    )
    return OrganizationOut(**serialize_row(row) or {})


@router.get("/{org_id}/delete-preview", response_model=OrganizationDeleteSummary)
async def preview_delete_organization(
    org_id: int,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> OrganizationDeleteSummary:
    summary = await organization_delete_summary(db, org_id)
    if not summary:
        raise NotFoundError("Organization not found")
    return OrganizationDeleteSummary(**summary)


@router.get("/{org_id}", response_model=OrganizationOut)
async def get_organization(
    org_id: int,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> OrganizationOut:
    row = await db.fetch_one("SELECT * FROM AIVA_organizations WHERE id = :id", {"id": org_id})
    if not row:
        raise NotFoundError("Organization not found")
    accounts_by_org = await _accounts_by_organization(db)
    return _to_organization_out(row, accounts_by_org.get(org_id, []))


@router.patch("/{org_id}", response_model=OrganizationOut)
async def update_organization(
    org_id: int,
    body: OrganizationUpdate,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> OrganizationOut:
    old = await db.fetch_one("SELECT * FROM AIVA_organizations WHERE id = :id", {"id": org_id})
    if not old:
        raise NotFoundError("Organization not found")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return OrganizationOut(**serialize_row(old) or {})

    set_parts = [f"{k} = :{k}" for k in updates]
    updates["id"] = org_id
    await db.execute(
        f"UPDATE AIVA_organizations SET {', '.join(set_parts)}, updated_at = CURRENT_TIMESTAMP WHERE id = :id",
        updates,
    )
    row = await db.fetch_one("SELECT * FROM AIVA_organizations WHERE id = :id", {"id": org_id})
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="organization",
        entity_id=org_id,
        action_type="UPDATE",
        old_value=serialize_row(old),
        new_value=updates,
    )
    return OrganizationOut(**serialize_row(row) or {})


@router.delete("/{org_id}", response_model=OrganizationDeleteResult)
async def delete_organization(
    org_id: int,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> OrganizationDeleteResult:
    old = await db.fetch_one("SELECT * FROM AIVA_organizations WHERE id = :id", {"id": org_id})
    if not old:
        raise NotFoundError("Organization not found")

    summary = await organization_delete_summary(db, org_id)
    stats = await delete_organization_cascade(db, org_id)
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="organization",
        entity_id=org_id,
        action_type="DELETE",
        old_value={**(serialize_row(old) or {}), "cascade_summary": summary},
        new_value=stats,
    )
    return OrganizationDeleteResult(
        message=f"Organization '{old['name']}' and all related data were permanently deleted.",
        accounts_deleted=stats["accounts_deleted"],
        users_deleted=stats["users_deleted"],
        tickets_deleted=stats["tickets_deleted"],
    )
