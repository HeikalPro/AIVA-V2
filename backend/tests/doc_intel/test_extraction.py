"""document-extractor integration (backend.doc_intel.extraction and extraction_worker).

The real-extraction tests start the actual child process on documents generated in
memory. The page-OCR fallback is exercised in-process through ``extract_job``. Tests
that need Tesseract skip themselves when the binary or its language data is missing.
No database, no network.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.doc_intel import arabic, extraction, extraction_worker
from backend.doc_intel.arabic import LamAlefScore, lam_alef_score, text_layer_reversed
from backend.doc_intel.extraction import (
    ExtractionFailed,
    extraction_available,
    extractor_info,
    run_extraction,
    run_smoke_test,
)
from backend.doc_intel.normalized import NormalizedDocument

try:  # the test directory may or may not be a package
    from . import _doc_fixtures as fx
except ImportError:
    import _doc_fixtures as fx

ROOT = Path(__file__).resolve().parents[3]
TESSERACT = fx.find_tesseract()
TESSERACT_LANGS = fx.tesseract_languages(TESSERACT)
needs_tesseract_eng = pytest.mark.skipif(
    TESSERACT is None or "eng" not in TESSERACT_LANGS, reason="Tesseract with the eng language pack is not installed"
)
needs_tesseract_ara_eng = pytest.mark.skipif(
    TESSERACT is None or not {"ara", "eng"} <= TESSERACT_LANGS,
    reason="Tesseract with the ara and eng language packs is not installed",
)


@pytest.fixture
def settings(tmp_path):
    return fx.make_settings(tmp_path, tesseract_cmd=TESSERACT)


def _extract(tmp_path: Path, settings, data: bytes, *, kind: str) -> NormalizedDocument:
    source = tmp_path / f"original.{kind}"
    source.write_bytes(data)
    return run_extraction(
        source, filename=f"upload.{kind}",
        media_type=fx.PDF_MEDIA_TYPE if kind == "pdf" else fx.DOCX_MEDIA_TYPE,
        sha256=hashlib.sha256(data).hexdigest(), out_path=tmp_path / "normalized.json", settings=settings,
    )


def _failure(tmp_path: Path, settings, data: bytes, *, kind: str) -> ExtractionFailed:
    with pytest.raises(ExtractionFailed) as info:
        _extract(tmp_path, settings, data, kind=kind)
    assert not (tmp_path / "normalized.json").exists(), "a failed extraction must not leave a result behind"
    assert not (tmp_path / "normalized.json.tmp").exists()
    return info.value


# ---- the lam-alef heuristic (pure) -----------------------------------------------------------

CORRECT_ARABIC = (
    "لا توجد رسوم إضافية، ولا حاجة للانتظار. يمكن الاتصال خلال ساعات العمل إلا في العطلات. "
    "للاستفسار عن الاشتراك أو الاسترداد يرجى التواصل معنا. لا نقبل الطلبات بعد الموعد، "
    "وسيتم الرد على السؤال في مجال الخدمة."
)


def _reverse_lam_alef(text: str) -> str:
    """What a broken ToUnicode map produces: every lam-alef pair stored as alef-lam."""
    return text.replace(arabic.LAM_ALEF, arabic.ALEF_LAM)


def test_lam_alef_score_counts_exactly():
    lam_alef, alef_lam = arabic.LAM_ALEF, arabic.ALEF_LAM
    text = f"{lam_alef} {lam_alef}x {alef_lam} x{alef_lam} english words 123"
    score = lam_alef_score(text.replace("x", "\u0628"))  # ARABIC LETTER BEH
    assert (score.words, score.correct, score.broken) == (4, 2, 2)


def test_correct_arabic_is_not_flagged():
    score = lam_alef_score(CORRECT_ARABIC)
    assert score.correct > score.broken
    assert not score.reversed
    assert not text_layer_reversed(CORRECT_ARABIC)


def test_reversed_arabic_is_flagged():
    broken = _reverse_lam_alef(CORRECT_ARABIC)
    score = lam_alef_score(broken)
    assert score.broken >= arabic.MIN_BROKEN
    assert score.broken > score.correct
    assert text_layer_reversed(broken)
    assert score.as_dict()["reversed"] is True


def test_short_or_non_arabic_text_is_never_flagged():
    alef_lam = arabic.ALEF_LAM
    assert not text_layer_reversed(" ".join([alef_lam] * (arabic.MIN_BROKEN - 1)))
    assert not text_layer_reversed("Plain English text only, no Arabic at all.")
    assert not text_layer_reversed("")
    assert LamAlefScore(words=0, correct=0, broken=0).reversed is False


def test_diacritics_and_tatweel_do_not_hide_correct_pairs():
    fatha, tatweel = "\u064e", "\u0640"
    lam, alef = arabic.LAM, arabic.ALEF
    voweled = " ".join([lam + fatha + alef] * 6 + [lam + tatweel + alef] * 6)
    score = lam_alef_score(voweled)
    assert score.correct == 12 and score.broken == 0


# ---- worker helpers (pure) -------------------------------------------------------------------


def test_scrub_detail_removes_paths_and_control_characters():
    text = ("failed at C:\\Users\\someone\\AppData\\x.pdf and /srv/aiva/data/doc_intel/kb/abc/original.pdf"
            " (see \\\\fileserver\\share\\y)\x00\x1b[31m; and/or 1/2 pages")
    clean = extraction_worker.scrub_detail(text)
    assert "Users" not in clean and "/srv" not in clean and "fileserver" not in clean
    assert "\x00" not in clean and "\x1b" not in clean
    assert "and/or 1/2 pages" in clean
    assert len(extraction_worker.scrub_detail("x" * 1000)) == 240


def test_clean_text_drops_invisible_bidi_marks_only():
    rlm, rlo, zwnj = "\u200f", "\u202e", "\u200c"
    assert extraction_worker.clean_text(f"  abc{rlm} def{rlo}{zwnj} ") == f"abc def{zwnj}"


def test_to_normalized_maps_blocks_tables_images_and_heading_paths():
    from document_extractor import Block, Document, ImageResource, Page, Provenance, Table, TableCell

    def block(i, kind, text, page, parent=None, level=None, **kw):
        return Block(id=f"b{i}", kind=kind, text=text, reading_index=i, provenance=[Provenance(page=page)],
                     parent_id=parent, level=level, **kw)

    table = Table(cells=[TableCell(0, 0, "Name", is_header=True), TableCell(0, 1, "Fee", is_header=True),
                         TableCell(1, 0, "Card"), TableCell(1, 1, "50")], n_rows=2, n_cols=2)
    doc = Document(id="d", media_type=fx.PDF_MEDIA_TYPE, metadata={"page_count": 3},
                   pages=[Page(1, classification="text"), Page(2, classification="text"),
                          Page(3, classification="scanned")])
    doc.images = {
        "img1": ImageResource(id="img1", sha256="a", mime="image/png", width=10, height=10, ocr_text="Chart label"),
        "img2": ImageResource(id="img2", sha256="b", mime="image/png", width=10, height=10, ocr_text="Scan dup"),
        "img3": ImageResource(id="img3", sha256="c", mime="image/png", width=10, height=10),
    }
    doc.blocks = [
        block(0, "header", "Running header", 1),
        block(1, "heading", "Top", 1, level=1),
        block(2, "paragraph", "Intro", 1, parent="b1"),
        block(3, "heading", "Sub", 2, parent="b1", level=2),
        block(4, "paragraph", "Body", 2, parent="b3"),
        block(5, "table", "", 2, parent="b3", table=table),
        block(6, "image", "", 2, parent="b3", image_id="img1"),
        block(7, "image", "", 3, image_id="img2"),  # scanned page: the page OCR blocks carry the text
        block(8, "image", "", 2, image_id="img3"),  # no text at all
        block(9, "footer", "Page 2", 2),
        block(10, "page_break", "", 2),
        block(11, "list_item", "Item \u200fone", 3, level=1),
    ]
    out = extraction_worker.to_normalized(doc, filename="f.pdf", media_type=fx.PDF_MEDIA_TYPE, sha256="0" * 64,
                                          kind="pdf", extractor={"name": "document-extractor"})
    summary = [(b.kind, b.text, b.pages, b.heading_path, b.level) for b in out.blocks]
    assert summary == [
        ("heading", "Top", [1], [], 1),
        ("paragraph", "Intro", [1], ["Top"], None),
        ("heading", "Sub", [2], ["Top"], 2),
        ("paragraph", "Body", [2], ["Top", "Sub"], None),
        ("table", "| Name | Fee |\n| --- | --- |\n| Card | 50 |", [2], ["Top", "Sub"], None),
        ("other", "Chart label", [2], ["Top", "Sub"], None),
        ("list_item", "Item one", [3], [], None),
    ]
    assert out.page_count == 3
    assert [(p.number, p.classification) for p in out.pages] == [(1, "text"), (2, "text"), (3, "scanned")]
    assert out.pages[0].text == "Top\n\nIntro"


# ---- availability and diagnostics ------------------------------------------------------------


def test_extraction_available_in_this_environment():
    assert extraction_available() == (True, None)


def test_extraction_available_reports_a_missing_library(monkeypatch):
    monkeypatch.setitem(sys.modules, "document_extractor", None)  # makes the import fail
    ok, reason = extraction_available()
    assert ok is False and "not installed" in reason


def test_extractor_info_with_a_wrong_tesseract_path_never_raises(tmp_path):
    info = extractor_info(fx.make_settings(tmp_path, tesseract_cmd=str(tmp_path / "no" / "tesseract.exe")))
    import document_extractor

    assert info["available"] is True
    assert info["document_extractor_version"] == document_extractor.__version__
    assert info["pdf_engine"] == "pdfium"
    assert info["tesseract"]["found"] is False
    assert info["tesseract"]["available"] is False and info["tesseract_available"] is False
    assert "configured" in info["tesseract"]["error"]
    assert info["ocr_available"] is False
    assert info["ocr_languages_requested"] == ["ara", "eng"]
    assert info["ocr_languages_installed"] is None  # unknown, not "none installed"


def test_extractor_info_survives_internal_errors(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(extraction, "_resolve_tesseract", explode)
    info = extractor_info(fx.make_settings(tmp_path))
    assert info["error"] == "Diagnostics failed (RuntimeError)"


@needs_tesseract_eng
def test_extractor_info_reports_tesseract(settings):
    info = extractor_info(settings)
    assert info["tesseract"]["found"] is True
    assert info["tesseract"]["available"] is True and info["tesseract_available"] is True
    assert info["tesseract"]["version"][0].isdigit()
    assert "eng" in info["ocr_languages_installed"]
    assert info["ocr_available"] == ({"ara", "eng"} <= TESSERACT_LANGS)


# ---- real extractions in the child process ---------------------------------------------------


def test_docx_extraction_produces_the_normalized_shape(tmp_path, settings):
    import document_extractor

    doc = _extract(tmp_path, settings, fx.make_docx(), kind="docx")
    saved = NormalizedDocument.load(tmp_path / "normalized.json")
    assert saved.model_dump() == doc.model_dump()
    assert doc.schema_id == "aiva.doc_intel.normalized.v1"
    assert doc.filename == "upload.docx" and doc.media_type == fx.DOCX_MEDIA_TYPE

    by_text = {b.text: b for b in doc.blocks}
    title = by_text["Customer Guide"]
    assert (title.kind, title.level, title.heading_path) == ("heading", 1, [])
    section = by_text["Card Services"]
    assert (section.kind, section.level, section.heading_path) == ("heading", 2, ["Customer Guide"])
    body = by_text["Cards can be blocked from the mobile application."]
    assert body.kind == "paragraph" and body.heading_path == ["Customer Guide", "Card Services"]
    assert by_text[fx.ARABIC_SENTENCE].heading_path == ["Customer Guide", "Card Services"]
    assert by_text["Fees are charged monthly."].heading_path == ["Customer Guide", "Fees"]

    tables = [b for b in doc.blocks if b.kind == "table"]
    assert len(tables) == 1
    assert "| Service | Fee |" in tables[0].text and "| --- | --- |" in tables[0].text
    assert "| Replacement card | 50 EGP |" in tables[0].text
    assert tables[0].heading_path == ["Customer Guide", "Card Services"]

    assert all(b.pages == [] for b in doc.blocks), "a DOCX without page breaks has no real page numbers"
    assert doc.page_count == 1 and len(doc.pages) == 1
    assert fx.ARABIC_SENTENCE in doc.pages[0].text

    ext = doc.extractor
    assert ext["name"] == "document-extractor" and ext["version"] == document_extractor.__version__
    assert ext["mode"] == "balanced" and ext["ocr_languages"] == ["ara", "eng"]
    assert ext["pdf_engine"] is None and ext["fallback"] is None
    assert isinstance(ext["seconds"], float)


def test_pdf_extraction_produces_text_and_pages(tmp_path, settings):
    pdf = fx.make_pdf([["Opening hours", "The branch opens at nine. Marker PDF-ALPHA."],
                       ["Card fees", "Replacement costs fifty pounds. Marker PDF-BETA."]])
    doc = _extract(tmp_path, settings, pdf, kind="pdf")
    text = "\n".join(b.text for b in doc.blocks)
    assert "PDF-ALPHA" in text and "PDF-BETA" in text
    assert {p for b in doc.blocks for p in b.pages} == {1, 2}
    alpha = next(b for b in doc.blocks if "PDF-ALPHA" in b.text)
    assert alpha.pages == [1]
    assert doc.page_count == 2
    assert [(p.number, p.classification) for p in doc.pages] == [(1, "text"), (2, "text")]
    assert "PDF-BETA" in doc.pages[1].text
    assert doc.extractor["pdf_engine"] == "pdfium" and doc.extractor["fallback"] is None
    assert doc.extractor["arabic_check"]["reversed"] is False


def test_password_protected_pdf_fails_with_a_clear_reason(tmp_path, settings):
    failure = _failure(tmp_path, settings, fx.make_pdf(password="s3cret"), kind="pdf")
    assert failure.code == "password_protected"
    assert failure.reason == "The PDF is password-protected — remove the password and upload it again"
    assert failure.suggested_action


def test_pdf_over_the_page_limit_fails(tmp_path):
    settings = fx.make_settings(tmp_path, max_pages=2)
    failure = _failure(tmp_path, settings, fx.make_pdf([["One", "a"], ["Two", "b"], ["Three", "c"]]), kind="pdf")
    assert failure.code == "too_many_pages"
    assert failure.reason == "The document has 3 pages; the limit is 2"


def test_docx_over_the_page_limit_fails(tmp_path):
    settings = fx.make_settings(tmp_path, max_pages=2)
    failure = _failure(tmp_path, settings, fx.make_docx(page_breaks=2), kind="docx")
    assert failure.code == "too_many_pages"
    assert failure.reason == "The document has 3 pages; the limit is 2"


def test_corrupt_pdf_fails_without_leaking_paths(tmp_path, settings):
    failure = _failure(tmp_path, settings, b"%PDF-1.7\n" + bytes(range(256)) * 40, kind="pdf")
    assert failure.code == "parse_error"
    assert failure.reason.startswith("The file could not be parsed")
    assert str(tmp_path) not in failure.reason and ":\\" not in failure.reason


def test_pdf_without_text_fails_with_no_text(tmp_path, settings):
    failure = _failure(tmp_path, settings, fx.make_pdf([[]]), kind="pdf")
    assert failure.code == "no_text"
    assert failure.reason == extraction_worker.NO_TEXT_REASON
    assert failure.suggested_action


def test_missing_source_file_fails_before_starting_a_child(tmp_path, settings):
    with pytest.raises(ExtractionFailed) as info:
        run_extraction(tmp_path / "gone.pdf", filename="gone.pdf", media_type=fx.PDF_MEDIA_TYPE,
                       sha256="0" * 64, out_path=tmp_path / "normalized.json", settings=settings)
    assert info.value.code == "source_missing"


def test_settings_file_named_in_the_environment_is_never_loaded(tmp_path, settings, monkeypatch):
    toml = tmp_path / "document_extractor.toml"
    toml.write_text('[intelligence]\nenabled = true\nmodel = "x"\nbase_url = "http://127.0.0.1:9/v1"\n'
                    '[not_a_real_table]\nbroken = true\n', encoding="utf-8")
    monkeypatch.setenv("DOCUMENT_EXTRACTOR_CONFIG", str(toml))
    doc = _extract(tmp_path, settings, fx.make_docx(), kind="docx")  # loading that file would raise
    assert any(b.text == "Customer Guide" for b in doc.blocks)


def test_worker_never_imports_backend_config():
    probe = ("import sys, backend.doc_intel.extraction_worker; "
             "print([m for m in ('backend.config', 'backend.doc_intel.settings', 'pydantic_settings', "
             "'backend.database', 'oracledb', 'document_extractor') if m in sys.modules])")
    result = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT), capture_output=True, timeout=60, check=True)
    assert result.stdout.decode().strip() == "[]"


# ---- timeouts, crashes and the child's environment --------------------------------------------


def test_real_timeout_kills_the_child(tmp_path, settings):
    source = tmp_path / "original.pdf"
    source.write_bytes(fx.make_pdf())
    with pytest.raises(ExtractionFailed) as info:
        extraction._run_child(source, filename="a.pdf", media_type=fx.PDF_MEDIA_TYPE, sha256="0" * 64,
                              out_path=tmp_path / "normalized.json", settings=settings, timeout=0.05)
    assert info.value.code == "timeout"
    assert info.value.reason == (
        "Extraction timed out after 0.05 s — the file may be very large or scanned; split it or retry"
    )
    assert not (tmp_path / "normalized.json").exists()


def test_timeout_kills_the_whole_process_tree(tmp_path):
    """Tesseract (a grandchild) must die with the worker, not keep burning CPU."""
    psutil = pytest.importorskip("psutil")
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        extraction._spawn([sys.executable, "-c", script], cwd=tmp_path, env=dict(os.environ), timeout=3)
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 15
    while psutil.pid_exists(grandchild) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not psutil.pid_exists(grandchild)


def test_timeout_uses_the_configured_limit(tmp_path, settings, monkeypatch):
    def slow(command, *, cwd, env, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(extraction, "_spawn", slow)
    failure = _failure(tmp_path, settings, fx.make_pdf(), kind="pdf")
    assert failure.code == "timeout"
    assert failure.reason == (
        "Extraction timed out after 120 s — the file may be very large or scanned; split it or retry"
    )


def test_child_reported_timeout_uses_the_standard_reason(tmp_path, settings, monkeypatch):
    def child_deadline(command, *, cwd, env, timeout):
        job = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        Path(job["error_path"]).write_text(json.dumps(
            {"error": {"code": "timeout", "reason": "Extraction timed out", "suggested_action": "Split it"}}),
            encoding="utf-8")
        return subprocess.CompletedProcess(command, 1, b"", b"")

    monkeypatch.setattr(extraction, "_spawn", child_deadline)
    failure = _failure(tmp_path, settings, fx.make_pdf(), kind="pdf")
    assert failure.code == "timeout"
    assert failure.reason.startswith("Extraction timed out after 120 s")
    assert failure.suggested_action == "Split it"


def test_crash_without_an_error_file_reports_the_last_stderr_line(tmp_path, settings, monkeypatch):
    def crash(command, *, cwd, env, timeout):
        stderr = b"Traceback (most recent call last):\n  File \"C:\\srv\\x.py\"\nRuntimeError: boom in C:\\srv\\worker.py\n"
        return subprocess.CompletedProcess(command, 3, b"", stderr)

    monkeypatch.setattr(extraction, "_spawn", crash)
    failure = _failure(tmp_path, settings, fx.make_pdf(), kind="pdf")
    assert failure.code == "crash"
    assert failure.reason == "The extraction process crashed (exit 3): RuntimeError: boom in <path>"


def test_child_environment_is_scrubbed(tmp_path, monkeypatch):
    secrets = {
        "DOCUMENT_EXTRACTOR_CONFIG": "D:/repo/document_extractor.toml",
        "OPENAI_API_KEY": "sk-test-openai",
        "SOVEREIGNEG_API_KEY": "sov-test",
        "ORACLE_PASSWORD": "oracle-test",
        "JWT_SECRET_KEY": "jwt-test",
        "ZOHO_CLIENT_SECRET": "zoho-test",
        "DOC_INTEL_SECRETS_KEY": "fernet-test",
        "WIDGET_LOG_SECRET": "widget-test",
        "REDIS_URL": "redis://:hunter2@cache:6379/0",
        "HTTPS_PROXY": "http://user:pass@proxy:8080",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AIVA_HARMLESS_SETTING", "kept")
    monkeypatch.setenv("TESSDATA_PREFIX", "/usr/share/tessdata")
    env = extraction._child_env(fx.make_settings(tmp_path, tesseract_cmd="/opt/tesseract/bin/tesseract"))
    upper = {k.upper(): v for k, v in env.items()}
    for name in secrets:
        assert name not in upper, f"{name} leaked into the child environment"
    assert not any(value in secrets.values() for value in env.values())
    assert upper["TESSERACT_CMD"] == "/opt/tesseract/bin/tesseract"
    assert upper["AIVA_HARMLESS_SETTING"] == "kept" and upper["TESSDATA_PREFIX"] == "/usr/share/tessdata"
    assert "PATH" in upper and upper["OMP_THREAD_LIMIT"]


def test_the_spawned_child_gets_the_scrubbed_environment(tmp_path, settings, monkeypatch):
    monkeypatch.setenv("DOCUMENT_EXTRACTOR_CONFIG", str(tmp_path / "document_extractor.toml"))
    seen: dict[str, object] = {}

    def capture(command, *, cwd, env, timeout):
        seen.update(command=command, cwd=cwd, env=env, timeout=timeout)
        return subprocess.CompletedProcess(command, 3, b"", b"")

    monkeypatch.setattr(extraction, "_spawn", capture)
    _failure(tmp_path, settings, fx.make_pdf(), kind="pdf")
    assert not any(k.upper() == "DOCUMENT_EXTRACTOR_CONFIG" for k in seen["env"])
    assert seen["command"][:4] == [sys.executable, "-m", "backend.doc_intel.extraction_worker", "--job"]
    assert Path(seen["cwd"]) == ROOT
    assert seen["timeout"] == 120.0


# ---- smoke test ------------------------------------------------------------------------------


def test_smoke_test_passes(settings):
    result = run_smoke_test(settings, timeout_seconds=60)
    assert result["ok"] is True, result["detail"]
    assert result["detail"] is None
    assert 0 < result["seconds"] < 60
    assert result["info"]["available"] is True


def test_smoke_test_reports_a_timeout_without_raising(settings, monkeypatch):
    def slow(command, *, cwd, env, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(extraction, "_spawn", slow)
    result = run_smoke_test(settings, timeout_seconds=5)
    assert result["ok"] is False
    assert result["detail"] == "Smoke test timed out after 5 s"
    assert isinstance(result["info"], dict)


# ---- Arabic page-OCR fallback (in-process) ---------------------------------------------------


def _job(tmp_path: Path, pdf: bytes, *, tesseract_cmd: str | None, languages: list[str]) -> dict:
    source = tmp_path / "original.pdf"
    source.write_bytes(pdf)
    return {
        "version": extraction_worker.JOB_VERSION,
        "input_path": str(source),
        "output_path": str(tmp_path / "normalized.json"),
        "error_path": str(tmp_path / "error.json"),
        "filename": "scan.pdf",
        "media_type": fx.PDF_MEDIA_TYPE,
        "sha256": hashlib.sha256(pdf).hexdigest(),
        "mode": "balanced",
        "ocr_languages": languages,
        "max_pages": 10,
        "tesseract_cmd": tesseract_cmd,
        "page_ocr_dpi": 200,
        "timeout_seconds": 120,
    }


def _pretend_reversed(monkeypatch):
    monkeypatch.setattr(arabic, "lam_alef_score", lambda text: LamAlefScore(words=40, correct=0, broken=12))


@needs_tesseract_eng
def test_reversed_text_layer_is_replaced_by_page_ocr(tmp_path, monkeypatch):
    _pretend_reversed(monkeypatch)
    pdf = fx.make_pdf([["Fallback heading", "Marker 4471 appears on page one"],
                       ["Second page", "Marker 5582 appears on page two"]], font_size=18)
    doc = extraction_worker.extract_job(_job(tmp_path, pdf, tesseract_cmd=TESSERACT, languages=["eng"]))
    assert doc.extractor["fallback"] == "page_ocr"
    assert doc.extractor["page_ocr"]["pages"] == 2 and doc.extractor["page_ocr"]["mode"] == "accurate"
    assert doc.warnings[0].code == "arabic_text_layer_reversed"
    assert {b.kind for b in doc.blocks} == {"paragraph"}
    assert all(b.heading_path == [] for b in doc.blocks)
    assert [(p.number, p.classification) for p in doc.pages] == [(1, "ocr"), (2, "ocr")]
    assert "4471" in doc.pages[0].text and "5582" in doc.pages[1].text
    assert {tuple(b.pages) for b in doc.blocks} == {(1,), (2,)}
    assert doc.page_count == 2


def test_reversed_text_layer_is_kept_with_a_warning_when_ocr_is_unavailable(tmp_path, monkeypatch):
    _pretend_reversed(monkeypatch)
    missing = tmp_path / "missing" / "tesseract.exe"
    pdf = fx.make_pdf([["Kept heading", "Text layer marker 9981"]])
    doc = extraction_worker.extract_job(_job(tmp_path, pdf, tesseract_cmd=str(missing), languages=["ara", "eng"]))
    assert doc.extractor["fallback"] is None
    codes = [w.code for w in doc.warnings]
    assert codes[:2] == ["arabic_text_layer_reversed", "arabic_ocr_unavailable"]
    assert "9981" in "\n".join(b.text for b in doc.blocks)
    assert all(str(tmp_path) not in w.message and "missing" not in w.message for w in doc.warnings)


@needs_tesseract_ara_eng
def test_page_ocr_runs_with_the_arabic_language_pack(tmp_path, monkeypatch):
    """reportlab cannot shape Arabic, so a real reversed-Arabic PDF cannot be generated
    here. This checks the plumbing on an English page instead: ara+eng are passed to
    Tesseract and the page is OCR'd. The real-document check is described in the report."""
    _pretend_reversed(monkeypatch)
    pdf = fx.make_pdf([["Mixed page", "Marker 7310 with Arabic packs loaded"]], font_size=18)
    doc = extraction_worker.extract_job(_job(tmp_path, pdf, tesseract_cmd=TESSERACT, languages=["ara", "eng"]))
    assert doc.extractor["fallback"] == "page_ocr"
    assert doc.extractor["ocr_languages"] == ["ara", "eng"]
    assert "7310" in doc.pages[0].text


