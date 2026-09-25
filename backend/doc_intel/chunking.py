"""Normalized document -> knowledge-base chunks (pure Python, no I/O).

Chunks follow the document's structure:

- A heading starts a new section. When the chunk being built already holds at least
  ``min_chars`` it is closed first, so sections do not bleed into each other; a smaller
  remainder carries on into the next section with the new heading shown inline (the
  chunk header keeps the section the chunk started in, so no heading is lost and none
  is printed twice).
- Tables stay whole when they fit in one chunk; bigger tables are split by lines.
- Long blocks are split at sentence ends (``. ! ? ؟`` and line breaks), falling back to
  word boundaries and, as a last resort, a hard cut.
- A chunk closed because it is full passes its last ``overlap`` characters (cut at a
  word boundary) on to the next chunk. Chunks closed at a heading carry nothing over.

Every chunk starts with a one-line header, because the chat model sees chunk text only::

    [Document: Card FAQ.pdf · Section: Cards › Limits · Pages 3–4]

The header is counted in ``max_chars``: room for it is reserved before the body is
filled, and long file names or section paths are shortened to fit.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from backend.doc_intel.constants import KB_PAYLOAD_SOURCE, kb_vertical_for
from backend.doc_intel.normalized import NormalizedBlock, NormalizedDocument

_HEADER_SECTION_SEP = " › "
_PAYLOAD_SECTION_SEP = " > "
_HEADER_FIELD_SEP = " · "
_MIN_MAX_CHARS = 120
_MAX_INLINE_HEADING_CHARS = 300

# Whitespace after a sentence end, or any whitespace run containing a line break.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?؟])[ \t ]+|[ \t ]*\n\s*")
_BLANK_LINES = re.compile(r"\n[ \t ]*\n")
_MANY_NEWLINES = re.compile(r"\n{3,}")
_SPACES = re.compile(r"\s+")


class ChunkingFailed(Exception):
    """The document cannot be chunked; ``reason`` is shown to the admin as-is."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PreparedChunk:
    index: int
    text: str  # header line + body, <= max_chars
    content_hash: str  # sha256 hex of ``text``
    pages: tuple[int, ...]
    section: str | None  # "A > B"
    payload: dict[str, Any]


def build_chunks(
    doc: NormalizedDocument,
    *,
    document_id: int,
    filename: str,
    queue_keys: list[str],
    max_chars: int,
    overlap: int,
    max_chunks: int,
    min_chars: int = 200,
) -> list[PreparedChunk]:
    """Split ``doc`` into header-prefixed chunks of at most ``max_chars`` characters.

    Raises ``ChunkingFailed`` when the document yields no text, or more than
    ``max_chunks`` chunks. ``ValueError`` for a ``max_chars`` below 120 (corpus configs
    enforce >= 200).
    """
    max_chars = int(max_chars)
    if max_chars < _MIN_MAX_CHARS:
        raise ValueError(f"max_chars must be at least {_MIN_MAX_CHARS}")
    overlap = max(0, int(overlap))
    min_chars = max(0, int(min_chars))
    max_page = _max_page(doc)

    drafts: list[_Draft] = []
    blocks = [b for b in doc.blocks if _clean_text(b.text)]
    if blocks:
        drafts = _chunk_blocks(blocks, filename, max_chars, overlap, min_chars, max_page)
    if not drafts:
        # OCR output (or a structureless extraction): paragraphs per page.
        page_blocks = _page_blocks(doc)
        if page_blocks:
            drafts = _chunk_blocks(page_blocks, filename, max_chars, overlap, min_chars, max_page)

    if not drafts:
        raise ChunkingFailed("Document produced no text chunks")
    if len(drafts) > max_chunks:
        raise ChunkingFailed(f"Document produces {len(drafts):,} chunks; limit is {max_chunks:,}")

    vertical = kb_vertical_for(document_id)
    header_cap = _header_cap(max_chars)
    chunks: list[PreparedChunk] = []
    for index, draft in enumerate(drafts):
        header = _header(filename, draft.section, draft.pages, min(header_cap, max_chars - 1 - len(draft.body)))
        text = f"{header}\n{draft.body}"
        section = _PAYLOAD_SECTION_SEP.join(draft.section) or None
        pages = list(draft.pages)
        chunks.append(
            PreparedChunk(
                index=index,
                text=text,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                pages=tuple(pages),
                section=section,
                payload={
                    "vertical": vertical,
                    "source": KB_PAYLOAD_SOURCE,
                    "doc_id": int(document_id),
                    "filename": filename,
                    "section": section,
                    "pages": pages,
                    "queues": list(queue_keys),
                    "chunk_index": index,
                },
            )
        )
    return chunks


