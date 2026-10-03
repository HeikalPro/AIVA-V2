"""The CRM layer's entry point: an ingested Document in, validated CRM entities out."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from document_extractor import Document
from pydantic import BaseModel

from ..config import CRMSettings
from ..ingestion.models import IngestedDocument
from .extractors.base import EntityExtractor
from .extractors.composite import CompositeExtractor
from .extractors.intelligence import IntelligenceExtractor, confidence_value
from .extractors.rules import PatternExtractor
from .schemas.base import ExtractionResult
from .schemas.definitions import SchemaRegistry
from .schemas.generic import default_registry
from .validators.validator import EntityValidator, ValidationReport


def registry_from_settings(settings: CRMSettings) -> SchemaRegistry:
    """The generic starter schemas plus every file in `settings.schema_paths`, in order.

    Unreadable files raise OSError; invalid schemas raise pydantic.ValidationError; a
    name already registered raises ValueError (file schemas may not silently replace
    generic ones - give them a different name)."""
    registry = default_registry()
    for path in settings.schema_paths:
        registry.load_json(path)
    return registry


class CRMProcessingResult(BaseModel):
    """What the service returns for one document: the extraction and its validation."""

    extraction: ExtractionResult
    validation: ValidationReport

    @property
    def valid(self) -> bool:
        return self.validation.valid

    def to_crm_json(self) -> list[dict[str, Any]]:
        """Every entity as flat CRM JSON."""
        return self.extraction.to_crm_json()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


class CRMExtractionService:
    """Document -> entity extractors -> validation.

    Defaults: IntelligenceExtractor then PatternExtractor, merged by a
    CompositeExtractor; the generic starter schemas plus any `settings.schema_paths`
    files (ignored when `registry` is given); `CRMSettings().min_confidence`.
    With `coerce_values` (default), values that validate are normalised by their field
    type (ISO dates, numbers, digit-only phones) after validation."""

    def __init__(
        self,
        extractors: Sequence[EntityExtractor] | None = None,
        *,
        registry: SchemaRegistry | None = None,
        settings: CRMSettings | None = None,
        coerce_values: bool = True,
        fail_fast: bool = False,
    ) -> None:
        self.settings = settings if settings is not None else CRMSettings()
        self.registry = registry if registry is not None else registry_from_settings(self.settings)
        chosen = list(extractors) if extractors is not None else [IntelligenceExtractor(), PatternExtractor()]
        self.extractor = CompositeExtractor(chosen, fail_fast=fail_fast)
        self.validator = EntityValidator(self.registry, min_confidence=self.settings.min_confidence)
        self.coerce_values = coerce_values

    def process(self, ingested: IngestedDocument) -> CRMProcessingResult:
        """Extract and validate entities from a pipeline result, keeping its source metadata."""
        return self._process(
            ingested.document,
            source=ingested.source.model_dump(mode="json"),
            warnings=[f"ingestion: {w}" for w in ingested.warnings],
        )

    def process_document(self, document: Document) -> CRMProcessingResult:
        """Same as `process` for callers holding only a Document; source metadata is
        taken from ``document.metadata["source"]`` when present."""
        source = document.metadata.get("source") if isinstance(document.metadata, Mapping) else None
        return self._process(document, source=dict(source) if isinstance(source, Mapping) else None)

    def _process(
        self, document: Document, *, source: dict[str, Any] | None, warnings: Sequence[str] = ()
    ) -> CRMProcessingResult:
        out = self.extractor.run(document, schemas=self.registry)
        report = self.validator.validate(out.entities)
        entities = [self.validator.coerce(e) for e in out.entities] if self.coerce_values else out.entities
        intel = document.intelligence if isinstance(document.intelligence, Mapping) else {}
        doc_type = intel.get("document_type")
        summary = intel.get("summary")
        result = ExtractionResult(
            document_id=document.id,
            source=source,
            entities=entities,
            document_type=doc_type if isinstance(doc_type, str) and doc_type else None,
            document_type_confidence=confidence_value(intel.get("confidence")),
            summary=summary if isinstance(summary, str) and summary else None,
            extractors=out.extractors,
            warnings=[*warnings, *out.warnings],
        )
        return CRMProcessingResult(extraction=result, validation=report)
