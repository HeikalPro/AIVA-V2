"""Apply batch2 scripts to skipped kb_chunk rows with matching questions."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
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

BATCH1_IDS = [
    "1259", "525", "546", "550", "559", "491", "1637", "1581",
    "1258", "862", "699", "1005", "1083",
]

_QUESTION_EN_RE = re.compile(r"^Question EN:\s*(.+)$", re.MULTILINE)
_QUESTION_AR_RE = re.compile(r"^Question AR:\s*(.+)$", re.MULTILINE)


def _norm(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.strip())


def _read_lob(v) -> str:
    if v is None:
        return ""
    return v.read() if hasattr(v, "read") else str(v)


def _is_skipped(chunk_text: str, answer_status: str | None) -> bool:
    return answer_status == "skipped" or "answer.status=skipped" in chunk_text


def _build_chunk_text(item: dict) -> str:
    lines = [
        f"Vertical: {item['vertical']} | Type: {item['type']} | Issue: {item['issue']} | Escalation: {item['escalation']}",
        "",
        f"Question EN: {item['q_en']}",
    ]
    if item.get("q_ar"):
        lines.append(f"Question AR: {item['q_ar']}")
    lines.extend(["", "Script:", item["script"]])
    return "\n".join(lines)


def _extract_questions(chunk_text: str) -> tuple[str | None, str | None]:
    ar = _QUESTION_AR_RE.search(chunk_text)
    en = _QUESTION_EN_RE.search(chunk_text)
    return (
        en.group(1).strip() if en else None,
        ar.group(1).strip() if ar else None,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    batch2 = json.loads((Path(__file__).parent / "batch2_scripts.json").read_text(encoding="utf-8"))
    by_ar = {_norm(x["q_ar"]): x for x in batch2 if x.get("q_ar")}
    by_en = {_norm(x["q_en"]): x for x in batch2 if x.get("q_en")}

    corpus_id = hex_to_bytes(CORPUS_HEX)
    settings = get_settings()
    dbm = DatabaseManager(settings)
    dbm.init_pool()

    lines: list[str] = ["=== BATCH 1 vs BATCH 2 ===", ""]
    batch1_hits: dict[str, dict] = {}

    with dbm.connection() as conn:
        skipped_rows: list[tuple[str, bytes, str, str | None, str]] = []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TO_CHAR(external_parent_id), chunk_id,
                       JSON_VALUE(payload_json, '$.answer_status'),
                       DBMS_LOB.SUBSTR(chunk_text, 4000, 1)
                FROM kb_chunk
                WHERE corpus_id = :cid AND chunk_index = 0
                  AND (
                    JSON_VALUE(payload_json, '$.answer_status') = 'skipped'
                    OR DBMS_LOB.INSTR(chunk_text, 'answer.status=skipped') > 0
                  )
                ORDER BY external_parent_id
                """,
                cid=corpus_id,
            )
            for pid, chunk_id, st, text_lob in cur.fetchall():
                text = _read_lob(text_lob)
                skipped_rows.append((pid, chunk_id, text, st, pid))

        for pid, _cid, text, _st, _ in skipped_rows:
            if pid not in BATCH1_IDS:
                continue
            q_en, q_ar = _extract_questions(text)
            item = None
            if q_ar and _norm(q_ar) in by_ar:
                item = by_ar[_norm(q_ar)]
            elif q_en and _norm(q_en) in by_en:
                item = by_en[_norm(q_en)]
            if item:
                batch1_hits[pid] = item
                lines.append(f"{pid}: FILL from batch2 #{item['n']} (exact question)")
            else:
                lines.append(f"{pid}: no exact batch2 script")

        lines.extend(["", "=== ALL SKIPPED MATCHED BY BATCH 2 ===", ""])
        updates: list[tuple[str, bytes, dict]] = []

        for pid, chunk_id, text, st, _ in skipped_rows:
            q_en, q_ar = _extract_questions(text)
            item = None
            if q_ar and _norm(q_ar) in by_ar:
                item = by_ar[_norm(q_ar)]
            elif q_en and _norm(q_en) in by_en:
                item = by_en[_norm(q_en)]
            if not item:
                continue
            updates.append((pid, chunk_id, item))
            lines.append(f"{pid} <- batch2 #{item['n']}: {item['q_en'][:80]}")

        lines.append(f"\nTotal updates: {len(updates)}")

        if not args.dry_run and updates:
            for pid, chunk_id, item in updates:
                chunk_text = _build_chunk_text(item)
                payload = {
                    "vertical": item["vertical"],
                    "interaction_type": item["type"],
                    "issue_type": item["issue"],
                    "escalation": item["escalation"],
                    "queue": "Halan",
                    "answer_status": "ok",
                    "canonical_id": int(pid),
                }
                payload_json = json.dumps(payload, ensure_ascii=False)
                content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
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
                        WHERE chunk_id = :cid
                        """,
                        text=chunk_text,
                        payload=payload_json,
                        hash=content_hash,
                        cid=chunk_id,
                    )
                lines.append(f"UPDATED {pid}")
            conn.commit()

            corpus_row = repo.get_corpus_by_id(conn, corpus_id)
            cfg = CorpusConfig.model_validate(corpus_row.config)
            embedder = make_embedder(cfg, settings)
            remaining = len(updates)
            while remaining > 0:
                batch = repo.fetch_chunks_missing_embedding(conn, corpus_id, limit=20)
                if not batch:
                    break
                batch_res = embedder.embed([t for _, t in batch], conn)
                for (cid, _), vec in zip(batch, batch_res.vectors):
                    repo.update_chunk_embedding(
                        conn,
                        chunk_id=cid,
                        vector=vec,
                        model=cfg.embedder.model,
                        version="1",
                        dimension=cfg.embedder.dimension,
                    )
                conn.commit()
                remaining -= len(batch)

    out = Path(__file__).parent / "apply_batch2_report.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Batch1 fills: {len(batch1_hits)}, total updates: {len(updates) if 'updates' in dir() else 0}")
    dbm.close_pool()


if __name__ == "__main__":
    main()
