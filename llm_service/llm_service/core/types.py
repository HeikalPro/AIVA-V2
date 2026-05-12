"""Enums and type aliases."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    TOOL_CALLS = "tool_calls"
    OTHER = "other"


class ProviderId(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    AZURE_OPENAI = "azure_openai"
    OLLAMA = "ollama"
    VLLM = "vllm"
    OPENROUTER = "openrouter"
    HUGGINGFACE = "huggingface"
    LOCAL = "local"
    MOCK = "mock"
