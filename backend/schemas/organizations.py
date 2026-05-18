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
    created_at: str | None = None
    updated_at: str | None = None
