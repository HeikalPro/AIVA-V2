"""Child-process entry point for document extraction.

    python -m backend.doc_intel.extraction_worker --job <job.json>

``backend.doc_intel.extraction`` (Flow 1) and ``backend.doc_intel.crm_extraction``
(Flow 2) start this process, one document at a time. It is never run by hand in
production. A job's ``kind`` selects what it does:
- ``"extract"`` (the default): the normalized document is written to ``output_path``;
- ``"crm"``: the same extraction, then crm-document-ingestion's pattern extractors and
  validator; ONE result JSON (normalized document, CRM entities, full result, validity,
  issues, metrics) is written to ``output_path``. See "CRM job" below.

It imports only the following. It never imports ``backend.config`` or anything else
that reads ``.env``, so it holds no secrets and cannot reach the database:
- document-extractor;
- pypdfium2 and Pillow, to render pages for the OCR fallback;
- ``backend.doc_intel.normalized`` and ``backend.doc_intel.arabic``;
- for CRM jobs only (imported lazily): crm-document-ingestion, whose settings classes
  are only ever built with ``_env_file=None`` and explicit values.

Every extractor option is explicit:
- document intelligence off;
- no vision provider, no cloud backend or OCR engine;
- the pdfium engine;
- no settings file (``DOCUMENT_EXTRACTOR_*`` variables are removed before the library
  is imported, and ``DocumentExtractor`` is always given explicit options).
So no document text leaves the server.

Result protocol:
- exit 0: the normalized document (CRM job: the result JSON) was written to ``output_path``;
- exit 1: a handled failure. ``{"error": {"code", "reason", "suggested_action"}}``
  was written to ``error_path`` (CRM jobs add ``"stage"``: extraction | intelligence |
  entities);
- exit 2: the job file is unusable;
- exit 3: an unexpected error. The same error file is written with code ``crash``,
  and a traceback goes to stderr for the server log.

Every reason is short, admin-readable and free of server paths.

Arabic fallback, PDF only: when the text layer stores lam-alef pairs reversed (see
``arabic.py``), every page is rendered with pypdfium2 (not AGPL PyMuPDF) and OCR'd as
an image. document-extractor's ``ocr="always"`` does not re-read PDF pages that
already have a text layer, however broken, which is why the pages are rendered here.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import traceback
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backend.doc_intel import arabic
from backend.doc_intel.normalized import (
    NormalizedBlock,
    NormalizedDocument,
    NormalizedPage,
    NormalizedWarning,
)

JOB_VERSION = 1
JOB_KIND_EXTRACT = "extract"
JOB_KIND_CRM = "crm"
# The child-side stages of a SharePoint file (CrmChildStage in crm_extraction.py).
CRM_CHILD_STAGES = ("extraction", "intelligence", "entities")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CRASH = 3

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_KIND_BY_MEDIA_TYPE = {PDF_MEDIA_TYPE: "pdf", DOCX_MEDIA_TYPE: "docx"}

_REQUIRED_JOB_KEYS = (
    "input_path", "output_path", "error_path", "filename", "media_type", "sha256",
    "mode", "ocr_languages", "max_pages", "timeout_seconds",
)

# Library block kinds that never reach the knowledge base (running headers/footers, breaks).
_FURNITURE = frozenset({"header", "footer", "page_break"})
_KIND_MAP = {
    "heading": "heading", "paragraph": "paragraph", "list_item": "list_item", "table": "table",
    "caption": "caption", "footnote": "footnote", "code": "code", "formula": "formula",
    "key_value": "key_value",
}
_MAX_WARNINGS = 200
_MAX_WARNING_CHARS = 400
_MAX_HEADING_CHARS = 200
_DETAIL_CHARS = 240

_PAGE_OCR_MODE = "accurate"
_DEFAULT_PAGE_OCR_DPI = 300
_MAX_RENDER_PIXELS = 30_000_000  # document-extractor's own Limits.max_render_pixels

INSTALL_ACTION = (
    "Install the document-intelligence dependencies in the backend environment "
    "(the document-extractor wheel and backend/requirements-docintel.txt; see the runbook), then Retry"
)
OCR_INSTALL_ACTION = (
    "Install Tesseract with the Arabic and English language packs on the server "
    "(apt install tesseract-ocr tesseract-ocr-ara tesseract-ocr-eng), then Retry"
)
_CHECK_FILE_ACTION = "Open the file on your computer to check it is not damaged, save a new copy and upload that"
_SETTINGS_ACTION = "Check the DOC_INTEL_EXTRACTION_MODE and DOC_INTEL_OCR_LANGUAGES settings"
_SPLIT_ACTION = "Split the document into smaller files and upload them separately"
NO_TEXT_REASON = (
    "No text could be extracted — if this is a scanned document, OCR (Tesseract + Arabic data) "
    "must be installed on the server"
)

_WINDOWS_PATH = re.compile(r"(?:\b[A-Za-z]:[\\/]|\\\\)[^\s'\"<>|),;]*")
_POSIX_PATH = re.compile(r"(?<![\w/.~-])/[^\s/'\"<>),;]+/[^\s'\"<>),;]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
# Invisible bidi marks (LRM/RLM/ALM, embeddings, overrides, isolates) and BOMs. Tesseract
# sprinkles RLM into mixed Arabic/Latin lines; they carry no content and only add noise
# to embeddings and keyword search. ZWJ/ZWNJ are kept: they can change how a word is written.
_INVISIBLE_MARKS = re.compile(r"[\u200E\u200F\u061C\u202A-\u202E\u2066-\u2069\uFEFF]")


class JobFailed(Exception):
    """A handled failure: ``reason`` is shown to the admin as-is. ``stage`` (CRM jobs) names
    the child-side stage that failed; None means "the stage the job was in"."""

    def __init__(self, code: str, reason: str, suggested_action: str | None = None, *,
                 stage: str | None = None) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.suggested_action = suggested_action
        self.stage = stage


class _FallbackUnavailable(Exception):
    """Page OCR could not run; the text layer is kept and a warning says why."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def scrub_detail(text: object, limit: int = _DETAIL_CHARS) -> str:
    """One short line that is safe to show an admin: control characters and absolute
    server paths are removed, and the text is truncated to ``limit`` characters."""
    value = _CONTROL.sub(" ", str(text or ""))
    value = _WINDOWS_PATH.sub("<path>", value)
    value = _POSIX_PATH.sub("<path>", value)
    value = " ".join(value.split())
    if len(value) > limit:
        value = value[: max(1, limit - 1)].rstrip() + "…"
    return value


