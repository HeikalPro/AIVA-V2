"""CRM extraction of a downloaded SharePoint file (Flow 2).

Runs in the SAME kind of isolated child process as knowledge extraction (see
extraction.py): document-extractor with explicit options (intelligence OFF), then
crm-document-ingestion's CRMExtractionService (generic organization / contact /
document_reference schemas plus DOC_INTEL_CRM_SCHEMA_PATHS), then its validator. Shares
the process-wide extraction slot, so at most one extraction child runs at a time across
both flows; the child is registered for ``terminate_active_extractions`` on shutdown, gets
the scrubbed environment and the ``DOC_INTEL_EXTRACTION_TIMEOUT_SECONDS`` hard timeout.

Stage mapping (the three child-side stages of a SharePoint file):
  extraction    document-extractor -> normalized document
  intelligence  CRM entity extractors (pattern rules; plus the library's LLM intelligence
                only when the source's use_intelligence flag is on)
  entities      validation report (valid / issues / confidence)

LLM intelligence is not enabled in this version: ``use_intelligence=True`` fails the
intelligence stage (code ``not_supported``) before any child is started, because sending
document text to an LLM has not been approved. Pattern extraction is local.

``CrmExtractionFailed`` codes: ``not_supported``, ``unavailable``, ``source_missing``,
``unsupported``, ``timeout``, ``crash``, and from the child: ``missing_dependency``,
``schema_error``, ``entity_extraction_failed``, ``storage_error``, ``resource_limit`` and
every extraction code of ``extraction.ExtractionFailed`` (``password_protected``,
``too_many_pages``, ``parse_error``, ``no_text``, ``ocr_failed``, ...).
"""
from __future__ import annotations

import importlib
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from backend.doc_intel import extraction
from backend.doc_intel.extraction import ExtractionFailed
from backend.doc_intel.extraction_worker import CRM_CHILD_STAGES, JOB_KIND_CRM, scrub_detail
from backend.doc_intel.normalized import NormalizedDocument
from backend.doc_intel.settings import DocIntelSettings

_log = logging.getLogger(__name__)

CrmChildStage = Literal["extraction", "intelligence", "entities"]

INTELLIGENCE_NOT_SUPPORTED_REASON = "LLM-based CRM intelligence is not enabled in this version"
_INTELLIGENCE_ACTION = (
    "Switch LLM intelligence off for this source; entities are then extracted locally with pattern rules"
)
_CRM_INSTALL_ACTION = (
    "Install crm-document-ingestion in the backend environment (see backend/requirements-docintel.txt), "
    "then restart the backend"
)
_CRASH_ACTION = "Retry; if it fails again, check the server log for the CRM extraction error"

# Job files inside the caller's private work_dir.
_JOB_NAME = "crm_job.json"
_RESULT_NAME = "crm_result.json"
_ERROR_NAME = "crm_error.json"
_PROGRESS_NAME = "crm_progress.txt"


