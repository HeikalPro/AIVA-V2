"""Search kb_chunk by partial question text."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.config import get_settings
from embedding_service.db.manager import DatabaseManager
from embedding_service.util.uuids import hex_to_bytes

CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"


def main() -> None:
    terms = sys.argv[1:] or ["ايداع البنكي", "Bank deposit", "deposits"]
    corpus_id = hex_to_bytes(CORPUS_HEX)
    settings = get_settings()
    dbm = DatabaseManager(settings)
    dbm.init_pool()

    with dbm.connection() as conn:
        for term in terms:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT external_parent_id,
                           DBMS_LOB.SUBSTR(chunk_text, 600, 1) AS preview
                    FROM kb_chunk
                    WHERE corpus_id = :cid
                      AND DBMS_LOB.INSTR(LOWER(chunk_text), LOWER(:term)) > 0
                    FETCH FIRST 15 ROWS ONLY
                    """,
                    cid=corpus_id,
                    term=term,
                )
                rows = cur.fetchall()
            print(f"\n=== term: {term!r} ({len(rows)} hits) ===")
            for pid, preview in rows:
                text = preview.read() if hasattr(preview, "read") else (preview or "")
                print(f"parent={pid}")
                print(text[:500])
                print("---")

    dbm.close_pool()


if __name__ == "__main__":
    main()
