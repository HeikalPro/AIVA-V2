"""Update kb_chunk by external_parent_id and re-embed."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.config import get_settings
from embedding_service.db import repo
from embedding_service.db.manager import DatabaseManager
from embedding_service.models.corpus_config import CorpusConfig
from embedding_service.services.embedder_factory import make_embedder
from embedding_service.util.uuids import bytes_to_hex, hex_to_bytes

CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-id", required=True)
    parser.add_argument("--chunk-file", type=Path, required=True)
    args = parser.parse_args()

    chunk_text = args.chunk_file.read_text(encoding="utf-8")
    corpus_id = hex_to_bytes(CORPUS_HEX)
    settings = get_settings()
    dbm = DatabaseManager(settings)
    dbm.init_pool()

    with dbm.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chunk_id
                FROM kb_chunk
                WHERE corpus_id = :cid
                  AND external_parent_id = :pid
                  AND chunk_index = 0
                """,
                cid=corpus_id,
                pid=args.parent_id,
            )
            row = cur.fetchone()
            if not row:
                raise SystemExit(f"No chunk for external_parent_id={args.parent_id}")

        first_line = chunk_text.split("\n", 1)[0]
        parts = {p.split(": ", 1)[0].strip(): p.split(": ", 1)[1].strip() for p in first_line.split(" | ")}
        payload = {
            "vertical": parts.get("Vertical", ""),
            "interaction_type": parts.get("Type", ""),
            "issue_type": parts.get("Issue", ""),
            "escalation": parts.get("Escalation", ""),
            "queue": "Halan",
            "answer_status": "ok",
            "canonical_id": int(args.parent_id),
        }
        content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
        payload_json = json.dumps(payload, ensure_ascii=False)

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE kb_chunk
                SET chunk_text = :text,
                    payload_json = CAST(:payload AS JSON),
                    content_hash = :hash,
                    embedding = NULL,
                    embedding_model = NULL,
                    embedding_version = NULL,
                    updated_at = SYSTIMESTAMP
                WHERE corpus_id = :cid AND external_parent_id = :pid
                """,
                text=chunk_text,
                payload=payload_json,
                hash=content_hash,
                cid=corpus_id,
                pid=args.parent_id,
            )
            print(f"Updated {cur.rowcount} row(s) for parent_id={args.parent_id}")
        conn.commit()

        corpus_row = repo.get_corpus_by_id(conn, corpus_id)
        cfg = CorpusConfig.model_validate(corpus_row.config)
        embedder = make_embedder(cfg, settings)
        batch = repo.fetch_chunks_missing_embedding(conn, corpus_id, limit=10)
        if batch:
            batch_res = embedder.embed([t for _, t in batch], conn)
            for (chunk_id, _), vec in zip(batch, batch_res.vectors):
                repo.update_chunk_embedding(
                    conn,
                    chunk_id=chunk_id,
                    vector=vec,
                    model=cfg.embedder.model,
                    version="1",
                    dimension=cfg.embedder.dimension,
                )
                print(f"Embedded chunk_id={bytes_to_hex(chunk_id)}")
            conn.commit()
        print("Done.")

    dbm.close_pool()


if __name__ == "__main__":
    main()
