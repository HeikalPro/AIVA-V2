from pydantic import BaseModel, Field


class NavPermissionCatalogItem(BaseModel):
    key: str
    label: str


class RoleOut(BaseModel):
    id: int
    name: str
    nav_permissions: list[str]


class RoleNavPermissionsUpdate(BaseModel):
    nav_permissions: list[str] = Field(default_factory=list)
