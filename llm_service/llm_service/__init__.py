"""
llm-service: unified async-first LLM SDK.
"""

from __future__ import annotations

from llm_service.client import LLMClient
from llm_service.config import LibrarySettings
from llm_service.core import (
    BaseLLMProvider,
    LLMRequest,
    LLMResponse,
    LLMServiceError,
    Message,
    StreamChunk,
    TokenUsage,
)
from llm_service.providers.registry import create_provider, list_providers, register

__all__ = [
    "BaseLLMProvider",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "LLMServiceError",
    "LibrarySettings",
    "Message",
    "StreamChunk",
    "TokenUsage",
    "create_provider",
    "list_providers",
    "register",
]

__version__ = "0.1.2"
