from typing import Any

from pydantic import BaseModel, Field


class IngestionRequestCreate(BaseModel):
    account_id: int
    request_type: str = Field(min_length=1, max_length=100)
    priority: str = "MEDIUM"
    description: str = Field(min_length=1)


class IngestionRequestOut(BaseModel):
    id: int
    account_id: int
    requested_by: int
    request_type: str | None
    status: str | None
    priority: str | None
    description: str | None
    created_at: str | None


class IngestionTrigger(BaseModel):
    corpus_id: str
    lines: list[str] | None = None
    records: list[dict[str, Any]] | None = None
    reindex: bool = False


class JobOut(BaseModel):
    job_id: str
    mode: str | None = None
    status: str | None = None
    error_msg: str | None = None
