"""Section-aware chunk builder: headers, sizes, sentence splitting, tables, OCR fallback, limits."""
from __future__ import annotations

import hashlib

import pytest

from backend.doc_intel.chunking import ChunkingFailed, PreparedChunk, build_chunks
from backend.doc_intel.constants import KB_PAYLOAD_SOURCE, kb_vertical_for
from backend.doc_intel.normalized import NormalizedBlock, NormalizedDocument, NormalizedPage

FILENAME = "Card FAQ.pdf"


def make_doc(blocks=(), pages=(), page_count=0) -> NormalizedDocument:
    return NormalizedDocument(
        filename=FILENAME,
        media_type="application/pdf",
        sha256="0" * 64,
        page_count=page_count,
        blocks=list(blocks),
        pages=list(pages),
    )


def para(text, page=None, path=(), kind="paragraph") -> NormalizedBlock:
    return NormalizedBlock(kind=kind, text=text, pages=[page] if page else [], heading_path=list(path))


def heading(text, path=(), page=None, level=1) -> NormalizedBlock:
    return NormalizedBlock(kind="heading", text=text, pages=[page] if page else [], heading_path=list(path), level=level)


def build(doc, **kw) -> list[PreparedChunk]:
    args = dict(document_id=12, filename=FILENAME, queue_keys=["HALAN"], max_chars=600, overlap=80, max_chunks=100)
    args.update(kw)
    return build_chunks(doc, **args)


def body(chunk: PreparedChunk) -> str:
    return chunk.text.split("\n", 1)[1]


def sentences(n: int, prefix: str = "Rule", mark: str = ".") -> str:
    return " ".join(f"{prefix} {i:02d} explains how the replacement card fee is charged to the wallet{mark}" for i in range(n))


def test_header_with_section_and_single_page():
    chunks = build(make_doc([heading("Cards", page=1), para("Replacement cards cost 50 EGP.", page=1, path=["Cards"])]))
    assert len(chunks) == 1
    assert chunks[0].text == "[Document: Card FAQ.pdf · Section: Cards · Page 1]\nReplacement cards cost 50 EGP."
    assert chunks[0].pages == (1,)
    assert chunks[0].section == "Cards"


def test_header_shows_a_page_range():
    doc = make_doc([para("First part on page three.", page=3), para("Second part on page four.", page=4)])
    (chunk,) = build(doc)
    assert chunk.text.startswith("[Document: Card FAQ.pdf · Pages 3–4]\n")
    assert chunk.pages == (3, 4)


def test_header_omits_unknown_section_and_page():
    doc = make_doc([para("A DOCX paragraph without page information.")])
    (chunk,) = build(doc, filename="notes.docx")
    assert chunk.text == "[Document: notes.docx]\nA DOCX paragraph without page information."
    assert chunk.section is None and chunk.pages == ()


def test_nested_sections_use_arrow_in_header_and_gt_in_payload():
    doc = make_doc(
        [
            heading("Cards", page=1),
            heading("Limits", path=["Cards"], page=1, level=2),
            para("Daily limit is 10,000 EGP.", page=1, path=["Cards", "Limits"]),
        ]
    )
    (chunk,) = build(doc)
    assert "Section: Cards › Limits" in chunk.text.splitlines()[0]
    assert chunk.section == "Cards > Limits"
    assert chunk.payload["section"] == "Cards > Limits"
    assert body(chunk) == "Daily limit is 10,000 EGP."  # headings are not repeated in the body


