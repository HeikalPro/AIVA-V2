"""Unified exception hierarchy."""


class LLMServiceError(Exception):
    """Root exception for the library."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class ProviderError(LLMServiceError):
    """Provider returned an error response."""


class AuthenticationError(ProviderError):
    """Invalid or missing credentials."""


class RateLimitError(ProviderError):
    """Rate limited by provider."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = 429,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, status_code=status_code)
        self.retry_after = retry_after


class InvalidRequestError(ProviderError):
    """Malformed or unsupported request."""


class ContextLengthExceededError(InvalidRequestError):
    """Context / token limit exceeded."""


class TimeoutError(LLMServiceError):
    """Request timed out."""


class ConnectionError(LLMServiceError):
    """Network / connection failure."""


class StreamingError(LLMServiceError):
    """Failure while consuming a stream."""


class RetryExhaustedError(LLMServiceError):
    """All retry attempts failed."""


class CircuitOpenError(LLMServiceError):
    """Circuit breaker is open."""


class CacheError(LLMServiceError):
    """Cache backend error."""


class ConfigurationError(LLMServiceError):
    """Invalid configuration."""


class MiddlewareError(LLMServiceError):
    """Middleware pipeline failure."""


class ImportExtraError(LLMServiceError):
    """Optional dependency missing; install the relevant extra."""
