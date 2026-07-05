from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from backend.auth.deps import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    UserContext,
    require_account_access,
    require_roles,
)
from backend.dependencies import DbDep
from backend.exceptions import ForbiddenError, NotFoundError
from backend.schemas.roles import NavPermissionCatalogItem, RoleNavPermissionsUpdate, RoleOut
from backend.services.role_nav_permissions import (
    NAV_PERMISSION_CATALOG,
    list_roles_with_nav_permissions_for_account,
    reset_account_role_nav_permissions,
    set_account_role_nav_permissions,
)
from backend.services.role_report import build_role_report, build_role_report_pdf

router = APIRouter(prefix="/roles", tags=["roles"])


async def _require_account_for_roles(
    db: DbDep,
    user: UserContext,
    account_id: int,
) -> dict:
    account = await db.fetch_one(
        "SELECT id, organization_id FROM AIVA_accounts WHERE id = :id",
        {"id": account_id},
    )
    if not account:
        raise NotFoundError("Account not found")
    account_org = int(account["organization_id"])
    require_account_access(account_id, user, account_org)
    if not user.is_super_admin and account_org != user.organization_id:
        raise ForbiddenError("Cannot manage roles for an account in another organization")
    return account


@router.get("/nav-catalog", response_model=list[NavPermissionCatalogItem])
async def nav_catalog(
    _user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
) -> list[NavPermissionCatalogItem]:
    return [NavPermissionCatalogItem(**item) for item in NAV_PERMISSION_CATALOG]


@router.get("", response_model=list[RoleOut])
async def list_roles(
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)),
    ],
    db: DbDep,
    account_id: int = Query(..., description="Account whose role page access to list"),
) -> list[RoleOut]:
    await _require_account_for_roles(db, user, account_id)
    rows = await list_roles_with_nav_permissions_for_account(db, account_id)
    return [RoleOut(**row) for row in rows]


@router.get("/reports/pdf")
async def download_role_report_pdf(
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN))],
    db: DbDep,
    organization_id: int | None = Query(None),
    account_id: int | None = Query(None),
) -> Response:
    if account_id is not None:
        await _require_account_for_roles(db, user, account_id)
    report = await build_role_report(db, user, organization_id, account_id=account_id)
    pdf_bytes = build_role_report_pdf(report)
    stamp = report["generated_at"][:10]
    suffix = f"-account-{account_id}" if account_id is not None else ""
    filename = f"gochat247-role-report{suffix}-{stamp}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{role_id}", response_model=RoleOut)
async def get_role(
    role_id: int,
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)),
    ],
    db: DbDep,
    account_id: int = Query(..., description="Account whose role page access to view"),
) -> RoleOut:
    await _require_account_for_roles(db, user, account_id)
    rows = await list_roles_with_nav_permissions_for_account(db, account_id)
    match = next((r for r in rows if r["id"] == role_id), None)
    if not match:
        raise NotFoundError("Role not found")
    return RoleOut(**match)


@router.post("/{role_id}/nav-permissions/reset", response_model=RoleOut)
async def reset_role_nav_permissions(
    role_id: int,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
    account_id: int = Query(..., description="Account whose role page access to reset"),
) -> RoleOut:
    await _require_account_for_roles(db, user, account_id)
    rows = await list_roles_with_nav_permissions_for_account(db, account_id)
    role_name = next((r["name"] for r in rows if r["id"] == role_id), None)
    if role_name is None:
        raise NotFoundError("Role not found")

    nav_permissions = await reset_account_role_nav_permissions(db, account_id, role_id)
    return RoleOut(id=role_id, name=role_name, nav_permissions=nav_permissions)


@router.put("/{role_id}/nav-permissions", response_model=RoleOut)
async def update_role_nav_permissions(
    role_id: int,
    body: RoleNavPermissionsUpdate,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
    account_id: int = Query(..., description="Account whose role page access to update"),
) -> RoleOut:
    await _require_account_for_roles(db, user, account_id)
    rows = await list_roles_with_nav_permissions_for_account(db, account_id)
    if not any(r["id"] == role_id for r in rows):
        raise NotFoundError("Role not found")

    nav_permissions = await set_account_role_nav_permissions(
        db,
        account_id,
        role_id,
        body.nav_permissions,
    )
    role_name = next(r["name"] for r in rows if r["id"] == role_id)
    return RoleOut(id=role_id, name=role_name, nav_permissions=nav_permissions)