# ---- chunk builder -------------------------------------------------------------------------


@dataclass
class _Draft:
    body: str
    section: tuple[str, ...]
    pages: tuple[int, ...]


@dataclass
class _Builder:
    filename: str
    max_chars: int
    overlap: int
    min_chars: int
    max_page: int
    drafts: list[_Draft] = field(default_factory=list)
    section: tuple[str, ...] = ()  # section of the content being read
    chunk_section: tuple[str, ...] = ()  # section the open chunk started in (its header)
    body: str = ""
    fresh: bool = False  # body holds content not emitted yet (not just carried-over overlap)
    pages: set[int] = field(default_factory=set)
    last_kind: str | None = None
    last_pages: tuple[int, ...] = ()
    body_before_heading: str | None = None  # body before a trailing inline heading
    _budget_cache: dict[tuple[str, ...], int] = field(default_factory=dict)

    # -- sizes --

    def budget_for(self, section: tuple[str, ...]) -> int:
        """Body characters available in a chunk of ``section`` (its header line reserved)."""
        cached = self._budget_cache.get(section)
        if cached is None:
            worst_pages = (self.max_page, self.max_page) if self.max_page > 0 else ()
            header = _header(self.filename, section, worst_pages, _header_cap(self.max_chars))
            cached = self.max_chars - len(header) - 1
            self._budget_cache[section] = cached
        return cached

    def budget(self) -> int:
        return self.budget_for(self.chunk_section)

    def tight_budget(self) -> int:
        # The next chunk starts in the current section, whose header may be longer.
        return min(self.budget(), self.budget_for(self.section))

    def overlap_chars(self) -> int:
        return min(self.overlap, self.tight_budget() // 3)

    def piece_limit(self) -> int:
        # A piece must still fit after the overlap carried into the next chunk.
        return max(20, self.tight_budget() - self.overlap_chars() - 2)

    # -- input --

    def feed(self, block: NormalizedBlock) -> None:
        text = _clean_text(block.text)
        if not text:
            return
        if block.kind == "heading":
            self.enter_section((*_clean_path(block.heading_path), _one_line(text)))
            return
        path = _clean_path(block.heading_path)
        if path:
            self.enter_section(path)
        pages = tuple(sorted({int(p) for p in block.pages if isinstance(p, int) and p > 0}))
        if block.kind == "table":
            self.add_table(text, pages)
        else:
            self.add_text(text, pages, block.kind)

    def enter_section(self, new: tuple[str, ...]) -> None:
        if new == self.section:
            return
        if self.fresh and len(self.body.strip()) >= self.min_chars:
            self.flush(carry_overlap=False)
            self.section = new
            self.chunk_section = new
            return
        if self.fresh:
            # Too little to stand alone: keep it, and show the new heading inline because
            # the header keeps the section this chunk started in.
            common = _common_prefix_len(self.section, new)
            label = _truncate(
                _HEADER_SECTION_SEP.join(new[common:]) or (new[-1] if new else ""), _MAX_INLINE_HEADING_CHARS
            )
            self.section = new
            if label and len(self.body) + 2 + len(label) <= self.budget():
                before = self.body
                self.append(label, (), "\n\n", "heading")
                self.body_before_heading = before
            else:
                self.flush(carry_overlap=False)  # no room: the next chunk's header shows the section
            return
        # Nothing new in the open chunk (at most overlap from the previous section): drop it.
        self.reset()
        self.section = new
        self.chunk_section = new

    def add_text(self, text: str, pages: tuple[int, ...], kind: str) -> None:
        sep = "\n" if kind == "list_item" and self.last_kind == "list_item" else "\n\n"
        if len(text) <= self.piece_limit():
            self.append(text, pages, sep, kind)
            return
        # Too long for one chunk: pour it in sentence by sentence.
        for i, (piece, piece_sep) in enumerate(_sentence_pieces(text, self.piece_limit())):
            self.append(piece, pages, sep if i == 0 else piece_sep, kind)

    def add_table(self, text: str, pages: tuple[int, ...]) -> None:
        limit = self.tight_budget()
        if len(text) <= limit:
            self.append(text, pages, "\n\n", "table")
            return
        for i, (group, group_sep) in enumerate(_group_lines(text, limit)):
            self.append(group, pages, "\n\n" if i == 0 else group_sep, "table")

    # -- chunk assembly --

    def append(self, text: str, pages: tuple[int, ...], sep: str, kind: str) -> None:
        if len(text) > self.tight_budget():
            for i, (part, part_sep) in enumerate(_hard_split(text, self.piece_limit())):
                self.append(part, pages, sep if i == 0 else part_sep, kind)
            return
        if self.body and len(self.body) + len(sep) + len(text) > self.budget():
            # Overlap only continues prose into prose; table rows are never repeated.
            self.flush(carry_overlap=kind != "table")
        if self.body and len(self.body) + len(sep) + len(text) > self.budget():
            self.reset()  # the carried overlap leaves no room: start clean
        self.body = f"{self.body}{sep}{text}" if self.body else text
        self.pages.update(pages)
        self.fresh = True
        self.last_kind = kind
        self.last_pages = pages
        self.body_before_heading = None

    def flush(self, *, carry_overlap: bool) -> None:
        body = self.body
        trailing_heading = self.last_kind == "heading" and self.body_before_heading is not None
        if trailing_heading:
            # A heading with nothing after it belongs to the next chunk, whose header shows it.
            body = self.body_before_heading or ""
        body = body.strip()
        carry = ""
        carry_pages: tuple[int, ...] = ()
        if self.fresh and body:
            self.drafts.append(_Draft(body=body, section=self.chunk_section, pages=tuple(sorted(self.pages))))
            if carry_overlap and self.overlap > 0 and self.last_kind not in ("table", "heading"):
                carry = _tail(body, self.overlap_chars())
                carry_pages = self.last_pages
        self.body = carry
        self.pages = set(carry_pages) if carry else set()
        self.fresh = False
        self.last_kind = None
        self.body_before_heading = None
        self.chunk_section = self.section

    def reset(self) -> None:
        self.body = ""
        self.pages = set()
        self.fresh = False
        self.last_kind = None
        self.body_before_heading = None
        self.chunk_section = self.section

    def finish(self) -> list[_Draft]:
        self.flush(carry_overlap=False)
        return self.drafts


def _chunk_blocks(
    blocks: Iterable[NormalizedBlock], filename: str, max_chars: int, overlap: int, min_chars: int, max_page: int
) -> list[_Draft]:
    builder = _Builder(filename=filename, max_chars=max_chars, overlap=overlap, min_chars=min_chars, max_page=max_page)
    for block in blocks:
        builder.feed(block)
    return builder.finish()


def _page_blocks(doc: NormalizedDocument) -> list[NormalizedBlock]:
    """Paragraph blocks per page (split on blank lines), for documents without blocks."""
    out: list[NormalizedBlock] = []
    for page in doc.pages:
        text = _clean_text(page.text)
        if not text:
            continue
        for para in _BLANK_LINES.split(text):
            para = para.strip()
            if para:
                pages = [page.number] if page.number and page.number > 0 else []
                out.append(NormalizedBlock(kind="paragraph", text=para, pages=pages))
    return out


# ---- text helpers --------------------------------------------------------------------------


def _clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    return _MANY_NEWLINES.sub("\n\n", text).strip()


def _one_line(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def _clean_path(path: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(p for p in (_one_line(str(x)) for x in (path or [])) if p)


def _common_prefix_len(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _tail(body: str, n: int) -> str:
    """The last ``n`` characters of ``body``, starting at a word boundary."""
    if n <= 0 or not body:
        return ""
    if len(body) <= n:
        return body.strip()
    tail = body[-n:]
    if not body[-n - 1].isspace() and not tail[0].isspace():
        m = re.search(r"\s", tail)
        if m:
            tail = tail[m.end():]
    return tail.strip()


def _hard_split(text: str, limit: int) -> list[tuple[str, str]]:
    """Split at the last whitespace before ``limit`` (else hard cut); (piece, separator-before)."""
    limit = max(1, limit)
    parts: list[tuple[str, str]] = []
    sep = ""
    while len(text) > limit:
        window = text[: limit + 1]
        cut = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"), window.rfind(" "))
        if cut >= limit // 2:
            head = text[:cut].rstrip()
            parts.append((head, sep))
            sep = "\n" if "\n" in text[cut : cut + 1] else " "
            text = text[cut:].lstrip()
        else:
            parts.append((text[:limit], sep))
            sep = ""  # cut inside a word: re-joining must not insert a space
            text = text[limit:]
    if text:
        parts.append((text, sep))
    return [(p, s) for p, s in parts if p]


def _sentence_pieces(text: str, limit: int) -> list[tuple[str, str]]:
    """Sentences of ``text`` (hard-split when longer than ``limit``); (piece, separator-before)."""
    sentences: list[tuple[str, str]] = []
    start = 0
    sep = ""
    for m in _SENTENCE_BREAK.finditer(text):
        piece = text[start : m.start()]
        if piece.strip():
            sentences.append((piece.strip(), sep))
        sep = "\n" if "\n" in m.group() else " "
        start = m.end()
    tail = text[start:]
    if tail.strip():
        sentences.append((tail.strip(), sep))
    pieces: list[tuple[str, str]] = []
    for sentence, s_sep in sentences:
        if len(sentence) <= limit:
            pieces.append((sentence, s_sep))
            continue
        for j, (part, p_sep) in enumerate(_hard_split(sentence, limit)):
            pieces.append((part, s_sep if j == 0 else p_sep))
    return pieces


def _group_lines(text: str, limit: int) -> list[tuple[str, str]]:
    """Table lines packed into groups of at most ``limit`` chars; (group, separator-before)."""
    groups: list[tuple[str, str]] = []
    buf = ""
    for line in text.split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        if len(line) > limit:
            if buf:
                groups.append((buf, "\n"))
                buf = ""
            groups.extend((part, "\n" if j == 0 else sep) for j, (part, sep) in enumerate(_hard_split(line, limit)))
            continue
        if buf and len(buf) + 1 + len(line) > limit:
            groups.append((buf, "\n"))
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        groups.append((buf, "\n"))
    return groups


# ---- header --------------------------------------------------------------------------------


def _header_cap(max_chars: int) -> int:
    return max(48, max_chars // 4)


def _page_label(pages: tuple[int, ...] | list[int]) -> str | None:
    if not pages:
        return None
    lo, hi = min(pages), max(pages)
    return f"Page {lo}" if lo == hi else f"Pages {lo}–{hi}"


def _ellipsize_middle(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    keep_end = min(12, (limit - 1) // 3)
    return text[: limit - 1 - keep_end] + "…" + (text[-keep_end:] if keep_end else "")


def _ellipsize_left(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    return "…" + text[-(limit - 1):]


def _compose(name: str, section: str | None, page: str | None) -> str:
    parts = [f"Document: {name}"]
    if section:
        parts.append(f"Section: {section}")
    if page:
        parts.append(page)
    return "[" + _HEADER_FIELD_SEP.join(parts) + "]"


# (file name max, section max (0 = drop), keep page label), tried in order until the header fits.
_HEADER_STEPS: tuple[tuple[int | None, int | None, bool], ...] = (
    (None, None, True),
    (120, 160, True),
    (80, 80, True),
    (48, 40, True),
    (32, 0, True),
    (24, 0, False),
)


def _header(filename: str, section: tuple[str, ...], pages: tuple[int, ...] | list[int], limit: int) -> str:
    """The chunk header, shortened (section path and file name first) to fit ``limit`` chars."""
    name = _one_line(filename) or "document"
    section_text = _HEADER_SECTION_SEP.join(section)
    page = _page_label(pages)
    text = ""
    for name_max, section_max, keep_page in _HEADER_STEPS:
        sec = None if section_max == 0 else _ellipsize_left(section_text, section_max)
        text = _compose(_ellipsize_middle(name, name_max), sec, page if keep_page else None)
        if len(text) <= limit:
            return text
    return text[: max(1, limit - 1)] + "]"


def _max_page(doc: NormalizedDocument) -> int:
    numbers = [p for b in doc.blocks for p in b.pages if isinstance(p, int)]
    numbers += [p.number for p in doc.pages if isinstance(p.number, int)]
    numbers.append(int(doc.page_count or 0))
    return max([n for n in numbers if n > 0], default=0)
