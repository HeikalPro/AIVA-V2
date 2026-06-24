from pydantic import BaseModel, Field


class AccountUpdateCreate(BaseModel):
    account_id: int
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1)
    is_active: bool = True


class AccountUpdateUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    body: str | None = None
    is_active: bool | None = None


class AccountUpdateOut(BaseModel):
    id: int
    account_id: int
    account_name: str | None = None
    organization_id: int | None = None
    organization_name: str | None = None
    title: str
    body: str
    is_active: bool
    created_by: int
    created_at: str | None
    updated_at: str | None
