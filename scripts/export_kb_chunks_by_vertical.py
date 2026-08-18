"""Export kb_chunk rows into one CSV file per vertical (Tasaheel, Gomla, CF, ...)."""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.config import get_settings
from embedding_service.db.manager import DatabaseManager
from embedding_service.util.uuids import bytes_to_hex, hex_to_bytes

MAIN_CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"

_QUESTION_EN_RE = re.compile(r"^Question EN:\s*(.+)$", re.MULTILINE)
_QUESTION_AR_RE = re.compile(r"^Question AR:\s*(.+)$", re.MULTILINE)

CSV_FIELDS = [
    "corpus_name",
    "external_parent_id",
    "chunk_id",
    "chunk_index",
    "vertical",
    "interaction_type",
    "issue_type",
    "escalation",
    "answer_status",
    "question_en",
    "question_ar",
    "has_embedding",
    "chunk_text",
]


def _read_lob(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "read"):
        return value.read() or ""
    return str(value)


def _slug(label: str) -> str:
    slug = re.sub(r"[^\w\-]+", "_", label.strip(), flags=re.UNICODE).strip("_")
    return slug or "unknown"


def _bucket_key(corpus_name: str, corpus_hex: str, vertical: str | None) -> str:
    if corpus_hex.upper() != MAIN_CORPUS_HEX:
        return _slug(corpus_name)
    return _slug(vertical or "unknown")


def _extract_questions(chunk_text: str) -> tuple[str, str]:
    en = _QUESTION_EN_RE.search(chunk_text)
    ar = _QUESTION_AR_RE.search(chunk_text)
    return (
        en.group(1).strip() if en else "",
        ar.group(1).strip() if ar else "",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "exports" / "by_vertical",
        help="Directory for CSV output (default: scripts/exports/by_vertical)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Export 100%% of kb_chunk: all chunk_index values and all corpora (694 rows)",
    )
    parser.add_argument(
        "--all-chunks",
        action="store_true",
        help="Include split script segments (chunk_index > 0)",
    )
    parser.add_argument(
        "--include-demo",
        action="store_true",
        help="Include non-main corpora (e.g. Demo Smoke Test)",
    )
    args = parser.parse_args()

    all_chunks = args.full or args.all_chunks
    include_demo = args.full or args.include_demo

    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    main_corpus_id = hex_to_bytes(MAIN_CORPUS_HEX)
    settings = get_settings()
    dbm = DatabaseManager(settings)
    dbm.init_pool()

    buckets: dict[str, list[dict[str, str]]] = {}
    all_rows: list[dict[str, str]] = []

    corpus_filter = "" if include_demo else "AND k.corpus_id = :main_corpus_id"
    chunk_filter = "" if all_chunks else "AND k.chunk_index = 0"

    binds: dict = {}
    if not include_demo:
        binds["main_corpus_id"] = main_corpus_id

    with dbm.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT c.name AS corpus_name,
                       RAWTOHEX(k.corpus_id) AS corpus_hex,
                       TO_CHAR(k.external_parent_id) AS external_parent_id,
                       k.chunk_id,
                       k.chunk_index,
                       JSON_VALUE(k.payload_json, '$.vertical') AS vertical,
                       JSON_VALUE(k.payload_json, '$.interaction_type') AS interaction_type,
                       JSON_VALUE(k.payload_json, '$.issue_type') AS issue_type,
                       JSON_VALUE(k.payload_json, '$.escalation') AS escalation,
                       JSON_VALUE(k.payload_json, '$.answer_status') AS answer_status,
                       CASE WHEN k.embedding IS NULL THEN 'no' ELSE 'yes' END AS has_embedding,
                       k.chunk_text
                FROM kb_chunk k
                JOIN kb_corpus c ON c.corpus_id = k.corpus_id
                WHERE 1=1
                {corpus_filter}
                {chunk_filter}
                ORDER BY c.name, k.external_parent_id, k.chunk_index
                """,
                **binds,
            )
            rows = cur.fetchall()

    for row in rows:
        (
            corpus_name,
            corpus_hex,
            parent_id,
            chunk_id_raw,
            chunk_index,
            vertical,
            interaction_type,
            issue_type,
            escalation,
            answer_status,
            has_embedding,
            chunk_text_lob,
        ) = row
        chunk_text = _read_lob(chunk_text_lob)
        q_en, q_ar = _extract_questions(chunk_text)
        vertical_label = vertical or "(null)"
        key = _bucket_key(corpus_name, corpus_hex, vertical)

        record = {
            "corpus_name": corpus_name,
            "external_parent_id": parent_id,
            "chunk_id": bytes_to_hex(chunk_id_raw),
            "chunk_index": str(chunk_index),
            "vertical": vertical_label,
            "interaction_type": interaction_type or "",
            "issue_type": issue_type or "",
            "escalation": escalation or "",
            "answer_status": answer_status or "",
            "question_en": q_en,
            "question_ar": q_ar,
            "has_embedding": has_embedding,
            "chunk_text": chunk_text,
        }
        buckets.setdefault(key, []).append(record)
        all_rows.append(record)

    written: list[str] = []
    summary_lines = [
        f"Total rows exported: {len(rows)}",
        f"All chunks (incl. split segments): {all_chunks}",
        f"Include demo / other corpora: {include_demo}",
        f"Output directory: {out_dir}",
        "",
    ]

    for key in sorted(buckets.keys(), key=lambda s: (-len(buckets[s]), s)):
        data = buckets[key]
        if key == _slug("Demo Smoke Test"):
            label = "Demo Smoke Test"
        else:
            label = data[0]["vertical"]
        filename = f"kb_chunks_{key}.csv"
        path = out_dir / filename
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            writer.writerows(data)
        written.append(f"{filename}: {len(data)} rows ({label})")
        summary_lines.append(f"{label}: {len(data)} rows -> {filename}")

    all_path = out_dir / "kb_chunks_ALL.csv"
    with all_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(all_rows)
    written.append(f"kb_chunks_ALL.csv: {len(all_rows)} rows (complete dump)")
    summary_lines.extend(["", f"Complete dump: kb_chunks_ALL.csv ({len(all_rows)} rows)"])

    summary_path = out_dir / "kb_chunks_by_vertical_summary.txt"
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")

    print("\n".join(written))
    print(f"Summary: {summary_path}")
    dbm.close_pool()


if __name__ == "__main__":
    main()
