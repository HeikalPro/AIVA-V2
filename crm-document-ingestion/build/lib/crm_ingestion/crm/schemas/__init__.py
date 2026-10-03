"""CRM entity shapes and the data-driven schema system."""

from .base import CRMEntity, Evidence, ExtractionResult, FieldValue
from .definitions import EntitySchema, FieldDefinition, FieldType, SchemaRegistry
from .generic import GENERIC_SCHEMA_DATA, default_registry, generic_schemas

__all__ = [
    "GENERIC_SCHEMA_DATA",
    "CRMEntity",
    "EntitySchema",
    "Evidence",
    "ExtractionResult",
    "FieldDefinition",
    "FieldType",
    "FieldValue",
    "SchemaRegistry",
    "default_registry",
    "generic_schemas",
]