# ---- entry point ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.doc_intel.extraction_worker",
        description="Extract one document into the normalized JSON (started by backend.doc_intel.extraction).",
    )
    parser.add_argument("--job", required=True, help="job JSON written by backend.doc_intel.extraction")
    args = parser.parse_args(argv)
    try:
        job = load_job(Path(args.job))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"extraction_worker: unusable job file ({type(exc).__name__})", file=sys.stderr)
        return EXIT_USAGE

    _prepare_process()
    error_path = Path(job["error_path"])
    if job.get("kind", JOB_KIND_EXTRACT) == JOB_KIND_CRM:
        return _main_crm(job, error_path)
    try:
        document = extract_job(job)
        try:
            document.save(Path(job["output_path"]))
        except OSError:
            raise JobFailed(
                "storage_error", "The extracted text could not be saved on the server",
                "Check the free disk space of the document store (DOC_INTEL_STORAGE_DIR), then Retry",
            ) from None
    except JobFailed as exc:
        _write_error(error_path, exc.code, exc.reason, exc.suggested_action)
        return EXIT_FAILED
    except MemoryError:
        _write_error(error_path, "resource_limit", "The document needs more memory than the server can give it",
                     _SPLIT_ACTION)
        return EXIT_FAILED
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        _write_error(error_path, "crash", f"The extraction failed unexpectedly ({type(exc).__name__})", None)
        return EXIT_CRASH
    return EXIT_OK


def load_job(path: Path) -> dict[str, Any]:
    job = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(job, dict) or job.get("version") != JOB_VERSION:
        raise ValueError("unsupported job version")
    for key in _REQUIRED_JOB_KEYS:
        if key not in job:
            raise KeyError(key)
    kind = job.get("kind", JOB_KIND_EXTRACT)
    if kind not in (JOB_KIND_EXTRACT, JOB_KIND_CRM):
        raise ValueError("unsupported job kind")
    if kind == JOB_KIND_CRM and not isinstance(job.get("crm"), dict):
        raise KeyError("crm")
    return job


def _prepare_process() -> None:
    # Never let the library pick up a settings file: the repo's document_extractor.toml
    # turns LLM "intelligence" on. Nor crm-document-ingestion's own settings (MICROSOFT_*,
    # CRM_*): a CRM job passes every option explicitly. The parent already removed these;
    # belt and braces.
    for key in [k for k in os.environ if k.upper().startswith(("DOCUMENT_EXTRACTOR_", "CRM_", "MICROSOFT_"))]:
        os.environ.pop(key, None)
    # One Tesseract thread and a lower CPU priority: the API process serves chat.
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    if hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")


def _write_error(path: Path, code: str, reason: str, suggested_action: str | None, *,
                 stage: str | None = None) -> None:
    error: dict[str, Any] = {"code": code, "reason": reason, "suggested_action": suggested_action}
    if stage is not None:
        error["stage"] = stage
    payload = {"error": error}
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        print(f"extraction_worker: could not write the error file ({code})", file=sys.stderr)


# ---- extraction ----------------------------------------------------------------------------


@dataclass
class _Extracted:
    normalized: NormalizedDocument
    document: Any  # document-extractor's Document from the text-layer pass
    module: Any  # the document_extractor module
    size_bytes: int


def extract_job(job: dict[str, Any]) -> NormalizedDocument:
    """Run one job in this process and return the normalized document (raises JobFailed)."""
    return _extract(job).normalized


