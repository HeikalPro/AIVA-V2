from pydantic import BaseModel, Field


class HttpRequestLogOut(BaseModel):
    id: int
    created_at: str | None = None
    http_method: str
    path: str
    query_string: str | None = None
    handler_name: str | None = None
    route_template: str | None = None
    status_code: int
    duration_ms: int
    user_id: int | None = None
    user_email: str | None = None
    org_id: int | None = None
    user_roles: str | None = None
    actor_label: str | None = None
    client_ip: str | None = None
    summary: str | None = None


class HttpRequestLogListOut(BaseModel):
    items: list[HttpRequestLogOut]
    limit: int
    offset: int
