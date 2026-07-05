from pydantic import BaseModel


class AuditLogOut(BaseModel):
    id: int
    created_at: str | None = None
    user_id: int | None = None
    actor_email: str | None = None
    actor_org_id: int | None = None
    entity_type: str
    entity_id: str
    action_type: str
    old_value: str | None = None
    new_value: str | None = None
    ip_address: str | None = None
    summary: str | None = None


class AuditLogListOut(BaseModel):
    items: list[AuditLogOut]
    limit: int
    offset: int


class SignInLogOut(BaseModel):
    id: int
    created_at: str | None = None
    user_id: int | None = None
    user_email: str | None = None
    event_type: str
    ip_address: str | None = None
    user_agent: str | None = None
    metadata: str | None = None
    summary: str | None = None


class SignInLogListOut(BaseModel):
    items: list[SignInLogOut]
    limit: int
    offset: int
