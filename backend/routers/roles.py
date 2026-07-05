from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from backend.auth.deps import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    UserContext,
    require_roles,
)
from backend.dependencies import DbDep
from backend.exceptions import NotFoundError
from backend.schemas.roles import NavPermissionCatalogItem, RoleNavPermissionsUpdate, RoleOut
from backend.services.role_nav_permissions import (
    NAV_PERMISSION_CATALOG,
    list_roles_with_nav_permissions,
    set_role_nav_permissions,
)
from backend.services.role_report import build_role_report, build_role_report_pdf

router = APIRouter(prefix="/roles", tags=["roles"])


@router.get("/nav-catalog", response_model=list[NavPermissionCatalogItem])
async def nav_catalog(
    _user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
) -> list[NavPermissionCatalogItem]:
    return [NavPermissionCatalogItem(**item) for item in NAV_PERMISSION_CATALOG]


@router.get("", response_model=list[RoleOut])
async def list_roles(
    _user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)),
    ],
    db: DbDep,
) -> list[RoleOut]:
    rows = await list_roles_with_nav_permissions(db)
    return [RoleOut(**row) for row in rows]


@router.get("/reports/pdf")
async def download_role_report_pdf(
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN))],
    db: DbDep,
    organization_id: int | None = Query(None),
) -> Response:
    report = await build_role_report(db, user, organization_id)
    pdf_bytes = build_role_report_pdf(report)
    stamp = report["generated_at"][:10]
    filename = f"gochat247-role-report-{stamp}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{role_id}", response_model=RoleOut)
async def get_role(
    role_id: int,
    _user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)),
    ],
    db: DbDep,
) -> RoleOut:
    rows = await list_roles_with_nav_permissions(db)
    match = next((r for r in rows if r["id"] == role_id), None)
    if not match:
        raise NotFoundError("Role not found")
    return RoleOut(**match)


@router.put("/{role_id}/nav-permissions", response_model=RoleOut)
async def update_role_nav_permissions(
    role_id: int,
    body: RoleNavPermissionsUpdate,
    _user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> RoleOut:
    rows = await list_roles_with_nav_permissions(db)
    if not any(r["id"] == role_id for r in rows):
        raise NotFoundError("Role not found")

    nav_permissions = await set_role_nav_permissions(db, role_id, body.nav_permissions)
    role_name = next(r["name"] for r in rows if r["id"] == role_id)
    return RoleOut(id=role_id, name=role_name, nav_permissions=nav_permissions)
