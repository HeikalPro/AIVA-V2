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
    session_id: int | None = None
    account_id: int | None = None
    account_name: str | None = None
    organization_id: int | None = None
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
