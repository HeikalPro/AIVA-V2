"""Monitoring: six component checks, their persisted state, failures and the activity feed.

Checks run on demand (``run_checks``), concurrently, each bounded by a timeout, and are
stored in ``AIVA_health_checks`` with ``AIVA_health_check_events`` rows on status
changes. The UI reads the stored results (``load_overview``); reading never triggers a
live check. Nothing stored here contains a secret: API keys are only ever sent as a
request header, and database targets are named by service, never by connect string.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from backend.doc_intel.constants import (
    CRM_STAGES,
    DOC_FAILED,
    DOC_PROCESSING,
    DOC_PUBLISHED,
    DOC_QUEUED,
    DOC_UNPUBLISHED,
    HEALTH_COMPONENT_LABELS,
    HEALTH_COMPONENTS,
    HEALTH_FAILED,
    HEALTH_HEALTHY,
    HEALTH_NOT_CONFIGURED,
    KB_STAGES,
    MAX_ACTION_BYTES,
    MAX_REASON_BYTES,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_QUEUED,
    RUN_RUNNING,
    kb_vertical_for,
)
from backend.doc_intel.embedding import scrub_secrets
from backend.doc_intel.kb_repo import loads_json, normalize_corpus_id, parse_queue_keys, parse_utc, stage_column
from backend.doc_intel.queue_config import queues_with_vertical
from backend.doc_intel.schemas import (
    ActivityItemOut,
    ActivityOut,
    FailureItemOut,
    FailuresOut,
    HealthComponentOut,
    HealthEventOut,
    HealthOverviewOut,
)
from backend.doc_intel.settings import DocIntelSettings
from backend.doc_intel.textutil import iso_utc, truncate_utf8, utc_now
from backend.doc_intel.threads import run_blocking
from embedding_service.models.corpus_config import parse_corpus_config

_log = logging.getLogger(__name__)

DB_PROBE_TIMEOUT_SECONDS = 5.0
EMBEDDING_PROBE_TIMEOUT_SECONDS = 10.0
DEFAULT_EMBEDDING_BASE_URL = "https://api.openai.com/v1"
PHASE2_REASON = "Part of Phase 2 — not installed yet"
NO_SOURCE_REASON = "No SharePoint source is configured"
CRM_STORE_ACTION = "The CRM store is unreachable — see the Database component"
SYNC_FAILED_ACTION = "Fix the cause shown, then use Sync now on the Integrations page"
SYNC_OVERDUE_ACTION = "Check the backend log for 'doc_intel' scheduler errors; use Sync now meanwhile"
_DETAIL_LIST_LIMIT = 20
_EPOCH = datetime(1970, 1, 1)

# (components it applies to (None = all), pattern on the reason, suggested action). First match wins.
SUGGESTED_ACTIONS: tuple[tuple[frozenset[str] | None, re.Pattern[str], str], ...] = (
    (None, re.compile(r"ORA-00257"), "The database host disk is full (archiver error): free space on the DB server"),
    (None, re.compile(r"ORA-01017"), "Database credentials were rejected: check the ORACLE_USER / ORACLE_PASSWORD of this pool"),
    (None, re.compile(r"ORA-28000"), "The database account is locked: unlock it on the DB server"),
    (
        None,
        re.compile(r"ORA-(?:12541|12514|12505|12170|12543|03113|03114|03135)|DPY-(?:6005|6000|6001|4011)"),
        "The database is unreachable: check the Oracle container/host",
    ),
    (frozenset({"database"}), re.compile(r"time(?:d)? ?out", re.I), "The database is unreachable: check the Oracle container/host"),
    (
        frozenset({"embedding"}),
        re.compile(r"No API key", re.I),
        "Set the embedding API key (e.g. SOVEREIGNEG_API_KEY) named by the corpus embedder.api_key_env",
    ),
    (
        frozenset({"embedding"}),
        re.compile(r"rejected the API key|\b40[13]\b"),
        "Update the embedding API key (e.g. SOVEREIGNEG_API_KEY) used by the corpus",
    ),
    (frozenset({"embedding"}), re.compile(r"invalid configuration", re.I), "Fix the corpus embedder configuration (type, base_url, model, dimension)"),
    (
        frozenset({"embedding"}),
        re.compile(r"Knowledge-base database|knowledge base database", re.I),
        "The knowledge-base database is unreachable: check the Oracle container/host",
    ),
    (frozenset({"embedding"}), re.compile(r"HTTP 5\d\d", re.I), "The embedding provider is failing: retry later or contact the provider"),
    (
        frozenset({"embedding"}),
        re.compile(r"unreachable|time(?:d)? ?out|HTTP \d{3}", re.I),
        "Check the embedding endpoint (base_url) and outbound HTTPS from the server to the provider",
    ),
    (
        frozenset({"extraction"}),
        re.compile(r"\bara\b|Arabic", re.I),
        "Install the Tesseract Arabic language pack (apt install tesseract-ocr-ara) or set DOC_INTEL_TESSERACT_CMD",
    ),
    (
        frozenset({"extraction"}),
        re.compile(r"tesseract", re.I),
        "Install Tesseract OCR (apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-ara) or set DOC_INTEL_TESSERACT_CMD",
    ),
    (
        frozenset({"extraction"}),
        re.compile(r"document-extractor|not installed|No module", re.I),
        "Install document-extractor in the backend environment (see the deployment runbook)",
    ),
    (
        frozenset({"extraction"}),
        re.compile(r"time(?:d)? ?out", re.I),
        "The extraction smoke test timed out: check the server's CPU load and the extractor installation",
    ),
    (
        frozenset({"knowledge_sync"}),
        re.compile(r"lost|missing", re.I),
        "Republish the affected documents (the corpus may have been re-indexed or its queue groups overwritten)",
    ),
    (frozenset({"knowledge_sync"}), re.compile(r"worker", re.I), "Restart the backend and check its log for 'doc_intel' errors"),
    (
        frozenset({"knowledge_sync"}),
        re.compile(r"stuck", re.I),
        "Check the backend log for the stuck documents; they are marked failed once their heartbeat stops — then Retry",
    ),
    (
        frozenset({"knowledge_sync"}),
        re.compile(r"database", re.I),
        "The database is unreachable: check the Oracle container/host",
    ),
)


def suggested_action_for(component: str, text: str | None) -> str | None:
    if not text:
        return None
    for components, pattern, action in SUGGESTED_ACTIONS:
        if components is not None and component not in components:
            continue
        if pattern.search(text):
            return action
    return None


@dataclass
class CheckResult:
    key: str
    status: str
    reason: str | None = None
    suggested_action: str | None = None
    latency_ms: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthDeps:
    db: Any  # backend.database.Database (app pool)
    kb: Any  # KbStore (KB pool)
    kb_repo: Any  # KbRepo
    health_repo: Any  # HealthRepo
    settings: DocIntelSettings
    embedding_settings: Any = None  # embedding_service Settings (default key)
    worker_running: Callable[[], bool] | None = None
    extraction: Any = None  # module with extraction_available() / run_smoke_test()
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None
    app_db_service: str | None = None
    kb_db_service: str | None = None
    # Phase 2 (migration V002). None until SharePoint sync is installed: the Microsoft and CRM
    # components then say "not installed yet" and the feeds hold knowledge documents only.
    crm_repo: Any = None  # CrmRepo
    secret_box_factory: Callable[[], Any] | None = None  # () -> SecretBox; raises SecretsUnavailable
    graph_factory: Callable[[Any], Any] | None = None  # GraphCredentials -> GraphSource
    sync_worker_running: Callable[[], bool] | None = None


# ---- throttle ------------------------------------------------------------------------------

_last_run_monotonic: float | None = None
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


def reset_throttle() -> None:
    """Forget the last run (tests)."""
    global _last_run_monotonic
    _last_run_monotonic = None


# ---- entry points --------------------------------------------------------------------------


async def run_checks(deps: HealthDeps, *, component: str | None = None) -> HealthOverviewOut:
    """Run all checks (or one), persist them, and return the overview.

    Calls within ``health_min_interval_seconds`` of the previous run return the stored
    results with ``throttled=True`` instead of checking again.
    """
    global _last_run_monotonic
    keys = [component] if component else [key for key, _ in HEALTH_COMPONENTS]
    for key in keys:
        if key not in HEALTH_COMPONENT_LABELS:
            raise ValueError(f"Unknown component: {key}")
    async with _get_lock():
        now_m = time.monotonic()
        if _last_run_monotonic is not None and now_m - _last_run_monotonic < deps.settings.health_min_interval_seconds:
            overview = await load_overview(deps)
            return overview.model_copy(update={"throttled": True})
        _last_run_monotonic = now_m
        checked_at = utc_now()
        results = list(await asyncio.gather(*(_run_one(deps, key) for key in keys)))
        persisted = True
        for result in results:
            try:
                changed = await deps.health_repo.save(result, label=HEALTH_COMPONENT_LABELS[result.key], checked_at=checked_at)
                if changed:
                    _log.info("doc_intel: health %s -> %s (%s)", result.key, result.status, result.reason)
            except Exception:
                persisted = False
                _log.warning("doc_intel: could not store health result for %s", result.key, exc_info=True)
        if persisted:
            try:
                return await load_overview(deps)
            except Exception:
                _log.warning("doc_intel: could not reload health results", exc_info=True)
        return _overview_from_results(results, checked_at)


async def load_overview(deps: HealthDeps) -> HealthOverviewOut:
    """The stored results (never runs a check)."""
    try:
        rows = await deps.health_repo.load_all()
    except Exception as ex:
        _log.warning("doc_intel: could not load health results", exc_info=True)
        return _unreadable_overview(ex)
    by_key = {str(r.get("component_key")): r for r in rows}
    components = [
        _component_from_row(by_key[key], label) if key in by_key else _not_checked(key, label)
        for key, label in HEALTH_COMPONENTS
    ]
    stamps = [t for t in (parse_utc(r.get("checked_at")) for r in rows) if t is not None]
    newest = max(stamps) if stamps else None
    stale = newest is None or newest < utc_now() - timedelta(minutes=deps.settings.health_stale_minutes)
    return HealthOverviewOut(
        overall=_overall(components),
        checked_at=iso_utc(newest),
        stale=stale,
        components=components,
    )


async def load_events(deps: HealthDeps, limit: int) -> list[HealthEventOut]:
    rows = await deps.health_repo.list_events(limit)
    out: list[HealthEventOut] = []
    for r in rows:
        key = str(r.get("component_key"))
        out.append(
            HealthEventOut(
                id=int(r["id"]),
                component_key=key,
                label=HEALTH_COMPONENT_LABELS.get(key, key),
                old_status=_health_status(r.get("old_status")),
                new_status=_health_status(r.get("new_status")) or HEALTH_NOT_CONFIGURED,
                reason=r.get("reason"),
                created_at=iso_utc(r.get("created_at")),
            )
        )
    return out


async def load_failures(deps: HealthDeps, days: int) -> FailuresOut:
    """Knowledge documents, and (Phase 2) SharePoint files and sync runs, that failed in the
    last ``days`` days, newest first."""
    rows = await deps.kb_repo.failures(days)
    items = [
        FailureItemOut(
            kind="kb_document",
            id=int(r["id"]),
            title=str(r.get("filename") or f"Document #{r['id']}"),
            account_name=r.get("account_name"),
            stage=r.get("failed_stage"),
            reason=r.get("error_message"),
            occurred_at=iso_utc(r.get("finished_at") or r.get("updated_at")),
        )
        for r in rows
    ]
    if deps.crm_repo is not None:
        since = utc_now() - timedelta(days=int(days))
        try:
            file_rows, run_rows = await asyncio.gather(
                deps.crm_repo.failed_files_since(since), deps.crm_repo.failed_runs_since(since)
            )
        except Exception:
            _log.warning("doc_intel: SharePoint failures unavailable", exc_info=True)
            file_rows, run_rows = [], []
        items += [_crm_file_failure(r) for r in file_rows]
        items += [_sync_run_failure(r) for r in run_rows]
        items.sort(key=lambda item: parse_utc(item.occurred_at) or _EPOCH, reverse=True)
    return FailuresOut(days=days, items=items)


async def load_activity(deps: HealthDeps, limit: int) -> ActivityOut:
    """Newest document state changes and health transitions (plus, in Phase 2, SharePoint file
    and sync-run changes), merged, newest first."""
    doc_rows, event_rows = await asyncio.gather(deps.kb_repo.activity(limit), deps.health_repo.list_events(limit))
    items: list[tuple[str, ActivityItemOut]] = []
    for r in doc_rows:
        item = _document_activity(r)
        items.append((str(r.get("updated_at") or ""), item))
    if deps.crm_repo is not None:
        try:
            file_rows, run_rows = await asyncio.gather(deps.crm_repo.file_activity(limit), deps.crm_repo.run_activity(limit))
        except Exception:
            _log.warning("doc_intel: SharePoint activity unavailable", exc_info=True)
            file_rows, run_rows = [], []
        items += [(str(r.get("updated_at") or ""), _crm_file_activity(r)) for r in file_rows]
        items += [(str(r.get("updated_at") or ""), _sync_run_activity(r)) for r in run_rows]
    for r in event_rows:
        key = str(r.get("component_key"))
        label = HEALTH_COMPONENT_LABELS.get(key, key)
        new = str(r.get("new_status") or "")
        old = r.get("old_status") or "—"
        reason = _short(r.get("reason"))
        level = "error" if new == HEALTH_FAILED else ("info" if new == HEALTH_HEALTHY else "warning")
        items.append(
            (
                str(r.get("created_at") or ""),
                ActivityItemOut(
                    kind="health",
                    level=level,
                    message=f"{label}: {old} → {new}" + (f" — {reason}" if reason else ""),
                    ref_id=int(r["id"]),
                    occurred_at=iso_utc(r.get("created_at")),
                ),
            )
        )
    items.sort(key=lambda pair: parse_utc(pair[0]) or parse_utc("1970-01-01T00:00:00"), reverse=True)
    return ActivityOut(items=[item for _, item in items[:limit]])


# ---- one check -----------------------------------------------------------------------------


async def _run_one(deps: HealthDeps, key: str) -> CheckResult:
    timeout = (
        deps.settings.extraction_smoke_timeout_seconds + 10
        if key == "extraction"
        else deps.settings.health_check_timeout_seconds
    )
    start = time.perf_counter()
    try:
        result = await asyncio.wait_for(_CHECKS[key](deps), timeout)
    except TimeoutError:
        result = CheckResult(key, HEALTH_FAILED, f"Check timed out after {timeout:g} s")
    except Exception as ex:
        _log.warning("doc_intel: health check %s raised", key, exc_info=True)
        result = CheckResult(key, HEALTH_FAILED, f"Check failed: {_error_text(ex)}")
    if result.latency_ms is None and result.status != HEALTH_NOT_CONFIGURED:
        result.latency_ms = _ms(start)
    if result.status == HEALTH_FAILED and not result.suggested_action:
        result.suggested_action = suggested_action_for(key, result.reason)
    result.reason = truncate_utf8(result.reason, MAX_REASON_BYTES)
    result.suggested_action = truncate_utf8(result.suggested_action, MAX_ACTION_BYTES)
    result.details = json.loads(json.dumps(result.details or {}, default=str))
    return result


async def _check_database(deps: HealthDeps) -> CheckResult:
    async def app_probe() -> None:
        await deps.db.fetch_one("SELECT 1 AS ok FROM dual")

    async def kb_probe() -> None:
        await asyncio.to_thread(deps.kb.ping)

    app, kb = await asyncio.gather(_probe(app_probe, DB_PROBE_TIMEOUT_SECONDS), _probe(kb_probe, DB_PROBE_TIMEOUT_SECONDS))
    details = {
        "app_pool": {**app, "service": deps.app_db_service},
        "kb_pool": {**kb, "service": deps.kb_db_service},
    }
    latency = max((p["latency_ms"] or 0) for p in (app, kb))
    problems = []
    if not app["ok"]:
        problems.append(f"Application database: {app['error']}")
    if not kb["ok"]:
        problems.append(f"Knowledge-base database: {kb['error']}")
    if problems:
        return CheckResult("database", HEALTH_FAILED, "; ".join(problems), latency_ms=latency, details=details)
    return CheckResult(
        "database",
        HEALTH_HEALTHY,
        f"Application and knowledge-base databases reachable ({app['latency_ms']} ms / {kb['latency_ms']} ms)",
        latency_ms=latency,
        details=details,
    )


async def _check_embedding(deps: HealthDeps) -> CheckResult:
    async def kb_probe() -> None:
        await asyncio.to_thread(deps.kb.ping)

    ping = await _probe(kb_probe, DB_PROBE_TIMEOUT_SECONDS)
    details: dict[str, Any] = {"kb_pool": {**ping, "service": deps.kb_db_service}}
    if not ping["ok"]:
        return CheckResult(
            "embedding", HEALTH_FAILED, f"Knowledge-base database unreachable: {ping['error']}", details=details
        )

    corpora = await deps.kb_repo.distinct_account_corpora()
    if not corpora:
        return CheckResult(
            "embedding",
            HEALTH_NOT_CONFIGURED,
            "No account has a knowledge base yet",
            latency_ms=ping["latency_ms"],
            details=details,
        )

    problems: list[str] = []
    missing: list[str] = []
    endpoints: dict[tuple[Any, ...], dict[str, Any]] = {}
    secrets: dict[tuple[Any, ...], tuple[str, str | None]] = {}  # never stored
    for cid in corpora:
        short = f"{cid[:8]}…"
        try:
            config = await asyncio.wait_for(asyncio.to_thread(deps.kb.get_corpus_config, cid), DB_PROBE_TIMEOUT_SECONDS)
        except Exception as ex:
            problems.append(f"Knowledge base {short}: {_error_text(ex)}")
            continue
        if config is None:
            missing.append(short)
            continue
        try:
            cfg = parse_corpus_config(config)
        except Exception:
            problems.append(f"Knowledge base {short} has an invalid configuration")
            continue
        emb = cfg.embedder
        if emb.type == "oracle":
            ident: tuple[Any, ...] = ("oracle", emb.model)
            entry = endpoints.setdefault(ident, {"type": "oracle", "models": [], "corpora": 0, "ok": True})
        else:
            key, source = _resolve_api_key(emb, deps.embedding_settings)
            base = (emb.base_url or DEFAULT_EMBEDDING_BASE_URL).rstrip("/")
            fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else None
            ident = ("http", base, fingerprint)
            entry = endpoints.setdefault(
                ident,
                {"type": "http", "base_url": _safe_url(base), "models": [], "corpora": 0, "key_source": source},
            )
            secrets[ident] = (base, key)
        if emb.model not in entry["models"]:
            entry["models"].append(emb.model)
        entry["corpora"] += 1

    http_idents = [ident for ident in endpoints if ident[0] == "http"]
    probes = await asyncio.gather(*(_probe_models(deps, *secrets[ident]) for ident in http_idents))
    latencies = [ping["latency_ms"] or 0]
    for ident, probe in zip(http_idents, probes):
        entry = endpoints[ident]
        entry.update(probe)
        if probe["latency_ms"] is not None:
            latencies.append(probe["latency_ms"])
        if not probe["ok"]:
            problems.append(f"{probe['error']} ({entry['base_url']})")
    details["endpoints"] = list(endpoints.values())
    if missing:
        details["missing_corpora"] = missing
    latency = max(latencies)
    if problems:
        reason = problems[0] + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else "")
        return CheckResult("embedding", HEALTH_FAILED, reason, latency_ms=latency, details=details)
    n = len(endpoints)
    return CheckResult(
        "embedding",
        HEALTH_HEALTHY,
        f"{n} embedding endpoint{'s' if n != 1 else ''} reachable for {len(corpora)} knowledge base{'s' if len(corpora) != 1 else ''}",
        latency_ms=latency,
        details=details,
    )


async def _check_extraction(deps: HealthDeps) -> CheckResult:
    ext = deps.extraction
    if ext is None:
        from backend.doc_intel import extraction as ext
    available, why = await asyncio.to_thread(ext.extraction_available)
    if not available:
        return CheckResult("extraction", HEALTH_FAILED, why or "document-extractor is not installed in the backend environment")
    timeout = deps.settings.extraction_smoke_timeout_seconds
    smoke = await asyncio.to_thread(ext.run_smoke_test, deps.settings, timeout_seconds=timeout)
    smoke = smoke if isinstance(smoke, dict) else {}
    info = smoke.get("info") if isinstance(smoke.get("info"), dict) else {}
    details: dict[str, Any] = {
        **info,
        "smoke_test": {"ok": bool(smoke.get("ok")), "seconds": smoke.get("seconds"), "busy": bool(smoke.get("busy"))},
    }
    if not smoke.get("ok"):
        detail = str(smoke.get("detail") or "no detail")
        reason = detail if detail.lower().startswith("smoke test") else f"Extraction smoke test failed: {detail}"
        return CheckResult("extraction", HEALTH_FAILED, reason, details=details)

    requested = list(deps.settings.ocr_language_list)
    installed = _installed_languages(info)
    if installed is None and _tesseract_missing(info):
        return CheckResult("extraction", HEALTH_FAILED, "Tesseract OCR was not found on the server", details=details)
    if installed is not None:
        missing = [lang for lang in requested if lang not in installed]
        if "ara" in missing:
            return CheckResult("extraction", HEALTH_FAILED, "Tesseract Arabic language data (ara) not found", details=details)
        if missing:
            return CheckResult(
                "extraction",
                HEALTH_FAILED,
                f"Tesseract language data not found: {', '.join(missing)}",
                details=details,
            )
    seconds = smoke.get("seconds")
    took = f" in {float(seconds):.1f} s" if isinstance(seconds, (int, float)) else ""
    version = info.get("document_extractor_version") or info.get("version")
    name = f"document-extractor {version}" if version else "document-extractor"
    if smoke.get("busy"):
        return CheckResult(
            "extraction", HEALTH_HEALTHY,
            f"{name} ready; a document is being extracted right now (smoke test skipped)", details=details,
        )
    return CheckResult("extraction", HEALTH_HEALTHY, f"{name} ready; smoke test passed{took}", details=details)


async def _check_knowledge_sync(deps: HealthDeps) -> CheckResult:
    now = utc_now()
    worker_running = deps.worker_running() if deps.worker_running is not None else None
    counts = await deps.kb_repo.status_counts()
    stuck = await deps.kb_repo.stuck_documents(now - timedelta(minutes=deps.settings.stuck_after_minutes))
    failed_count, failed_rows = await deps.kb_repo.failed_since(now - timedelta(hours=24))
    published = await deps.kb_repo.published_documents()
    integrity = await _integrity_problems(deps, published)

    details: dict[str, Any] = {
        "worker_running": worker_running,
        "queued": counts.get(DOC_QUEUED, 0),
        "processing": counts.get(DOC_PROCESSING, 0),
        "published": counts.get(DOC_PUBLISHED, 0),
        "failed_last_24h": failed_count,
        "published_checked": len(published),
    }
    if stuck:
        details["stuck"] = [
            {"id": int(r["id"]), "filename": r.get("filename"), "started_at": iso_utc(r.get("started_at"))}
            for r in stuck[:_DETAIL_LIST_LIMIT]
        ]
    if integrity:
        details["integrity_problems"] = integrity[:_DETAIL_LIST_LIMIT]
    if failed_rows:
        details["recent_failures"] = [
            {"id": int(r["id"]), "filename": r.get("filename"), "stage": r.get("failed_stage"), "reason": _short(r.get("error_message"))}
            for r in failed_rows[:_DETAIL_LIST_LIMIT]
        ]

    problems: list[str] = []
    if worker_running is False:
        problems.append("The import worker is not running")
    if integrity:
        n = len(integrity)
        problems.append(
            f"{n} published document{'s' if n != 1 else ''} lost {'their' if n != 1 else 'its'} chunks or queue assignment "
            "(corpus re-indexed or queue groups overwritten?) — Republish"
        )
    if stuck:
        n = len(stuck)
        problems.append(
            f"{n} document{'s' if n != 1 else ''} stuck in processing for more than {deps.settings.stuck_after_minutes} min"
        )
    failed_note = f"{failed_count} import{'s' if failed_count != 1 else ''} failed in the last 24 h" if failed_count else None
    # Phase 2: the SharePoint side of "knowledge sync" (last run per source, stuck runs, overdue schedules).
    sharepoint = await _sharepoint_sync_state(deps, now) if deps.crm_repo is not None else None
    if sharepoint is not None:
        details["sharepoint"] = sharepoint.details
    sp_problems = sharepoint.problems if sharepoint is not None else []
    sp_notes = sharepoint.notes if sharepoint is not None else []
    if problems or sp_problems:
        reason = "; ".join(problems + [text for text, _ in sp_problems])
        reason += "".join(f" · {note}" for note in ([failed_note] if failed_note else []) + sp_notes)
        action = suggested_action_for("knowledge_sync", problems[0]) if problems else sp_problems[0][1]
        return CheckResult("knowledge_sync", HEALTH_FAILED, reason, suggested_action=action, details=details)
    parts = ["Import worker running"] if worker_running else []
    parts += [
        f"{details['queued']} queued",
        f"{len(published)} published document{'s' if len(published) != 1 else ''} verified",
    ]
    if failed_note:
        parts.append(failed_note)
    parts += sp_notes
    return CheckResult("knowledge_sync", HEALTH_HEALTHY, " · ".join(parts), details=details)


@dataclass
class _SharePointState:
    problems: list[tuple[str, str | None]]  # (reason, suggested action)
    notes: list[str]  # informational, never failing
    details: dict[str, Any]


async def _sharepoint_sync_state(deps: HealthDeps, now: datetime) -> _SharePointState:
    """Last run per ACTIVE source (FAILED fails the component), RUNNING runs whose heartbeat
    stopped, schedules overdue by more than a day while the scheduler is on, and the worker."""
    settings = deps.settings
    try:
        sources = [s for s in await deps.crm_repo.list_sources() if s.get("status") == "ACTIVE"]
        last = await deps.crm_repo.last_runs()
        stuck = await deps.crm_repo.stuck_runs(now - timedelta(minutes=settings.stuck_after_minutes))
    except Exception as ex:
        reason = f"SharePoint sync state unavailable: {_error_text(ex)}"
        return _SharePointState(
            [(reason, suggested_action_for("database", reason) or "The database is unreachable: check the Oracle container/host")],
            [],
            {"error": _error_text(ex)},
        )
    scheduler_on = bool(settings.scheduler_enabled)
    sync_running = deps.sync_worker_running() if deps.sync_worker_running is not None else None
    problems: list[tuple[str, str | None]] = []
    notes: list[str] = []
    entries: list[dict[str, Any]] = []
    never = partial = 0
    if sync_running is False and sources:
        problems.append(("The sync worker is not running", "Restart the backend and check its log for 'doc_intel' errors"))
    for source in sources:
        name = str(source.get("name") or f"Source #{source['id']}")
        run = last.get(int(source["id"]))
        due = parse_utc(source.get("next_sync_at"))
        enabled = _flag(source.get("sync_enabled"))
        entries.append(
            {
                "id": int(source["id"]),
                "name": name,
                "last_run_id": int(run["id"]) if run else None,
                "last_run_status": run.get("status") if run else None,
                "last_run_at": iso_utc((run.get("finished_at") or run.get("created_at")) if run else None),
                "sync_enabled": enabled,
                "next_sync_at": iso_utc(due),
            }
        )
        if run is None:
            never += 1
        elif run.get("status") == RUN_FAILED:
            why = _short(run.get("error_message")) or "no reason recorded"
            problems.append((f"Last sync of '{name}' failed: {why}", SYNC_FAILED_ACTION))
        elif run.get("status") == RUN_PARTIAL:
            partial += 1
        if scheduler_on and enabled and due is not None and due < now - timedelta(days=1):
            problems.append((f"Scheduled sync is overdue for '{name}' (was due {iso_utc(due)})", SYNC_OVERDUE_ACTION))
    if stuck:
        n = len(stuck)
        problems.append(
            (
                f"{n} sync run{'s' if n != 1 else ''} stuck: no heartbeat for more than {settings.stuck_after_minutes} min",
                "Restart the backend (interrupted runs are then marked failed), then use Sync now",
            )
        )
    if sources:
        n = len(sources)
        summary = f"{n} SharePoint source{'s' if n != 1 else ''}"
        extra = [f"{never} never synced"] if never else []
        extra += [f"{partial} last synced with problems"] if partial else []
        notes.append(summary + (f" ({', '.join(extra)})" if extra else ""))
    if not scheduler_on and any(_flag(s.get("sync_enabled")) for s in sources):
        notes.append("Automatic syncs are off (DOC_INTEL_SCHEDULER_ENABLED=false)")
    details: dict[str, Any] = {
        "sources": entries[:_DETAIL_LIST_LIMIT],
        "scheduler_enabled": scheduler_on,
        "sync_worker_running": sync_running,
    }
    if stuck:
        details["stuck_runs"] = [
            {
                "id": int(r["id"]),
                "source_id": int(r["source_id"]),
                "source_name": r.get("source_name"),
                "started_at": iso_utc(r.get("started_at")),
                "last_heartbeat_at": iso_utc(r.get("updated_at")),
            }
            for r in stuck[:_DETAIL_LIST_LIMIT]
        ]
    return _SharePointState(problems, notes, details)


async def _integrity_problems(deps: HealthDeps, published: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PUBLISHED documents whose chunks or queue verticals are gone from the KB."""
    by_corpus: dict[str, list[dict[str, Any]]] = {}
    for row in published:
        corpus = normalize_corpus_id(row.get("corpus_id")) or str(row.get("corpus_id") or "")
        by_corpus.setdefault(corpus, []).append(row)
    problems: list[dict[str, Any]] = []
    for corpus, rows in by_corpus.items():
        config = await asyncio.to_thread(deps.kb.get_corpus_config, corpus)
        verticals = [str(r.get("vertical") or kb_vertical_for(int(r["id"]))) for r in rows]
        counts = await asyncio.to_thread(deps.kb.chunk_counts, corpus, verticals)
        for row, vertical in zip(rows, verticals):
            expected = int(row.get("chunk_count") or 0)
            have = int(counts.get(vertical, 0))
            keys = parse_queue_keys(row.get("queue_keys"))
            present = set(queues_with_vertical(config, vertical)) if config is not None else set()
            missing_queues = [k for k in keys if k not in present]
            lost_chunks = have == 0 or (expected > 0 and have < expected)
            if lost_chunks or missing_queues:
                problems.append(
                    {
                        "id": int(row["id"]),
                        "filename": row.get("filename"),
                        "chunks": have,
                        "expected_chunks": expected or None,
                        "missing_queues": missing_queues,
                        "corpus_missing": config is None,
                    }
                )
    return problems


