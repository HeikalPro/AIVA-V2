"""Normalize messages for provider-specific quirks (extension point)."""

from __future__ import annotations

from typing import Any

from llm_service.core.models import Message


def openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    from llm_service.providers._base_http import message_to_openai_dict

    return [message_to_openai_dict(m) for m in messages]
