"""Library-wide settings: env, optional file, runtime overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from llm_service.core.exceptions import ConfigurationError

from .loader import env_file_candidates, load_config_file, mask_secrets
from .provider_config import (
    AnthropicProviderConfig,
    AzureOpenAIProviderConfig,
    GeminiProviderConfig,
    HuggingFaceProviderConfig,
    LocalProviderConfig,
    MockProviderConfig,
    OllamaProviderConfig,
    OpenAIProviderConfig,
    OpenRouterProviderConfig,
    VLLMProviderConfig,
)


class LibrarySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=env_file_candidates(),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    default_provider: str = "openai"
    default_model: str | None = None
    config_file: str | None = None

    log_level: str = "INFO"
    log_format: str = "json"
    redact_secrets: bool = True
    enable_tracing: bool = False
    enable_metrics: bool = False

    openai: OpenAIProviderConfig = Field(default_factory=OpenAIProviderConfig)
    anthropic: AnthropicProviderConfig = Field(default_factory=AnthropicProviderConfig)
    gemini: GeminiProviderConfig = Field(default_factory=GeminiProviderConfig)
    azure_openai: AzureOpenAIProviderConfig = Field(default_factory=AzureOpenAIProviderConfig)
    ollama: OllamaProviderConfig = Field(default_factory=OllamaProviderConfig)
    vllm: VLLMProviderConfig = Field(default_factory=VLLMProviderConfig)
    openrouter: OpenRouterProviderConfig = Field(default_factory=OpenRouterProviderConfig)
    huggingface: HuggingFaceProviderConfig = Field(default_factory=HuggingFaceProviderConfig)
    local: LocalProviderConfig = Field(default_factory=LocalProviderConfig)
    mock: MockProviderConfig = Field(default_factory=MockProviderConfig)

    @model_validator(mode="before")
    @classmethod
    def merge_config_file(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        path = data.get("config_file") or data.get("LLM_CONFIG_FILE")
        if path:
            file_data = load_config_file(Path(path))
            merged = {**file_data, **{k: v for k, v in data.items() if v is not None}}
            return merged
        return data

    @classmethod
    def from_file(cls, path: str | Path) -> LibrarySettings:
        return cls.model_validate(load_config_file(path))

    def provider_config(self, name: str) -> Any:
        key = name.lower().replace("-", "_")
        mapping: dict[str, Any] = {
            "openai": self.openai,
            "anthropic": self.anthropic,
            "gemini": self.gemini,
            "azure_openai": self.azure_openai,
            "azure": self.azure_openai,
            "ollama": self.ollama,
            "vllm": self.vllm,
            "openrouter": self.openrouter,
            "huggingface": self.huggingface,
            "hf": self.huggingface,
            "local": self.local,
            "mock": self.mock,
        }
        if key not in mapping:
            raise ConfigurationError(f"Unknown provider config key: {name}")
        return mapping[key]

    def resolved_default_model(self) -> str | None:
        if self.default_model:
            return self.default_model
        try:
            pc = self.provider_config(self.default_provider)
            return getattr(pc, "default_model", None)
        except ConfigurationError:
            return None

    def safe_dict(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        return mask_secrets(d) if self.redact_secrets else d
