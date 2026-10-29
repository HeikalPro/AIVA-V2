"""Shared machinery for extractors: matching labels to schema fields, merging
competing field values, and assembling candidate values into entities."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from ..schemas.base import CRMEntity, Evidence, FieldValue
from ..schemas.definitions import EntitySchema, SchemaRegistry
from ..text import normalize_digits, normalize_label


@dataclass(frozen=True, slots=True)
class FieldMatch:
    """A label that names `schema.field`. `explicit` when it matched a declared alias
    rather than just the field name; `order` is the schema's registration index."""

    schema: str
    field: str
    explicit: bool
    order: int


class LabelIndex:
    """Normalised label -> the schema fields it can mean, across a whole registry."""

    def __init__(self, registry: SchemaRegistry) -> None:
        self._index: dict[str, list[FieldMatch]] = {}
        for order, schema in enumerate(registry):
            for f in schema.fields:
                explicit_labels = {normalize_label(a) for a in f.aliases} - {""}
                for label in f.labels():
                    self._index.setdefault(label, []).append(
                        FieldMatch(schema.name, f.name, label in explicit_labels, order)
                    )

    def match(self, label: str) -> list[FieldMatch]:
        return list(self._index.get(normalize_label(label), []))


def as_bbox(value: Any) -> tuple[float, float, float, float] | None:
    """A 4-number box, or None for anything else."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    return (x0, y0, x1, y1)


def comparable(value: Any) -> str:
    """A loose identity for values: case, spacing and punctuation ignored."""
    if isinstance(value, (list, tuple)):
        return "|".join(comparable(v) for v in value)
    return re.sub(r"[\W_]+", "", normalize_digits(str(value)).casefold())


KeyFunc = Callable[[Any], str]


def field_key(schema: EntitySchema | None, field: str) -> KeyFunc:
    """A `comparable` that first normalises values by the field's type, so
    "01/03/2024" equals "2024-03-01" for a date field and "acme.com" equals
    "https://acme.com" for a URL field."""
    if schema is None or schema.field(field) is None:
        return comparable
    adapter = schema.field_adapter(field)

    def key(value: Any) -> str:
        try:
            return comparable(to_jsonable_python(adapter.validate_python(value)))
        except (ValidationError, ValueError, TypeError):
            return comparable(value)

    return key


def _conf(v: FieldValue) -> float:
    return -1.0 if v.confidence is None else v.confidence


def _merge_evidence(a: list[Evidence], b: list[Evidence]) -> list[Evidence]:
    seen = {e.key() for e in a}
    out = list(a)
    for e in b:
        if e.key() not in seen:
            seen.add(e.key())
            out.append(e)
    return out


def _merge_alternatives(
    winner_value: Any, *groups: list[FieldValue], key: KeyFunc = comparable
) -> list[FieldValue]:
    out: dict[str, FieldValue] = {}
    key_winner = key(winner_value)
    for group in groups:
        for alt in group:
            k = key(alt.value)
            if k == key_winner:
                continue
            flat = alt.model_copy(update={"alternatives": []})
            out[k] = merge_field_values(out[k], flat, key=key) if k in out else flat
    return list(out.values())


def merge_field_values(
    existing: FieldValue, new: FieldValue, *, as_list: bool = False, key: KeyFunc = comparable
) -> FieldValue:
    """Combine two candidates for one field.

    Equal values (loosely compared) pool their evidence and keep the higher confidence.
    Different values: the higher confidence wins (ties keep `existing`) and the loser is
    kept in `alternatives`. With `as_list`, values are concatenated without duplicates.
    `key` decides equality (see `field_key`)."""
    winner, loser = (new, existing) if _conf(new) > _conf(existing) else (existing, new)
    confidence = winner.confidence if winner.confidence is not None else loser.confidence
    if as_list:
        values: list[Any] = []
        seen: set[str] = set()
        for v in (existing.value, new.value):
            for item in v if isinstance(v, list) else [v]:
                if key(item) not in seen:
                    seen.add(key(item))
                    values.append(item)
        return FieldValue(
            value=values,
            confidence=confidence,
            evidence=_merge_evidence(existing.evidence, new.evidence),
            extractor=winner.extractor,
        )
    if key(existing.value) == key(new.value):
        return FieldValue(
            value=winner.value,
            confidence=confidence,
            evidence=_merge_evidence(winner.evidence, loser.evidence),
            extractor=winner.extractor,
            alternatives=_merge_alternatives(winner.value, winner.alternatives, loser.alternatives, key=key),
        )
    return winner.model_copy(
        update={
            "alternatives": _merge_alternatives(
                winner.value, winner.alternatives, [loser], loser.alternatives, key=key
            )
        }
    )


def merge_entities(base: CRMEntity, other: CRMEntity, registry: SchemaRegistry | None = None) -> CRMEntity:
    """`base` and `other` (same entity type) as one entity; per field, see
    `merge_field_values`. The aggregate confidence is recomputed."""
    schema = registry.get(base.entity_type) if registry is not None else None
    fields = dict(base.fields)
    for name, fv in other.fields.items():
        fdef = schema.field(name) if schema is not None else None
        as_list = fdef is not None and fdef.type == "list"
        if name in fields:
            fields[name] = merge_field_values(fields[name], fv, as_list=as_list, key=field_key(schema, name))
        else:
            fields[name] = fv
    extractors = list(dict.fromkeys([*base.extractors, *other.extractors]))
    return CRMEntity(
        entity_type=base.entity_type,
        fields=fields,
        source_document_id=base.source_document_id or other.source_document_id,
        extractors=extractors,
    )


class EntityBuilder:
    """Collects candidate values from one extractor and assembles one entity per schema.

    - `add`: a value for a known schema field (the schema becomes "anchored").
    - `add_ambiguous`: a label that fits several schema fields; resolved in `build` to
      an anchored schema first, then an explicit alias over a bare field name, then
      registration order.
    - `add_typed`: an unlabeled detection (e.g. an email found in running text);
      resolved to a schema already holding that value, else the first anchored schema
      with a free field of that type, else `fallback_schema`, else the first schema
      with such a field."""

    def __init__(
        self,
        registry: SchemaRegistry,
        *,
        extractor: str,
        document_id: str | None,
        fallback_schema: str | None = None,
    ) -> None:
        self._registry = registry
        self._extractor = extractor
        self._document_id = document_id
        self._fallback = fallback_schema
        self._values: dict[str, dict[str, FieldValue]] = {}
        self._ambiguous: list[tuple[list[FieldMatch], FieldValue]] = []
        self._typed: list[tuple[str, FieldValue]] = []

    def add(self, schema: str, field: str, value: FieldValue) -> None:
        sch = self._registry[schema]
        fdef = sch.field(field)
        as_list = fdef is not None and fdef.type == "list"
        slot = self._values.setdefault(schema, {})
        if field in slot:
            slot[field] = merge_field_values(slot[field], value, as_list=as_list, key=field_key(sch, field))
        else:
            slot[field] = value

    def add_match(self, matches: list[FieldMatch], value: FieldValue) -> None:
        """`add` for a single match, `add_ambiguous` for several."""
        if len(matches) == 1:
            self.add(matches[0].schema, matches[0].field, value)
        elif matches:
            self._ambiguous.append((matches, value))

    def add_ambiguous(self, matches: list[FieldMatch], value: FieldValue) -> None:
        self._ambiguous.append((matches, value))

    def add_typed(self, field_type: str, value: FieldValue) -> None:
        self._typed.append((field_type, value))

    def _resolve_typed(self, field_type: str, value: FieldValue) -> tuple[str, str] | None:
        schemas = [s for s in self._registry if s.fields_of_type(field_type)]
        if not schemas:
            return None
        for s in schemas:  # already known: add evidence to it
            for f in s.fields_of_type(field_type):
                have = self._values.get(s.name, {}).get(f.name)
                key = field_key(s, f.name)
                if have is not None and key(have.value) == key(value.value):
                    return s.name, f.name
        anchored = [s for s in schemas if s.name in self._values]
        for s in anchored:
            for f in s.fields_of_type(field_type):
                if f.name not in self._values[s.name]:
                    return s.name, f.name
        if anchored:
            return anchored[0].name, anchored[0].fields_of_type(field_type)[0].name
        fallback = self._registry.get(self._fallback) if self._fallback else None
        if fallback is not None and fallback.fields_of_type(field_type):
            return fallback.name, fallback.fields_of_type(field_type)[0].name
        return schemas[0].name, schemas[0].fields_of_type(field_type)[0].name

    def build(self) -> list[CRMEntity]:
        """One entity per schema that received values, in registration order."""
        anchored = set(self._values)
        for matches, value in self._ambiguous:
            best = min(matches, key=lambda m: (m.schema not in anchored, not m.explicit, m.order))
            self.add(best.schema, best.field, value)
        for field_type, value in self._typed:
            target = self._resolve_typed(field_type, value)
            if target is not None:
                self.add(target[0], target[1], value)
        self._ambiguous.clear()
        self._typed.clear()
        return [
            CRMEntity(
                entity_type=s.name,
                fields=dict(self._values[s.name]),
                source_document_id=self._document_id,
                extractors=[self._extractor],
            )
            for s in self._registry
            if self._values.get(s.name)
        ]
