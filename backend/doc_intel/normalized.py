"""The normalized document: the contract between extraction and chunking.

Extraction (``extraction.py``, child process) turns a PDF/DOCX into this
library-agnostic structure and writes it to ``normalized.json`` next to the
uploaded original. Chunking (``chunking.py``) reads only this, so the pipeline
does not depend on document-extractor's internal model, and retry/republish can
re-chunk without re-extracting.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

NORMALIZED_SCHEMA = "aiva.doc_intel.normalized.v1"

BlockKind = Literal[
    "heading",
    "paragraph",
    "list_item",
    "table",
    "caption",
    "footnote",
    "code",
    "formula",
    "key_value",
    "other",
]


class NormalizedBlock(BaseModel):
    """One unit of content in reading order."""

    kind: BlockKind = "paragraph"
    text: str
    # 1-based page numbers the block came from (empty when unknown, e.g. DOCX without breaks).
    pages: list[int] = Field(default_factory=list)
    # Heading titles above this block, outermost first. Headings carry their own path
    # *excluding* themselves.
    heading_path: list[str] = Field(default_factory=list)
    # Heading level (1 = top) for kind == "heading".
    level: int | None = None


class NormalizedPage(BaseModel):
    number: int
    text: str = ""
    # text | scanned | mixed | empty | failed | ocr (page-image OCR fallback)
    classification: str | None = None


class NormalizedWarning(BaseModel):
    code: str
    message: str
    page: int | None = None


class NormalizedDocument(BaseModel):
    schema_id: str = Field(default=NORMALIZED_SCHEMA, alias="schema")
    filename: str
    media_type: str
    sha256: str
    page_count: int = 0
    blocks: list[NormalizedBlock] = Field(default_factory=list)
    pages: list[NormalizedPage] = Field(default_factory=list)
    warnings: list[NormalizedWarning] = Field(default_factory=list)
    # name, version, mode, ocr_languages, pdf_engine, fallback (None | "page_ocr"), seconds, ...
    extractor: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @property
    def text_chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.model_dump_json(by_alias=True, indent=None), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "NormalizedDocument":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
