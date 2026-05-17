"""Provider registry and factory."""

from __future__ import annotations

from typing import Any, TypeVar

from llm_service.config.settings import LibrarySettings
from llm_service.core.base import BaseLLMProvider
from llm_service.core.exceptions import ConfigurationError

T = TypeVar("T", bound=BaseLLMProvider)

_REGISTRY: dict[str, type[BaseLLMProvider]] = {}


def register(name: str) -> Any:
    """Class decorator for provider registration."""

    def decorator(cls: type[T]) -> type[T]:
        _REGISTRY[name.lower()] = cls
        return cls

    return decorator


def register_alias(alias: str, existing: str) -> None:
    """Register an alternate name for an existing provider."""
    key = existing.lower()
    if key not in _REGISTRY:
        raise ConfigurationError(f"Cannot alias unknown provider: {existing}")
    _REGISTRY[alias.lower()] = _REGISTRY[key]


def create_provider(
    name: str,
    settings: LibrarySettings | None = None,
    *,
    config: Any | None = None,
) -> BaseLLMProvider:
    import llm_service.providers  # noqa: F401 - register built-in providers

    key = name.lower()
    if key not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ConfigurationError(f"Unknown provider '{name}'. Available: {available}")
    settings = settings or LibrarySettings()
    cfg = config if config is not None else settings.provider_config(key)
    cls = _REGISTRY[key]
    return cls(cfg)  # type: ignore[call-arg]


def list_providers() -> list[str]:
    import llm_service.providers  # noqa: F401

    return sorted(_REGISTRY.keys())