async def _check_phase2(key: str) -> CheckResult:
    return CheckResult(key, HEALTH_NOT_CONFIGURED, PHASE2_REASON)


async def _check_microsoft_graph(deps: HealthDeps) -> CheckResult:
    """For each ACTIVE source: decrypt, sign in, resolve the folder (cheap; nothing is listed)."""
    if deps.crm_repo is None:
        return await _check_phase2("microsoft_graph")
    sources = [s for s in await deps.crm_repo.list_sources() if s.get("status") == "ACTIVE"]
    if not sources:
        return CheckResult("microsoft_graph", HEALTH_NOT_CONFIGURED, NO_SOURCE_REASON)
    from backend.doc_intel.crm_sync import secrets_action
    from backend.doc_intel.crypto import SecretBox, SecretsUnavailable

    try:
        box = deps.secret_box_factory() if deps.secret_box_factory is not None else SecretBox.from_settings(deps.settings)
    except SecretsUnavailable as ex:
        details = {
            "sources": [{"id": int(s["id"]), "name": s.get("name"), "ok": False, "reason": ex.reason} for s in sources]
        }
        return CheckResult("microsoft_graph", HEALTH_FAILED, ex.reason, suggested_action=secrets_action(ex), details=details)
    probes = await asyncio.gather(*(_probe_source(deps, box, s) for s in sources))
    entries = [entry for entry, _ in probes]
    latency = max((e["latency_ms"] or 0) for e in entries)
    details: dict[str, Any] = {"sources": entries[:_DETAIL_LIST_LIMIT]}
    failures = [(entry, action) for entry, action in probes if not entry["ok"]]
    if failures:
        entry, action = failures[0]
        reason = f"{entry['name'] or 'Source #' + str(entry['id'])}: {entry['reason']}"
        if len(failures) > 1:
            reason += f" (and {len(failures) - 1} more)"
        return CheckResult("microsoft_graph", HEALTH_FAILED, reason, suggested_action=action, latency_ms=latency, details=details)
    n = len(entries)
    return CheckResult(
        "microsoft_graph",
        HEALTH_HEALTHY,
        f"{n} SharePoint source{'s' if n != 1 else ''} reachable (sign-in and folder)",
        latency_ms=latency,
        details=details,
    )


