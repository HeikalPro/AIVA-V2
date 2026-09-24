"""Data-driven entity schemas.

A schema is plain data (`EntitySchema` / `FieldDefinition`), so a client's CRM fields
can live in a JSON config file and be loaded with `SchemaRegistry.register_from_dict`
or `load_json`, with no code. A schema can also be declared as a Pydantic model and
registered with `SchemaRegistry.register_model`."""

from __future__ import annotations

import builtins
import json
import re
import types
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Self, Union, get_args, get_origin

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PrivateAttr,
    TypeAdapter,
    create_model,
    model_validator,
)

from ..text import (
    EMAIL_RE,
    is_plausible_phone,
    looks_like_url,
    normalize_digits,
    normalize_label,
    phone_digits,
)

FieldType = Literal["string", "number", "integer", "boolean", "date", "email", "phone", "url", "list"]

_DATE_FORMATS = (
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d.%m.%y",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%m/%d/%y",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d-%b-%Y",
)
_CURRENCY_NOISE = re.compile(r"[^\d.,+\-]")


# ---- value coercion (one function per field type) ------------------------------------


def _as_text(v: Any) -> str:
    if isinstance(v, str):
        return normalize_digits(v).strip()
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return str(v)
    raise ValueError(f"expected text, got {type(v).__name__}")


def parse_number(v: Any) -> float:
    """Numbers as written in documents: currency symbols/codes, thousands separators,
    either decimal convention, Arabic-Indic digits."""
    if isinstance(v, bool):
        raise ValueError("expected a number, got a boolean")  # noqa: TRY004 - pydantic needs ValueError
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    s = _CURRENCY_NOISE.sub("", _as_text(v))
    if not re.search(r"\d", s):
        raise ValueError(f"not a number: {v!r}")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:  # 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        # 1,234 / 1,234,567 are thousands; 12,5 is a decimal comma
        s = s.replace(",", "") if re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"not a number: {v!r}") from None


def parse_integer(v: Any) -> int:
    f = parse_number(v)
    if not f.is_integer():
        raise ValueError(f"not a whole number: {v!r}")
    return int(f)


def parse_date(v: Any) -> date:
    """ISO dates first, then common written forms, day-first before month-first."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = re.sub(r"\s+", " ", _as_text(v)).strip()
    for parse in (date.fromisoformat, lambda x: datetime.fromisoformat(x).date()):
        try:
            return parse(s)
        except ValueError:
            pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()  # noqa: DTZ007 - only the date part is used
        except ValueError:
            continue
    raise ValueError(f"not a recognisable date: {v!r}")


def parse_email(v: Any) -> str:
    s = _as_text(v)
    if not EMAIL_RE.fullmatch(s):
        raise ValueError(f"not a valid email address: {v!r}")
    local, _, domain = s.rpartition("@")
    return f"{local}@{domain.lower()}"


def parse_phone(v: Any) -> str:
    """Normalised to digits with an optional leading '+'."""
    s = _as_text(v)
    if not is_plausible_phone(s, strict=False) or re.search(r"[A-Za-z]", s):
        raise ValueError(f"not a valid phone number: {v!r}")
    return phone_digits(s)


def parse_url(v: Any) -> str:
    s = _as_text(v)
    if not looks_like_url(s):
        raise ValueError(f"not a valid URL: {v!r}")
    return s if re.match(r"^https?://", s, re.IGNORECASE) else f"https://{s}"


def parse_list(v: Any) -> list[str]:
    if isinstance(v, str):
        return [p.strip() for p in re.split(r"[,;\n]", v) if p.strip()]
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    raise ValueError(f"expected a list, got {type(v).__name__}")


def parse_string(v: Any) -> str:
    if isinstance(v, str):
        return v.strip()
    return _as_text(v)


_TYPES: dict[str, tuple[Any, Callable[[Any], Any] | None]] = {
    "string": (str, parse_string),
    "number": (float, parse_number),
    "integer": (int, parse_integer),
    "boolean": (bool, None),  # pydantic's lax bool parsing (yes/no/true/1/...)
    "date": (date, parse_date),
    "email": (str, parse_email),
    "phone": (str, parse_phone),
    "url": (str, parse_url),
    "list": (list[str], parse_list),
}


# ---- schema definitions -------------------------------------------------------------


class FieldDefinition(BaseModel):
    """One CRM field. `aliases` are the labels it may appear under in documents
    ("E-mail", "Email address", Arabic labels...), matched case- and punctuation-insensitively."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: FieldType = "string"
    required: bool = False
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    pattern: str | None = None

    @model_validator(mode="after")
    def _check_pattern(self) -> Self:
        if self.pattern is not None:
            re.compile(self.pattern)
        return self

    def labels(self) -> set[str]:
        """Normalised labels this field answers to: its name and every alias."""
        out = {normalize_label(self.name)}
        out.update(normalize_label(a) for a in self.aliases)
        out.discard("")
        return out

    def annotation(self) -> Any:
        """The Pydantic type used to validate and coerce this field's value."""
        py_type, parser = _TYPES[self.type]
        pattern = re.compile(self.pattern) if self.pattern else None
        if parser is None and pattern is None:
            return py_type

        def _validate(v: Any) -> Any:
            if pattern is not None and not pattern.fullmatch(
                _as_text(v) if not isinstance(v, str) else v.strip()
            ):
                raise ValueError(f"does not match pattern {pattern.pattern!r}")
            return parser(v) if parser is not None else v

        return Annotated[py_type, BeforeValidator(_validate)]


