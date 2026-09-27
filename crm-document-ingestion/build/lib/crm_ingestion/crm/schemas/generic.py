"""Generic, industry-neutral starter schemas.

These are EXAMPLES that make the pipeline useful out of the box; they are not any
client's CRM fields. A client schema is added as data (`SchemaRegistry.register_from_dict`
/ `load_json`) or as a Pydantic model (`register_model`), and can replace these entirely
by building its own `SchemaRegistry`."""

from __future__ import annotations

from typing import Any

from .definitions import EntitySchema, SchemaRegistry

GENERIC_SCHEMA_DATA: list[dict[str, Any]] = [
    {
        "name": "organization",
        "description": "Generic example: a company or other organisation named in a document.",
        "aliases": ["org", "organisation", "company"],
        "display_field": "name",
        "fields": [
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Legal or trading name.",
                "aliases": [
                    "company",
                    "company name",
                    "organization",
                    "organization name",
                    "organisation",
                    "organisation name",
                    "business name",
                    "legal name",
                    "اسم الشركة",
                    "الشركة",
                ],
            },
            {
                "name": "email",
                "type": "email",
                "description": "Organisation email address.",
                "aliases": ["company email", "organization email", "business email", "general email"],
            },
            {
                "name": "phone",
                "type": "phone",
                "description": "Organisation phone number.",
                "aliases": [
                    "company phone",
                    "organization phone",
                    "office phone",
                    "telephone",
                    "tel",
                    "switchboard",
                    "fax",
                ],
            },
            {
                "name": "website",
                "type": "url",
                "description": "Website URL.",
                "aliases": ["web", "web site", "url", "homepage", "company website", "الموقع الإلكتروني"],
            },
            {
                "name": "address",
                "type": "string",
                "description": "Postal or registered address.",
                "aliases": [
                    "company address",
                    "registered address",
                    "business address",
                    "office address",
                    "head office",
                    "العنوان",
                ],
            },
            {
                "name": "tax_id",
                "type": "string",
                "description": "Tax or VAT registration number.",
                "aliases": [
                    "tax id",
                    "tax number",
                    "tax no",
                    "tax registration number",
                    "vat",
                    "vat number",
                    "vat no",
                    "vat id",
                    "tin",
                    "trn",
                    "الرقم الضريبي",
                ],
            },
        ],
    },
    {
        "name": "contact",
        "description": "Generic example: a person named as a point of contact.",
        "aliases": ["person", "per", "individual"],
        "display_field": "full_name",
        "fields": [
            {
                "name": "full_name",
                "type": "string",
                "required": True,
                "description": "Person's full name.",
                "aliases": [
                    "name",
                    "contact",
                    "contact name",
                    "contact person",
                    "attention",
                    "attn",
                    "full name",
                    "الاسم",
                ],
            },
            {
                "name": "email",
                "type": "email",
                "description": "Personal or work email.",
                "aliases": [
                    "email",
                    "e-mail",
                    "email address",
                    "e-mail address",
                    "contact email",
                    "mail",
                    "البريد الإلكتروني",
                ],
            },
            {
                "name": "phone",
                "type": "phone",
                "description": "Direct or mobile number.",
                "aliases": [
                    "phone",
                    "phone number",
                    "mobile",
                    "mobile number",
                    "cell",
                    "contact phone",
                    "contact number",
                    "direct line",
                    "الهاتف",
                    "الموبايل",
                    "رقم الهاتف",
                ],
            },
            {
                "name": "job_title",
                "type": "string",
                "description": "Role or position.",
                "aliases": ["title", "job title", "position", "role", "designation", "المسمى الوظيفي"],
            },
        ],
    },
    {
        "name": "document_reference",
        "description": "Generic example: identifying numbers, date and amount of a business document.",
        "aliases": ["reference"],
        "display_field": "reference_number",
        "fields": [
            {
                "name": "reference_number",
                "type": "string",
                "description": "Document or reference number.",
                "aliases": [
                    "reference",
                    "ref",
                    "ref no",
                    "reference no",
                    "reference number",
                    "document number",
                    "document no",
                    "doc no",
                    "invoice number",
                    "invoice no",
                    "order number",
                    "order no",
                    "po number",
                    "po no",
                    "contract number",
                    "رقم المرجع",
                ],
            },
            {
                "name": "date",
                "type": "date",
                "description": "Issue date of the document.",
                "aliases": ["date", "issue date", "document date", "invoice date", "dated", "التاريخ"],
            },
            {
                "name": "amount",
                "type": "number",
                "description": "Total amount.",
                "aliases": [
                    "amount",
                    "total",
                    "total amount",
                    "grand total",
                    "amount due",
                    "total due",
                    "balance due",
                    "المبلغ",
                    "الإجمالي",
                ],
            },
            {
                "name": "currency",
                "type": "string",
                "description": "Currency code or name.",
                "aliases": ["currency", "العملة"],
                "pattern": r"[A-Za-z]{3}|[^\d\s]{1,12}",
            },
        ],
    },
]


def generic_schemas() -> list[EntitySchema]:
    """Fresh copies of the generic starter schemas."""
    return [EntitySchema.model_validate(d) for d in GENERIC_SCHEMA_DATA]


def default_registry() -> SchemaRegistry:
    """A new registry holding only the generic starter schemas."""
    return SchemaRegistry(generic_schemas())
