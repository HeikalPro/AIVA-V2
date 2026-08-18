"""Export kb_chunk rows into HALAN / Gomla / Tasaheel CSV files for queue routing."""
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

CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"

_QUESTION_EN_RE = re.compile(r"^Question EN:\s*(.+)$", re.MULTILINE)
_QUESTION_AR_RE = re.compile(r"^Question AR:\s*(.+)$", re.MULTILINE)

CSV_FIELDS = [
    "external_parent_id",
    "chunk_id",
    "chunk_index",
    "queue_category",
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

QUEUE_FILES = {
    "halan": "kb_chunks_halan.csv",
    "gomla": "kb_chunks_gomla.csv",
    "tasaheel": "kb_chunks_tasaheel.csv",
}


def _read_lob(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "read"):
        return value.read() or ""
    return str(value)


def _queue_category(vertical: str | None) -> str:
    v = (vertical or "").strip().lower()
    if v == "tasaheel":
        return "tasaheel"
    if v == "gomla":
        return "gomla"
    return "halan"


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
        default=Path(__file__).resolve().parent / "exports",
        help="Directory for CSV output (default: scripts/exports)",
    )
    parser.add_argument(
        "--primary-only",
        action="store_true",
        help="Export only chunk_index=0 (one row per parent / FAQ)",
    )
    args = parser.parse_args()

    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    corpus_id = hex_to_bytes(CORPUS_HEX)
    settings = get_settings()
    dbm = DatabaseManager(settings)
    dbm.init_pool()

    buckets: dict[str, list[dict[str, str]]] = {k: [] for k in QUEUE_FILES}
    vertical_by_queue: dict[str, dict[str, int]] = {k: {} for k in QUEUE_FILES}

    chunk_filter = "AND chunk_index = 0" if args.primary_only else ""

    with dbm.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT TO_CHAR(external_parent_id) AS external_parent_id,
                       chunk_id,
                       chunk_index,
                       JSON_VALUE(payload_json, '$.vertical') AS vertical,
                       JSON_VALUE(payload_json, '$.interaction_type') AS interaction_type,
                       JSON_VALUE(payload_json, '$.issue_type') AS issue_type,
                       JSON_VALUE(payload_json, '$.escalation') AS escalation,
                       JSON_VALUE(payload_json, '$.answer_status') AS answer_status,
                       CASE WHEN embedding IS NULL THEN 'no' ELSE 'yes' END AS has_embedding,
                       chunk_text
                FROM kb_chunk
                WHERE corpus_id = :cid
                {chunk_filter}
                ORDER BY external_parent_id, chunk_index
                """,
                cid=corpus_id,
            )
            rows = cur.fetchall()

    for row in rows:
        (
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
        queue = _queue_category(vertical)
        vertical_label = vertical or "(null)"
        vertical_by_queue[queue][vertical_label] = vertical_by_queue[queue].get(vertical_label, 0) + 1

        buckets[queue].append(
            {
                "external_parent_id": parent_id,
                "chunk_id": bytes_to_hex(chunk_id_raw),
                "chunk_index": str(chunk_index),
                "queue_category": queue,
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
        )

    written: list[str] = []
    for queue, filename in QUEUE_FILES.items():
        path = out_dir / filename
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            writer.writerows(buckets[queue])
        written.append(f"{filename}: {len(buckets[queue])} rows")

    summary_lines = [
        f"Total rows exported: {len(rows)}",
        f"Primary only (chunk_index=0): {args.primary_only}",
        f"Output directory: {out_dir}",
        "",
        "Queue mapping:",
        "  tasaheel <- vertical = Tasaheel",
        "  gomla    <- vertical = Gomla",
        "  halan    <- all other verticals (CF, Pay, General, Halan, Saving, Gold, Commerce, ...)",
        "",
    ]
    for queue in QUEUE_FILES:
        summary_lines.append(f"=== {queue.upper()} ({len(buckets[queue])} rows) ===")
        for vertical, count in sorted(vertical_by_queue[queue].items(), key=lambda x: (-x[1], x[0])):
            summary_lines.append(f"  {vertical}: {count}")
        summary_lines.append("")

    summary_path = out_dir / "kb_chunks_export_summary.txt"
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")

    print("\n".join(written))
    print(f"Summary: {summary_path}")
    dbm.close_pool()


if __name__ == "__main__":
    main()
