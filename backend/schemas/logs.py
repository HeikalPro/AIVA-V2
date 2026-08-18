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


class RagRetrievalOut(BaseModel):
    id: int
    created_at: str | None = None
    session_id: int | None = None
    account_id: int | None = None
    account_name: str | None = None
    corpus_id: str | None = None
    query_text: str | None = None
    top_k: int | None = None
    verticals: str | None = None
    status: str
    chunks_returned: int | None = None
    top_score: float | None = None
    retrieval_ms: int | None = None
    error_message: str | None = None
    chunks_json: str | None = None
    source: str | None = None
    summary: str | None = None


class RagRetrievalListOut(BaseModel):
    items: list[RagRetrievalOut]
    limit: int
    offset: int


class AiRequestOut(BaseModel):
    id: int
    created_at: str | None = None
    session_id: int | None = None
    account_id: int | None = None
    account_name: str | None = None
    organization_id: int | None = None
    user_email: str | None = None
    model_name: str | None = None
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    response_time_ms: int | None = None
    total_cost: float | None = None
    status: str | None = None
    error_message: str | None = None
    source: str | None = None
    summary: str | None = None


class AiRequestListOut(BaseModel):
    items: list[AiRequestOut]
    limit: int
    offset: int


class AiMetricsSummary(BaseModel):
    total_calls: int
    avg_latency_ms: float | None = None
    min_latency_ms: float | None = None
    max_latency_ms: float | None = None
    total_tokens: int
    total_cost: float | None = None
    success_count: int
    failed_count: int
    success_rate: float | None = None
    error_rate: float | None = None


class AiMetricsBreakdownItem(BaseModel):
    model_name: str
    provider: str | None = None
    count: int
    avg_latency_ms: float | None = None
    min_latency_ms: float | None = None
    max_latency_ms: float | None = None
    total_tokens: int
    error_rate: float | None = None


class AiMetricsOut(BaseModel):
    summary: AiMetricsSummary
    by_model: list[AiMetricsBreakdownItem]


class ErrorLogOut(BaseModel):
    id: int
    created_at: str | None = None
    exception_type: str
    exception_message: str | None = None
    stack_trace: str | None = None
    source: str | None = None
    http_method: str | None = None
    path: str | None = None
    route_template: str | None = None
    status_code: int | None = None
    request_id: str | None = None
    user_id: int | None = None
    user_email: str | None = None
    org_id: int | None = None
    client_ip: str | None = None


class ErrorTypeCount(BaseModel):
    exception_type: str
    count: int


class ErrorLogListOut(BaseModel):
    items: list[ErrorLogOut]
    type_counts: list[ErrorTypeCount]
    limit: int
    offset: int


class WidgetTurnChunkIn(BaseModel):
    parent_id: str | None = None
    chunk_index: int | None = None
    score: float | None = None
    text: str | None = None


class WidgetTurnIn(BaseModel):
    """One widget conversation turn, sent by the standalone chatbot for logging."""

    corpus_id: str | None = None
    query_text: str | None = None
    top_k: int | None = None
    verticals: list[str] | None = None
    retrieval_status: str | None = None
    retrieval_error: str | None = None
    retrieval_ms: int | None = None
    chunks: list[WidgetTurnChunkIn] = []
    model_name: str | None = None
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    response_time_ms: int | None = None
    total_cost: float | None = None
    llm_error: str | None = None


class WidgetTurnAck(BaseModel):
    ok: bool


class WidgetErrorIn(BaseModel):
    """One failed widget turn, reported by the standalone chatbot for the Errors view."""

    corpus_id: str | None = None
    query_text: str | None = None
    exception_type: str
    exception_message: str | None = None
    stack_trace: str | None = None
    status_code: int | None = None
