from typing import Any

from pydantic import BaseModel, Field


class IngestionRequestCreate(BaseModel):
    account_id: int
    request_type: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1)
    requester_phone: str = Field(min_length=1, max_length=50)


INGESTION_STATUSES = ("PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", "CANCELLED")


class IngestionPendingCountOut(BaseModel):
    pending_count: int


class IngestionRequestUpdate(BaseModel):
    status: str = Field(min_length=1, max_length=50)


class IngestionRequestOut(BaseModel):
    id: int
    account_id: int
    account_name: str | None = None
    organization_id: int | None = None
    organization_name: str | None = None
    requested_by: int
    requester_name: str | None = None
    requester_email: str | None = None
    requester_phone: str | None = None
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
