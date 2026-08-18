"""Delete kb_chunk rows that have answer_status=skipped and no question in chunk_text."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.config import get_settings
from embedding_service.db.manager import DatabaseManager
from embedding_service.util.uuids import bytes_to_hex, hex_to_bytes

CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"

# Canonical IDs from the user's skipped batch (external_parent_id).
CANDIDATE_IDS = [
    "554", "1019", "1259", "525", "546", "550", "557", "491", "1637", "1581",
    "1258", "618", "699", "1433", "1005", "1083", "1084", "1085", "1086",
]

_QUESTION_RE = re.compile(r"^Question (?:EN|AR):\s*(.+)$", re.MULTILINE)


def _read_lob(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "read"):
        return value.read() or ""
    return str(value)


def _has_question(chunk_text: str) -> bool:
    for match in _QUESTION_RE.finditer(chunk_text):
        if match.group(1).strip():
            return True
    return False


def main() -> None:
    corpus_id = hex_to_bytes(CORPUS_HEX)
    settings = get_settings()
    dbm = DatabaseManager(settings)
    dbm.init_pool()

    deleted: list[str] = []
    kept: list[str] = []
    missing: list[str] = []

    with dbm.connection() as conn:
        with conn.cursor() as cur:
            for parent_id in CANDIDATE_IDS:
                cur.execute(
                    """
                    SELECT chunk_id,
                           DBMS_LOB.SUBSTR(chunk_text, 4000, 1) AS chunk_text,
                           JSON_VALUE(payload_json, '$.answer_status') AS answer_status
                    FROM kb_chunk
                    WHERE corpus_id = :cid
                      AND external_parent_id = :pid
                      AND chunk_index = 0
                    """,
                    cid=corpus_id,
                    pid=parent_id,
                )
                row = cur.fetchone()
                if not row:
                    missing.append(parent_id)
                    continue

                chunk_id, chunk_text_lob, answer_status = row
                chunk_text = _read_lob(chunk_text_lob)

                if answer_status != "skipped" and "answer.status=skipped" not in chunk_text:
                    print(f"SKIP {parent_id}: not skipped (status={answer_status})")
                    kept.append(parent_id)
                    continue

                if _has_question(chunk_text):
                    print(f"KEEP {parent_id}: has question")
                    kept.append(parent_id)
                    continue

                cur.execute(
                    "DELETE FROM kb_chunk WHERE chunk_id = :cid",
                    cid=chunk_id,
                )
                deleted.append(parent_id)
                print(f"DELETE {parent_id}: chunk_id={bytes_to_hex(chunk_id)}")

        conn.commit()

    print(f"\nDeleted: {len(deleted)} -> {deleted}")
    print(f"Kept: {len(kept)} -> {kept}")
    if missing:
        print(f"Not found: {missing}")

    dbm.close_pool()


if __name__ == "__main__":
    main()
