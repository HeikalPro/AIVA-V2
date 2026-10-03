from __future__ import annotations

from typing import Any

import pytest
from document_extractor import Document

from crm_ingestion.crm import (
    CompositeExtractor,
    CRMEntity,
    EntityExtractor,
    FieldValue,
    IntelligenceExtractor,
    PatternExtractor,
    SchemaRegistry,
    default_registry,
)
from crm_ingestion.crm.extractors import merge_field_values
from crm_ingestion.errors import EntityExtractionError

from .fakes import CUSTOM_SCHEMA, INTELLIGENCE, business_letter, make_document, table_block, text_block


def _by_type(entities: list[CRMEntity]) -> dict[str, CRMEntity]:
    return {e.entity_type: e for e in entities}


# ---- PatternExtractor ------------------------------------------------------------------


def test_pattern_extracts_labeled_values_with_evidence() -> None:
    ents = _by_type(PatternExtractor().extract(business_letter(), schemas=default_registry()))
    org, contact, ref = ents["organization"], ents["contact"], ents["document_reference"]
    assert org.get("name") == "ACME Trading LLC"
    assert org.get("tax_id") == "123-456-789"
    assert org.get("website") == "acme-trading.example.com"
    assert contact.get("full_name") == "Sara Ahmed"
    assert contact.get("job_title") == "Procurement Manager"
    assert contact.get("email") == "sara.ahmed@acme-trading.example.com"
    assert contact.get("phone") == "+20 100 123 4567"
    ev = contact.fields["email"].evidence[0]
    assert (ev.block_id, ev.page, ev.source_text, ev.extractor) == (
        "b2",
        1,
        "Email: sara.ahmed@acme-trading.example.com",
        "pattern",
    )
    assert ev.bbox == (10.0, 20.0, 300.0, 40.0)
    assert contact.fields["email"].confidence == 0.9
    assert all(e.source_document_id == "doc0001" for e in ents.values())
    # table rows
    assert ref.get("reference_number") == "REF-2024-0042"
    assert ref.get("date") == "01/03/2024"
    assert ref.get("amount") == "12,500.00"
    assert ref.fields["amount"].evidence[0].block_id == "b3"


def test_pattern_unlabeled_detections_and_page_evidence() -> None:
    ents = _by_type(PatternExtractor().extract(business_letter(), schemas=default_registry()))
    org = ents["organization"]
    # the footer email had no label: it goes to the anchored organization (contact email is taken)
    assert org.get("email") == "info@acme-trading.example.com"
    assert org.fields["email"].confidence == 0.6
    assert org.fields["email"].evidence[0].page == 2
    # the footer URL equals the labeled website once normalised: evidence is pooled
    assert {e.block_id for e in org.fields["website"].evidence} == {"b1", "b4"}


def test_pattern_detects_in_free_text_and_arabic_digits() -> None:
    doc = make_document(
        [
            text_block("p1", "Call us on +٢٠ ١٠٠ ١٢٣ ٤٥٦٧ or email hello@example.org.", page=3),
            text_block("p2", "Invoice 2024-03-01 total 123456789 see www.example.org/help"),
        ]
    )
    ents = PatternExtractor().extract(doc, schemas=default_registry())
    [org] = ents
    assert org.get("email") == "hello@example.org"
    assert org.get("phone") == "+20 100 123 4567"
    assert org.get("website") == "www.example.org/help"
    assert org.fields["phone"].evidence[0].page == 3
    # neither the date nor the bare 9-digit number became a phone
    assert org.fields["phone"].alternatives == [] and len(org.fields["phone"].evidence) == 1


def test_pattern_ocr_confidence_scales_values() -> None:
    doc = make_document([text_block("o1", "Company: Globex", confidence=0.5)])
    [org] = PatternExtractor().extract(doc, schemas=default_registry())
    assert org.fields["name"].confidence == pytest.approx(0.425)


def test_pattern_custom_schema_from_dict_needs_no_code() -> None:
    reg = SchemaRegistry()
    reg.register_from_dict(CUSTOM_SCHEMA)
    doc = make_document(
        [
            text_block("c1", "REQUEST #: SR-0042   |   Opened: 5 February 2024"),
            table_block("c2", [["Qty:", "3"], ["Tags", "urgent, onsite"], ["Unrelated", "x"]], page=2),
        ]
    )
    [ent] = PatternExtractor().extract(doc, schemas=reg)
    assert ent.entity_type == "service_request"
    assert ent.values() == {
        "request_id": "SR-0042",
        "opened_on": "5 February 2024",
        "units": "3",
        "tags": "urgent, onsite",
    }
    assert ent.fields["units"].evidence[0].page == 2
    assert ent.fields["units"].evidence[0].source_text == "Qty: 3"


def test_pattern_arabic_labels() -> None:
    doc = make_document([text_block("a1", "اسم الشركة: شركة المثال\nالبريد الإلكتروني: info@example.com")])
    ents = _by_type(PatternExtractor().extract(doc, schemas=default_registry()))
    assert ents["organization"].get("name") == "شركة المثال"
    assert ents["contact"].get("email") == "info@example.com"
    # tatweel (kashida) and harakat in a label do not stop it matching
    doc2 = make_document([text_block("a2", "العــنوان: 1 Main St")])
    [org] = PatternExtractor().extract(doc2, schemas=default_registry())
    assert org.get("address") == "1 Main St"


def test_pattern_nothing_found() -> None:
    doc = make_document([text_block("n1", "Just a sentence. Meeting at 10:30 tomorrow.")])
    assert PatternExtractor().extract(doc, schemas=default_registry()) == []


# ---- IntelligenceExtractor -------------------------------------------------------------


def test_intelligence_absent_returns_empty() -> None:
    assert IntelligenceExtractor().extract(business_letter(), schemas=default_registry()) == []


