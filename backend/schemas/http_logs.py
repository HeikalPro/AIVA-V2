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


class HttpRequestStatsSummary(BaseModel):
    total_requests: int
    unique_users: int
    unique_endpoints: int
    avg_duration_ms: float | None = None
    max_duration_ms: int | None = None
    error_count: int
    error_rate: float | None = None


class HttpEndpointStatOut(BaseModel):
    http_method: str
    endpoint: str
    handler_name: str | None = None
    count: int
    unique_users: int
    avg_duration_ms: float | None = None
    max_duration_ms: int | None = None
    error_count: int
    error_rate: float | None = None
    last_called_at: str | None = None


class HttpUserStatOut(BaseModel):
    actor: str
    user_id: int | None = None
    count: int
    unique_endpoints: int
    error_count: int
    last_seen_at: str | None = None


class HttpRequestStatsOut(BaseModel):
    summary: HttpRequestStatsSummary
    by_endpoint: list[HttpEndpointStatOut]
    by_user: list[HttpUserStatOut]