class EntitySchema(BaseModel):
    """An entity type: a name, its fields, and other names it is known by
    (`aliases`, e.g. an NER type such as "person"). `display_field` holds the entity's
    name; it defaults to the first field."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    fields: list[FieldDefinition] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    display_field: str | None = None

    _model: type[BaseModel] | None = PrivateAttr(default=None)
    _adapters: dict[str, TypeAdapter[Any]] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        names = [f.name for f in self.fields]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate field names in schema {self.name!r}: {dupes}")
        if self.display_field is not None and self.display_field not in names:
            raise ValueError(f"display_field {self.display_field!r} is not a field of {self.name!r}")
        return self

    # ---- lookups

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    @property
    def required_fields(self) -> list[str]:
        return [f.name for f in self.fields if f.required]

    @property
    def primary_field(self) -> str | None:
        """The field that holds the entity's name (display_field, else the first field)."""
        return self.display_field or (self.fields[0].name if self.fields else None)

    def field(self, name: str) -> FieldDefinition | None:
        return next((f for f in self.fields if f.name == name), None)

    def type_names(self) -> set[str]:
        """Normalised names this entity type answers to."""
        return {normalize_label(self.name), *(normalize_label(a) for a in self.aliases)} - {""}

    def fields_of_type(self, field_type: str) -> list[FieldDefinition]:
        return [f for f in self.fields if f.type == field_type]

    # ---- typed validation

    def build_model(self) -> type[BaseModel]:
        """A Pydantic model validating this entity's flat values (cached).

        Unknown keys are ignored; optional fields default to None."""
        if self._model is None:
            defs: dict[str, Any] = {}
            for f in self.fields:
                ann = f.annotation()
                defs[f.name] = (ann, ...) if f.required else (ann | None, None)
            model_name = (
                "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", self.name) if p) or "Entity"
            )
            self._model = create_model(
                f"{model_name}Entity",
                __config__=ConfigDict(extra="ignore", str_strip_whitespace=True),
                **defs,
            )
        return self._model

    def field_adapter(self, name: str) -> TypeAdapter[Any]:
        """A validator for one field's non-null value, with the model's constraints."""
        adapter = self._adapters.get(name)
        if adapter is None:
            info = self.build_model().model_fields[name]
            ann: Any = info.annotation
            args = [a for a in get_args(ann) if a is not type(None)]
            if _is_union(ann) and len(args) == 1:
                ann = args[0]
            annotated: Any = Annotated
            tp = annotated[(ann, *info.metadata)] if info.metadata else ann
            adapter = TypeAdapter(tp)
            self._adapters[name] = adapter
        return adapter

    @classmethod
    def from_model(
        cls,
        model: type[BaseModel],
        *,
        name: str | None = None,
        description: str | None = None,
        aliases: Sequence[str] = (),
        display_field: str | None = None,
    ) -> EntitySchema:
        """Describe a Pydantic model as an EntitySchema; the model itself is used for
        validation. Per-field `json_schema_extra` may set ``aliases`` and ``type``
        (e.g. ``Field(json_schema_extra={"type": "email", "aliases": ["E-mail"]})``)."""
        fields: list[FieldDefinition] = []
        for fname, info in model.model_fields.items():
            extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
            ftype = extra.get("type") or _infer_type(info.annotation)
            raw_aliases = extra.get("aliases") or []
            field_aliases = [str(a) for a in raw_aliases] if isinstance(raw_aliases, list) else []
            if info.alias and info.alias != fname:
                field_aliases.append(info.alias)
            fields.append(
                FieldDefinition(
                    name=fname,
                    type=ftype,
                    required=info.is_required(),
                    description=info.description or "",
                    aliases=field_aliases,
                )
            )
        schema = cls(
            name=name or model.__name__,
            description=description if description is not None else (model.__doc__ or "").strip(),
            fields=fields,
            aliases=list(aliases),
            display_field=display_field,
        )
        schema._model = model
        return schema


