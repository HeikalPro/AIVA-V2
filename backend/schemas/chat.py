from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    account_id: int


class SessionOut(BaseModel):
    id: int
    account_id: int
    user_id: int
    session_status: str | None
    started_at: str | None
    ended_at: str | None
    message_count: int | None = None


class MessageCreate(BaseModel):
    message_text: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=50)


class MessageOut(BaseModel):
    id: int
    session_id: int
    sender_type: str
    message_text: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None
    created_at: str | None