def test_intelligence_maps_entities_fields_and_dates() -> None:
    doc = make_document([], intelligence=INTELLIGENCE)
    ents = IntelligenceExtractor().extract(doc, schemas=default_registry())
    types = [e.entity_type for e in ents]
    assert types == ["organization", "contact", "document_reference", "invoice"]  # "location" not registered
    org, contact, ref, rest = ents
    assert org.get("name") == "ACME Trading LLC"
    assert org.fields["name"].confidence == 0.95
    ev = org.fields["name"].evidence[0]
    assert (ev.block_id, ev.page, ev.bbox, ev.extractor) == (
        "b0",
        1,
        (10.0, 20.0, 300.0, 40.0),
        "intelligence",
    )
    assert org.get("tax_id") == "123-456-789"  # extracted field merged into the NER entity
    assert contact.get("full_name") == "Sara Ahmed"  # "person" is an alias of contact
    assert ref.get("reference_number") == "REF-2024-0042"
    assert ref.get("date") == "2024-03-01"  # from dates[] via role "issue_date"
    assert ref.fields["reference_number"].confidence == 0.97
    assert rest.values() == {"payment_terms": "Net 30"}  # unmatched fields are kept, not dropped


def test_intelligence_prefers_document_type_schema() -> None:
    reg = default_registry()
    reg.register_from_dict(
        {"name": "purchase_order", "fields": [{"name": "reference_number"}, {"name": "buyer"}]}
    )
    intel = {
        "document_type": "purchase_order",
        "extracted_fields": {"reference_number": {"value": "PO-1", "confidence": 0.8}, "buyer": "Initech"},
    }
    ents = IntelligenceExtractor(include_unmatched_fields=False).extract(
        make_document([], intelligence=intel), schemas=reg
    )
    [po] = ents
    assert po.entity_type == "purchase_order"
    assert po.values() == {"reference_number": "PO-1", "buyer": "Initech"}
    assert po.fields["buyer"].confidence is None


# ---- CompositeExtractor ----------------------------------------------------------------


class _Static(EntityExtractor):
    def __init__(self, name: str, entities: list[CRMEntity]) -> None:
        self.name = name
        self._entities = entities

    def extract(self, document: Document, *, schemas: SchemaRegistry) -> list[CRMEntity]:
        return [e.model_copy(deep=True) for e in self._entities]


class _Boom(EntityExtractor):
    name = "boom"

    def extract(self, document: Document, *, schemas: SchemaRegistry) -> list[CRMEntity]:
        raise RuntimeError("model endpoint unavailable")


def _org(extractor: str, **values: Any) -> CRMEntity:
    return CRMEntity(
        entity_type="organization",
        extractors=[extractor],
        fields={k: FieldValue(value=v, confidence=c, extractor=extractor) for k, (v, c) in values.items()},
    )


def test_composite_merges_by_confidence_and_keeps_alternatives() -> None:
    a = _Static("a", [_org("a", name=("ACME Ltd", 0.6), email=("x@acme.example", 0.9))])
    b = _Static(
        "b",
        [
            _org("b", name=("ACME Limited", 0.8), phone=("+201001234567", 0.7)),
            _org("b", name=("Globex", 0.9)),
        ],
    )
    out = CompositeExtractor([a, b]).run(make_document([]), schemas=default_registry())
    assert out.warnings == [] and out.extractors == ["a", "b"]
    first, second = out.entities
    assert first.values() == {"name": "ACME Limited", "email": "x@acme.example", "phone": "+201001234567"}
    assert first.fields["name"].extractor == "b"
    assert [alt.value for alt in first.fields["name"].alternatives] == ["ACME Ltd"]
    assert first.extractors == ["a", "b"]
    assert first.confidence == pytest.approx((0.8 + 0.9 + 0.7) / 3)
    assert second.get("name") == "Globex"


def test_composite_isolates_failures() -> None:
    ok = _Static("ok", [_org("ok", name=("ACME", 0.9))])
    out = CompositeExtractor([_Boom(), ok]).run(make_document([]), schemas=default_registry())
    assert [e.get("name") for e in out.entities] == ["ACME"]
    assert out.extractors == ["ok"]
    assert len(out.warnings) == 1 and "boom" in out.warnings[0] and "unavailable" in out.warnings[0]
    with pytest.raises(EntityExtractionError):
        CompositeExtractor([_Boom(), ok], fail_fast=True).run(make_document([]), schemas=default_registry())


def test_equal_values_pool_evidence() -> None:
    from crm_ingestion.crm import Evidence

    a = FieldValue(
        value="ACME Ltd", confidence=0.6, extractor="a", evidence=[Evidence(block_id="b1", extractor="a")]
    )
    b = FieldValue(
        value="acme ltd.", confidence=0.9, extractor="b", evidence=[Evidence(block_id="b2", extractor="b")]
    )
    merged = merge_field_values(a, b)
    assert merged.value == "acme ltd." and merged.confidence == 0.9 and merged.alternatives == []
    assert [e.block_id for e in merged.evidence] == ["b2", "b1"]


def test_composite_pattern_plus_intelligence() -> None:
    doc = business_letter()
    doc.intelligence = dict(INTELLIGENCE)
    ents = _by_type(
        CompositeExtractor([IntelligenceExtractor(), PatternExtractor()]).extract(
            doc, schemas=default_registry()
        )
    )
    ref = ents["document_reference"]
    # "01/03/2024" (pattern) equals "2024-03-01" (intelligence) once typed: one value, both evidences
    assert ref.get("date") == "2024-03-01"
    assert {e.extractor for e in ref.fields["date"].evidence} == {"intelligence", "pattern"}
    assert ents["organization"].extractors == ["intelligence", "pattern"]