# ---- one extraction at a time --------------------------------------------------------------


def test_run_extraction_holds_the_process_wide_slot(tmp_path, settings, monkeypatch):
    seen: list[bool] = []

    def fake_child(*args, **kwargs):
        # The slot is taken while the child runs: a second caller would have to wait.
        seen.append(not extraction._EXTRACTION_SLOT.acquire(blocking=False))
        return NormalizedDocument(filename="x.pdf", media_type=fx.PDF_MEDIA_TYPE, sha256="0" * 64)

    monkeypatch.setattr(extraction, "_run_child", fake_child)
    run_extraction(tmp_path / "x.pdf", filename="x.pdf", media_type=fx.PDF_MEDIA_TYPE, sha256="0" * 64,
                   out_path=tmp_path / "normalized.json", settings=settings)
    assert seen == [True]
    # ... and released afterwards.
    assert extraction._EXTRACTION_SLOT.acquire(blocking=False)
    extraction._EXTRACTION_SLOT.release()


def test_smoke_test_skips_instead_of_queueing_behind_a_running_extraction(settings, monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("the smoke test must not start a child while the slot is busy")

    monkeypatch.setattr(extraction, "_run_child", must_not_run)
    assert extraction._EXTRACTION_SLOT.acquire(blocking=False)
    try:
        result = run_smoke_test(settings, timeout_seconds=5)
    finally:
        extraction._EXTRACTION_SLOT.release()
    assert result["ok"] is True and result["busy"] is True
    assert "being extracted" in (result["detail"] or "")
    assert isinstance(result["info"], dict)
