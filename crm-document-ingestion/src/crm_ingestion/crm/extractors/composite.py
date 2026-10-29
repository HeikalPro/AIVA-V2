"""Runs several extractors and merges what they found, isolating their failures."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from document_extractor import Document

from ...errors import EntityExtractionError
from ..schemas.base import CRMEntity
from ..schemas.definitions import SchemaRegistry
from .assembly import merge_entities
from .base import EntityExtractor

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CompositeOutput:
    """Merged entities, one warning per failed extractor, and the extractors that succeeded."""

    entities: list[CRMEntity] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extractors: list[str] = field(default_factory=list)


def merge_entity_groups(groups: Sequence[Sequence[CRMEntity]], registry: SchemaRegistry) -> list[CRMEntity]:
    """Merge the outputs of several extractors, earlier extractors first.

    Entities are merged by type, position-wise: the k-th entity of a type from each
    extractor becomes one entity (extractors list their most salient entity first).
    Per field the highest-confidence value wins and the others are kept as
    alternatives; all evidence is kept."""
    merged: list[CRMEntity] = []
    for group in groups:
        seen: dict[str, int] = {}
        for entity in group:
            k = seen.get(entity.entity_type, 0)
            seen[entity.entity_type] = k + 1
            same_type = [i for i, e in enumerate(merged) if e.entity_type == entity.entity_type]
            if k < len(same_type):
                i = same_type[k]
                merged[i] = merge_entities(merged[i], entity, registry)
            else:
                merged.append(entity)
    return merged


class CompositeExtractor(EntityExtractor):
    """Runs `extractors` in order and merges their entities (see `merge_entity_groups`).

    An extractor that raises becomes a warning and the others still run; with
    `fail_fast` the first failure raises EntityExtractionError instead."""

    def __init__(self, extractors: Sequence[EntityExtractor], *, fail_fast: bool = False) -> None:
        self.extractors = list(extractors)
        self.fail_fast = fail_fast
        self.name = "+".join(e.name for e in self.extractors) or "composite"

    def run(self, document: Document, *, schemas: SchemaRegistry) -> CompositeOutput:
        """Entities plus per-extractor warnings."""
        out = CompositeOutput()
        groups: list[list[CRMEntity]] = []
        for extractor in self.extractors:
            try:
                groups.append(list(extractor.extract(document, schemas=schemas)))
            except Exception as exc:
                message = f"extractor {extractor.name!r} failed: {type(exc).__name__}: {exc}"
                if self.fail_fast:
                    raise EntityExtractionError(message) from exc
                log.warning("%s (document %s)", message, document.id, exc_info=True)
                out.warnings.append(message)
                continue
            out.extractors.append(extractor.name)
        out.entities = merge_entity_groups(groups, schemas)
        return out

    def extract(self, document: Document, *, schemas: SchemaRegistry) -> list[CRMEntity]:
        return self.run(document, schemas=schemas).entities