class CrmExtractionFailed(Exception):
    """``stage`` is the child-side stage that failed; ``reason`` is shown to admins as-is."""

    def __init__(
        self,
        reason: str,
        *,
        stage: CrmChildStage = "extraction",
        code: str = "crm_extraction_failed",
        suggested_action: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.stage = stage
        self.code = code
        self.suggested_action = suggested_action


@dataclass
class CrmExtractionResult:
    normalized: NormalizedDocument
    # CRMProcessingResult.to_crm_json(): one dict per entity, record fields plus "_meta"
    # (entity_type, confidence, per-field provenance).
    entities: list[dict[str, Any]]
    # CRMProcessingResult.to_json() (extraction + validation), stored as the file's result_json.
    result: dict[str, Any]
    valid: bool
    # validation issues: [{entity_type, entity_index, field, severity, code, message}]
    issues: list[dict[str, Any]] = field(default_factory=list)
    # document_type, extractor versions, seconds, ...
    metrics: dict[str, Any] = field(default_factory=dict)


def crm_extraction_available() -> tuple[bool, str | None]:
    """(True, None) when document-extractor AND crm-document-ingestion import here. Cheap; never raises."""
    ok, why, _ = _availability()
    return ok, why


def run_crm_extraction(
    source_path: Path,
    *,
    filename: str,
    media_type: str,
    sha256: str,
    work_dir: Path,
    settings: DocIntelSettings,
    use_intelligence: bool = False,
) -> CrmExtractionResult:
    """BLOCKING (call via asyncio.to_thread). Child-process extraction + CRM entity
    extraction + validation of one file, under settings.extraction_timeout_seconds.
    ``work_dir`` is a private temp directory for the job files (the caller deletes it).
    Raises CrmExtractionFailed(stage=...) with an admin-readable reason.
    """
    if use_intelligence:
        raise CrmExtractionFailed(INTELLIGENCE_NOT_SUPPORTED_REASON, stage="intelligence", code="not_supported",
                                  suggested_action=_INTELLIGENCE_ACTION)
    ok, why, stage = _availability()
    if not ok:
        action = extraction._INSTALL_ACTION if stage == "extraction" else _CRM_INSTALL_ACTION
        raise CrmExtractionFailed(why or "CRM extraction is not available", stage=stage, code="unavailable",
                                  suggested_action=action)
    source_path = Path(source_path)
    if not source_path.is_file():
        raise CrmExtractionFailed("The downloaded file is missing — sync again", stage="extraction",
                                  code="source_missing")
    try:
        media_type = extraction._media_type_for(media_type, source_path)
    except ExtractionFailed as exc:
        raise CrmExtractionFailed(exc.reason, stage="extraction", code=exc.code,
                                  suggested_action=exc.suggested_action) from None
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    # The same slot as Flow 1: one extraction child per process at a time (plan risk R2).
    with extraction._EXTRACTION_SLOT:
        return _run_crm_child(
            source_path, filename=filename, media_type=media_type, sha256=sha256, work_dir=work_dir,
            settings=settings, timeout=float(settings.extraction_timeout_seconds),
        )


def _run_crm_child(
    source_path: Path,
    *,
    filename: str,
    media_type: str,
    sha256: str,
    work_dir: Path,
    settings: DocIntelSettings,
    timeout: float,
) -> CrmExtractionResult:
    job_path, result_path, error_path, progress_path = (
        work_dir / name for name in (_JOB_NAME, _RESULT_NAME, _ERROR_NAME, _PROGRESS_NAME)
    )
    # A stale result or error from an earlier attempt must never be mistaken for this one.
    for stale in (result_path, result_path.with_suffix(result_path.suffix + ".tmp"), error_path, progress_path):
        extraction._remove_quietly(stale)
    job = extraction._job_base(source_path, filename=filename, media_type=media_type, sha256=sha256,
                               settings=settings, timeout=timeout)
    job.update({
        "kind": JOB_KIND_CRM,
        "output_path": str(result_path.resolve()),
        "error_path": str(error_path.resolve()),
        "progress_path": str(progress_path.resolve()),
        "crm": {
            "schema_paths": [str(p) for p in schema_paths(settings)],
            "min_confidence": float(settings.crm_min_confidence),
            "use_intelligence": False,
        },
    })
    started = time.perf_counter()
    try:
        proc = extraction._spawn_job(job, job_path, settings, timeout)
    except subprocess.TimeoutExpired:
        stage = _read_stage(progress_path)
        _log.warning("doc_intel: CRM extraction of %s killed after %.0f s (stage %s)", sha256[:12], timeout, stage)
        raise CrmExtractionFailed(extraction._timeout_reason(timeout), stage=stage, code="timeout",
                                  suggested_action=extraction._TIMEOUT_ACTION) from None
    elapsed = time.perf_counter() - started

    if proc.returncode == 0:
        try:
            result = _load_result(result_path)
        except (OSError, ValueError, KeyError, TypeError):
            raise CrmExtractionFailed("The CRM extraction process finished without a usable result",
                                      stage="entities", code="crash", suggested_action=_CRASH_ACTION) from None
        _log.info(
            "doc_intel: CRM-extracted %s in %.1f s: %s pages, %s entities (%s)",
            sha256[:12], elapsed, result.normalized.page_count, len(result.entities),
            "valid" if result.valid else f"{sum(1 for i in result.issues if i.get('severity') == 'error')} errors",
        )
        return result

    failure = _read_failure(error_path, progress_path, timeout)
    if failure is None:
        failure = CrmExtractionFailed(extraction._crash_reason(proc), stage=_read_stage(progress_path), code="crash",
                                      suggested_action=_CRASH_ACTION)
    _log.warning(
        "doc_intel: CRM extraction of %s failed after %.1f s (%s at %s, exit %s): %s",
        sha256[:12], elapsed, failure.code, failure.stage, proc.returncode,
        extraction._stderr_tail(proc.stderr) or "-",
    )
    raise failure


def schema_paths(settings: DocIntelSettings) -> list[Path]:
    """DOC_INTEL_CRM_SCHEMA_PATHS as absolute paths (relative ones resolve against AIVA-V2/,
    like DOC_INTEL_STORAGE_DIR)."""
    out: list[Path] = []
    for raw in settings.crm_schema_path_list:
        path = Path(raw)
        out.append(path if path.is_absolute() else extraction._ROOT / path)
    return out


def _availability() -> tuple[bool, str | None, CrmChildStage]:
    """(ok, reason, the stage that cannot run)."""
    ok, why = extraction.extraction_available()
    if not ok:
        return False, why, "extraction"
    try:
        importlib.import_module("crm_ingestion")
    except ImportError as exc:
        name = getattr(exc, "name", None) or ""
        if not name or name.startswith("crm_ingestion"):
            return False, "crm-document-ingestion is not installed in the backend environment — see the runbook", \
                "intelligence"
        return False, f"crm-document-ingestion cannot be loaded: {name} is missing", "intelligence"
    except Exception as exc:  # a broken install
        return False, f"crm-document-ingestion cannot be imported ({type(exc).__name__})", "intelligence"
    return True, None, "extraction"


def _stage(value: Any) -> CrmChildStage | None:
    return value if value in CRM_CHILD_STAGES else None  # type: ignore[return-value]


def _read_stage(progress_path: Path) -> CrmChildStage:
    """The stage the child last entered (it writes it as it goes); "extraction" when unknown."""
    try:
        return _stage(progress_path.read_text(encoding="utf-8").strip()) or "extraction"
    except (OSError, ValueError):
        return "extraction"


def _read_failure(error_path: Path, progress_path: Path, timeout: float) -> CrmExtractionFailed | None:
    error = extraction._read_error_payload(error_path)
    if error is None:
        return None
    code = str(error.get("code") or "crm_extraction_failed")
    stage = _stage(error.get("stage")) or _read_stage(progress_path)
    action = error.get("suggested_action")
    if code == "timeout":
        return CrmExtractionFailed(extraction._timeout_reason(timeout), stage=stage, code="timeout",
                                   suggested_action=scrub_detail(action, 400) if action else extraction._TIMEOUT_ACTION)
    reason = str(error.get("reason") or "").strip()
    if not reason:
        return None
    return CrmExtractionFailed(scrub_detail(reason, 400), stage=stage, code=code,
                               suggested_action=scrub_detail(action, 400) if action else None)


def _load_result(result_path: Path) -> CrmExtractionResult:
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("the result is not a JSON object")
    normalized = NormalizedDocument.model_validate(payload["normalized"])
    entities, result = payload["entities"], payload["result"]
    issues, metrics = payload.get("issues") or [], payload.get("metrics") or {}
    if not (isinstance(entities, list) and isinstance(result, dict) and isinstance(issues, list)
            and isinstance(metrics, dict)):
        raise ValueError("the result has an unexpected shape")
    return CrmExtractionResult(
        normalized=normalized,
        entities=[e for e in entities if isinstance(e, dict)],
        result=result,
        valid=bool(payload.get("valid")),
        issues=[i for i in issues if isinstance(i, dict)],
        metrics=metrics,
    )
