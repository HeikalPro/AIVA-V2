from __future__ import annotations

import json
import logging
import sys
import time
from typing import Callable

from tqdm import tqdm

from embedding_service.adapters.generic_jsonl import adapt_generic_jsonl_line
from embedding_service.adapters.halan_records import adapt_halan_records_line
from embedding_service.config import Settings
from embedding_service.db import repo
from embedding_service.models.corpus_config import CorpusConfig
from embedding_service.services.embedder_factory import make_embedder
from embedding_service.util.uuids import bytes_to_hex

_log = logging.getLogger(__name__)

# Remote Oracle + one huge transaction = long freezes and held locks; commit in chunks.
_PARSE_COMMIT_EVERY_N_LINES = 50


def _flush_logging_handlers() -> None:
    """Force handlers to write so uvicorn/tqdm interleaving still shows trace lines."""
    seen: set[int] = set()
    for lg in (logging.root, _log):
        for h in getattr(lg, "handlers", []) or []:
            hid = id(h)
            if hid in seen:
                continue
            seen.add(hid)
            try:
                h.flush()
            except Exception:
                pass


def _preview_text(s: str, max_chars: int = 160) -> str:
    one = s.replace("\r", "\\r").replace("\n", "\\n")
    if len(one) <= max_chars:
        return one
    return one[:max_chars] + "…"


def _ingest_progress_enabled() -> bool:
    return sys.stderr.isatty()


def _adapt_line(line: str, line_no: int, cfg: CorpusConfig) -> list:
    if cfg.adapter == "halan_records_v1":
        return adapt_halan_records_line(line, line_no, cfg)
    if cfg.adapter == "generic_jsonl_v1":
        return adapt_generic_jsonl_line(line, line_no, cfg)
    raise ValueError(f"Unknown adapter {cfg.adapter}")


def _maybe_commit_parse_phase(conn, line_no: int, *, verbose: bool) -> None:
    if line_no > 0 and line_no % _PARSE_COMMIT_EVERY_N_LINES == 0:
        t0 = time.perf_counter()
        conn.commit()
        if verbose:
            _log.info(
                "ingest trace: committed parse checkpoint after line %s in %.4fs",
                line_no,
                time.perf_counter() - t0,
            )
            _flush_logging_handlers()


