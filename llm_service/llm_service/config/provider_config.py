"""Per-provider typed configuration models."""

from __future__ import annotations

from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .loader import env_file_candidates


def _provider_env(prefix: str) -> SettingsConfigDict:
    """Load provider keys from the same `.env` discovery as ``LibrarySettings``."""
    return SettingsConfigDict(
        env_prefix=prefix,
        env_file=env_file_candidates(),
        env_file_encoding="utf-8",
        extra="ignore",
    )


class ProviderConfigBase(BaseSettings):
    """Shared fields for HTTP-based providers."""

    model_config = SettingsConfigDict(
        env_file=env_file_candidates(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    timeout: float = 60.0
    # Single attempt by default for lowest latency; set e.g. OPENAI_MAX_RETRIES=3 to retry on failures.
    max_retries: int = 1
    base_url: str = ""
    api_key: SecretStr | None = None
    default_model: str = "gpt-4.1"
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)


class OpenAIProviderConfig(ProviderConfigBase):
    model_config = _provider_env("OPENAI_")

    organization_id: str | None = None
    default_model: str = "gpt-4.1"
    base_url: str = "https://api.openai.com/v1"


class AnthropicProviderConfig(ProviderConfigBase):
    model_config = _provider_env("ANTHROPIC_")

    default_model: str = "claude-3-5-sonnet-20241022"
    base_url: str = "https://api.anthropic.com"


class GeminiProviderConfig(ProviderConfigBase):
    model_config = _provider_env("GEMINI_")

    default_model: str = "gemini-1.5-flash"
    base_url: str = "https://generativelanguage.googleapis.com"


class AzureOpenAIProviderConfig(ProviderConfigBase):
    model_config = _provider_env("AZURE_OPENAI_")

    default_model: str = "gpt-4"
    azure_endpoint: str = ""
    api_version: str = "2024-02-15-preview"
    deployment_name: str | None = None


class OllamaProviderConfig(ProviderConfigBase):
    model_config = _provider_env("OLLAMA_")

    default_model: str = "llama3"
    base_url: str = "http://127.0.0.1:11434"


class VLLMProviderConfig(ProviderConfigBase):
    model_config = _provider_env("VLLM_")

    default_model: str = "meta-llama/Meta-Llama-3-8B-Instruct"
    base_url: str = "http://127.0.0.1:8000/v1"


class OpenRouterProviderConfig(ProviderConfigBase):
    model_config = _provider_env("OPENROUTER_")

    default_model: str = "openai/gpt-4o"
    base_url: str = "https://openrouter.ai/api/v1"


class HuggingFaceProviderConfig(ProviderConfigBase):
    model_config = _provider_env("HUGGINGFACE_")

    default_model: str = "meta-llama/Meta-Llama-3-8B-Instruct"
    base_url: str = "https://api-inference.huggingface.co"


class LocalProviderConfig(ProviderConfigBase):
    model_config = _provider_env("LOCAL_LLM_")

    default_model: str = "local-model"
    model_path: str | None = None
    device: str = "cpu"


class MockProviderConfig(ProviderConfigBase):
    model_config = _provider_env("MOCK_")

    default_model: str = "mock-model"
