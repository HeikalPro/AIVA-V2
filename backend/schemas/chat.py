from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator


class SessionCreate(BaseModel):
    account_id: int


class SessionOut(BaseModel):
    id: int
    account_id: int
    user_id: int
    agent_first_name: str | None = None
    agent_last_name: str | None = None
    agent_email: str | None = None
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
    rating: Literal["up", "down"] | None = None
    feedback: str | None = None
    rated_at: str | None = None


class MessageRatingCreate(BaseModel):
    rating: Literal["up", "down"]
    feedback: str | None = None

    @model_validator(mode="after")
    def require_feedback_on_down(self) -> Self:
        if self.rating == "down" and not (self.feedback or "").strip():
            raise ValueError("feedback is required when rating is down")
        return self


class MessageRatingOut(BaseModel):
    message_id: int
    session_id: int
    account_id: int
    account_name: str | None
    organization_id: int
    organization_name: str | None
    agent_user_id: int
    agent_email: str | None
    agent_first_name: str | None
    agent_last_name: str | None
    message_text: str
    rating: Literal["up", "down"]
    feedback: str | None
    rated_at: str | None
