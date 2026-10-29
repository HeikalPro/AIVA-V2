"""Maps document-extractor's optional `Document.intelligence` (model-reported entities,
extracted fields and dates) onto CRM entities. Returns [] when intelligence did not run."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from document_extractor import Document

from ..schemas.base import CRMEntity, Evidence, FieldValue
from ..schemas.definitions import EntitySchema, SchemaRegistry
from .assembly import EntityBuilder, FieldMatch, LabelIndex, as_bbox, comparable, merge_entities
from .base import EntityExtractor

UNMATCHED_ENTITY_TYPE = "document"


def confidence_value(raw: Any) -> float | None:
    """0..1 from ``{"value": x, ...}`` or a bare number; None when absent or invalid."""
    if isinstance(raw, Mapping):
        raw = raw.get("value")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(v):
        return None
    return min(1.0, max(0.0, v))


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, dict)) and not value)


class IntelligenceExtractor(EntityExtractor):
    """Entities from `document.intelligence`, keeping the model's confidence and the
    block provenance it cited.

    - ``entities`` items become one entity each when their type names a registered
      schema (by name or alias); the text fills the schema's primary field.
    - ``extracted_fields`` and role-tagged ``dates`` are matched to schema fields by
      name/alias, preferring the schema named by ``document_type``.
    - With `include_unmatched_fields`, extracted fields no schema claims are kept on an
      entity typed ``document_type`` (or "document"), so nothing the model found is lost."""

    name = "intelligence"

    def __init__(self, *, include_unmatched_fields: bool = True) -> None:
        self.include_unmatched_fields = include_unmatched_fields

    def extract(self, document: Document, *, schemas: SchemaRegistry) -> list[CRMEntity]:
        intel = document.intelligence
        if not isinstance(intel, Mapping) or not intel:
            return []
        entities = self._ner_entities(intel, schemas, document.id)
        doc_type = intel.get("document_type")
        preferred = schemas.get(doc_type) if isinstance(doc_type, str) and doc_type else None

        index = LabelIndex(schemas)
        builder = EntityBuilder(schemas, extractor=self.name, document_id=document.id)
        unmatched: dict[str, FieldValue] = {}
        fields = intel.get("extracted_fields")
        if isinstance(fields, Mapping):
            for key, item in fields.items():
                fv = self._field_value(item)
                if fv is None:
                    continue
                matches = self._prefer(index.match(str(key)), preferred)
                if matches:
                    builder.add_match(matches, fv)
                elif self.include_unmatched_fields:
                    unmatched[str(key)] = fv
        for item in intel.get("dates") or []:
            if isinstance(item, Mapping) and item.get("role"):
                matches = self._prefer(index.match(str(item["role"])), preferred)
                fv = self._field_value(item)
                if matches and fv is not None:
                    builder.add_match(matches, fv)

        for built in builder.build():
            target = next((i for i, e in enumerate(entities) if e.entity_type == built.entity_type), None)
            if target is None:
                entities.append(built)
            else:
                entities[target] = merge_entities(entities[target], built, schemas)
        if unmatched:
            entity_type = doc_type if isinstance(doc_type, str) and doc_type else UNMATCHED_ENTITY_TYPE
            entities.append(
                CRMEntity(
                    entity_type=entity_type,
                    fields=unmatched,
                    source_document_id=document.id,
                    extractors=[self.name],
                )
            )
        return entities

    # ---- helpers

    @staticmethod
    def _prefer(matches: list[FieldMatch], preferred: EntitySchema | None) -> list[FieldMatch]:
        if preferred is not None:
            own = [m for m in matches if m.schema == preferred.name]
            if own:
                return own
        return matches

    def _evidence(self, provenance: Any) -> list[Evidence]:
        out: list[Evidence] = []
        for p in provenance if isinstance(provenance, list) else []:
            if not isinstance(p, Mapping):
                continue
            page = p.get("page")
            out.append(
                Evidence(
                    block_id=str(p["block_id"]) if p.get("block_id") is not None else None,
                    page=page if isinstance(page, int) and not isinstance(page, bool) else None,
                    bbox=as_bbox(p.get("bbox")),
                    source_text=str(p["source_text"]) if p.get("source_text") is not None else None,
                    extractor=self.name,
                )
            )
        return out

    def _field_value(self, item: Any) -> FieldValue | None:
        """A FieldValue from ``{"value", "confidence", "provenance"}`` or a bare value."""
        if isinstance(item, Mapping):
            value = item.get("value")
            conf = confidence_value(item.get("confidence"))
            evidence = self._evidence(item.get("provenance"))
        else:
            value, conf, evidence = item, None, []
        if _empty(value):
            return None
        if isinstance(value, str):
            value = re.sub(r"\s+", " ", value).strip()
        return FieldValue(value=value, confidence=conf, evidence=evidence, extractor=self.name)

    def _ner_entities(
        self, intel: Mapping[str, Any], schemas: SchemaRegistry, document_id: str
    ) -> list[CRMEntity]:
        out: list[CRMEntity] = []
        for item in intel.get("entities") or []:
            if not isinstance(item, Mapping):
                continue
            schema = schemas.get(str(item.get("type") or ""))
            primary = schema.primary_field if schema is not None else None
            text = item.get("text")
            if schema is None or primary is None or not isinstance(text, str) or not text.strip():
                continue
            fv = FieldValue(
                value=re.sub(r"\s+", " ", text).strip(),
                confidence=confidence_value(item.get("confidence")),
                evidence=self._evidence(item.get("provenance")),
                extractor=self.name,
            )
            entity = CRMEntity(
                entity_type=schema.name,
                fields={primary: fv},
                source_document_id=document_id,
                extractors=[self.name],
            )
            dup = next(
                (
                    i
                    for i, e in enumerate(out)
                    if e.entity_type == schema.name and comparable(e.get(primary)) == comparable(fv.value)
                ),
                None,
            )
            if dup is None:
                out.append(entity)
            else:  # the same name mentioned twice: one entity, pooled evidence
                out[dup] = merge_entities(out[dup], entity, schemas)
        return out
