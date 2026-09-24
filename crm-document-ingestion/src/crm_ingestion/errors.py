"""Exception hierarchy. Every error this project raises derives from IngestionError."""

from __future__ import annotations


class IngestionError(Exception):
    """Base class for every error raised by crm_ingestion."""


class ConfigurationError(IngestionError):
    """A required setting is missing or invalid (e.g. Microsoft credentials not set)."""


class AuthenticationError(IngestionError):
    """A Microsoft Entra ID token could not be obtained."""


class GraphAPIError(IngestionError):
    """Microsoft Graph returned an error response."""

    def __init__(self, status_code: int, message: str, *, code: str | None = None) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(f"Graph API error {status_code}{f' ({code})' if code else ''}: {message}")


class GraphTransportError(GraphAPIError):
    """Graph could not be reached (DNS, TLS, timeout); no HTTP response exists, so status_code is 0."""

    def __init__(self, message: str) -> None:
        super().__init__(0, message, code="transportError")


class ItemNotFoundError(GraphAPIError):
    """The drive item does not exist or the app cannot see it (HTTP 404)."""


class DownloadError(IngestionError):
    """The file content could not be downloaded (too large, not a file, network failure)."""


class DocumentExtractionFailed(IngestionError):
    """document-extractor raised while converting a downloaded file. The original
    ExtractionError is chained as __cause__."""

    def __init__(self, filename: str, cause: Exception) -> None:
        self.filename = filename
        super().__init__(f"extraction failed for {filename!r}: {cause}")


class EntityExtractionError(IngestionError):
    """A CRM entity extractor failed."""
