from pydantic import BaseModel, EmailStr, Field, field_validator

from backend.utils import normalize_allowed_email


class UserCreate(BaseModel):
    organization_id: int
    email: EmailStr
    password: str = Field(min_length=8)
    first_name: str | None = None
    last_name: str | None = None
    status: str = "ACTIVE"
    role_id: int
    account_id: int | None = None

    @field_validator("email", mode="before")
    @classmethod
    def enforce_email_domain(cls, v: object) -> str:
        if not isinstance(v, str):
            raise ValueError("Email is required")
        return normalize_allowed_email(v)


class UserUpdate(BaseModel):
    organization_id: int | None = None
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8)
    first_name: str | None = None
    last_name: str | None = None
    status: str | None = None

    @field_validator("email", mode="before")
    @classmethod
    def enforce_email_domain(cls, v: object) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            return None
        return normalize_allowed_email(v)


class UserNavPermissionsUpdate(BaseModel):
    extra_nav_permissions: list[str] = Field(default_factory=list)


class UserRoleAssign(BaseModel):
    role_id: int
    account_id: int | None = None


class AccountUserAssign(BaseModel):
    account_id: int
    status: str = "ACTIVE"


class UserOut(BaseModel):
    id: int
    organization_id: int
    organization_name: str | None = None
    organization_code: str | None = None
    email: str
    first_name: str | None
    last_name: str | None
    status: str
    roles: list[str] = []
    account_ids: list[int] = []
    extra_nav_permissions: list[str] = []
    created_at: str | None = None
