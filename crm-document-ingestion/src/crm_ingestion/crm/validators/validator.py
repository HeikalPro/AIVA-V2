"""Checks extracted entities against their schemas. Reports problems; never raises on bad data."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from pydantic_core import to_jsonable_python

from ..schemas.base import CRMEntity, FieldValue
from ..schemas.definitions import EntitySchema, SchemaRegistry

Severity = Literal["error", "warning"]


class ValidationIssue(BaseModel):
    """One problem with one entity (or one of its fields)."""

    entity_type: str
    entity_index: int | None = None  # position in the list that was validated
    field: str | None = None
    severity: Severity
    code: str  # unknown_entity_type | missing_required_field | invalid_value | model_validation
    #            | unknown_field | low_confidence
    message: str


class ValidationReport(BaseModel):
    """`valid` is False when any issue is an error; warnings do not invalidate."""

    valid: bool = True
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def for_entity(self, index: int) -> list[ValidationIssue]:
        return [i for i in self.issues if i.entity_index == index]

    @classmethod
    def from_issues(cls, issues: Sequence[ValidationIssue]) -> ValidationReport:
        return cls(valid=not any(i.severity == "error" for i in issues), issues=list(issues))


def _first_error(exc: ValidationError) -> str:
    errs = exc.errors(include_url=False)
    if not errs:
        return str(exc)
    msg = str(errs[0]["msg"])
    return msg.removeprefix("Value error, ")


class EntityValidator:
    """Validates entities against a SchemaRegistry.

    Checks: entity type registered (warning), required fields present (error), each
    value valid for its field type (error), whole-model validation for schemas declared
    as Pydantic models (error), unknown fields (warning), field and entity confidence
    below `min_confidence` (warning)."""

    def __init__(self, registry: SchemaRegistry, *, min_confidence: float = 0.5) -> None:
        self.registry = registry
        self.min_confidence = min_confidence

    def validate(self, entities: Sequence[CRMEntity]) -> ValidationReport:
        issues: list[ValidationIssue] = []
        for i, entity in enumerate(entities):
            issues.extend(self.validate_entity(entity, index=i))
        return ValidationReport.from_issues(issues)

    def validate_entity(self, entity: CRMEntity, *, index: int | None = None) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []

        def issue(severity: Severity, code: str, message: str, field: str | None = None) -> None:
            issues.append(
                ValidationIssue(
                    entity_type=entity.entity_type,
                    entity_index=index,
                    field=field,
                    severity=severity,
                    code=code,
                    message=message,
                )
            )

        if entity.confidence is not None and entity.confidence < self.min_confidence:
            issue(
                "warning",
                "low_confidence",
                f"entity confidence {entity.confidence:.2f} is below {self.min_confidence:.2f}",
            )
        for name, fv in entity.fields.items():
            if fv.confidence is not None and fv.confidence < self.min_confidence:
                issue(
                    "warning",
                    "low_confidence",
                    f"confidence {fv.confidence:.2f} is below {self.min_confidence:.2f}",
                    name,
                )

        schema = self.registry.get(entity.entity_type)
        if schema is None:
            issue(
                "warning",
                "unknown_entity_type",
                f"no schema is registered for entity type {entity.entity_type!r}",
            )
            return issues

        present = {k for k, v in entity.fields.items() if not _is_empty(v.value)}
        for name in schema.required_fields:
            if name not in present:
                issue("error", "missing_required_field", f"required field {name!r} is missing", name)
        field_errors = False
        for name, fv in entity.fields.items():
            if schema.field(name) is None:
                issue("warning", "unknown_field", f"{name!r} is not a field of {schema.name!r}", name)
                continue
            if _is_empty(fv.value):
                continue
            try:
                schema.field_adapter(name).validate_python(fv.value)
            except ValidationError as exc:
                field_errors = True
                issue("error", "invalid_value", f"{fv.value!r}: {_first_error(exc)}", name)
        if not field_errors and all(n in present for n in schema.required_fields):
            try:
                schema.build_model().model_validate(self._known_values(entity, schema))
            except ValidationError as exc:
                issue("error", "model_validation", _first_error(exc))
        return issues

    def coerce(self, entity: CRMEntity) -> CRMEntity:
        """A copy whose values are normalised by their field types (dates to ISO,
        numbers to floats, phones to digits...). Values that do not validate, and fields
        of unknown schemas, are left unchanged."""
        schema = self.registry.get(entity.entity_type)
        if schema is None:
            return entity
        fields: dict[str, FieldValue] = {}
        for name, fv in entity.fields.items():
            fields[name] = fv
            if schema.field(name) is None or _is_empty(fv.value):
                continue
            try:
                value = schema.field_adapter(name).validate_python(fv.value)
            except ValidationError:
                continue
            fields[name] = fv.model_copy(update={"value": to_jsonable_python(value)})
        return entity.model_copy(update={"entity_type": schema.name, "fields": fields})

    @staticmethod
    def _known_values(entity: CRMEntity, schema: EntitySchema) -> dict[str, Any]:
        return {
            k: v.value
            for k, v in entity.fields.items()
            if schema.field(k) is not None and not _is_empty(v.value)
        }


def _is_empty(value: Any) -> bool:
    return value is None or (
        isinstance(value, (str, list, dict)) and not (value.strip() if isinstance(value, str) else value)
    )