def _extract(job: dict[str, Any]) -> _Extracted:
    started = time.perf_counter()
    budget = float(job["timeout_seconds"])
    # Finish (or fail with a clean "timeout") before the parent's hard kill.
    deadline = started + max(1.0, budget - min(15.0, max(3.0, budget * 0.05)))

    de = _import_extractor()
    media_type = str(job["media_type"])
    kind = _KIND_BY_MEDIA_TYPE.get(media_type)
    if kind is None:
        raise JobFailed("unsupported", "Unsupported or corrupt file", _CHECK_FILE_ACTION)
    max_pages = int(job["max_pages"])

    data = Path(job["input_path"]).read_bytes()
    size_bytes = len(data)
    if kind == "pdf":
        data = _from_pdf_header(data)
        page_total = _pdf_page_count(data)
        if page_total is not None and page_total > max_pages:
            raise _too_many_pages(page_total, max_pages)

    options = _options(
        de, job,
        images="lazy" if kind == "pdf" else "extract",  # PDF: region OCR without keeping rendered pictures
        timeout=_remaining(deadline),
    )
    doc = _run_library(de, data, f"document.{kind}", options)
    if doc.media_type != media_type:
        raise JobFailed("unsupported", "Unsupported or corrupt file", _CHECK_FILE_ACTION)
    if kind == "docx" and len(doc.pages) > max_pages:
        raise _too_many_pages(len(doc.pages), max_pages)

    extractor = {
        "name": "document-extractor",
        "version": getattr(de, "__version__", None),
        "mode": options.mode,
        "ocr_languages": list(options.ocr_languages),
        "pdf_engine": "pdfium" if kind == "pdf" else None,
        "fallback": None,
        "seconds": None,
        "ocr_used": any(p.source == "ocr" for b in doc.blocks for p in b.provenance),
        "timing": {k: round(float(v), 3) for k, v in (doc.timing or {}).items() if isinstance(v, (int, float))},
    }
    normalized = to_normalized(
        doc, filename=str(job["filename"]), media_type=media_type, sha256=str(job["sha256"]),
        kind=kind, extractor=extractor,
    )
    if kind == "pdf":
        normalized = _check_arabic_layer(de, normalized, data, job, deadline)
    if not any(b.text.strip() for b in normalized.blocks):
        raise _no_text(normalized)
    _cap_warnings(normalized)
    normalized.extractor["seconds"] = round(time.perf_counter() - started, 2)
    return _Extracted(normalized=normalized, document=doc, module=de, size_bytes=size_bytes)


def _import_extractor() -> Any:
    try:
        import document_extractor
    except ImportError as exc:
        name = getattr(exc, "name", None) or "document_extractor"
        missing = "document-extractor" if name.startswith("document_extractor") else name
        raise JobFailed("missing_dependency", f"The server is missing an extraction component: {missing}",
                        INSTALL_ACTION) from None
    return document_extractor


def _options(de: Any, job: dict[str, Any], **overrides: Any) -> Any:
    """Every option explicit, so no library default or settings file decides anything."""
    max_pages = int(job["max_pages"])
    extra: dict[str, Any] = {}
    tesseract_cmd = str(job.get("tesseract_cmd") or "").strip()
    if tesseract_cmd:
        extra["tesseract_cmd"] = tesseract_cmd
    fields: dict[str, Any] = {
        "mode": str(job["mode"]),
        "ocr": "auto",
        "ocr_languages": tuple(str(x) for x in job["ocr_languages"]),
        "ocr_engine": "tesseract",  # local; never the cloud engines
        "describe_images": False,  # vision providers send images to an API
        "vision_provider": None,
        "max_pages": max_pages,
        "pdf_engine": "pdfium",  # never PyMuPDF (AGPL)
        "backend": "native",  # never docling/llamaparse (llamaparse is a cloud service)
        "isolate": False,  # this process already is the isolation boundary
        "workers": 1,
        "limits": de.Limits(max_pages=max_pages),
        "extra": extra,
        "intelligence": de.IntelligenceConfig(enabled=False),  # no document text to any LLM
    }
    fields.update(overrides)
    return de.ExtractionOptions(**fields)


def _run_library(de: Any, data: bytes, filename: str, options: Any) -> Any:
    try:
        options.validate()  # ValueError / ConfigurationError name the bad setting
    except ValueError as exc:
        raise JobFailed("config_error", f"The extraction settings are invalid: {scrub_detail(exc)}",
                        _SETTINGS_ACTION) from None
    try:
        return de.DocumentExtractor(options).extract(data, filename=filename)
    except de.ExtractionError as exc:
        raise _library_failure(de, exc) from None


