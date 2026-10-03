from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, ValidationError

from crm_ingestion.crm import (
    CRMEntity,
    EntitySchema,
    Evidence,
    ExtractionResult,
    FieldDefinition,
    FieldValue,
    SchemaRegistry,
    default_registry,
)
from crm_ingestion.crm.schemas.generic import GENERIC_SCHEMA_DATA

from .fakes import CUSTOM_SCHEMA


def _entity() -> CRMEntity:
    return CRMEntity(
        entity_type="organization",
        source_document_id="doc0001",
        extractors=["pattern"],
        fields={
            "name": FieldValue(
                value="ACME",
                confidence=0.9,
                extractor="pattern",
                evidence=[
                    Evidence(
                        block_id="b1",
                        page=1,
                        bbox=(1, 2, 3, 4),
                        source_text="Company: ACME",
                        extractor="pattern",
                    )
                ],
            ),
            "email": FieldValue(value="info@acme.example", confidence=0.5, extractor="pattern"),
        },
    )


def test_generic_schemas_are_registered_by_default() -> None:
    reg = default_registry()
    assert reg.names() == ["organization", "contact", "document_reference"]
    assert reg["organization"].field_names == ["name", "email", "phone", "website", "address", "tax_id"]
    assert reg["contact"].field_names == ["full_name", "email", "phone", "job_title"]
    assert reg["document_reference"].field_names == ["reference_number", "date", "amount", "currency"]
    assert reg.get("person") is reg["contact"]  # alias lookup
    assert "Company" in reg and "nope" not in reg


def test_generic_data_is_marked_as_examples() -> None:
    assert all("Generic example" in d["description"] for d in GENERIC_SCHEMA_DATA)


def test_register_from_dict_and_build_model_coerces() -> None:
    reg = SchemaRegistry()
    [schema] = reg.register_from_dict(CUSTOM_SCHEMA)
    model = schema.build_model()
    obj = model.model_validate(
        {"request_id": "SR-0042", "opened_on": "05/02/2024", "units": "1,000", "tags": "a, b;c", "ignored": 1}
    )
    assert obj.model_dump() == {
        "request_id": "SR-0042",
        "opened_on": date(2024, 2, 5),
        "units": 1000,
        "tags": ["a", "b", "c"],
    }
    with pytest.raises(ValidationError):
        model.model_validate({"request_id": "BAD-1"})  # pattern
    with pytest.raises(ValidationError):
        model.model_validate({})  # required


def test_register_from_dict_accepts_lists_and_rejects_bad_data(tmp_path: Path) -> None:
    reg = SchemaRegistry()
    path = tmp_path / "schemas.json"
    path.write_text(json.dumps({"schemas": [CUSTOM_SCHEMA]}), encoding="utf-8")
    assert [s.name for s in reg.load_json(path)] == ["service_request"]
    with pytest.raises(ValueError):
        reg.register_from_dict(CUSTOM_SCHEMA)  # duplicate
    reg.register_from_dict([CUSTOM_SCHEMA], replace=True)
    with pytest.raises(ValidationError):
        reg.register_from_dict({"name": "x", "fields": [{"name": "a", "type": "colour"}]})
    with pytest.raises(ValidationError):
        reg.register_from_dict({"name": "x", "fields": [{"name": "a"}, {"name": "a"}]})
    assert SchemaRegistry().register_from_dict(reg.to_dict())[0].name == "service_request"


def test_register_model_declares_schema_from_pydantic() -> None:
    class Vendor(BaseModel):
        """Test-only model schema."""

        vendor_name: str = Field(json_schema_extra={"aliases": ["Supplier"]})
        contact_email: str | None = Field(default=None, json_schema_extra={"type": "email"})
        employees: int | None = None
        founded: date | None = None

    reg = SchemaRegistry()
    schema = reg.register_model(Vendor, name="vendor")
    assert [(f.name, f.type, f.required) for f in schema.fields] == [
        ("vendor_name", "string", True),
        ("contact_email", "email", False),
        ("employees", "integer", False),
        ("founded", "date", False),
    ]
    assert "supplier" in schema.fields[0].labels()
    assert schema.build_model() is Vendor


def test_field_types_coerce() -> None:
    schema = EntitySchema(
        name="t",
        fields=[
            FieldDefinition(name="e", type="email"),
            FieldDefinition(name="p", type="phone"),
            FieldDefinition(name="u", type="url"),
            FieldDefinition(name="n", type="number"),
            FieldDefinition(name="d", type="date"),
            FieldDefinition(name="b", type="boolean"),
        ],
    )

    def check(name: str, v: object) -> object:
        result: object = schema.field_adapter(name).validate_python(v)
        return result

    assert check("e", " Info@ACME.Example ") == "Info@acme.example"
    assert check("p", "+٢٠ ١٠٠ ١٢٣ ٤٥٦٧") == "+201001234567"  # Arabic-Indic digits
    assert check("p", "0020 100 123 4567") == "+201001234567"
    assert check("u", "acme.example/about") == "https://acme.example/about"
    assert check("n", "EGP 1.234,50") == 1234.5
    assert check("d", "March 1, 2024") == date(2024, 3, 1)
    assert check("b", "yes") is True
    for name, bad in [("e", "not-an-email"), ("p", "12"), ("u", "hello world"), ("n", "n/a"), ("d", "soon")]:
        with pytest.raises(ValidationError):
            check(name, bad)


def test_entity_confidence_defaults_to_field_mean() -> None:
    assert _entity().confidence == pytest.approx(0.7)
    assert CRMEntity(entity_type="x").confidence is None
    assert (
        CRMEntity(
            entity_type="x", confidence=0.3, fields={"a": FieldValue(value=1, confidence=0.9)}
        ).confidence
        == 0.3
    )


def test_to_crm_json_shape() -> None:
    out = _entity().to_crm_json()
    assert out["name"] == "ACME" and out["email"] == "info@acme.example"
    meta = out["_meta"]
    assert meta["entity_type"] == "organization"
    assert meta["confidence"] == pytest.approx(0.7)
    assert meta["source_document_id"] == "doc0001"
    assert meta["fields"]["name"]["provenance"] == [
        {
            "block_id": "b1",
            "page": 1,
            "bbox": [1.0, 2.0, 3.0, 4.0],
            "source_text": "Company: ACME",
            "extractor": "pattern",
        }
    ]
    json.dumps(out)  # JSON-serialisable


def test_extraction_result_json_round_trip() -> None:
    result = ExtractionResult(
        document_id="doc0001",
        source={"filename": "a.pdf"},
        entities=[_entity()],
        document_type="letter",
        summary="s",
        extractors=["pattern"],
        warnings=["w"],
    )
    text = result.to_json(indent=2)
    back = ExtractionResult.from_json(text)
    assert back == result
    assert back.entities_of("organization")[0].get("name") == "ACME"
    assert json.loads(text)["entities"][0]["fields"]["name"]["evidence"][0]["block_id"] == "b1"