def _is_union(ann: Any) -> bool:
    return get_origin(ann) in (Union, types.UnionType)


def _infer_type(ann: Any) -> FieldType:
    """Best-effort field type for a model annotation (Optional and Annotated unwrapped)."""
    origin = get_origin(ann)
    if origin is Annotated:
        return _infer_type(get_args(ann)[0])
    if _is_union(ann):
        args = [a for a in get_args(ann) if a is not type(None)]
        return _infer_type(args[0]) if len(args) == 1 else "string"
    if origin in (list, tuple, set, frozenset) or ann in (list, tuple, set, frozenset):
        return "list"
    if ann is bool:
        return "boolean"
    if ann is int:
        return "integer"
    if ann in (float, Decimal):
        return "number"
    if ann in (date, datetime):
        return "date"
    return "string"


class SchemaRegistry:
    """The entity schemas known to the CRM layer, in registration order (earlier
    schemas win ambiguous label matches)."""

    def __init__(self, schemas: Iterable[EntitySchema] = ()) -> None:
        self._schemas: dict[str, EntitySchema] = {}
        for s in schemas:
            self.register(s)

    def register(self, schema: EntitySchema, *, replace: bool = False) -> EntitySchema:
        """Add a schema; a duplicate name raises ValueError unless `replace`."""
        if schema.name in self._schemas and not replace:
            raise ValueError(f"entity schema {schema.name!r} is already registered")
        self._schemas[schema.name] = schema
        return schema

    def register_model(
        self,
        model: type[BaseModel],
        *,
        name: str | None = None,
        description: str | None = None,
        aliases: Sequence[str] = (),
        display_field: str | None = None,
        replace: bool = False,
    ) -> EntitySchema:
        """Register a schema declared as a Pydantic model (see `EntitySchema.from_model`)."""
        schema = EntitySchema.from_model(
            model, name=name, description=description, aliases=aliases, display_field=display_field
        )
        return self.register(schema, replace=replace)

    def register_from_dict(
        self, data: Mapping[str, Any] | Sequence[Mapping[str, Any]], *, replace: bool = False
    ) -> builtins.list[EntitySchema]:
        """Register schemas from config data: one schema dict, a list of them, or
        ``{"schemas": [...]}``. Invalid data raises pydantic.ValidationError."""
        if isinstance(data, Mapping):
            items: Sequence[Mapping[str, Any]] = data.get("schemas", [data])
        else:
            items = data
        parsed = [EntitySchema.model_validate(dict(item)) for item in items]  # all-or-nothing
        return [self.register(s, replace=replace) for s in parsed]

    def load_json(self, path: str | Path, *, replace: bool = False) -> builtins.list[EntitySchema]:
        """Register schemas from a JSON file (same shapes as `register_from_dict`)."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return self.register_from_dict(data, replace=replace)

    def unregister(self, name: str) -> None:
        self._schemas.pop(name, None)

    def get(self, name: str) -> EntitySchema | None:
        """A schema by name, or by one of its aliases (normalised)."""
        if name in self._schemas:
            return self._schemas[name]
        key = normalize_label(name)
        return next((s for s in self._schemas.values() if key in s.type_names()), None)

    def __getitem__(self, name: str) -> EntitySchema:
        schema = self.get(name)
        if schema is None:
            raise KeyError(name)
        return schema

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def __iter__(self) -> Iterator[EntitySchema]:
        return iter(list(self._schemas.values()))

    def __len__(self) -> int:
        return len(self._schemas)

    def list(self) -> builtins.list[EntitySchema]:
        return list(self._schemas.values())

    def names(self) -> builtins.list[str]:
        return list(self._schemas)

    def copy(self) -> SchemaRegistry:
        """A new registry with the same schemas (schemas are shared, not copied)."""
        reg = SchemaRegistry()
        reg._schemas = dict(self._schemas)
        return reg

    def to_dict(self) -> dict[str, Any]:
        """``{"schemas": [...]}``, loadable with `register_from_dict`."""
        return {"schemas": [s.model_dump(mode="json") for s in self._schemas.values()]}
