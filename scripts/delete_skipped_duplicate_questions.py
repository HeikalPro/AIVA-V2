"""Delete duplicate skipped kb_chunk rows that share the same question text.

Keeps one row per question (lowest external_parent_id). Only targets rows where
answer_status=skipped or chunk_text contains answer.status=skipped.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.config import get_settings
from embedding_service.db.manager import DatabaseManager
from embedding_service.util.uuids import bytes_to_hex, hex_to_bytes

CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"

_QUESTION_EN_RE = re.compile(r"^Question EN:\s*(.+)$", re.MULTILINE)
_QUESTION_AR_RE = re.compile(r"^Question AR:\s*(.+)$", re.MULTILINE)


def _read_lob(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "read"):
        return value.read() or ""
    return str(value)


def _is_skipped(chunk_text: str, answer_status: str | None) -> bool:
    if answer_status == "skipped":
        return True
    return "answer.status=skipped" in chunk_text


def _question_key(chunk_text: str) -> str | None:
    ar = _QUESTION_AR_RE.search(chunk_text)
    if ar and ar.group(1).strip():
        return ar.group(1).strip()
    en = _QUESTION_EN_RE.search(chunk_text)
    if en and en.group(1).strip():
        return en.group(1).strip()
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report only, do not delete")
    args = parser.parse_args()

    corpus_id = hex_to_bytes(CORPUS_HEX)
    settings = get_settings()
    dbm = DatabaseManager(settings)
    dbm.init_pool()

    rows_by_question: dict[str, list[tuple[str, bytes, str]]] = defaultdict(list)
    no_question = 0
    skipped_total = 0

    with dbm.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TO_CHAR(external_parent_id) AS parent_id,
                       chunk_id,
                       JSON_VALUE(payload_json, '$.answer_status') AS answer_status,
                       JSON_VALUE(payload_json, '$.vertical') AS vertical,
                       chunk_text
                FROM kb_chunk
                WHERE corpus_id = :cid
                  AND chunk_index = 0
                  AND (
                    JSON_VALUE(payload_json, '$.answer_status') = 'skipped'
                    OR DBMS_LOB.INSTR(chunk_text, 'answer.status=skipped') > 0
                  )
                ORDER BY external_parent_id
                """,
                cid=corpus_id,
            )
            for parent_id, chunk_id, answer_status, vertical, chunk_text_lob in cur.fetchall():
                chunk_text = _read_lob(chunk_text_lob)
                if not _is_skipped(chunk_text, answer_status):
                    continue
                skipped_total += 1
                key = _question_key(chunk_text)
                if not key:
                    no_question += 1
                    continue
                rows_by_question[key].append((parent_id, chunk_id, vertical or "?"))

        duplicate_groups = {k: v for k, v in rows_by_question.items() if len(v) > 1}
        to_delete: list[tuple[str, bytes, str, str]] = []

        lines: list[str] = [
            f"Skipped chunks: {skipped_total}",
            f"Skipped with question: {skipped_total - no_question}",
            f"Duplicate question groups: {len(duplicate_groups)}",
            "",
        ]

        for question, group in sorted(duplicate_groups.items(), key=lambda x: (-len(x[1]), x[0])):
            group_sorted = sorted(group, key=lambda x: int(x[0]) if str(x[0]).isdigit() else x[0])
            keep_id, _keep_chunk, keep_vertical = group_sorted[0]
            dup_ids = [g[0] for g in group_sorted[1:]]
            lines.append(f"Q ({len(group)}): {question[:200]}")
            lines.append(f"  KEEP {keep_id} ({keep_vertical})")
            lines.append(f"  DELETE {', '.join(dup_ids)}")
            lines.append("")
            for parent_id, chunk_id, vertical in group_sorted[1:]:
                to_delete.append((parent_id, chunk_id, vertical, question))

        report_path = Path(__file__).resolve().parent / "duplicate_skipped_report.txt"
        if args.dry_run:
            lines.append(f"DRY RUN: would delete {len(to_delete)} row(s)")
        else:
            with conn.cursor() as cur:
                for parent_id, chunk_id, _vertical, _question in to_delete:
                    cur.execute("DELETE FROM kb_chunk WHERE chunk_id = :cid", cid=chunk_id)
                    lines.append(f"Deleted parent_id={parent_id} chunk_id={bytes_to_hex(chunk_id)}")
            conn.commit()
            lines.append(f"Deleted {len(to_delete)} duplicate skipped row(s)")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"Wrote {report_path}")
        print(f"Duplicate groups: {len(duplicate_groups)}, to delete: {len(to_delete)}")

    dbm.close_pool()


if __name__ == "__main__":
    main()