def run_ingest_job(
    conn,
    *,
    corpus_id: bytes,
    job_id: bytes,
    lines: list[str],
    cfg: CorpusConfig,
    settings: Settings,
    progress_cb: Callable[[dict], None] | None = None,
) -> dict:
    """Parse lines, MERGE chunks, embed NULL embeddings, update job row."""
    verbose = settings.ingest_verbose_log
    chunker_ver = cfg.chunker_version
    if verbose:
        _log.info(
            "ingest trace: start job_id=%s corpus_id=%s lines=%s adapter=%s chunker_ver=%s",
            bytes_to_hex(job_id),
            bytes_to_hex(corpus_id),
            len(lines),
            cfg.adapter,
            chunker_ver,
        )
        _flush_logging_handlers()

    t_job0 = time.perf_counter()
    stats: dict = {
        "lines_submitted": len(lines),
        "lines_parsed": 0,
        "chunks_merged": 0,
        "vectors_written": 0,
    }
    line_no = 0
    n_in = len(lines)
    parse_pbar = None
    if _ingest_progress_enabled() and n_in > 0:
        parse_pbar = tqdm(
            total=n_in,
            desc="ingest parse",
            unit="line",
            file=sys.stderr,
            leave=True,
            mininterval=0.15,
        )
    try:
        for raw in lines:
            line_no += 1
            if parse_pbar is not None:
                parse_pbar.set_postfix_str(f"#{line_no}", refresh=True)
            raw = raw.strip()
            if not raw:
                if verbose:
                    _log.info("ingest trace: line %s/%s (blank skip)", line_no, n_in)
                    _flush_logging_handlers()
                if parse_pbar is not None:
                    parse_pbar.update(1)
                _maybe_commit_parse_phase(conn, line_no, verbose=verbose)
                continue
            if verbose:
                _log.info(
                    "ingest trace: line %s/%s begin len=%s preview=%r",
                    line_no,
                    n_in,
                    len(raw),
                    _preview_text(raw),
                )
                _flush_logging_handlers()
            t_adapt0 = time.perf_counter()
            try:
                drafts = _adapt_line(raw, line_no, cfg)
            except Exception as ex:
                _log.exception("Line %s failed", line_no)
                raise RuntimeError(f"Line {line_no}: {ex}") from ex
            t_adapt1 = time.perf_counter()
            if verbose:
                _log.info(
                    "ingest trace: line %s/%s adapt ok drafts=%s in %.4fs",
                    line_no,
                    n_in,
                    len(drafts),
                    t_adapt1 - t_adapt0,
                )
                _flush_logging_handlers()
            for mi, d in enumerate(drafts):
                if verbose:
                    parent = (d.external_parent_id or "")[:48]
                    _log.info(
                        "ingest trace: line %s oracle MERGE starting %s/%s parent=%r chunk_index=%s text_len=%s",
                        line_no,
                        mi + 1,
                        len(drafts),
                        parent,
                        d.chunk_index,
                        len(d.text or ""),
                    )
                    _flush_logging_handlers()
                t_m0 = time.perf_counter()
                repo.merge_chunk_row(
                    conn,
                    corpus_id=corpus_id,
                    chunker_version=chunker_ver,
                    draft=d,
                )
                dt_m = time.perf_counter() - t_m0
                stats["chunks_merged"] += 1
                if verbose:
                    parent = (d.external_parent_id or "")[:48]
                    _log.info(
                        "ingest trace: line %s merge %s/%s parent=%r chunk_index=%s dt=%.4fs",
                        line_no,
                        mi + 1,
                        len(drafts),
                        parent,
                        d.chunk_index,
                        dt_m,
                    )
                    _flush_logging_handlers()
            stats["lines_parsed"] += 1
            if verbose:
                _log.info(
                    "ingest trace: line %s/%s done merged=%s rows",
                    line_no,
                    n_in,
                    len(drafts),
                )
                _flush_logging_handlers()
            if progress_cb and line_no % 50 == 0:
                progress_cb(stats.copy())
            if parse_pbar is not None:
                parse_pbar.update(1)
            _maybe_commit_parse_phase(conn, line_no, verbose=verbose)
    finally:
        if parse_pbar is not None:
            parse_pbar.close()

    t_parse_done = time.perf_counter()
    stats["parse_seconds"] = round(t_parse_done - t_job0, 4)

    if verbose:
        t0 = time.perf_counter()
    conn.commit()
    if verbose:
        _log.info("ingest trace: post-parse commit in %.4fs", time.perf_counter() - t0)
        _flush_logging_handlers()
    repo.update_job(conn, job_id, status="EMBEDDING", stats=stats)
    if verbose:
        t0 = time.perf_counter()
    conn.commit()
    if verbose:
        _log.info("ingest trace: EMBEDDING status commit in %.4fs", time.perf_counter() - t0)
        _flush_logging_handlers()

    embedder = make_embedder(cfg, settings)
    model_label = cfg.embedder.model
    ev = "1"
    dim = cfg.embedder.dimension

    total_vec = 0
    tokens_api_sum: int | None = None
    tokens_est_sum = 0
    embed_batches = 0
    t_embed0 = time.perf_counter()
    pending_embed = repo.count_chunks_missing_embedding(conn, corpus_id)
    if verbose:
        _log.info("ingest trace: embed phase pending_chunks=%s batch_size=%s", pending_embed, settings.ingest_batch_embed_size)
        _flush_logging_handlers()
    embed_pbar = None
    if _ingest_progress_enabled() and pending_embed > 0:
        embed_pbar = tqdm(
            total=pending_embed,
            desc="ingest embed",
            unit="chunk",
            file=sys.stderr,
            leave=True,
        )
    try:
        while True:
            batch = repo.fetch_chunks_missing_embedding(
                conn, corpus_id, settings.ingest_batch_embed_size
            )
            if not batch:
                break
            texts = [t for _, t in batch]
            embed_batches += 1
            if embed_pbar is not None:
                embed_pbar.set_postfix_str(
                    f"batch {embed_batches} x{len(batch)}",
                    refresh=True,
                )
            if verbose:
                _log.info(
                    "ingest trace: embed batch %s fetch_ok n=%s calling HTTP embedder…",
                    embed_batches,
                    len(batch),
                )
                _flush_logging_handlers()
            t_http0 = time.perf_counter()
            batch_res = embedder.embed(texts, conn)
            t_http1 = time.perf_counter()
            if verbose:
                _log.info(
                    "ingest trace: embed batch %s HTTP ok in %.4fs (vectors=%s)",
                    embed_batches,
                    t_http1 - t_http0,
                    len(batch),
                )
                _flush_logging_handlers()
            vectors = batch_res.vectors
            if len(vectors) != len(batch):
                raise RuntimeError("Embedder returned wrong batch size")
            tokens_est_sum += batch_res.estimated_tokens
            if batch_res.api_total_tokens is not None:
                tokens_api_sum = (tokens_api_sum or 0) + batch_res.api_total_tokens
            for (cid, _), vec in zip(batch, vectors):
                if len(vec) != dim:
                    _log.warning("Vector dim %s != config %s", len(vec), dim)
                repo.update_chunk_embedding(
                    conn,
                    chunk_id=cid,
                    vector=vec,
                    model=model_label,
                    version=ev,
                    dimension=dim,
                )
                total_vec += 1
                if embed_pbar is not None:
                    embed_pbar.update(1)
            stats["vectors_written"] = total_vec
            if progress_cb:
                progress_cb(stats.copy())
            if verbose:
                t0 = time.perf_counter()
            conn.commit()
            if verbose:
                _log.info(
                    "ingest trace: embed batch %s DB commit in %.4fs (total_vec=%s)",
                    embed_batches,
                    time.perf_counter() - t0,
                    total_vec,
                )
                _flush_logging_handlers()
    finally:
        if embed_pbar is not None:
            embed_pbar.close()

    t_embed_done = time.perf_counter()
    stats["vectors_written"] = total_vec
    stats["embed_seconds"] = round(t_embed_done - t_embed0, 4)
    stats["total_seconds"] = round(t_embed_done - t_job0, 4)
    stats["embedding_model"] = model_label
    stats["embedding_batches"] = embed_batches
    stats["tokens_estimated"] = int(tokens_est_sum)
    if tokens_api_sum is not None:
        stats["tokens_from_api"] = int(tokens_api_sum)
        tokens_for_cost = int(tokens_api_sum)
    else:
        stats["tokens_from_api"] = None
        tokens_for_cost = int(tokens_est_sum)
    stats["tokens_used_for_cost"] = int(tokens_for_cost)

    price = cfg.embedder.pricing_usd_per_million_tokens
    if price is None:
        price = settings.embedding_default_usd_per_million_tokens
    stats["pricing_usd_per_million_tokens"] = price
    if price is None:
        stats["cost_usd"] = None
    else:
        stats["cost_usd"] = round((tokens_for_cost / 1_000_000) * float(price), 6)

    repo.update_job(conn, job_id, status="COMPLETED", stats=stats)

    _log.info(
        "corpus_embed_done corpus_id=%s job_id=%s model=%s lines_submitted=%s lines_parsed=%s "
        "chunks=%s vectors=%s parse_s=%s embed_s=%s total_s=%s batches=%s tokens_api=%s "
        "tokens_est=%s tokens_cost=%s price_per_1m_usd=%s cost_usd=%s",
        bytes_to_hex(corpus_id),
        bytes_to_hex(job_id),
        model_label,
        stats.get("lines_submitted"),
        stats["lines_parsed"],
        stats["chunks_merged"],
        stats["vectors_written"],
        stats.get("parse_seconds"),
        stats.get("embed_seconds"),
        stats.get("total_seconds"),
        embed_batches,
        stats.get("tokens_from_api"),
        stats.get("tokens_estimated"),
        stats.get("tokens_used_for_cost"),
        price,
        stats.get("cost_usd"),
    )
    return stats


def run_reindex_job(
    conn,
    *,
    corpus_id: bytes,
    job_id: bytes,
    lines: list[str],
    cfg: CorpusConfig,
    settings: Settings,
) -> dict:
    repo.delete_chunks_for_corpus(conn, corpus_id)
    return run_ingest_job(
        conn,
        corpus_id=corpus_id,
        job_id=job_id,
        lines=lines,
        cfg=cfg,
        settings=settings,
    )


def lines_from_ingest_request(
    lines: list[str] | None,
    records: list[dict] | None,
) -> list[str]:
    if lines is not None and len(lines) > 0:
        return [str(x) for x in lines]
    if records is not None and len(records) > 0:
        return [json.dumps(r, ensure_ascii=False) for r in records]
    raise ValueError("Provide non-empty lines or records")
