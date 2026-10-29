"""Entity extractors: Document -> CRM entities."""

from .assembly import EntityBuilder, FieldMatch, LabelIndex, merge_entities, merge_field_values
from .base import EntityExtractor
from .composite import CompositeExtractor, CompositeOutput, merge_entity_groups
from .intelligence import IntelligenceExtractor
from .rules import PatternExtractor

__all__ = [
    "CompositeExtractor",
    "CompositeOutput",
    "EntityBuilder",
    "EntityExtractor",
    "FieldMatch",
    "IntelligenceExtractor",
    "LabelIndex",
    "PatternExtractor",
    "merge_entities",
    "merge_entity_groups",
    "merge_field_values",
]
