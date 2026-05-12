"""Tool / function calling models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Tool(BaseModel):
    model_config = {"frozen": True}

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    model_config = {"frozen": True}

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    model_config = {"frozen": True}

    tool_call_id: str
    content: str
    is_error: bool = False
