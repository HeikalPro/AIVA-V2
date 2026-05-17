# side-effect: register all built-in providers
from llm_service.providers.anthropic.adapter import AnthropicProvider  # noqa: F401
from llm_service.providers.azure_openai.adapter import AzureOpenAIProvider  # noqa: F401
from llm_service.providers.gemini.adapter import GeminiProvider  # noqa: F401
from llm_service.providers.huggingface.adapter import HuggingFaceProvider  # noqa: F401
from llm_service.providers.local.adapter import LocalProvider  # noqa: F401
from llm_service.providers.mock.adapter import MockProvider  # noqa: F401
from llm_service.providers.ollama.adapter import OllamaProvider  # noqa: F401
from llm_service.providers.openai.adapter import OpenAIProvider  # noqa: F401
from llm_service.providers.openrouter.adapter import OpenRouterProvider  # noqa: F401
from llm_service.providers.registry import create_provider, list_providers, register, register_alias
from llm_service.providers.vllm.adapter import VLLMProvider  # noqa: F401

__all__ = [
    "AnthropicProvider",
    "AzureOpenAIProvider",
    "GeminiProvider",
    "HuggingFaceProvider",
    "LocalProvider",
    "MockProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "OpenRouterProvider",
    "VLLMProvider",
    "create_provider",
    "list_providers",
    "register",
    "register_alias",
]
