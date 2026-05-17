from __future__ import annotations

import logging
from typing import Any

from embedding_service.config import Settings, get_settings
from embedding_service.db import repo as _repo
from embedding_service.db.manager import DatabaseManager
from embedding_service.db.repository import CorpusRepository
from embedding_service.models.corpus_config import default_corpus_config, parse_corpus_config
from embedding_service.services.embedder_factory import make_embedder
from embedding_service.services.pipeline import lines_from_ingest_request, run_ingest_job, run_reindex_job
from embedding_service.util.uuids import bytes_to_hex, hex_to_bytes, new_uuid_bytes
from embedding_service.workers.redis_queue import push_json_job

_log = logging.getLogger(__name__)


class EmbeddingService:
    """In-process KB: corpora, ingest/reindex, vector search, job status."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        db: DatabaseManager | None = None,
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        self._db = db if db is not None else DatabaseManager(self.settings)
        self._owns_db = db is None
        self._repo = CorpusRepository()
        if self._owns_db:
            self._db.init_pool()

    @property
    def db(self) -> DatabaseManager:
        return self._db

    def close(self) -> None:
        if self._owns_db:
            self._db.close_pool()

    def _corpus_dict(self, row: _repo.CorpusRow) -> dict[str, Any]:
        return {
            "corpus_id": bytes_to_hex(row.corpus_id),
            "name": row.name,
            "slug": row.slug,
            "config": row.config,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def list_corpora(self) -> list[dict[str, Any]]:
        with self._db.connection() as conn:
            rows = self._repo.list_corpora(conn)
        return [self._corpus_dict(r) for r in rows]

    def get_corpus(self, corpus_id: str) -> dict[str, Any] | None:
        try:
            cid = hex_to_bytes(corpus_id)
        except ValueError:
            return None
        with self._db.connection() as conn:
            row = self._repo.get_corpus_by_id(conn, cid)
        return self._corpus_dict(row) if row else None

    def create_corpus(self, name: str, slug: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg_dict = config if config is not None else default_corpus_config()
        parse_corpus_config(cfg_dict)
        with self._db.connection() as conn:
            existing = self._repo.get_corpus_by_slug(conn, slug)
            if existing:
                raise ValueError("Slug already exists")
            cid = self._repo.insert_corpus(conn, name, slug, cfg_dict)
            row = self._repo.get_corpus_by_id(conn, cid)
        assert row is not None
        return self._corpus_dict(row)

    def patch_corpus(
        self,
        corpus_id: str,
        *,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            cid = hex_to_bytes(corpus_id)
        except ValueError as ex:
            raise ValueError("Invalid corpus_id") from ex
        with self._db.connection() as conn:
            row = self._repo.get_corpus_by_id(conn, cid)
            if not row:
                raise LookupError("Corpus not found")
            cfg_update = None
            if config is not None:
                merged = {**row.config, **config}
                parse_corpus_config(merged)
                cfg_update = merged
            self._repo.update_corpus(conn, cid, name=name, config=cfg_update)
            row2 = self._repo.get_corpus_by_id(conn, cid)
        assert row2 is not None
        return self._corpus_dict(row2)

    def search(
        self,
        corpus_id: str,
        query: str,
        *,
        top_k: int | None = None,
        vertical: str | None = None,
        interaction_type: str | None = None,
        issue_type: str | None = None,
        escalation: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            cid = hex_to_bytes(corpus_id)
        except ValueError as ex:
            raise ValueError("Invalid corpus_id") from ex
        tk = top_k if top_k is not None else self.settings.search_default_top_k
        tk = min(tk, self.settings.search_max_top_k)

        with self._db.connection() as conn:
            row = self._repo.get_corpus_by_id(conn, cid)
            if not row:
                raise LookupError("Corpus not found")
            cfg = parse_corpus_config(row.config)
            embedder = make_embedder(cfg, self.settings)
            qvec = embedder.embed([query], conn).vectors[0]
            return self._repo.search_similar(
                conn,
                corpus_id=cid,
                query_vector=qvec,
                top_k=tk,
                metric=cfg.distance_metric,
                vertical=vertical,
                interaction_type=interaction_type,
                issue_type=issue_type,
                escalation=escalation,
            )

    def get_job(self, job_id: str) -> dict[str, Any]:
        try:
            jid = hex_to_bytes(job_id.replace("-", ""))
        except ValueError as ex:
            raise ValueError("Invalid job_id") from ex
        with self._db.connection() as conn:
            row = self._repo.get_job(conn, jid)
        if not row:
            raise LookupError("Job not found")
        return row

    def ingest(
        self,
        corpus_id: str,
        *,
        lines: list[str] | None = None,
        records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        try:
            cid = hex_to_bytes(corpus_id)
        except ValueError as ex:
            raise ValueError("Invalid corpus_id") from ex
        data_lines = lines_from_ingest_request(lines, records)

        with self._db.connection() as conn:
            row = self._repo.get_corpus_by_id(conn, cid)
            if not row:
                raise LookupError("Corpus not found")
            jid = new_uuid_bytes()
            self._repo.insert_job(conn, jid, cid, "INGEST", "QUEUED")
        job_hex = bytes_to_hex(jid)

        payload = {
            "kind": "ingest",
            "corpus_id_hex": corpus_id,
            "job_id_hex": job_hex,
            "lines": data_lines,
        }

        if self.settings.redis_url:
            push_json_job(self.settings.redis_url, self.settings.redis_job_queue, payload)
            return {"job_id": job_hex, "mode": "queued"}

        self._run_ingest_inline(corpus_id, job_hex, data_lines, "ingest")
        return {"job_id": job_hex, "mode": "inline"}

    def reindex(
        self,
        corpus_id: str,
        *,
        lines: list[str] | None = None,
        records: list[dict[str, Any]] | None = None,
        config_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            cid = hex_to_bytes(corpus_id)
        except ValueError as ex:
            raise ValueError("Invalid corpus_id") from ex
        data_lines = lines_from_ingest_request(lines, records)

        with self._db.connection() as conn:
            row = self._repo.get_corpus_by_id(conn, cid)
            if not row:
                raise LookupError("Corpus not found")
            if config_patch:
                merged = {**row.config, **config_patch}
                parse_corpus_config(merged)
                self._repo.update_corpus(conn, cid, name=None, config=merged)
            jid = new_uuid_bytes()
            self._repo.insert_job(conn, jid, cid, "REINDEX", "QUEUED")
        job_hex = bytes_to_hex(jid)

        payload = {
            "kind": "reindex",
            "corpus_id_hex": corpus_id,
            "job_id_hex": job_hex,
            "lines": data_lines,
        }

        if self.settings.redis_url:
            push_json_job(self.settings.redis_url, self.settings.redis_job_queue, payload)
            return {"job_id": job_hex, "mode": "queued"}

        self._run_ingest_inline(corpus_id, job_hex, data_lines, "reindex")
        return {"job_id": job_hex, "mode": "inline"}

    def _run_ingest_inline(self, corpus_id_hex: str, job_id_hex: str, lines: list[str], kind: str) -> None:
        self._db.init_pool()

        cid = hex_to_bytes(corpus_id_hex.replace("-", ""))
        jid = hex_to_bytes(job_id_hex.replace("-", ""))
        with self._db.connection() as conn:
            row = self._repo.get_corpus_by_id(conn, cid)
        if not row:
            _log.error("Corpus %s missing in background job", corpus_id_hex)
            return
        cfg = parse_corpus_config(row.config)
        try:
            with self._db.connection() as conn:
                self._repo.update_job(conn, jid, status="RUNNING")
                if kind == "reindex":
                    run_reindex_job(
                        conn,
                        corpus_id=cid,
                        job_id=jid,
                        lines=lines,
                        cfg=cfg,
                        settings=self.settings,
                    )
                else:
                    run_ingest_job(
                        conn,
                        corpus_id=cid,
                        job_id=jid,
                        lines=lines,
                        cfg=cfg,
                        settings=self.settings,
                    )
        except Exception as ex:
            _log.exception("Job %s failed", job_id_hex)
            with self._db.connection() as conn:
                self._repo.update_job(conn, jid, status="FAILED", error_msg=str(ex)[:4000])

    def process_job_payload(self, payload: dict[str, Any]) -> None:
        """Execute one ingest/reindex job from a Redis queue payload."""
        kind = payload.get("kind")
        corpus_hex = payload.get("corpus_id_hex")
        job_hex = payload.get("job_id_hex")
        lines = payload.get("lines") or []
        if not corpus_hex or not job_hex or kind not in ("ingest", "reindex"):
            _log.error("Invalid job payload keys")
            return
        cid = hex_to_bytes(str(corpus_hex).replace("-", ""))
        jid = hex_to_bytes(str(job_hex).replace("-", ""))
        try:
            with self._db.connection() as conn:
                row = self._repo.get_corpus_by_id(conn, cid)
                if not row:
                    self._repo.update_job(conn, jid, status="FAILED", error_msg="Corpus not found")
                    return
                cfg = parse_corpus_config(row.config)
                self._repo.update_job(conn, jid, status="RUNNING")
                if kind == "reindex":
                    run_reindex_job(
                        conn,
                        corpus_id=cid,
                        job_id=jid,
                        lines=lines,
                        cfg=cfg,
                        settings=self.settings,
                    )
                else:
                    run_ingest_job(
                        conn,
                        corpus_id=cid,
                        job_id=jid,
                        lines=lines,
                        cfg=cfg,
                        settings=self.settings,
                    )
        except Exception as ex:
            _log.exception("Job failed")
            try:
                with self._db.connection() as conn:
                    self._repo.update_job(conn, jid, status="FAILED", error_msg=str(ex)[:4000])
            except Exception:
                _log.exception("Could not persist FAILED job status")
