"""Deterministic, regex-based extraction from a document's blocks.

Finds emails, phone numbers and URLs anywhere in the text, and "Label: value" pairs
(in text lines and in two-column tables) whose label matches a registered field's
name or aliases. Any schema registered at runtime is picked up with zero code."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

from document_extractor import Block, Document

from ..schemas.base import CRMEntity, Evidence, FieldValue
from ..schemas.definitions import FieldDefinition, SchemaRegistry
from ..text import find_emails, find_phones, find_urls, looks_like_url, normalize_digits
from .assembly import EntityBuilder, FieldMatch, LabelIndex, as_bbox
from .base import EntityExtractor

_KV_RE = re.compile(r"^\s*(?P<label>[^:：\n]{1,60}?)\s*[:：]\s*(?P<value>\S.*?)\s*$")
_SEGMENT_SPLIT = re.compile(r"\s+\|\s+|\s{3,}")
_MAX_LABEL_WORDS = 6

Finder = Callable[[str], list[tuple[str, int]]]


def _strict_phones(text: str) -> list[tuple[str, int]]:
    return find_phones(text, strict=True)


def _loose_phones(text: str) -> list[tuple[str, int]]:
    return find_phones(text, strict=False)


_UNLABELED: tuple[tuple[str, Finder], ...] = (
    ("email", find_emails),
    ("url", find_urls),
    ("phone", _strict_phones),
)
_TYPED_FINDERS: dict[str, Finder] = {"email": find_emails, "url": find_urls, "phone": _loose_phones}


def _block_confidence(block: Block) -> float | None:
    """Mean OCR/table confidence of a block, when the extractor reported one."""
    confs = [p.confidence for p in block.provenance if p.confidence is not None]
    if block.table is not None and block.table.confidence is not None:
        confs.append(block.table.confidence)
    return sum(confs) / len(confs) if confs else None


class PatternExtractor(EntityExtractor):
    """Regex and label/alias matching over text lines and two-column tables.

    Confidences are fixed per kind of evidence and scaled by the block's OCR/table
    confidence when one is reported. Unlabeled detections go to the schema that
    `EntityBuilder.add_typed` picks (`unlabeled_target` overrides the fallback)."""

    name = "pattern"

    def __init__(
        self,
        *,
        typed_confidence: float = 0.9,
        label_confidence: float = 0.85,
        unmatched_type_confidence: float = 0.5,
        unlabeled_confidence: float = 0.6,
        unlabeled_target: str | None = None,
        detect_unlabeled: bool = True,
    ) -> None:
        self.typed_confidence = typed_confidence
        self.label_confidence = label_confidence
        self.unmatched_type_confidence = unmatched_type_confidence
        self.unlabeled_confidence = unlabeled_confidence
        self.unlabeled_target = unlabeled_target
        self.detect_unlabeled = detect_unlabeled

    def extract(self, document: Document, *, schemas: SchemaRegistry) -> list[CRMEntity]:
        index = LabelIndex(schemas)
        builder = EntityBuilder(
            schemas, extractor=self.name, document_id=document.id, fallback_schema=self.unlabeled_target
        )
        for block in sorted(document.blocks, key=lambda b: b.reading_index):
            if block.kind == "page_break":
                continue
            scale = _block_confidence(block)
            for label, value, source in self._pairs(block):
                if label is not None and value is not None:
                    matches = index.match(label)
                    if matches and self._is_label(label):
                        self._add_labeled(
                            builder, schemas, matches, value, self._evidence(block, source), scale
                        )
                        continue
                if self.detect_unlabeled:
                    self._add_unlabeled(builder, source, self._evidence(block, source), scale)
        return builder.build()

    # ---- reading blocks

    def _pairs(self, block: Block) -> Iterator[tuple[str | None, str | None, str]]:
        """(label, value, source text) per line segment or table row; label/value are
        None when the segment is not a "Label: value" pair."""
        if block.table is not None:
            for row in block.table.to_rows():
                cells = [c.strip() for c in row if c and c.strip()]
                cells = [c for i, c in enumerate(cells) if i == 0 or c != cells[i - 1]]  # spans repeat text
                if len(cells) == 2:
                    label = cells[0].rstrip(":：").strip()
                    yield label, cells[1], f"{label}: {cells[1]}"
                else:
                    for cell in cells:
                        yield from self._line_pairs(cell)
            return
        for line in block.text.splitlines():
            yield from self._line_pairs(line)

    def _line_pairs(self, line: str) -> Iterator[tuple[str | None, str | None, str]]:
        for seg in _SEGMENT_SPLIT.split(line.strip()):
            seg = seg.strip()
            if not seg:
                continue
            m = _KV_RE.match(seg)
            if m is None and "\t" in seg:
                m = re.match(r"^\s*(?P<label>[^\t]{1,60}?)\t+(?P<value>\S.*?)\s*$", seg)
            if m is not None:
                yield m.group("label").strip(), m.group("value").strip(), seg
            else:
                yield None, None, seg

    @staticmethod
    def _is_label(label: str) -> bool:
        return 0 < len(label.split()) <= _MAX_LABEL_WORDS

    def _evidence(self, block: Block, source: str) -> Evidence:
        prov = block.provenance[0] if block.provenance else None
        return Evidence(
            block_id=block.id,
            page=block.page,
            bbox=as_bbox(prov.bbox) if prov is not None else None,
            source_text=source,
            extractor=self.name,
        )

    @staticmethod
    def _scaled(conf: float, scale: float | None) -> float:
        return round(conf * scale, 4) if scale is not None else conf

    # ---- turning matches into candidate values

    def _typed_value(self, fdef: FieldDefinition, raw: str) -> tuple[str, float]:
        """The value to record for a labeled field and its base confidence."""
        finder = _TYPED_FINDERS.get(fdef.type)
        if finder is not None:
            hits = finder(raw)
            if hits:
                return hits[0][0], self.typed_confidence
            if fdef.type == "url" and looks_like_url(raw):
                return raw, self.typed_confidence
            return raw, self.unmatched_type_confidence
        return normalize_digits(raw) if fdef.type in (
            "number",
            "integer",
            "date",
        ) else raw, self.label_confidence

    def _add_labeled(
        self,
        builder: EntityBuilder,
        schemas: SchemaRegistry,
        matches: list[FieldMatch],
        raw: str,
        evidence: Evidence,
        scale: float | None,
    ) -> None:
        # Candidates may differ per field type (an email label vs a string label), so
        # group matches by the value they would record.
        groups: dict[tuple[str, float], list[FieldMatch]] = {}
        types: set[str] = set()
        for m in matches:
            fdef = schemas[m.schema].field(m.field)
            if fdef is None:
                continue
            types.add(fdef.type)
            groups.setdefault(self._typed_value(fdef, raw), []).append(m)
        best = max(groups, key=lambda k: k[1]) if groups else None
        if best is not None:
            value, conf = best
            fv = FieldValue(
                value=value, confidence=self._scaled(conf, scale), evidence=[evidence], extractor=self.name
            )
            builder.add_match(groups[best], fv)
        # emails and URLs are unambiguous enough to pick out of any labeled value
        if self.detect_unlabeled:
            for ftype, finder in _UNLABELED[:2]:
                if ftype in types:
                    continue
                for hit, _ in finder(raw):
                    builder.add_typed(ftype, self._unlabeled_value(hit, evidence, scale))

    def _unlabeled_value(self, value: str, evidence: Evidence, scale: float | None) -> FieldValue:
        return FieldValue(
            value=value,
            confidence=self._scaled(self.unlabeled_confidence, scale),
            evidence=[evidence],
            extractor=self.name,
        )

    def _add_unlabeled(
        self, builder: EntityBuilder, text: str, evidence: Evidence, scale: float | None
    ) -> None:
        taken: list[tuple[int, int]] = []
        for ftype, finder in _UNLABELED:
            for hit, start in finder(text):
                end = start + len(hit)
                if any(start < e and s < end for s, e in taken):  # e.g. digits inside a URL
                    continue
                taken.append((start, end))
                builder.add_typed(ftype, self._unlabeled_value(hit, evidence, scale))
