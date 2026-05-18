from typing import Annotated

from fastapi import APIRouter, Depends

from backend.auth.deps import ROLE_SUPER_ADMIN, UserContext, require_roles
from backend.dependencies import DbDep
from backend.exceptions import ConflictError, NotFoundError
from backend.schemas.common import MessageResponse
from backend.schemas.organizations import OrganizationCreate, OrganizationOut, OrganizationUpdate
from backend.services.audit import write_audit_log
from backend.services.organization_dependencies import organization_delete_blockers
from backend.utils import serialize_row, serialize_rows

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.get("", response_model=list[OrganizationOut])
async def list_organizations(
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> list[OrganizationOut]:
    rows = await db.fetch_all("SELECT * FROM AIVA_organizations ORDER BY id")
    return [OrganizationOut(**serialize_row(r) or {}) for r in rows]


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


@router.get("/{org_id}", response_model=OrganizationOut)
async def get_organization(
    org_id: int,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> OrganizationOut:
    row = await db.fetch_one("SELECT * FROM AIVA_organizations WHERE id = :id", {"id": org_id})
    if not row:
        raise NotFoundError("Organization not found")
    return OrganizationOut(**serialize_row(row) or {})


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


@router.delete("/{org_id}", response_model=MessageResponse)
async def delete_organization(
    org_id: int,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> MessageResponse:
    old = await db.fetch_one("SELECT * FROM AIVA_organizations WHERE id = :id", {"id": org_id})
    if not old:
        raise NotFoundError("Organization not found")

    blockers = await organization_delete_blockers(db, org_id)
    if blockers:
        raise ConflictError(
            "Cannot delete organization while it still has: "
            + ", ".join(blockers)
            + ". Remove or reassign those records first."
        )

    await db.execute("DELETE FROM AIVA_organizations WHERE id = :id", {"id": org_id})
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="organization",
        entity_id=org_id,
        action_type="DELETE",
        old_value=serialize_row(old),
    )
    return MessageResponse(message="Organization deleted")
