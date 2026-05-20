from pydantic import BaseModel, Field

from backend.schemas.notifications import DeveloperNotifyOut


class TicketCreate(BaseModel):
    organization_id: int
    account_id: int | None = None
    ticket_type: str = Field(min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1)


class TicketUpdate(BaseModel):
    assigned_to: int | None = None
    ticket_type: str | None = None
    status: str | None = None
    subject: str | None = Field(default=None, max_length=500)
    description: str | None = None


class TicketOpenCountOut(BaseModel):
    open_count: int


class TicketOut(BaseModel):
    id: int
    organization_id: int
    account_id: int | None
    created_by: int
    assigned_to: int | None
    ticket_type: str | None
    priority: str | None
    status: str | None
    subject: str | None
    description: str | None
    created_at: str | None


class TicketCreateOut(TicketOut):
    """Create response includes developer email notification result."""

    developer_notify: DeveloperNotifyOut
