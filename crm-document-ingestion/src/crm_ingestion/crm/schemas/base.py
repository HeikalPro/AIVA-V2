"""Output shapes of the CRM layer: evidence-backed field values, entities and the
per-document extraction result. Nothing here knows about any particular client."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import to_jsonable_python

META_KEY = "_meta"


class Evidence(BaseModel):
    """Where a value was read from in the source document."""

    model_config = ConfigDict(extra="forbid")

    block_id: str | None = None
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    source_text: str | None = None
    extractor: str = ""

    def key(self) -> tuple[str | None, int | None, str | None, str]:
        """Identity used to de-duplicate evidence when values are merged."""
        return (self.block_id, self.page, self.source_text, self.extractor)


class FieldValue(BaseModel):
    """One extracted value with its confidence (0..1, None when unknown) and evidence.

    `alternatives` keeps competing candidates that lost a merge, so no evidence is lost."""

    value: Any
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    extractor: str = ""
    alternatives: list[FieldValue] = Field(default_factory=list)


def mean_confidence(values: list[FieldValue]) -> float | None:
    """Mean of the known field confidences, None when none is known."""
    known = [v.confidence for v in values if v.confidence is not None]
    return round(sum(known) / len(known), 4) if known else None


class CRMEntity(BaseModel):
    """A CRM record candidate: a typed bag of evidence-backed field values.

    `confidence` defaults to the mean of the field confidences."""

    entity_type: str
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source_document_id: str | None = None
    extractors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _default_confidence(self) -> Self:
        if self.confidence is None:
            self.confidence = mean_confidence(list(self.fields.values()))
        return self

    def get(self, name: str, default: Any = None) -> Any:
        """The plain value of a field, or `default`."""
        fv = self.fields.get(name)
        return fv.value if fv is not None else default

    def values(self) -> dict[str, Any]:
        """Field name -> plain value."""
        return {k: v.value for k, v in self.fields.items()}

    def to_crm_json(self) -> dict[str, Any]:
        """Flat, CRM-ready JSON: ``{field: value, ..., "_meta": {...}}``.

        `_meta` carries the entity type, aggregate confidence, source document and, per
        field, the confidence, extractor and evidence (provenance)."""
        out: dict[str, Any] = {k: to_jsonable_python(v.value) for k, v in self.fields.items()}
        out[META_KEY] = {
            "entity_type": self.entity_type,
            "confidence": self.confidence,
            "source_document_id": self.source_document_id,
            "extractors": list(self.extractors),
            "fields": {
                k: {
                    "confidence": v.confidence,
                    "extractor": v.extractor,
                    "provenance": [e.model_dump(mode="json") for e in v.evidence],
                }
                for k, v in self.fields.items()
            },
        }
        return out


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ExtractionResult(BaseModel):
    """Everything the CRM layer extracted from one document."""

    document_id: str
    source: dict[str, Any] | None = None
    entities: list[CRMEntity] = Field(default_factory=list)
    document_type: str | None = None
    document_type_confidence: float | None = None
    summary: str | None = None
    extractors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)

    def entities_of(self, entity_type: str) -> list[CRMEntity]:
        """Entities of one type, in extraction order."""
        return [e for e in self.entities if e.entity_type == entity_type]

    def to_crm_json(self) -> list[dict[str, Any]]:
        """Every entity as flat CRM JSON (see `CRMEntity.to_crm_json`)."""
        return [e.to_crm_json() for e in self.entities]

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible dict of the full result."""
        return self.model_dump(mode="json")

    def to_json(self, *, indent: int | None = None) -> str:
        """The full result as JSON; `ExtractionResult.from_json` reads it back."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, data: str | bytes) -> ExtractionResult:
        return cls.model_validate_json(data)
