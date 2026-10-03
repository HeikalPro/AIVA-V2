"""Upload storage and validation (backend.doc_intel.storage). No database, no network."""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from backend.doc_intel import storage
from backend.doc_intel.constants import ALLOWED_EXTENSIONS, MAX_FILENAME_CHARS
from backend.doc_intel.storage import (
    UNSUPPORTED_TYPE_REASON,
    UploadRejected,
    detect_kind,
    new_document_dir,
    normalized_path,
    remove_document_dir,
    sanitize_filename,
    save_upload,
)

try:  # the test directory may or may not be a package
    from . import _doc_fixtures as fx
except ImportError:
    import _doc_fixtures as fx


@pytest.fixture
def settings(tmp_path: Path):
    return fx.make_settings(tmp_path)


@pytest.fixture
def doc_dir(settings):
    return new_document_dir(settings)


# ---- sanitize_filename ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\evil.pdf", "evil.pdf"),
        ("C:\\Users\\someone\\Desktop\\Q3 report.pdf", "Q3 report.pdf"),
        ("/var/www/html/index.docx", "index.docx"),
        ("rep\x00ort\x07\x1b.pdf", "report.pdf"),
        ("invoice\u202efdp.docx", "invoicefdp.docx"),  # right-to-left override (extension spoofing)
        ("zero\u200bwidth\ufeff.pdf", "zerowidth.pdf"),
        ("  my   report \t final.pdf  ", "my report final.pdf"),
        ("line\nbreak\r\n.pdf", "line break .pdf"),
        ("تقرير المبيعات.pdf", "تقرير المبيعات.pdf"),
        ("e\u0301te\u0301.pdf", "\u00e9t\u00e9.pdf"),  # NFC
        ("report.pdf. . ", "report.pdf"),
        (".pdf", "document.pdf"),
        ("", "document"),
        (None, "document"),
        (".", "document"),
        ("..", "document"),
        ("\x00\x01\x02", "document"),
        ("dir/", "document"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_caps_length_and_keeps_extension():
    name = sanitize_filename("a" * 400 + ".docx")
    assert len(name) == MAX_FILENAME_CHARS
    assert name.endswith(".docx")
    arabic = sanitize_filename("ملف" * 200 + ".pdf")
    assert len(arabic) <= MAX_FILENAME_CHARS and arabic.endswith(".pdf")
    no_ext = sanitize_filename("b" * 300)
    assert no_ext == "b" * MAX_FILENAME_CHARS


# ---- detect_kind ---------------------------------------------------------------------------


def test_detect_kind_matches_extension_and_magic():
    pdf = fx.make_pdf()
    docx = fx.make_docx()
    assert detect_kind(pdf[:4096], "a.pdf") == "pdf"
    assert detect_kind(pdf[:4096], "A.PDF") == "pdf"
    assert detect_kind(docx[:4096], "a.docx") == "docx"
    assert detect_kind(b"\x00" * 100 + b"%PDF-1.7\n", "late-header.pdf") == "pdf"  # within the first 1 KiB


@pytest.mark.parametrize(
    ("head", "filename"),
    [
        (b"x" * 1100 + b"%PDF-1.7", "too-late.pdf"),  # header beyond 1 KiB
        (b"PK\x03\x04rest", "zip-named.pdf"),  # extension says PDF, content is a ZIP
        (b"%PDF-1.7\n", "pdf-named.docx"),  # extension says DOCX, content is a PDF
        (b"MZ\x90\x00", "program.pdf"),  # an executable renamed .pdf
        (b"%PDF-1.7\n", "report.pdf.exe"),
        (b"%PDF-1.7\n", "report.txt"),
        (b"%PDF-1.7\n", "no-extension"),
        (b"PK\x05\x06" + b"\x00" * 18, "empty-zip.docx"),
        (b"", "empty.pdf"),
    ],
)
def test_detect_kind_rejects_mismatch_and_unsupported(head, filename):
    assert detect_kind(head, filename) is None


# ---- directories ---------------------------------------------------------------------------


def test_new_document_dir_is_private_random_and_under_kb(settings):
    first = new_document_dir(settings)
    second = new_document_dir(settings)
    kb_root = settings.storage_path / "kb"
    assert first.parent == kb_root and second.parent == kb_root
    assert first != second
    assert len(first.name) == 32 and all(c in "0123456789abcdef" for c in first.name)
    assert first.is_dir() and not any(first.iterdir())
    assert normalized_path(first) == first / "normalized.json"
    if os.name == "posix":
        assert (first.stat().st_mode & 0o777) == 0o700


def test_remove_document_dir_deletes_a_document_directory(settings):
    doc_dir = new_document_dir(settings)
    (doc_dir / "original.pdf").write_bytes(b"%PDF-1.7")
    remove_document_dir(doc_dir, settings=settings)
    assert not doc_dir.exists()
    remove_document_dir(doc_dir, settings=settings)  # already gone: no error


def test_remove_document_dir_refuses_paths_outside_the_store(settings, tmp_path):
    kb_root = settings.storage_path / "kb"
    kb_root.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    not_a_dir = kb_root / "stray.txt"
    not_a_dir.write_text("x")

    for target in (outside, kb_root, settings.storage_path, kb_root / "abc" / ".." / "..", tmp_path):
        with pytest.raises(ValueError):
            remove_document_dir(target, settings=settings)
    with pytest.raises(ValueError):
        remove_document_dir(not_a_dir, settings=settings)
    assert (outside / "keep.txt").exists() and kb_root.exists()


def _link_directory(link: Path, target: Path) -> None:
    """A directory symlink, or on Windows without that privilege a junction (needs none)."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("creating symlinks is not permitted here")
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False)
    if result.returncode != 0:
        pytest.skip("could not create a symlink or a junction")


def test_remove_document_dir_refuses_link_escape(settings, tmp_path):
    kb_root = settings.storage_path / "kb"
    kb_root.mkdir(parents=True)
    outside = tmp_path / "victim"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    link = kb_root / ("f" * 32)
    _link_directory(link, outside)
    with pytest.raises(ValueError):
        remove_document_dir(link, settings=settings)
    assert (outside / "keep.txt").exists()
    # A link to a directory *inside* the store is refused too: only real directories are removed.
    inside = new_document_dir(settings)
    (inside / "original.pdf").write_bytes(b"%PDF-1.7")
    second = kb_root / ("e" * 32)
    _link_directory(second, inside)
    with pytest.raises(ValueError):
        remove_document_dir(second, settings=settings)
    assert (inside / "original.pdf").exists()


# ---- save_upload: accepted files -----------------------------------------------------------


def _left_over(doc_dir: Path) -> list[str]:
    return sorted(p.name for p in doc_dir.iterdir())


@pytest.mark.asyncio
async def test_save_upload_pdf(settings, doc_dir):
    data = fx.make_pdf()
    stored = await save_upload(fx.make_upload(data, "../Quarterly\x00 report.pdf"), doc_dir, settings=settings)
    assert stored.kind == "pdf"
    assert stored.content_type == ALLOWED_EXTENSIONS[".pdf"]
    assert stored.filename == "Quarterly report.pdf"
    assert stored.size_bytes == len(data)
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    assert stored.doc_dir == doc_dir
    assert stored.original_path == doc_dir / "original.pdf"
    assert stored.original_path.read_bytes() == data
    assert _left_over(doc_dir) == ["original.pdf"]
    if os.name == "posix":
        assert (stored.original_path.stat().st_mode & 0o777) == 0o600


@pytest.mark.asyncio
async def test_save_upload_docx(settings, doc_dir):
    data = fx.make_docx()
    stored = await save_upload(fx.make_upload(data, "Guide.DOCX"), doc_dir, settings=settings)
    assert stored.kind == "docx"
    assert stored.content_type == ALLOWED_EXTENSIONS[".docx"]
    assert stored.original_path == doc_dir / "original.docx"
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    assert _left_over(doc_dir) == ["original.docx"]


@pytest.mark.asyncio
async def test_save_upload_accepts_docx_whose_main_part_is_not_document_xml(settings, doc_dir):
    base = fx.make_docx()
    source = zipfile.ZipFile(io.BytesIO(base))
    content_types = source.read("[Content_Types].xml").replace(b"/word/document.xml", b"/word/document2.xml")
    data = fx.rewrite_zip(
        base, drop=("word/document.xml",),
        add={"word/document2.xml": source.read("word/document.xml")},
        replace={"[Content_Types].xml": content_types},
    )
    stored = await save_upload(fx.make_upload(data, "online.docx"), doc_dir, settings=settings)
    assert stored.kind == "docx"


# ---- save_upload: rejected files -----------------------------------------------------------


async def _rejected(settings, doc_dir, data: bytes, filename: str, **upload_kwargs) -> UploadRejected:
    with pytest.raises(UploadRejected) as info:
        await save_upload(fx.make_upload(data, filename, **upload_kwargs), doc_dir, settings=settings)
    assert _left_over(doc_dir) == [], "a rejected upload must leave no file behind"
    return info.value


@pytest.mark.asyncio
async def test_size_cap_is_enforced_while_streaming(tmp_path):
    settings = fx.make_settings(tmp_path, max_upload_mb=1)
    doc_dir = new_document_dir(settings)
    source = io.BytesIO(b"%PDF-1.7\n" + b"0" * (3 * 1024 * 1024))
    upload = fx.make_upload(b"", "big.pdf")
    upload.file = source
    with pytest.raises(UploadRejected) as info:
        await save_upload(upload, doc_dir, settings=settings)
    assert info.value.code == "too_large"
    assert info.value.reason == "File exceeds the 1 MB limit"
    assert source.tell() <= 2 * 1024 * 1024 + 16, "streaming must stop soon after the cap"
    assert _left_over(doc_dir) == []


@pytest.mark.asyncio
async def test_declared_size_over_the_cap_is_rejected_before_reading(tmp_path):
    settings = fx.make_settings(tmp_path, max_upload_mb=1)
    doc_dir = new_document_dir(settings)
    upload = fx.make_upload(fx.make_pdf(), "big.pdf", size=5 * 1024 * 1024)
    with pytest.raises(UploadRejected) as info:
        await save_upload(upload, doc_dir, settings=settings)
    assert info.value.code == "too_large"
    assert upload.file.tell() == 0


@pytest.mark.asyncio
async def test_exactly_at_the_cap_is_accepted(tmp_path):
    settings = fx.make_settings(tmp_path, max_upload_mb=1)
    doc_dir = new_document_dir(settings)
    data = b"%PDF-1.7\n" + b"0" * (1024 * 1024 - 9)
    stored = await save_upload(fx.make_upload(data, "edge.pdf"), doc_dir, settings=settings)
    assert stored.size_bytes == 1024 * 1024


@pytest.mark.asyncio
async def test_empty_file_is_rejected(settings, doc_dir):
    rejected = await _rejected(settings, doc_dir, b"", "empty.pdf")
    assert rejected.code == "empty"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["notes.txt", "setup.exe", "sheet.xlsx", "macro.docm", "old.doc", "no-extension"])
async def test_unsupported_extension_is_rejected(settings, doc_dir, filename):
    rejected = await _rejected(settings, doc_dir, fx.make_pdf(), filename)
    assert rejected.code == "unsupported_type"
    assert rejected.reason == UNSUPPORTED_TYPE_REASON


@pytest.mark.asyncio
async def test_spoofed_extension_is_rejected(settings, doc_dir):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    rejected = await _rejected(settings, doc_dir, png, "photo.pdf")
    assert rejected.code == "type_mismatch"
    assert "PDF" in rejected.reason
    rejected = await _rejected(settings, doc_dir, fx.make_pdf(), "renamed.docx")
    assert rejected.code == "type_mismatch"


@pytest.mark.asyncio
async def test_macro_enabled_docx_is_rejected(settings, doc_dir):
    base = fx.make_docx()
    with_vba = fx.rewrite_zip(base, add={"word/vbaProject.bin": b"\xd0\xcf\x11\xe0 macro"})
    rejected = await _rejected(settings, doc_dir, with_vba, "macro.docx")
    assert rejected.code == "macro_enabled"

    content_types = zipfile.ZipFile(io.BytesIO(base)).read("[Content_Types].xml").replace(
        b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        b"application/vnd.ms-word.document.macroEnabled.main+xml",
    )
    docm = fx.rewrite_zip(base, replace={"[Content_Types].xml": content_types})
    rejected = await _rejected(settings, doc_dir, docm, "renamed-docm.docx")
    assert rejected.code == "macro_enabled"


@pytest.mark.asyncio
async def test_zip_bomb_ratio_is_rejected(settings, doc_dir):
    base = fx.make_docx()
    bomb = fx.rewrite_zip(base, add={"word/media/filler.bin": b"\x00" * 5_000_000})  # ~1000:1
    rejected = await _rejected(settings, doc_dir, bomb, "bomb.docx")
    assert rejected.code == "zip_limits"


@pytest.mark.asyncio
async def test_zip_entry_count_and_total_size_caps(settings, doc_dir, monkeypatch):
    base = fx.make_docx()
    monkeypatch.setattr(storage, "_MAX_ZIP_ENTRIES", 20)
    many = fx.rewrite_zip(base, add={f"word/extra{i}.xml": b"<x/>" for i in range(30)})
    rejected = await _rejected(settings, doc_dir, many, "many.docx")
    assert rejected.code == "zip_limits"
    assert "too many internal parts" in rejected.reason

    monkeypatch.setattr(storage, "_MAX_ZIP_ENTRIES", 10_000)
    monkeypatch.setattr(storage, "_MAX_ZIP_UNCOMPRESSED", 200_000)
    big = fx.rewrite_zip(base, add={"word/media/photo.bin": os.urandom(300_000)})  # incompressible
    rejected = await _rejected(settings, doc_dir, big, "big.docx")
    assert rejected.code == "zip_limits"


@pytest.mark.asyncio
async def test_encrypted_ooxml_is_rejected(settings, doc_dir):
    encrypted = fx.make_cfb(("EncryptionInfo", "EncryptedPackage", "\x06DataSpaces"))
    rejected = await _rejected(settings, doc_dir, encrypted, "locked.docx")
    assert rejected.code == "encrypted"
    assert "password-protected" in rejected.reason

    legacy = fx.make_cfb(("WordDocument", "1Table"))
    rejected = await _rejected(settings, doc_dir, legacy, "old-format.docx")
    assert rejected.code == "unsupported_type"
    assert "Word 97-2003" in rejected.reason


@pytest.mark.asyncio
async def test_zip_with_encrypted_members_is_rejected(settings, doc_dir):
    # zipfile cannot write encrypted members, so set the "encrypted" flag bit of
    # word/document.xml directly in its central-directory record.
    data = bytearray(fx.make_docx())
    at = data.find(b"PK\x01\x02")
    while at != -1:
        name_length = int.from_bytes(data[at + 28:at + 30], "little")
        if data[at + 46:at + 46 + name_length] == b"word/document.xml":
            data[at + 8] |= 0x01
            break
        at = data.find(b"PK\x01\x02", at + 4)
    assert at != -1
    rejected = await _rejected(settings, doc_dir, bytes(data), "pkware.docx")
    assert rejected.code == "encrypted"


@pytest.mark.asyncio
async def test_zip_that_is_not_a_word_document_is_rejected(settings, doc_dir):
    plain_zip = fx.make_zip({"readme.txt": b"hello"})
    rejected = await _rejected(settings, doc_dir, plain_zip, "archive.docx")
    assert rejected.code == "type_mismatch"

    base = fx.make_docx()
    no_main = fx.rewrite_zip(base, drop=("word/document.xml",))
    rejected = await _rejected(settings, doc_dir, no_main, "hollow.docx")
    assert rejected.code == "type_mismatch"

    pptx_types = b'<?xml version="1.0"?><Types><Override PartName="/ppt/presentation.xml" ' \
                 b'ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>'
    pptx = fx.make_zip({"[Content_Types].xml": pptx_types, "word/document.xml": b"<w:document/>"})
    rejected = await _rejected(settings, doc_dir, pptx, "slides.docx")
    assert rejected.code == "type_mismatch"


@pytest.mark.asyncio
async def test_corrupt_zip_is_rejected(settings, doc_dir):
    rejected = await _rejected(settings, doc_dir, b"PK\x03\x04" + b"\x13\x37" * 500, "broken.docx")
    assert rejected.code == "corrupt"


@pytest.mark.asyncio
async def test_zip_with_traversal_member_is_rejected(settings, doc_dir):
    evil = fx.rewrite_zip(fx.make_docx(), add={"../../evil.xml": b"<x/>"})
    rejected = await _rejected(settings, doc_dir, evil, "traversal.docx")
    assert rejected.code == "corrupt"


@pytest.mark.asyncio
async def test_storage_failure_is_reported_without_paths_and_cleaned_up(settings, doc_dir, monkeypatch):
    def broken_write(sink, digest, chunk):
        raise OSError(f"No space left on device: {doc_dir / 'upload.part'}")

    monkeypatch.setattr(storage, "_write_chunk", broken_write)
    rejected = await _rejected(settings, doc_dir, fx.make_pdf(), "a.pdf")
    assert rejected.code == "storage_error"
    assert str(doc_dir) not in rejected.reason


@pytest.mark.asyncio
async def test_cancelled_upload_leaves_no_partial_file(settings, doc_dir):
    class CancellingUpload:
        filename = "slow.pdf"
        size = None

        def __init__(self) -> None:
            self.calls = 0

        async def read(self, size: int = -1) -> bytes:
            self.calls += 1
            if self.calls == 1:
                return b"%PDF-1.7\n" + b"x" * 1000
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await save_upload(CancellingUpload(), doc_dir, settings=settings)
    assert _left_over(doc_dir) == []


@pytest.mark.asyncio
async def test_stale_part_file_is_replaced(settings, doc_dir):
    (doc_dir / "upload.part").write_bytes(b"stale bytes from a crashed attempt")
    data = fx.make_pdf()
    stored = await save_upload(fx.make_upload(data, "retry.pdf"), doc_dir, settings=settings)
    assert stored.original_path.read_bytes() == data
    assert _left_over(doc_dir) == ["original.pdf"]
