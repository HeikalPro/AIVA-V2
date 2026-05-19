from pydantic import BaseModel, Field


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=1, max_length=100)
    status: str = "ACTIVE"


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = None


class OrganizationOut(BaseModel):
    id: int
    name: str
    code: str
    status: str
    account_count: int = 0
    account_names: list[str] = []
    created_at: str | None = None
    updated_at: str | None = None


class OrganizationDeleteSummary(BaseModel):
    organization_id: int
    name: str
    code: str
    user_count: int
    account_count: int
    ticket_count: int
    account_names: list[str] = []


class OrganizationDeleteResult(BaseModel):
    message: str
    accounts_deleted: int
    users_deleted: int
    tickets_deleted: int
