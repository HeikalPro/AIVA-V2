"""Pydantic domain models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .types import Role


class ContentPart(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["text", "image_url", "tool_use", "tool_result"]
    text: str | None = None
    image_url: dict[str, str] | None = None


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Role
    content: str | list[ContentPart]
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=False)

    messages: list[Message]
    model: str
    temperature: float = 0.7
    max_tokens: int | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=False)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    provider: str
    model: str
    content: str
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    latency_ms: float | None = None
    cost_usd: float | None = None
    correlation_id: str = ""
    tool_calls: list[dict[str, Any]] | None = None


class StreamChunk(BaseModel):
    model_config = ConfigDict(frozen=False)

    delta: str
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    correlation_id: str = ""
