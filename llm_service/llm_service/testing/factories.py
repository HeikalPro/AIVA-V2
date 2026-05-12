"""Test fixture builders."""

from __future__ import annotations

from llm_service.core.models import Message, TokenUsage
from llm_service.core.types import Role


def message_user(text: str) -> Message:
    return Message(role=Role.USER, content=text)


def usage_fixture() -> TokenUsage:
    return TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30)