def test_every_chunk_fits_and_hash_matches():
    blocks = [heading("Policy", page=1)]
    for i in range(30):
        blocks.append(para(sentences(3, prefix=f"P{i}"), page=1 + i // 5, path=["Policy"]))
    chunks = build(make_doc(blocks, page_count=6), max_chars=600)
    assert len(chunks) > 5
    for i, chunk in enumerate(chunks):
        assert len(chunk.text) <= 600
        assert chunk.content_hash == hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        assert chunk.index == i and chunk.payload["chunk_index"] == i


def test_heading_closes_a_chunk_that_is_big_enough():
    a_text = "Alpha " + "x" * 290
    b_text = "Bravo " + "y" * 290
    doc = make_doc(
        [heading("SectionOne", page=1), para(a_text, page=1, path=["SectionOne"]),
         heading("SectionTwo", page=2), para(b_text, page=2, path=["SectionTwo"])]
    )
    chunks = build(doc)
    assert [c.section for c in chunks] == ["SectionOne", "SectionTwo"]
    assert body(chunks[0]) == a_text
    assert body(chunks[1]) == b_text  # no overlap across a heading, heading not in the body
    assert chunks[1].text.count("SectionTwo") == 1


def test_small_section_continues_with_the_heading_inline():
    doc = make_doc(
        [heading("Intro", page=1), para("Short intro.", page=1, path=["Intro"]),
         heading("Fees", page=1), para("Replacement costs 50 EGP.", page=1, path=["Fees"])]
    )
    (chunk,) = build(doc)
    assert chunk.section == "Intro"  # header keeps the section the chunk started in
    assert body(chunk) == "Short intro.\n\nFees\n\nReplacement costs 50 EGP."


def test_long_block_is_split_at_sentence_ends():
    text = sentences(30)
    chunks = build(make_doc([para(text, page=1)]))
    assert len(chunks) >= 3
    for chunk in chunks:
        assert len(chunk.text) <= 600
        assert body(chunk).endswith(".")


def test_arabic_question_marks_are_sentence_ends():
    arabic = " ".join(f"هل يمكنني استبدال البطاقة رقم {i} من خلال التطبيق دون زيارة الفرع؟" for i in range(40))
    chunks = build(make_doc([para(arabic, page=1)]))
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= 600
        assert body(chunk).endswith("؟")


def test_overlap_repeats_the_previous_tail_from_a_word_boundary():
    chunks = build(make_doc([para(sentences(30), page=1)]), overlap=80)
    first, second = body(chunks[0]), body(chunks[1])
    overlap = max(k for k in range(0, 81) if k == 0 or second.startswith(first[-k:]))
    assert 0 < overlap <= 80
    assert first[-overlap - 1].isspace()  # started at a word boundary


def test_overlap_survives_a_section_change_inside_a_chunk():
    # Regression: the next chunk's header (longer section path) is smaller than the
    # current one's; pieces must be sized for it or the overlap would be dropped.
    doc = make_doc(
        [
            heading("PIN", page=1),
            para("The PIN protects transfers.", page=1, path=["PIN"]),
            heading("Resetting the PIN from the mobile application", path=["PIN"], page=1, level=2),
            para(sentences(12), page=1, path=["PIN", "Resetting the PIN from the mobile application"]),
        ]
    )
    chunks = build(doc, max_chars=400, overlap=60)
    assert len(chunks) >= 3
    for prev, nxt in zip(chunks, chunks[1:]):
        a, b = body(prev), body(nxt)
        assert any(b.startswith(a[-k:]) for k in range(20, 61)), (a[-80:], b[:80])


def test_trailing_inline_heading_moves_to_the_next_chunk():
    lines: list[str] = []
    while len("\n".join(lines)) < 505:  # fits a chunk alone (~540 chars), not after the intro
        lines.append(f"| Fee {len(lines):02d} | {len(lines) * 5:03d} EGP | details |")
    table = "\n".join(lines)
    doc = make_doc(
        [
            heading("Intro", page=1),
            para("A short introduction about fees.", page=1, path=["Intro"]),
            heading("Fee table", page=1),
            para(table, page=1, path=["Fee table"], kind="table"),
        ]
    )
    chunks = build(doc)
    assert body(chunks[0]) == "A short introduction about fees."  # no dangling heading
    assert chunks[1].section == "Fee table"
    assert body(chunks[1]) == table  # heading only in the header, not repeated
    assert chunks[1].text.count("Fee table") == 1


def test_no_overlap_when_disabled():
    chunks = build(make_doc([para(sentences(30), page=1)]), overlap=0)
    first, second = body(chunks[0]), body(chunks[1])
    assert not any(second.startswith(first[-k:]) for k in range(5, 40))


def test_table_stays_whole_when_it_fits():
    table = "\n".join(f"| Card {i} | {i * 10} EGP | 3 days |" for i in range(8))
    doc = make_doc([para("x " * 200, page=1), para(table, page=1, kind="table"), para("After the table.", page=1)])
    chunks = build(doc)
    assert any(table in c.text for c in chunks)
    assert all(len(c.text) <= 600 for c in chunks)


def test_big_table_is_split_by_lines():
    lines = [f"| Row {i:03d} | value {i * 7} | note for row {i} |" for i in range(60)]
    chunks = build(make_doc([para("\n".join(lines), page=2, kind="table")]))
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= 600
        for line in body(chunk).split("\n"):
            assert line in lines  # never cut inside a row
    joined = [line for c in chunks for line in body(c).split("\n")]
    assert joined == lines  # every row once, in order (no overlap inside tables)


def test_ocr_output_falls_back_to_page_paragraphs():
    doc = make_doc(
        blocks=[],
        pages=[
            NormalizedPage(number=1, text="First scanned paragraph.\n\nSecond scanned paragraph.", classification="ocr"),
            NormalizedPage(number=2, text="Third one on page two.", classification="ocr"),
            NormalizedPage(number=3, text="   ", classification="empty"),
        ],
        page_count=3,
    )
    (chunk,) = build(doc)
    assert chunk.pages == (1, 2)
    assert body(chunk) == "First scanned paragraph.\n\nSecond scanned paragraph.\n\nThird one on page two."
    assert "Pages 1–2" in chunk.text.splitlines()[0]


def test_headings_only_document_falls_back_to_page_text():
    doc = make_doc(
        blocks=[heading("Only a title", page=1)],
        pages=[NormalizedPage(number=1, text="Only a title\n\nBut the page has real text.")],
    )
    (chunk,) = build(doc)
    assert "But the page has real text." in chunk.text


def test_no_text_raises():
    doc = make_doc(blocks=[para("   ")], pages=[NormalizedPage(number=1, text="\n\n")])
    with pytest.raises(ChunkingFailed) as info:
        build(doc)
    assert info.value.reason == "Document produced no text chunks"


def test_chunk_limit_raises_with_counts():
    doc = make_doc([para(sentences(60), page=1)])
    total = len(build(doc, max_chunks=1000))
    with pytest.raises(ChunkingFailed) as info:
        build(doc, max_chunks=2)
    assert info.value.reason == f"Document produces {total} chunks; limit is 2"
    big = make_doc([para(f"Paragraph {i} " + "z" * 150) for i in range(2500)])
    with pytest.raises(ChunkingFailed) as info:
        build(big, max_chunks=2000, max_chars=200)
    assert info.value.reason.endswith("chunks; limit is 2,000")


def test_payload_and_parent_id():
    (chunk,) = build(
        make_doc([heading("Cards", page=5), para("Text.", page=5, path=["Cards"])]),
        document_id=12,
        queue_keys=["HALAN", "Gomla"],
    )
    assert chunk.payload == {
        "vertical": "kbdoc-12",
        "source": KB_PAYLOAD_SOURCE,
        "doc_id": 12,
        "filename": FILENAME,
        "section": "Cards",
        "pages": [5],
        "queues": ["HALAN", "Gomla"],
        "chunk_index": 0,
    }
    # external_parent_id (= vertical) fits kb_chunk.external_parent_id VARCHAR2(64) for any id
    assert len(kb_vertical_for(10**30)) <= 64


def test_long_filename_and_deep_sections_are_shortened_to_fit():
    path = [f"Chapter {i} " + "w" * 70 for i in range(6)]
    blocks = [heading(path[i], path=path[:i], page=1) for i in range(6)]
    blocks.append(para(sentences(12), page=1, path=path))
    chunks = build(make_doc(blocks), filename=("very long name " * 20) + ".pdf", max_chars=300)
    assert chunks
    for chunk in chunks:
        assert len(chunk.text) <= 300
        assert chunk.text.startswith("[Document: ")
        assert "…" in chunk.text.splitlines()[0]


def test_giant_word_is_hard_split():
    word = "x" * 1500
    chunks = build(make_doc([para(word, page=1)]), overlap=0)
    assert len(chunks) >= 3
    assert all(len(c.text) <= 600 for c in chunks)
    assert "".join(body(c) for c in chunks) == word


def test_list_items_are_kept_on_consecutive_lines():
    doc = make_doc([para("- first", kind="list_item"), para("- second", kind="list_item"), para("After list.")])
    (chunk,) = build(doc)
    assert body(chunk) == "- first\n- second\n\nAfter list."


def test_max_chars_below_minimum_is_rejected():
    with pytest.raises(ValueError):
        build(make_doc([para("text")]), max_chars=50)
