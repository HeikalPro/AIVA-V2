from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    organization_id: int
    email: EmailStr
    password: str = Field(min_length=8)
    first_name: str | None = None
    last_name: str | None = None
    status: str = "ACTIVE"
    role_id: int
    account_id: int | None = None


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8)
    first_name: str | None = None
    last_name: str | None = None
    status: str | None = None


class UserRoleAssign(BaseModel):
    role_id: int
    account_id: int | None = None


class AccountUserAssign(BaseModel):
    account_id: int
    status: str = "ACTIVE"


class UserOut(BaseModel):
    id: int
    organization_id: int
    email: str
    first_name: str | None
    last_name: str | None
    status: str
    roles: list[str] = []
    account_ids: list[int] = []
    created_at: str | None = None
