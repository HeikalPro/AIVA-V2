from pydantic import BaseModel, Field


class AccountCreate(BaseModel):
    organization_id: int
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    corpus_id: str | None = None
    llm_config_id: int | None = None
    status: str = "ACTIVE"


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    corpus_id: str | None = None
    llm_config_id: int | None = None
    status: str | None = None


class AccountOut(BaseModel):
    id: int
    organization_id: int
    llm_config_id: int | None
    name: str
    description: str | None
    corpus_id: str | None
    status: str
    created_at: str | None = None
