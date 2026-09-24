from __future__ import annotations

import json

import pytest

from crm_ingestion.config import CRMSettings
from crm_ingestion.crm import (
    CRMEntity,
    CRMExtractionService,
    CRMProcessingResult,
    EntityValidator,
    ExtractionResult,
    FieldValue,
    PatternExtractor,
    SchemaRegistry,
    default_registry,
)

from .fakes import CUSTOM_SCHEMA, INTELLIGENCE, business_letter, make_document, make_ingested, text_block


def _entity(entity_type: str, confidence: float = 0.9, **values: object) -> CRMEntity:
    return CRMEntity(
        entity_type=entity_type,
        fields={k: FieldValue(value=v, confidence=confidence) for k, v in values.items()},
    )


def _codes(report_issues: list) -> set[tuple[str, str | None]]:  # type: ignore[type-arg]
    return {(i.code, i.field) for i in report_issues}


# ---- EntityValidator -------------------------------------------------------------------


def test_valid_entity_has_no_issues() -> None:
    v = EntityValidator(default_registry(), min_confidence=0.5)
    report = v.validate(
        [_entity("contact", full_name="Sara Ahmed", email="sara@example.com", phone="+20 100 123 4567")]
    )
    assert report.valid and report.issues == []


def test_missing_required_and_bad_email_are_errors() -> None:
    v = EntityValidator(default_registry(), min_confidence=0.5)
    report = v.validate([_entity("contact", email="not-an-email")])
    assert not report.valid
    assert _codes(report.errors) == {("missing_required_field", "full_name"), ("invalid_value", "email")}
    assert all(i.entity_index == 0 and i.entity_type == "contact" for i in report.issues)
    assert "email" in next(i.message for i in report.errors if i.field == "email")


def test_low_confidence_and_unknown_fields_are_warnings() -> None:
    v = EntityValidator(default_registry(), min_confidence=0.7)
    report = v.validate([_entity("organization", confidence=0.4, name="ACME", favourite_colour="blue")])
    assert report.valid  # warnings only
    assert _codes(report.warnings) == {
        ("low_confidence", None),
        ("low_confidence", "name"),
        ("low_confidence", "favourite_colour"),
        ("unknown_field", "favourite_colour"),
    }


def test_unknown_entity_type_is_a_warning() -> None:
    report = EntityValidator(default_registry()).validate([_entity("spaceship", callsign="X")])
    assert report.valid
    assert _codes(report.issues) == {("unknown_entity_type", None)}


def test_validator_never_raises_on_odd_values() -> None:
    v = EntityValidator(default_registry())
    report = v.validate([_entity("document_reference", amount={"weird": 1}, date=["x"], currency=None)])
    assert {i.field for i in report.errors} == {"amount", "date"}


def test_custom_schema_pattern_and_types() -> None:
    reg = SchemaRegistry()
    reg.register_from_dict(CUSTOM_SCHEMA)
    v = EntityValidator(reg)
    report = v.validate([_entity("ticket", request_id="XX-1", units="2.5", opened_on="2024-02-05")])
    assert _codes(report.errors) == {("invalid_value", "request_id"), ("invalid_value", "units")}
    coerced = v.coerce(_entity("ticket", request_id="SR-0001", units="1,000", opened_on="05/02/2024"))
    assert coerced.entity_type == "service_request"
    assert coerced.values() == {"request_id": "SR-0001", "units": 1000, "opened_on": "2024-02-05"}


# ---- CRMExtractionService --------------------------------------------------------------


def test_service_process_ingested_document() -> None:
    doc = business_letter()
    doc.intelligence = dict(INTELLIGENCE)
    ingested = make_ingested(doc, warnings=["page 3 was empty"])
    service = CRMExtractionService(settings=CRMSettings(min_confidence=0.5))
    result = service.process(ingested)

    assert isinstance(result, CRMProcessingResult)
    ext = result.extraction
    assert ext.document_id == "doc0001"
    assert ext.source is not None and ext.source["filename"] == "sample.pdf"
    assert ext.source["source_system"] == "sharepoint"
    assert ext.document_type == "invoice" and ext.document_type_confidence == pytest.approx(0.92)
    assert ext.summary == "An invoice from ACME Trading LLC."
    assert ext.extractors == ["intelligence", "pattern"]
    assert ext.warnings == ["ingestion: page 3 was empty"]
    assert [e.entity_type for e in ext.entities] == [
        "organization",
        "contact",
        "document_reference",
        "invoice",
    ]

    crm = {e["_meta"]["entity_type"]: e for e in result.to_crm_json()}
    assert crm["organization"]["name"] == "ACME Trading LLC"
    assert crm["organization"]["website"] == "https://acme-trading.example.com"  # coerced
    assert crm["contact"]["phone"] == "+201001234567"
    assert crm["document_reference"]["date"] == "2024-03-01"
    assert crm["document_reference"]["amount"] == 12500.0
    assert crm["contact"]["_meta"]["fields"]["email"]["provenance"][0]["block_id"] == "b2"

    # the only issue: the model's unmatched "invoice" fields have no registered schema
    assert result.valid
    assert _codes(result.validation.issues) == {("unknown_entity_type", None)}
    json.loads(result.to_json())


def test_service_process_document_without_intelligence_or_source() -> None:
    doc = make_document([text_block("x1", "Contact: Omar Nabil\nEmail: omar@example.net")])
    result = CRMExtractionService(settings=CRMSettings()).process_document(doc)
    assert result.extraction.source is None
    assert result.extraction.document_type is None
    [contact] = result.extraction.entities
    assert contact.values() == {"full_name": "Omar Nabil", "email": "omar@example.net"}
    assert result.valid


def test_service_reports_extractor_failures_and_custom_registry() -> None:
    class Broken(PatternExtractor):
        name = "broken"

        def extract(self, document, *, schemas):  # type: ignore[no-untyped-def]
            raise ValueError("bad regex config")

    reg = SchemaRegistry()
    reg.register_from_dict(CUSTOM_SCHEMA)
    doc = make_document(
        [text_block("t1", "Ticket No: SR-1234\nQty: 7")], metadata={"source": {"filename": "t.pdf"}}
    )
    service = CRMExtractionService([Broken(), PatternExtractor()], registry=reg, settings=CRMSettings())
    result = service.process_document(doc)
    assert result.extraction.source == {"filename": "t.pdf"}
    assert result.extraction.extractors == ["pattern"]
    assert len(result.extraction.warnings) == 1 and "broken" in result.extraction.warnings[0]
    [ticket] = result.extraction.entities
    assert ticket.values() == {"request_id": "SR-1234", "units": 7}
    assert result.valid


def test_processing_result_round_trips_through_json() -> None:
    doc = business_letter()
    doc.intelligence = dict(INTELLIGENCE)
    result = CRMExtractionService(settings=CRMSettings()).process(make_ingested(doc))
    back = CRMProcessingResult.model_validate_json(result.to_json())
    assert back == result
    assert ExtractionResult.from_json(result.extraction.to_json()) == result.extraction
