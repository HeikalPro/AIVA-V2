"""Upload storage and validation for knowledge-document imports (Flow 1, upload stage).

Every upload gets its own private directory, ``<DOC_INTEL_STORAGE_DIR>/kb/<random hex>/``.

1. The original is streamed to ``upload.part``. The size cap is enforced while
   streaming and the SHA-256 is computed on the way.
2. The file is validated.
3. It is atomically renamed to ``original.pdf`` / ``original.docx``.

Files are never served back. The admin-facing filename is only a sanitized
display name and never touches the filesystem.

The type is checked from BOTH the extension and the content: magic bytes, and for
DOCX the package structure too. So a renamed executable, a macro-enabled .docm, an
encrypted Office file or a zip bomb is refused before any parser sees it. Parsing
itself happens later, in the extraction child process, under the library's own limits.

``UploadRejected.code`` values: ``unsupported_type``, ``type_mismatch``, ``too_large``,
``empty``, ``encrypted``, ``macro_enabled``, ``zip_limits``, ``corrupt``, ``storage_error``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shutil
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

from fastapi import UploadFile

from backend.doc_intel.constants import ALLOWED_EXTENSIONS, MAX_FILENAME_CHARS
from backend.doc_intel.settings import DocIntelSettings

_log = logging.getLogger(__name__)

DocKind = Literal["pdf", "docx"]


class UploadRejected(Exception):
    """The file cannot be accepted; ``reason`` is shown to the admin as-is."""

    def __init__(self, reason: str, *, code: str = "upload_rejected") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class StoredUpload:
    filename: str  # sanitized display name (<= 255 chars)
    kind: DocKind
    content_type: str  # canonical MIME type for ``kind``
    size_bytes: int
    sha256: str  # hex
    doc_dir: Path  # this document's private directory
    original_path: Path  # doc_dir / "original.<ext>"


UNSUPPORTED_TYPE_REASON = "Unsupported file type — only PDF and DOCX files are accepted"

_FALLBACK_NAME = "document"
_KB_SUBDIR = "kb"
_PART_NAME = "upload.part"
_CHUNK_BYTES = 1024 * 1024
_HEAD_BYTES = 4096

_PDF_MAGIC = b"%PDF-"
_PDF_MAGIC_WINDOW = 1024  # readers accept the header anywhere in the first 1 KiB
_ZIP_MAGIC = b"PK\x03\x04"
_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE2 compound file: legacy Office, or encrypted OOXML
_CFB_ENCRYPTION_MARKERS = ("EncryptionInfo".encode("utf-16-le"), "EncryptedPackage".encode("utf-16-le"))

# Zip-bomb guards. Same values and the same ratio rule as document-extractor's own
# Limits, so a file accepted here is never refused by the parser for the same reason.
_MAX_ZIP_ENTRIES = 10_000
_MAX_ZIP_UNCOMPRESSED = 1024 * 1024 * 1024
_MAX_ZIP_RATIO = 200.0
_RATIO_MIN_BYTES = 1_000_000  # small members legitimately compress far better than 200:1
_MAX_CONTENT_TYPES_BYTES = 1024 * 1024

_WORD_MAIN_CONTENT_TYPE = b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_OVERRIDE_TAG = re.compile(rb"<(?:[\w.-]+:)?Override\b([^>]*)>", re.IGNORECASE)
_XML_ATTR = re.compile(rb"""([\w.:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_MACRO_PARTS = ("vbaproject.bin", "vbadata.xml")

_WHITESPACE = re.compile(r"\s+")
# Control (incl. NUL), format (bidi overrides, zero-width marks) and lone surrogates.
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_MAX_EXTENSION_CHARS = 16

_POSIX = os.name == "posix"


# ---- names and kinds ----------------------------------------------------------------------


def sanitize_filename(name: str | None) -> str:
    """Basename only, control characters and path separators removed, <= 255 chars, never empty."""
    if not name:
        return _FALLBACK_NAME
    text = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    text = unicodedata.normalize("NFC", text)
    # Whitespace controls (tab, CR/LF, line separators) become spaces; every other
    # control or format character is dropped.
    text = "".join(
        " " if ch.isspace() else ch
        for ch in text
        if ch.isspace() or unicodedata.category(ch) not in _STRIPPED_CATEGORIES
    )
    # Trailing dots/spaces are dropped as Windows does, so "report.pdf." is "report.pdf".
    text = _WHITESPACE.sub(" ", text).strip().rstrip(". ")
    if not text:
        return _FALLBACK_NAME
    if text.startswith("."):
        text = _FALLBACK_NAME + text  # ".pdf" -> "document.pdf"
    if len(text) > MAX_FILENAME_CHARS:
        stem, ext = os.path.splitext(text)
        if not ext or len(ext) > _MAX_EXTENSION_CHARS:
            stem, ext = text, ""
        stem = stem[: MAX_FILENAME_CHARS - len(ext)].rstrip(" .") or _FALLBACK_NAME
        text = stem + ext
    return text


def _extension(filename: str | None) -> str:
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return os.path.splitext(base)[1].lower()


def detect_kind(head: bytes, filename: str) -> DocKind | None:
    """Kind from BOTH the extension and the magic bytes (PDF ``%PDF-``; DOCX = ZIP with
    ``word/document.xml``). Returns None when they disagree or the type is unsupported."""
    ext = _extension(filename)
    if ext not in ALLOWED_EXTENSIONS:
        return None
    head = bytes(head or b"")
    if ext == ".pdf":
        return "pdf" if _PDF_MAGIC in head[:_PDF_MAGIC_WINDOW] else None
    if ext == ".docx":
        # The head only shows the ZIP signature; save_upload() then opens the package
        # and requires the WordprocessingML main part (word/document.xml).
        return "docx" if head.startswith(_ZIP_MAGIC) else None
    return None


# ---- directories --------------------------------------------------------------------------


def _kb_root(settings: DocIntelSettings) -> Path:
    return settings.storage_path / _KB_SUBDIR


def _make_private(path: Path, mode: int) -> None:
    """Tighten permissions on POSIX; mkdir/open already masked them, this is belt and braces."""
    if _POSIX:
        os.chmod(path, mode)


def new_document_dir(settings: DocIntelSettings) -> Path:
    """Create and return a fresh private directory ``<storage>/kb/<random hex>/``."""
    root = _kb_root(settings)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    doc_dir = root / secrets.token_hex(16)
    doc_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    _make_private(doc_dir, 0o700)
    return doc_dir


def remove_document_dir(doc_dir: Path, *, settings: DocIntelSettings) -> None:
    """Delete a document directory; refuses paths outside ``settings.storage_path``.

    Only a directory strictly inside ``<storage>/kb`` is removed. The check is made on
    the fully resolved path, so ``..`` components and symlinks/junctions that point
    elsewhere are refused (ValueError). A missing directory is a no-op.
    """
    candidate = Path(doc_dir)
    if candidate.is_symlink() or _is_junction(candidate):
        raise ValueError("Refusing to delete a link inside the document store")
    root = _kb_root(settings).resolve()
    target = candidate.resolve()
    if target == root or root not in target.parents:
        raise ValueError("Refusing to delete a directory outside the document store")
    if not target.exists():
        return
    if not target.is_dir():
        raise ValueError("Refusing to delete something that is not a document directory")
    shutil.rmtree(target)


def _is_junction(path: Path) -> bool:
    check = getattr(path, "is_junction", None)  # Python 3.12+
    try:
        return bool(check()) if check is not None else False
    except OSError:
        return False


# ---- upload --------------------------------------------------------------------------------


async def save_upload(upload: UploadFile, doc_dir: Path, *, settings: DocIntelSettings) -> StoredUpload:
    """Stream ``upload`` into ``doc_dir`` (size cap enforced while streaming, SHA-256 computed),
    validate the type, and atomically move it to ``original.<ext>``.

    Raises UploadRejected (with a user-facing reason) and leaves no partial file behind.
    """
    filename = sanitize_filename(upload.filename)
    ext = _extension(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadRejected(UNSUPPORTED_TYPE_REASON, code="unsupported_type")
    limit = settings.max_upload_bytes
    declared = getattr(upload, "size", None)
    if isinstance(declared, int) and declared > limit:
        raise _too_large(settings)

    doc_dir = Path(doc_dir)
    part = doc_dir / _PART_NAME
    original = doc_dir / "original.pdf"
    kind: DocKind = "pdf"
    digest = hashlib.sha256()
    size = 0
    head = bytearray()
    sink: BinaryIO | None = None
    committed = False
    try:
        sink = await asyncio.to_thread(_open_part, part)
        while True:
            chunk = await upload.read(_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise _too_large(settings)
            if len(head) < _HEAD_BYTES:
                head += chunk[: _HEAD_BYTES - len(head)]
            await asyncio.to_thread(_write_chunk, sink, digest, chunk)
        await asyncio.to_thread(_finish_part, sink)
        sink = None
        if size == 0:
            raise UploadRejected("The file is empty", code="empty")

        detected = detect_kind(bytes(head), filename)
        if detected is None:
            raise await asyncio.to_thread(_mismatch_rejection, part, ext, bytes(head))
        kind = detected
        if kind == "docx":
            await asyncio.to_thread(_validate_docx, part)
        original = doc_dir / f"original.{kind}"
        await asyncio.to_thread(_commit, part, original)
        committed = True
    except UploadRejected:
        raise
    except Exception as exc:
        _log.warning("doc_intel: storing an upload failed (%s)", type(exc).__name__, exc_info=True)
        raise UploadRejected("The file could not be stored on the server — try again", code="storage_error") from exc
    finally:
        # Also runs on cancellation (client gone, shutdown): no partial file survives.
        if not committed:
            _discard(sink, part)

    return StoredUpload(
        filename=filename,
        kind=kind,
        content_type=ALLOWED_EXTENSIONS[f".{kind}"],
        size_bytes=size,
        sha256=digest.hexdigest(),
        doc_dir=doc_dir,
        original_path=original,
    )


def _too_large(settings: DocIntelSettings) -> UploadRejected:
    return UploadRejected(f"File exceeds the {settings.max_upload_mb} MB limit", code="too_large")


def _open_part(part: Path) -> BinaryIO:
    if part.is_symlink() or part.exists():
        part.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(part, flags, 0o600)
    return os.fdopen(fd, "wb")


def _write_chunk(sink: BinaryIO, digest: Any, chunk: bytes) -> None:
    digest.update(chunk)
    sink.write(chunk)


def _finish_part(sink: BinaryIO) -> None:
    try:
        sink.flush()
        os.fsync(sink.fileno())
    finally:
        sink.close()


def _commit(part: Path, original: Path) -> None:
    os.replace(part, original)
    _make_private(original, 0o600)


def _discard(sink: BinaryIO | None, part: Path) -> None:
    if sink is not None:
        try:
            sink.close()
        except OSError:
            pass
    try:
        part.unlink(missing_ok=True)
    except OSError:
        _log.warning("doc_intel: could not remove a partial upload file", exc_info=True)


# ---- content validation (runs in a worker thread) -----------------------------------------


def _mismatch_rejection(part: Path, ext: str, head: bytes) -> UploadRejected:
    """The reason for a file whose content does not match its (allowed) extension."""
    if ext == ".docx" and head.startswith(_CFB_MAGIC):
        if _file_contains(part, _CFB_ENCRYPTION_MARKERS):
            return UploadRejected(
                "The Word document is password-protected — remove the password and upload it again",
                code="encrypted",
            )
        return UploadRejected(
            "This is a legacy Word 97-2003 file with a .docx name — open it in Word and save it as .docx",
            code="unsupported_type",
        )
    label = "PDF" if ext == ".pdf" else "Word (.docx)"
    return UploadRejected(
        f"Unsupported file type — the content is not a real {label} file (only PDF and DOCX files are accepted)",
        code="type_mismatch",
    )


def _file_contains(path: Path, needles: tuple[bytes, ...]) -> bool:
    overlap = max(len(n) for n in needles) - 1
    tail = b""
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_CHUNK_BYTES)
            if not block:
                return False
            window = tail + block
            if any(n in window for n in needles):
                return True
            tail = window[-overlap:]


def _validate_docx(path: Path) -> None:
    """Refuse anything that is not a plain, unencrypted, macro-free WordprocessingML package."""
    not_word = UploadRejected("The file is not a valid Word (.docx) document", code="type_mismatch")
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_ZIP_ENTRIES:
                raise UploadRejected(
                    f"The DOCX has too many internal parts ({len(infos):,}; the limit is {_MAX_ZIP_ENTRIES:,})",
                    code="zip_limits",
                )
            names: set[str] = set()
            total = 0
            for info in infos:
                name = info.filename.replace("\\", "/")
                lower = name.lower()
                if info.flag_bits & 0x1 or lower in ("encryptioninfo", "encryptedpackage"):
                    raise UploadRejected(
                        "The Word document is password-protected — remove the password and upload it again",
                        code="encrypted",
                    )
                if lower.rsplit("/", 1)[-1] in _MACRO_PARTS:
                    raise _macro_rejection()
                if name.startswith("/") or ".." in name.split("/") or (len(name) > 1 and name[1] == ":"):
                    raise UploadRejected("The DOCX contains unsafe internal paths and was refused", code="corrupt")
                total += info.file_size
                if info.file_size > _RATIO_MIN_BYTES and info.file_size > _MAX_ZIP_RATIO * max(info.compress_size, 1):
                    raise _bomb_rejection()
                names.add(lower)
            if total > _MAX_ZIP_UNCOMPRESSED:
                raise _bomb_rejection()

            content_types = _read_member(zf, "[Content_Types].xml")
            if content_types is None:
                raise not_word
            lowered = content_types.lower()
            if b"macroenabled" in lowered or b"vbaproject" in lowered:
                raise _macro_rejection()
            if _WORD_MAIN_CONTENT_TYPE not in lowered:
                raise not_word
            # Normally word/document.xml; some producers name the main part differently
            # (document2.xml), which [Content_Types].xml then declares explicitly.
            main_parts = {"word/document.xml", *_declared_parts(content_types, _WORD_MAIN_CONTENT_TYPE)}
            if not main_parts & names:
                raise not_word
    except UploadRejected:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, RuntimeError, NotImplementedError,
            EOFError, ValueError, OSError) as exc:
        raise UploadRejected("The DOCX file is corrupt or not a valid Word document", code="corrupt") from exc


def _macro_rejection() -> UploadRejected:
    return UploadRejected(
        "Macro-enabled Word documents are not accepted — save it as a regular Word Document (.docx) and upload again",
        code="macro_enabled",
    )


def _bomb_rejection() -> UploadRejected:
    return UploadRejected(
        "The DOCX expands to an unsafe size (possible zip bomb) and was refused",
        code="zip_limits",
    )


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > _MAX_CONTENT_TYPES_BYTES:
        return None
    with zf.open(info) as fh:
        return fh.read(_MAX_CONTENT_TYPES_BYTES + 1)


def _declared_parts(content_types: bytes, content_type: bytes) -> set[str]:
    """Zip member names (lower case) that ``[Content_Types].xml`` overrides to ``content_type``.

    Parsed with two regular expressions rather than an XML parser: the file is
    untrusted, and a regex cannot be made to expand entities.
    """
    parts: set[str] = set()
    for tag in _OVERRIDE_TAG.finditer(content_types):
        attrs = {key.lower(): (dq or sq) for key, dq, sq in _XML_ATTR.findall(tag.group(1))}
        if attrs.get(b"contenttype", b"").strip().lower() == content_type:
            part = attrs.get(b"partname", b"").decode("utf-8", "replace").strip().lstrip("/").lower()
            if part:
                parts.add(part)
    return parts


def normalized_path(doc_dir: Path) -> Path:
    """Where extraction writes the normalized document JSON."""
    return doc_dir / "normalized.json"