async def _probe_source(deps: HealthDeps, box: Any, source: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """({id, name, ok, reason, latency_ms[, code]}, suggested action) of one source. Never raises;
    nothing returned contains a credential."""
    from backend.doc_intel.crm_sync import decrypt_credentials, secrets_action
    from backend.doc_intel.crypto import SecretsUnavailable
    from backend.doc_intel.graph_source import GraphFailed, GraphSource

    started = time.perf_counter()
    entry: dict[str, Any] = {"id": int(source["id"]), "name": source.get("name"), "ok": False, "reason": None, "latency_ms": None}
    action: str | None = None
    secret: str | None = None
    try:
        creds = decrypt_credentials(await deps.crm_repo.get_credentials(int(source["id"])), box)
        secret = creds.client_secret
        factory = deps.graph_factory or (lambda c: GraphSource(c, deps.settings))

        def probe() -> None:
            graph = factory(creds)
            try:
                graph.acquire_token()
                graph.resolve(source.get("site_url"), source.get("drive_name"), source.get("folder_path"))
            finally:
                try:
                    graph.close()
                except Exception:
                    pass

        # The doc-intel pool, not the default executor: a probe can outlive the check's timeout
        # while Graph throttles (Retry-After waits), and must never hold chat's threads (F26).
        await run_blocking(probe)
        entry["ok"] = True
    except SecretsUnavailable as ex:
        entry["reason"] = ex.reason
        action = secrets_action(ex)
    except GraphFailed as ex:
        entry["reason"] = scrub_secrets(ex.reason, secret)
        entry["code"] = ex.code
        action = scrub_secrets(ex.suggested_action, secret) if ex.suggested_action else None
    except Exception as ex:
        entry["reason"] = f"Check failed: {scrub_secrets(_error_text(ex), secret)}"
    entry["latency_ms"] = _ms(started)
    return entry, action


async def _check_crm(deps: HealthDeps) -> CheckResult:
    """The internal CRM store is queryable, and the latest runs had no persist-stage failures."""
    if deps.crm_repo is None:
        return await _check_phase2("crm")
    try:
        stats = await deps.crm_repo.store_stats()
    except Exception as ex:
        return CheckResult("crm", HEALTH_FAILED, f"CRM store unreachable: {_error_text(ex)}", suggested_action=CRM_STORE_ACTION)
    details: dict[str, Any] = dict(stats)
    if not stats.get("sources"):
        return CheckResult("crm", HEALTH_NOT_CONFIGURED, NO_SOURCE_REASON, details=details)
    live = {int(s["id"]) for s in await deps.crm_repo.list_sources()}  # a deleted source's history is not checked
    last = {source_id: run for source_id, run in (await deps.crm_repo.last_runs()).items() if source_id in live}
    persist = await deps.crm_repo.persist_failures([int(r["id"]) for r in last.values()])
    failing = [(run_id, info) for run_id, info in persist.items() if info.get("n")]
    if failing:
        n = sum(int(info["n"]) for _, info in failing)
        sample = _short(failing[0][1].get("reason"))
        details["persist_failures"] = [
            {"run_id": run_id, "files": int(info["n"]), "reason": _short(info.get("reason"))} for run_id, info in failing
        ][:_DETAIL_LIST_LIMIT]
        return CheckResult(
            "crm",
            HEALTH_FAILED,
            f"{n} file{'s' if n != 1 else ''} could not be stored in the CRM store in the latest sync"
            + (f": {sample}" if sample else ""),
            suggested_action="Check the Database component, then Retry the failed files on the Integrations page",
            details=details,
        )
    entities, files = int(stats.get("entities_active") or 0), int(stats.get("files_active") or 0)
    return CheckResult(
        "crm",
        HEALTH_HEALTHY,
        f"CRM store reachable · {entities} active entit{'ies' if entities != 1 else 'y'} "
        f"from {files} tracked file{'s' if files != 1 else ''}",
        details=details,
    )


_CHECKS: dict[str, Callable[[HealthDeps], Any]] = {
    "microsoft_graph": _check_microsoft_graph,
    "crm": _check_crm,
    "knowledge_sync": _check_knowledge_sync,
    "extraction": _check_extraction,
    "embedding": _check_embedding,
    "database": _check_database,
}


# ---- helpers -------------------------------------------------------------------------------


async def _probe(fn: Callable[[], Any], timeout: float) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        await asyncio.wait_for(fn(), timeout)
    except TimeoutError:
        return {"ok": False, "latency_ms": None, "error": f"timed out after {timeout:g} s"}
    except Exception as ex:
        return {"ok": False, "latency_ms": None, "error": _error_text(ex)}
    return {"ok": True, "latency_ms": _ms(start), "error": None}


async def _probe_models(deps: HealthDeps, base: str, key: str | None) -> dict[str, Any]:
    """``GET {base}/models`` with the resolved key: costs no tokens."""
    out: dict[str, Any] = {"ok": False, "status_code": None, "latency_ms": None, "error": None}
    if not key:
        out["error"] = "No API key is configured for the embedding endpoint"
        return out
    client = deps.http_client_factory() if deps.http_client_factory else httpx.AsyncClient()
    start = time.perf_counter()
    try:
        async with client:
            resp = await client.get(
                f"{base}/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=EMBEDDING_PROBE_TIMEOUT_SECONDS,
            )
    except httpx.TimeoutException:
        out["error"] = f"Embedding endpoint timed out after {EMBEDDING_PROBE_TIMEOUT_SECONDS:g} s"
        return out
    except Exception as ex:
        out["error"] = f"Embedding endpoint unreachable: {scrub_secrets(_error_text(ex), key)}"
        return out
    out["latency_ms"] = _ms(start)
    out["status_code"] = resp.status_code
    if resp.status_code == 200:
        out["ok"] = True
    elif resp.status_code in (401, 403):
        out["error"] = f"Embedding endpoint rejected the API key (HTTP {resp.status_code})"
    else:
        out["error"] = f"Embedding endpoint returned HTTP {resp.status_code}"
    return out


def _resolve_api_key(embedder_cfg: Any, embedding_settings: Any) -> tuple[str | None, str]:
    """(key, where it came from) resolved exactly like ``make_embedder``; the source names no secret."""
    key = embedder_cfg.api_key
    source = "inline api_key"
    if key is None and embedder_cfg.api_key_env:
        key = os.environ.get(embedder_cfg.api_key_env)
        source = f"env {embedder_cfg.api_key_env}"
    if key is None:
        key = getattr(embedding_settings, "default_openai_api_key", None)
        source = "DEFAULT_OPENAI_API_KEY"
    if not key:
        return None, f"none ({embedder_cfg.api_key_env or 'api_key'} not set)"
    return key, source


def _safe_url(url: str) -> str:
    """URL without credentials, query or fragment."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(invalid URL)"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def service_name(dsn: str | None) -> str | None:
    """The database service of a connect string (never user/password/host details)."""
    if not dsn:
        return None
    text = str(dsn).strip()
    m = re.search(r"SERVICE_NAME\s*=\s*([^)\s]+)", text, re.IGNORECASE) or re.search(
        r"\bSID\s*=\s*([^)\s]+)", text, re.IGNORECASE
    )
    if m:
        return m.group(1)
    if "(" in text:
        return None
    tail = text.rsplit("/", 1)[-1] if "/" in text else text
    tail = tail.split("?", 1)[0].split(":", 1)[0].strip()
    return tail or None


def _installed_languages(info: dict[str, Any]) -> list[str] | None:
    for key in (
        "ocr_languages_installed",
        "installed_ocr_languages",
        "installed_languages",
        "tesseract_languages",
        "languages_installed",
    ):
        value = info.get(key)
        if isinstance(value, (list, tuple, set)):
            return [str(v) for v in value]
    tess = info.get("tesseract")
    if isinstance(tess, dict):
        for key in ("languages", "installed_languages", "langs"):
            value = tess.get(key)
            if isinstance(value, (list, tuple, set)):
                return [str(v) for v in value]
    return None


def _tesseract_missing(info: dict[str, Any]) -> bool:
    if info.get("tesseract_available") is False:
        return True
    tess = info.get("tesseract")
    return isinstance(tess, dict) and tess.get("available") is False


def _component_from_row(row: dict[str, Any], label: str) -> HealthComponentOut:
    latency = row.get("latency_ms")
    return HealthComponentOut(
        key=str(row.get("component_key")),
        label=str(row.get("label") or label),
        status=_health_status(row.get("status")) or HEALTH_NOT_CONFIGURED,
        reason=row.get("reason"),
        suggested_action=row.get("suggested_action"),
        checked_at=iso_utc(row.get("checked_at")),
        last_success_at=iso_utc(row.get("last_success_at")),
        last_failure_at=iso_utc(row.get("last_failure_at")),
        consecutive_failures=int(row.get("consecutive_failures") or 0),
        latency_ms=int(latency) if latency is not None else None,
        details=loads_json(row.get("details_json"), {}),
    )


def _not_checked(key: str, label: str) -> HealthComponentOut:
    return HealthComponentOut(key=key, label=label, status=HEALTH_NOT_CONFIGURED, reason="Not checked yet")


def _overview_from_results(results: list[CheckResult], checked_at: Any) -> HealthOverviewOut:
    """Fallback when results could not be stored/reloaded (e.g. the app database is down)."""
    fresh = {r.key: r for r in results}
    stamp = iso_utc(checked_at)
    components: list[HealthComponentOut] = []
    for key, label in HEALTH_COMPONENTS:
        r = fresh.get(key)
        if r is None:
            components.append(_not_checked(key, label))
            continue
        components.append(
            HealthComponentOut(
                key=key,
                label=label,
                status=r.status,
                reason=r.reason,
                suggested_action=r.suggested_action,
                checked_at=stamp,
                last_success_at=stamp if r.status == HEALTH_HEALTHY else None,
                last_failure_at=stamp if r.status == HEALTH_FAILED else None,
                consecutive_failures=1 if r.status == HEALTH_FAILED else 0,
                latency_ms=r.latency_ms,
                details={**r.details, "stored": False},
            )
        )
    return HealthOverviewOut(overall=_overall(components), checked_at=stamp, stale=False, components=components)


def _unreadable_overview(ex: BaseException) -> HealthOverviewOut:
    reason = f"Could not read stored health results: {_error_text(ex)}"
    components = [
        HealthComponentOut(
            key=key,
            label=label,
            status=HEALTH_FAILED,
            reason=reason,
            suggested_action=suggested_action_for("database", reason)
            or "The database is unreachable: check the Oracle container/host",
        )
        if key == "database"
        else _not_checked(key, label)
        for key, label in HEALTH_COMPONENTS
    ]
    return HealthOverviewOut(overall=HEALTH_FAILED, checked_at=None, stale=True, components=components)


def _overall(components: list[HealthComponentOut]) -> str:
    return HEALTH_FAILED if any(c.status == HEALTH_FAILED for c in components) else HEALTH_HEALTHY


def _health_status(value: Any) -> str | None:
    text = str(value) if value is not None else None
    return text if text in (HEALTH_HEALTHY, HEALTH_FAILED, HEALTH_NOT_CONFIGURED) else None


def _document_activity(r: dict[str, Any]) -> ActivityItemOut:
    name = f'"{r.get("filename") or "document"}"'
    status = str(r.get("status") or "")
    if status == DOC_FAILED:
        stage = r.get("failed_stage") or "an unknown stage"
        reason = _short(r.get("error_message"))
        message = f"Import of {name} failed at {stage}" + (f": {reason}" if reason else "")
        level = "error"
    elif status == DOC_PUBLISHED:
        keys = parse_queue_keys(r.get("queue_keys"))
        chunks = r.get("chunk_count")
        message = f"Published {name}" + (f" to {', '.join(keys)}" if keys else "") + (
            f" ({int(chunks)} chunks)" if chunks is not None else ""
        )
        level = "info"
    elif status == DOC_PROCESSING:
        running = next((s for s in KB_STAGES if r.get(stage_column(s)) == "RUNNING"), None)
        message = f"Processing {name}" + (f" ({running})" if running else "")
        level = "info"
    elif status == DOC_UNPUBLISHED:
        message = f"Unpublished {name}"
        level = "warning"
    else:
        message = f"Queued {name} for import"
        level = "info"
    return ActivityItemOut(
        kind="kb_document", level=level, message=message, ref_id=int(r["id"]), occurred_at=iso_utc(r.get("updated_at"))
    )


def _flag(value: Any) -> bool:
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def _file_title(r: dict[str, Any]) -> str:
    name = str(r.get("name") or f"File #{r['id']}")
    source = r.get("source_name")
    return f"{name} ({source})" if source else name


def _crm_file_failure(r: dict[str, Any]) -> FailureItemOut:
    return FailureItemOut(
        kind="crm_file",
        id=int(r["id"]),
        title=_file_title(r),
        account_name=r.get("account_name"),
        stage=r.get("failed_stage"),
        reason=r.get("error_message"),
        occurred_at=iso_utc(r.get("processed_at") or r.get("updated_at")),
    )


def _sync_run_failure(r: dict[str, Any]) -> FailureItemOut:
    source = r.get("source_name") or f"source #{r.get('source_id')}"
    trigger = "scheduled" if r.get("trigger_type") == "SCHEDULED" else "manual"
    return FailureItemOut(
        kind="sync_run",
        id=int(r["id"]),
        title=f"Sync of {source} ({trigger})",
        account_name=r.get("account_name"),
        stage=None,
        reason=r.get("error_message"),
        occurred_at=iso_utc(r.get("finished_at") or r.get("updated_at")),
    )


def _crm_file_activity(r: dict[str, Any]) -> ActivityItemOut:
    name = f'"{r.get("name") or "file"}"' + (f" ({r['source_name']})" if r.get("source_name") else "")
    status = str(r.get("status") or "")
    if r.get("state") == "DELETED":
        message, level = f"{name} was deleted from SharePoint; its entities were withdrawn", "warning"
    elif status == "FAILED":
        reason = _short(r.get("error_message"))
        message = f"Processing of {name} failed at {r.get('failed_stage') or 'an unknown stage'}" + (f": {reason}" if reason else "")
        level = "error"
    elif status == "COMPLETED":
        count = r.get("entity_count")
        message = f"Processed {name}" + (f" ({int(count)} entit{'ies' if int(count) != 1 else 'y'})" if count is not None else "")
        level = "info"
    elif status == "PROCESSING":
        running = next((s for s in CRM_STAGES if r.get(f"{s}_status") == "RUNNING"), None)
        message, level = f"Processing {name}" + (f" ({running})" if running else ""), "info"
    elif status == "SKIPPED":
        message, level = f"Skipped {name}", "info"
    else:
        message, level = f"{name} is waiting to be processed", "info"
    return ActivityItemOut(kind="crm_file", level=level, message=message, ref_id=int(r["id"]), occurred_at=iso_utc(r.get("updated_at")))


def _sync_run_activity(r: dict[str, Any]) -> ActivityItemOut:
    source = f"'{r.get('source_name') or 'source #' + str(r.get('source_id'))}'"
    status = str(r.get("status") or "")
    counts = (
        f"{int(r.get('files_new') or 0)} new, {int(r.get('files_changed') or 0)} changed, "
        f"{int(r.get('files_deleted') or 0)} deleted, {int(r.get('files_failed') or 0)} failed"
    )
    reason = _short(r.get("error_message"))
    if status == RUN_FAILED:
        message, level = f"Sync of {source} failed" + (f": {reason}" if reason else ""), "error"
    elif status == RUN_PARTIAL:
        message, level = f"Sync of {source} finished with problems ({counts})" + (f": {reason}" if reason else ""), "warning"
    elif status == RUN_COMPLETED:
        message, level = f"Synced {source} ({counts})", "info"
    elif status == RUN_RUNNING:
        message, level = f"Syncing {source}", "info"
    elif status == RUN_QUEUED:
        trigger = "scheduled" if r.get("trigger_type") == "SCHEDULED" else "manual"
        message, level = f"Sync of {source} queued ({trigger})", "info"
    else:
        message, level = f"Sync of {source}: {status}", "info"
    return ActivityItemOut(kind="sync_run", level=level, message=message, ref_id=int(r["id"]), occurred_at=iso_utc(r.get("updated_at")))


def _short(text: Any, limit: int = 300) -> str | None:
    if not text:
        return None
    value = str(text).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _error_text(ex: BaseException) -> str:
    if isinstance(ex, TimeoutError):
        return "timed out"
    text = str(ex).strip()
    first = text.splitlines()[0] if text else type(ex).__name__
    return scrub_secrets(first)[:300]


def _ms(start: float) -> int:
    return int(round((time.perf_counter() - start) * 1000))