def _library_failure(de: Any, exc: Exception) -> JobFailed:
    detail = scrub_detail(exc)
    if isinstance(exc, de.PasswordRequiredError):
        return JobFailed(
            "password_protected", "The PDF is password-protected — remove the password and upload it again",
            "Open the PDF, remove its password (for example print it to a new PDF) and upload the new copy",
        )
    if isinstance(exc, de.ExtractionTimeoutError):
        return _timeout()
    if isinstance(exc, de.UnsupportedFormatError):
        return JobFailed("unsupported", "Unsupported or corrupt file", _CHECK_FILE_ACTION)
    if isinstance(exc, de.ResourceLimitError):
        return JobFailed("resource_limit", f"The file exceeds processing limits: {_describe_limit(exc)}",
                         "Split or simplify the document, then upload it again")
    if isinstance(exc, de.MissingDependencyError):
        package = getattr(exc, "package", None) or detail
        return JobFailed("missing_dependency", f"The server is missing an extraction component: {package}",
                         INSTALL_ACTION)
    if isinstance(exc, de.OCRError):
        return JobFailed("ocr_failed", f"OCR failed: {detail}",
                         "Check Tesseract under Monitoring → Extraction service, then Retry")
    if isinstance(exc, de.DocumentParsingError):
        return JobFailed("parse_error", f"The file could not be parsed: {detail}", _CHECK_FILE_ACTION)
    if isinstance(exc, de.ConfigurationError):
        return JobFailed("config_error", f"The extraction settings are invalid: {detail}", _SETTINGS_ACTION)
    return JobFailed("extraction_failed", f"Extraction failed: {detail}", None)


def _describe_limit(exc: Exception) -> str:
    limit = str(getattr(exc, "limit", "") or "")
    value, maximum = getattr(exc, "value", None), getattr(exc, "maximum", None)
    try:
        if limit == "max_bytes":
            return f"the file is larger than {int(maximum) // (1024 * 1024)} MB"
        if limit == "max_pages":
            return f"it has more than {maximum} pages"
        if limit == "max_zip_entries":
            return f"it has too many internal parts ({value} > {maximum})"
        if limit == "max_zip_uncompressed":
            return f"it expands to more than {int(maximum) // (1024 * 1024)} MB"
        if limit == "max_zip_ratio":
            return f"an internal part is compressed {value}:1 (the limit is {float(maximum):g}:1)"
        if limit == "zip_valid":
            return "it is not a readable archive"
        if limit == "zip_path":
            return "it contains unsafe internal paths"
        if limit == "max_image_pixels":
            return "an embedded image is too large"
    except (TypeError, ValueError):
        pass
    return scrub_detail(limit.replace("_", " ") or exc)


def _timeout(suggested_action: str | None = None) -> JobFailed:
    # The parent replaces the reason with the standard wording (it knows the configured limit).
    return JobFailed("timeout", "Extraction timed out", suggested_action)


def _remaining(deadline: float, suggested_action: str | None = None) -> float:
    left = deadline - time.perf_counter()
    if left <= 0:
        raise _timeout(suggested_action)
    return left


def _too_many_pages(pages: int, limit: int) -> JobFailed:
    return JobFailed(
        "too_many_pages", f"The document has {pages} pages; the limit is {limit}",
        f"Split it into parts of at most {limit} pages and upload them separately (or raise DOC_INTEL_MAX_PAGES)",
    )


def _no_text(doc: NormalizedDocument) -> JobFailed:
    codes = {w.code for w in doc.warnings}
    ocr_missing = bool(codes & {"ocr_unavailable", "ocr_skipped", "arabic_ocr_unavailable"})
    action = OCR_INSTALL_ACTION if ocr_missing else (
        "OCR ran but found no readable text — check the file, or upload a text-based PDF or DOCX"
    )
    return JobFailed("no_text", NO_TEXT_REASON, action)


def _from_pdf_header(data: bytes) -> bytes:
    """PDF readers accept junk before ``%PDF-`` (first 1 KiB); offsets count from the header."""
    at = data.find(b"%PDF-", 0, 1024)
    return data[at:] if at > 0 else data


def _pdf_page_count(data: bytes) -> int | None:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None  # the extractor reports the missing engine with the right reason
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:
        return None  # password or damage: the extractor reports it with the right reason
    try:
        return len(pdf)
    finally:
        pdf.close()


# ---- document-extractor Document -> NormalizedDocument -------------------------------------


def to_normalized(doc: Any, *, filename: str, media_type: str, sha256: str, kind: str,
                  extractor: dict[str, Any]) -> NormalizedDocument:
    """Map a document-extractor Document to the library-agnostic normalized document.

    - Blocks come in reading order. Running headers, footers and page breaks are
      dropped, and so are images that carry no OCR text.
    - A table becomes its Markdown.
    - A heading's path excludes the heading itself.
    """
    by_id = {b.id: b for b in doc.blocks}
    path_cache: dict[str, list[str]] = {}
    use_parents = any(b.parent_id for b in doc.blocks)
    scanned = {p.number for p in doc.pages if p.classification == "scanned"}
    # DOCX has no real pages; the library numbers them only from explicit page breaks.
    pages_known = kind == "pdf" or len(doc.pages) > 1
    stack: list[tuple[int, str]] = []  # heading tracker when the library gave no parent links
    blocks: list[NormalizedBlock] = []
    for block in sorted(doc.blocks, key=lambda b: b.reading_index):
        if block.kind in _FURNITURE:
            continue
        text = clean_text(_block_text(doc, block, scanned))
        if not text:
            continue
        kind_out = _KIND_MAP.get(block.kind, "other")
        level = (int(block.level) if block.level else 1) if kind_out == "heading" else None
        if use_parents:
            heading_path = _heading_path(block.parent_id, by_id, path_cache)
        else:
            if level is not None:
                while stack and stack[-1][0] >= level:
                    stack.pop()
            heading_path = [title for _, title in stack]
            if level is not None:
                stack.append((level, _short_title(text)))
        pages = sorted({p.page for p in block.provenance if p.page and p.page > 0}) if pages_known else []
        blocks.append(NormalizedBlock(kind=kind_out, text=text, pages=pages, heading_path=heading_path, level=level))

    first_page = doc.pages[0].number if doc.pages else 1
    page_texts: dict[int, list[str]] = {}
    for nb in blocks:
        page_texts.setdefault(nb.pages[0] if nb.pages else first_page, []).append(nb.text)
    pages_out = [
        NormalizedPage(number=p.number, text="\n\n".join(page_texts.get(p.number, [])), classification=p.classification)
        for p in doc.pages
    ]
    warnings = [
        NormalizedWarning(code=str(w.code), message=scrub_detail(w.message, _MAX_WARNING_CHARS), page=w.page)
        for w in doc.warnings
    ]
    page_count = int(doc.metadata.get("page_count") or len(doc.pages))
    return NormalizedDocument(
        filename=filename, media_type=media_type, sha256=sha256, page_count=page_count,
        blocks=blocks, pages=pages_out, warnings=warnings, extractor=extractor,
    )


