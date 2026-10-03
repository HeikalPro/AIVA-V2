"""crm_ingestion: Microsoft Graph -> document-extractor -> CRM entities."""

from __future__ import annotations

from .app import CRMIngestionApp
from .config import Settings, get_settings
from .connectors.sharepoint import DocumentDownloader, DownloadedFile, DriveItemReference, SourceMetadata
from .crm import (
    CRMEntity,
    CRMExtractionService,
    CRMProcessingResult,
    EntityExtractor,
    EntitySchema,
    ExtractionResult,
    FieldDefinition,
    SchemaRegistry,
    default_registry,
)
from .errors import (
    AuthenticationError,
    ConfigurationError,
    DocumentExtractionFailed,
    DownloadError,
    EntityExtractionError,
    GraphAPIError,
    GraphTransportError,
    IngestionError,
    ItemNotFoundError,
)
from .ingestion import IngestedDocument, IngestionPipeline, downloaded_file_from_path

__version__ = "0.1.0"

__all__ = [
    "AuthenticationError",
    "CRMEntity",
    "CRMExtractionService",
    "CRMIngestionApp",
    "CRMProcessingResult",
    "ConfigurationError",
    "DocumentDownloader",
    "DocumentExtractionFailed",
    "DownloadError",
    "DownloadedFile",
    "DriveItemReference",
    "EntityExtractionError",
    "EntityExtractor",
    "EntitySchema",
    "ExtractionResult",
    "FieldDefinition",
    "GraphAPIError",
    "GraphTransportError",
    "IngestedDocument",
    "IngestionError",
    "IngestionPipeline",
    "ItemNotFoundError",
    "SchemaRegistry",
    "Settings",
    "SourceMetadata",
    "__version__",
    "default_registry",
    "downloaded_file_from_path",
    "get_settings",
]
