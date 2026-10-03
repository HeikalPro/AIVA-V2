"""Generic CRM intelligence layer: Document -> EntityExtractor -> CRM entity JSON.

Nothing here is client-specific. Client schemas are registered as data
(`SchemaRegistry.register_from_dict` / `load_json`) or as Pydantic models
(`SchemaRegistry.register_model`)."""

from .extractors import (
    CompositeExtractor,
    CompositeOutput,
    EntityExtractor,
    IntelligenceExtractor,
    PatternExtractor,
)
from .schemas import (
    CRMEntity,
    EntitySchema,
    Evidence,
    ExtractionResult,
    FieldDefinition,
    FieldValue,
    SchemaRegistry,
    default_registry,
    generic_schemas,
)
from .service import CRMExtractionService, CRMProcessingResult, registry_from_settings
from .validators import EntityValidator, ValidationIssue, ValidationReport

__all__ = [
    "CRMEntity",
    "CRMExtractionService",
    "CRMProcessingResult",
    "CompositeExtractor",
    "CompositeOutput",
    "EntityExtractor",
    "EntitySchema",
    "EntityValidator",
    "Evidence",
    "ExtractionResult",
    "FieldDefinition",
    "FieldValue",
    "IntelligenceExtractor",
    "PatternExtractor",
    "SchemaRegistry",
    "ValidationIssue",
    "ValidationReport",
    "default_registry",
    "generic_schemas",
    "registry_from_settings",
]