def _block_text(doc: Any, block: Any, scanned: set[int]) -> str:
    if block.kind == "table" and block.table is not None:
        return block.table.to_markdown() or block.table.to_text()
    if block.kind == "image":
        # A picture's OCR text; on a scanned page the page's own OCR blocks already carry it.
        image = doc.images.get(block.image_id or "")
        if image is not None and image.ocr_text and block.page not in scanned:
            return image.ocr_text
    return block.text or ""


def clean_text(text: str | None) -> str:
    """Block text as stored: invisible bidi marks removed, surrounding whitespace trimmed."""
    return _INVISIBLE_MARKS.sub("", text or "").strip()


def _short_title(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_HEADING_CHARS else text[: _MAX_HEADING_CHARS - 1].rstrip() + "…"


def _heading_path(parent_id: str | None, by_id: dict[str, Any], cache: dict[str, list[str]]) -> list[str]:
    if not parent_id:
        return []
    if parent_id not in cache:
        titles: list[str] = []
        seen: set[str] = set()
        current = by_id.get(parent_id)
        while current is not None and current.id not in seen:
            seen.add(current.id)
            title = clean_text(current.text)
            if title:
                titles.append(_short_title(title))
            current = by_id.get(current.parent_id) if current.parent_id else None
        cache[parent_id] = list(reversed(titles))
    return list(cache[parent_id])


def _cap_warnings(doc: NormalizedDocument) -> None:
    if len(doc.warnings) > _MAX_WARNINGS:
        dropped = len(doc.warnings) - (_MAX_WARNINGS - 1)
        doc.warnings = doc.warnings[: _MAX_WARNINGS - 1] + [
            NormalizedWarning(code="warnings_truncated", message=f"{dropped} more warnings were not recorded")
        ]


# ---- Arabic text-layer check and page-OCR fallback -----------------------------------------


def _check_arabic_layer(de: Any, normalized: NormalizedDocument, data: bytes, job: dict[str, Any],
                        deadline: float) -> NormalizedDocument:
    score = arabic.lam_alef_score("\n".join(b.text for b in normalized.blocks))
    normalized.extractor["arabic_check"] = score.as_dict()
    if not score.reversed:
        return normalized
    evidence = f"broken={score.broken}, correct={score.correct}"
    try:
        replaced = _page_ocr(de, normalized, data, job, deadline)
    except _FallbackUnavailable as exc:
        normalized.warnings[:0] = [
            NormalizedWarning(
                code="arabic_text_layer_reversed",
                message=f"The PDF's Arabic text layer stores lam-alef pairs reversed ({evidence}); "
                        "it was kept because page OCR could not run",
            ),
            NormalizedWarning(
                code="arabic_ocr_unavailable",
                message=f"Page OCR is unavailable ({exc.detail}) — Arabic words in this document may be garbled. "
                        "Install Tesseract with the Arabic language pack, then Retry",
            ),
        ]
        return normalized
    replaced.warnings.insert(0, NormalizedWarning(
        code="arabic_text_layer_reversed",
        message=f"The PDF's Arabic text layer stores lam-alef pairs reversed ({evidence}); "
                "every page was re-read with OCR",
    ))
    return replaced


def _page_ocr(de: Any, text_doc: NormalizedDocument, data: bytes, job: dict[str, Any],
              deadline: float) -> NormalizedDocument:
    """Render every page with pypdfium2 and OCR it as an image; one paragraph block per OCR paragraph."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise _FallbackUnavailable("pypdfium2 is not installed") from None
    started = time.perf_counter()
    dpi = float(job.get("page_ocr_dpi") or _DEFAULT_PAGE_OCR_DPI)
    max_pages = int(job["max_pages"])
    timeout_action = (
        "The PDF's Arabic text layer is broken, so every page is re-read with OCR; "
        "split the document into smaller files and upload them separately"
    )
    blocks: list[NormalizedBlock] = []
    pages: list[NormalizedPage] = []
    warnings: list[NormalizedWarning] = []
    extractor: Any = None
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:
        raise _FallbackUnavailable("the PDF could not be opened for rendering") from None
    try:
        for index in range(min(len(pdf), max_pages)):
            number = index + 1
            options = _options(
                de, job,
                mode=_PAGE_OCR_MODE, ocr="always", images="skip", max_pages=1, limits=de.Limits(max_pages=1),
                timeout=_remaining(deadline, timeout_action),
            )
            try:
                png = _render_page_png(pdf, index, dpi)
            except MemoryError:
                raise
            except Exception:
                raise _FallbackUnavailable(f"page {number} could not be rendered") from None
            try:
                if extractor is None:
                    extractor = de.DocumentExtractor(options)  # one OCR engine for all pages
                page_doc = extractor.extract(png, filename=f"page-{number}.png", options=options)
            except de.ExtractionTimeoutError:
                raise _timeout(timeout_action) from None
            except (de.ExtractionError, ValueError) as exc:  # OCRError, MissingDependencyError, ...
                raise _FallbackUnavailable(scrub_detail(exc, 160)) from None
            texts: list[str] = []
            for block in sorted(page_doc.blocks, key=lambda b: b.reading_index):
                if block.kind == "image" or block.kind in _FURNITURE:
                    continue
                text = clean_text(block.text)
                if text:
                    texts.append(text)
                    blocks.append(NormalizedBlock(kind="paragraph", text=text, pages=[number]))
            pages.append(NormalizedPage(number=number, text="\n\n".join(texts), classification="ocr"))
            warnings.extend(
                NormalizedWarning(code=str(w.code), message=scrub_detail(w.message, _MAX_WARNING_CHARS), page=number)
                for w in page_doc.warnings
            )
    finally:
        pdf.close()
    if not blocks:
        raise _FallbackUnavailable("OCR found no text on the rendered pages")

    extractor_info = dict(text_doc.extractor)
    extractor_info["fallback"] = "page_ocr"
    extractor_info["ocr_used"] = True
    extractor_info["page_ocr"] = {
        "mode": _PAGE_OCR_MODE, "dpi": int(dpi), "pages": len(pages),
        "seconds": round(time.perf_counter() - started, 2),
    }
    extractor_info["arabic_check_after_ocr"] = arabic.lam_alef_score("\n".join(b.text for b in blocks)).as_dict()
    return NormalizedDocument(
        filename=text_doc.filename, media_type=text_doc.media_type, sha256=text_doc.sha256,
        page_count=text_doc.page_count, blocks=blocks, pages=pages, warnings=warnings, extractor=extractor_info,
    )


def _render_page_png(pdf: Any, index: int, dpi: float) -> bytes:
    page = pdf[index]
    try:
        width, height = page.get_size()
        scale = dpi / 72.0
        pixels = (width * scale) * (height * scale)
        if pixels > _MAX_RENDER_PIXELS:
            scale *= (_MAX_RENDER_PIXELS / pixels) ** 0.5
        bitmap = page.render(scale=scale, grayscale=True, draw_annots=True)
        try:
            image = bitmap.to_pil()
            buffer = io.BytesIO()
            effective_dpi = max(1, round(72.0 * scale))
            # The DPI tag tells Tesseract the real resolution; level 1 keeps encoding cheap.
            image.save(buffer, format="PNG", compress_level=1, dpi=(effective_dpi, effective_dpi))
        finally:
            bitmap.close()
    finally:
        page.close()
    return buffer.getvalue()


# ---- CRM job (Flow 2) ----------------------------------------------------------------------
#
# The SAME extraction as a Flow 1 job, then crm-document-ingestion over document-extractor's
# own Document: PatternExtractor (regex and label/value rules, local; document intelligence
# stays off, so no text goes to an LLM) inside a fail-fast CompositeExtractor, the generic
# organization / contact / document_reference schemas plus the job's schema files, then
# EntityValidator. Stages, reported as "stage" in the error file and mirrored to
# ``progress_path`` so the parent can name the stage of a hard crash or a timeout kill:
#   extraction    document-extractor -> normalized document (every Flow 1 failure)
#   intelligence  schema registry and entity extractors
#   entities      merging, validation, coercion and the result JSON

_CRM_INSTALL_ACTION = (
    "Install crm-document-ingestion in the backend environment (see backend/requirements-docintel.txt), "
    "then restart the backend and Retry"
)
_SCHEMA_ACTION = "Fix the file, or remove it from DOC_INTEL_CRM_SCHEMA_PATHS, then Retry"
_CRM_RETRY_ACTION = "Retry; if it fails again, check the server log for the CRM extraction error"
_CRM_SOURCE_SYSTEM = "sharepoint"


class _Progress:
    """The CRM job's current stage and when each stage began. Mirrored to ``path`` (when
    given) so the parent can attribute a crash or a timeout kill to a stage."""

    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self.stage: str | None = None
        self.started = time.perf_counter()
        self.entered: dict[str, float] = {}

    def enter(self, stage: str) -> None:
        if stage == self.stage:
            return
        self.stage = stage
        self.entered[stage] = time.perf_counter()
        if self.path is not None:
            try:
                self.path.write_text(stage, encoding="utf-8")
            except OSError:
                pass

    def seconds(self, stage: str, until: str | None = None) -> float | None:
        begin = self.entered.get(stage)
        if begin is None:
            return None
        end = self.entered.get(until, time.perf_counter()) if until else time.perf_counter()
        return round(end - begin, 3)


class _StageMarker:
    """Wraps the entity extractor: once it has returned, the job is in the "entities" stage
    (merging, validation, coercion). Duck-typed like crm_ingestion's EntityExtractor (a
    ``name`` and ``extract(document, *, schemas)``), because the library is imported lazily."""

    def __init__(self, inner: Any, progress: _Progress) -> None:
        self._inner = inner
        self._progress = progress
        self.name = inner.name

    def extract(self, document: Any, *, schemas: Any) -> Any:
        entities = self._inner.extract(document, schemas=schemas)
        self._progress.enter("entities")
        return entities


def _main_crm(job: dict[str, Any], error_path: Path) -> int:
    progress = _Progress(job.get("progress_path"))
    try:
        payload = crm_job(job, progress)
        text = json.dumps(payload, ensure_ascii=False)  # a TypeError here is a bug: reported as a crash
        output = Path(job["output_path"])
        tmp = output.with_suffix(output.suffix + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(output)
        except OSError:
            raise JobFailed(
                "storage_error", "The CRM extraction result could not be saved on the server",
                "Check the free disk space of the server's temporary directory, then Retry", stage="entities",
            ) from None
    except JobFailed as exc:
        _write_error(error_path, exc.code, exc.reason, exc.suggested_action,
                     stage=exc.stage or progress.stage or "extraction")
        return EXIT_FAILED
    except MemoryError:
        _write_error(error_path, "resource_limit", "The document needs more memory than the server can give it",
                     _SPLIT_ACTION, stage=progress.stage or "extraction")
        return EXIT_FAILED
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        _write_error(error_path, "crash", f"The CRM extraction failed unexpectedly ({type(exc).__name__})", None,
                     stage=progress.stage or "extraction")
        return EXIT_CRASH
    return EXIT_OK


def crm_job(job: dict[str, Any], progress: _Progress | None = None) -> dict[str, Any]:
    """Run a "crm" job in this process and return its result payload:
    ``{"normalized", "entities", "result", "valid", "issues", "metrics"}``.
    Raises JobFailed with ``stage`` set."""
    progress = progress if progress is not None else _Progress(None)
    options = job["crm"]
    if options.get("use_intelligence"):
        # The parent refuses this before starting a child; this process never sends text to an LLM.
        raise JobFailed("not_supported", "LLM-based CRM intelligence is not enabled in this version",
                        None, stage="intelligence")
    lib = _import_crm_library()  # before the (expensive) extraction: a missing library fails fast

    progress.enter("extraction")
    extracted = _extract(job)

    progress.enter("intelligence")
    registry = _crm_registry(lib, options.get("schema_paths") or [])
    min_confidence = float(options.get("min_confidence", 0.5))
    filename = str(job["filename"])
    source = lib.SourceMetadata(
        source_system=_CRM_SOURCE_SYSTEM, filename=filename, mime_type=str(job["media_type"]),
        size=extracted.size_bytes, extra={"sha256": str(job["sha256"])},
    )
    document = crm_document(extracted, filename=filename)
    document.metadata["source"] = source.model_dump(mode="json")
    ingested = lib.IngestedDocument(
        document=document,
        source=source,
        ingested_at=datetime.now(timezone.utc),
        content_sha256=str(job["sha256"]),
        warnings=[f"{w.code}: {w.message}" for w in extracted.normalized.warnings],
    )
    service = lib.CRMExtractionService(
        [_StageMarker(lib.PatternExtractor(), progress)],
        registry=registry,
        settings=lib.CRMSettings(_env_file=None, min_confidence=min_confidence, schema_paths=[]),
        fail_fast=True,
    )
    try:
        result = service.process(ingested)
    except lib.EntityExtractionError as exc:
        raise JobFailed("entity_extraction_failed", f"CRM entity extraction failed: {scrub_detail(exc)}",
                        _CRM_RETRY_ACTION, stage="intelligence") from None

    progress.enter("entities")
    entities = result.to_crm_json()
    return {
        "normalized": extracted.normalized.model_dump(mode="json", by_alias=True),
        "entities": entities,
        "result": result.to_dict(),
        "valid": bool(result.valid),
        "issues": [issue.model_dump(mode="json") for issue in result.validation.issues],
        "metrics": _crm_metrics(lib, registry, result, entities, extracted, progress, min_confidence),
    }


def _import_crm_library() -> SimpleNamespace:
    try:
        import crm_ingestion
        from crm_ingestion.config import CRMSettings
        from crm_ingestion.connectors.sharepoint.models import SourceMetadata
        from crm_ingestion.crm import CRMExtractionService, PatternExtractor, default_registry
        from crm_ingestion.errors import EntityExtractionError
        from crm_ingestion.ingestion.models import IngestedDocument
    except ImportError as exc:
        name = getattr(exc, "name", None) or "crm_ingestion"
        missing = "crm-document-ingestion" if name.startswith("crm_ingestion") else name
        raise JobFailed("missing_dependency", f"The server is missing a CRM extraction component: {missing}",
                        _CRM_INSTALL_ACTION, stage="intelligence") from None
    return SimpleNamespace(
        version=getattr(crm_ingestion, "__version__", None),
        CRMSettings=CRMSettings,
        SourceMetadata=SourceMetadata,
        CRMExtractionService=CRMExtractionService,
        PatternExtractor=PatternExtractor,
        default_registry=default_registry,
        EntityExtractionError=EntityExtractionError,
        IngestedDocument=IngestedDocument,
    )


def _crm_registry(lib: SimpleNamespace, schema_paths: list[str]) -> Any:
    """The generic schemas plus each client schema file, in order (reasons name the file, never its path)."""
    registry = lib.default_registry()
    for raw in schema_paths:
        path = Path(str(raw))
        try:
            registry.load_json(path)
        except OSError:
            raise JobFailed("schema_error", f"The CRM schema file {path.name} could not be read", _SCHEMA_ACTION,
                            stage="intelligence") from None
        except (ValueError, TypeError, AttributeError) as exc:  # JSON, pydantic ValidationError, duplicate name
            raise JobFailed("schema_error", f"The CRM schema file {path.name} is invalid: {scrub_detail(exc)}",
                            _SCHEMA_ACTION, stage="intelligence") from None
    return registry


def crm_document(extracted: _Extracted, *, filename: str) -> Any:
    """The document-extractor Document the CRM extractors read.

    Normally the extractor's own Document, so tables, provenance and OCR confidence are
    intact, with invisible bidi marks removed from text and table cells and the OCR text of
    pictures (e.g. a scanned business card inside a DOCX) as their block text. Running
    headers and footers are kept: letterheads carry company contact details. When the Arabic
    page-OCR fallback replaced the text layer, the OCR'd text is used instead (one paragraph
    block per OCR paragraph), so entities are read from the corrected text."""
    de, doc, normalized = extracted.module, extracted.document, extracted.normalized
    if normalized.extractor.get("fallback") == "page_ocr":
        return _document_from_ocr(de, doc, normalized, filename)
    scanned = {p.number for p in doc.pages if p.classification == "scanned"}
    blocks = []
    for block in sorted(doc.blocks, key=lambda b: b.reading_index):
        if block.table is not None:
            table = replace(block.table, cells=[replace(c, text=clean_text(c.text)) for c in block.table.cells])
            blocks.append(replace(block, text=clean_text(block.text), table=table))
        else:
            blocks.append(replace(block, text=clean_text(_block_text(doc, block, scanned))))
    return de.Document(
        id=doc.id, media_type=doc.media_type, source_name=filename, metadata=dict(doc.metadata),
        pages=list(doc.pages), blocks=blocks, warnings=list(doc.warnings), timing=dict(doc.timing),
    )


def _document_from_ocr(de: Any, doc: Any, normalized: NormalizedDocument, filename: str) -> Any:
    blocks = [
        de.Block(
            id=f"{doc.id}:ocr{index}", kind="paragraph", text=block.text, reading_index=index,
            provenance=[de.Provenance(page=block.pages[0], source="ocr", engine="tesseract")] if block.pages else [],
        )
        for index, block in enumerate(normalized.blocks)
    ]
    pages = [de.Page(number=page.number, classification="scanned") for page in normalized.pages]
    return de.Document(id=doc.id, media_type=doc.media_type, source_name=filename, metadata=dict(doc.metadata),
                       pages=pages, blocks=blocks)


def _crm_metrics(lib: SimpleNamespace, registry: Any, result: Any, entities: list[dict[str, Any]],
                 extracted: _Extracted, progress: _Progress, min_confidence: float) -> dict[str, Any]:
    normalized = extracted.normalized
    types = Counter(str((e.get("_meta") or {}).get("entity_type")) for e in entities)
    return {
        "document_type": result.extraction.document_type,
        "entity_count": len(entities),
        "entity_types": dict(types),
        "valid": bool(result.valid),
        "error_count": len(result.validation.errors),
        "warning_count": len(result.validation.warnings),
        "extractors": list(result.extraction.extractors),
        "use_intelligence": False,
        "min_confidence": min_confidence,
        # Per entity type: the field holding its display value and each field's type, so the
        # API process can build display values and match keys without importing the library.
        "schemas": {
            schema.name: {
                "display_field": schema.primary_field,
                "field_types": {f.name: f.type for f in schema.fields},
            }
            for schema in registry
        },
        "page_count": normalized.page_count,
        "ocr_used": bool(normalized.extractor.get("ocr_used")),
        "fallback": normalized.extractor.get("fallback"),
        "crm_ingestion_version": lib.version,
        "document_extractor_version": getattr(extracted.module, "__version__", None),
        "seconds": {
            "extraction": progress.seconds("extraction", "intelligence"),
            "intelligence": progress.seconds("intelligence", "entities"),
            "entities": progress.seconds("entities"),
            "total": round(time.perf_counter() - progress.started, 3),
        },
    }


if __name__ == "__main__":
    sys.exit(main())
