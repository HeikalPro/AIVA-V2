from __future__ import annotations

from typing import Any, Sequence

from embedding_service.adapters.base import ChunkDraft
from embedding_service.db import repo as _repo


class CorpusRepository:
    """Namespace for KB persistence operations (delegates to ``db.repo``)."""

    def insert_corpus(self, conn, name: str, slug: str, config: dict[str, Any]) -> bytes:
        return _repo.insert_corpus(conn, name, slug, config)

    def get_corpus_by_id(self, conn, corpus_id: bytes) -> _repo.CorpusRow | None:
        return _repo.get_corpus_by_id(conn, corpus_id)

    def list_corpora(self, conn) -> list[_repo.CorpusRow]:
        return _repo.list_corpora(conn)

    def get_corpus_by_slug(self, conn, slug: str) -> _repo.CorpusRow | None:
        return _repo.get_corpus_by_slug(conn, slug)

    def update_corpus(self, conn, corpus_id: bytes, *, name: str | None, config: dict[str, Any] | None) -> None:
        return _repo.update_corpus(conn, corpus_id, name=name, config=config)

    def merge_chunk_row(
        self,
        conn,
        *,
        corpus_id: bytes,
        chunker_version: str,
        draft: ChunkDraft,
    ) -> bytes:
        return _repo.merge_chunk_row(conn, corpus_id=corpus_id, chunker_version=chunker_version, draft=draft)

    def fetch_chunks_missing_embedding(self, conn, corpus_id: bytes, limit: int) -> list[tuple[bytes, str]]:
        return _repo.fetch_chunks_missing_embedding(conn, corpus_id, limit)

    def count_chunks_missing_embedding(self, conn, corpus_id: bytes) -> int:
        return _repo.count_chunks_missing_embedding(conn, corpus_id)

    def update_chunk_embedding(
        self,
        conn,
        *,
        chunk_id: bytes,
        vector: Sequence[float],
        model: str,
        version: str,
        dimension: int,
    ) -> None:
        return _repo.update_chunk_embedding(
            conn, chunk_id=chunk_id, vector=vector, model=model, version=version, dimension=dimension
        )

    def delete_chunks_for_corpus(self, conn, corpus_id: bytes) -> int:
        return _repo.delete_chunks_for_corpus(conn, corpus_id)

    def insert_job(self, conn, job_id: bytes, corpus_id: bytes, job_type: str, status: str) -> None:
        return _repo.insert_job(conn, job_id, corpus_id, job_type, status)

    def update_job(
        self,
        conn,
        job_id: bytes,
        *,
        status: str | None = None,
        stats: dict[str, Any] | None = None,
        error_msg: str | None = None,
    ) -> None:
        return _repo.update_job(conn, job_id, status=status, stats=stats, error_msg=error_msg)

    def get_job(self, conn, job_id: bytes) -> dict[str, Any] | None:
        return _repo.get_job(conn, job_id)

    def search_similar(
        self,
        conn,
        *,
        corpus_id: bytes,
        query_vector: Sequence[float],
        top_k: int,
        metric: str,
        vertical: str | None,
        verticals: Sequence[str] | None = None,
        interaction_type: str | None,
        issue_type: str | None,
        escalation: str | None,
    ) -> list[dict[str, Any]]:
        return _repo.search_similar(
            conn,
            corpus_id=corpus_id,
            query_vector=query_vector,
            top_k=top_k,
            metric=metric,
            vertical=vertical,
            verticals=verticals,
            interaction_type=interaction_type,
            issue_type=issue_type,
            escalation=escalation,
        )
