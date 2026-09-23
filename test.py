"""Extract 6 Digits.pdf properly and write the result to a file.

Why it is not a plain extract() call: this PDF's text layer stores Arabic
lam-alef pairs reversed (verified against raw PyMuPDF, before document-extractor
runs), so the fast path returns broken Arabic. The script measures that, and
falls back to rendering each page and OCR'ing it when the text layer is bad.

Run:  python test.py  [path-to-pdf]
Out:  <name>.extracted.md
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pymupdf

from document_extractor import DocumentExtractor, ExtractionOptions

PDF = Path(sys.argv[1] if len(sys.argv) > 1 else "6 Digits.pdf")
OUT = PDF.parent / f"{PDF.stem}.extracted.md"
LAM, ALEF = "ل", "ا"

extractor = DocumentExtractor(ExtractionOptions(pdf_engine="mupdf", ocr="auto",
                                                ocr_languages=("ara", "eng")))
OCR_OPTS = ExtractionOptions(ocr="always", ocr_languages=("ara", "eng"), mode="accurate")


def arabic_score(text: str) -> tuple[int, int]:
    """(correct, broken). lam+alef is common in real Arabic; words ENDING in
    alef+lam instead mean the pairs are stored backwards."""
    words = re.findall(r"[؀-ۿ]+", text)
    return (sum(1 for w in words if LAM + ALEF in w),
            sum(1 for w in words if w.endswith(ALEF + LAM)))


def render_table(block) -> str:
    grid: dict[int, dict[int, str]] = {}
    for c in block.table.cells:
        grid.setdefault(c.row, {})[c.col] = c.text.strip()
    lines = []
    for r in sorted(grid):
        cols = grid[r]
        cells = [cols.get(i, "") for i in range(block.table.n_cols)]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


print(f"reading {PDF} ...")
if not PDF.exists():
    sys.exit(f"not found: {PDF.resolve()}")

# ---- 1. fast path: the PDF's own text layer --------------------------------
t0 = time.perf_counter()
doc = extractor.extract(PDF)
good, bad = arabic_score(doc.text)
print(f"  text layer : {len(doc.pages)} pages, {len(doc.blocks)} blocks, "
      f"arabic ok={good} broken={bad}  ({time.perf_counter() - t0:.1f}s)")

use_ocr = bad > good
print(f"  verdict    : text layer is {'BROKEN -> using OCR' if use_ocr else 'fine -> keeping it'}")

# ---- 2. fallback: render each page and OCR it ------------------------------
if use_ocr:
    pdf = pymupdf.open(PDF)
    parts: list[str] = []
    t0 = time.perf_counter()
    for i in range(pdf.page_count):
        png = pdf[i].get_pixmap(dpi=300).tobytes("png")
        page_doc = extractor.extract(png, filename=f"page{i + 1}.png", options=OCR_OPTS)
        parts.append(f"\n\n## Page {i + 1}\n\n{page_doc.text.strip()}")
        print(f"    page {i + 1}/{pdf.page_count} ocr'd", end="\r")
    body = "".join(parts)
    g2, b2 = arabic_score(body)
    print(f"\n  ocr        : arabic ok={g2} broken={b2}  ({time.perf_counter() - t0:.1f}s)")
else:
    chunks = []
    for b in doc.blocks:
        chunks.append(render_table(b) if b.table else b.text)
    body = "\n\n".join(c for c in chunks if c.strip())

OUT.write_text(f"# {PDF.stem}\n{body}\n", encoding="utf-8")
print(f"\nwrote {OUT}  ({len(body):,} chars)")
print("\n--- first 600 chars ---")
print(body[:600])
