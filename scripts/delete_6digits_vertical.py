"""Delete every kb_chunk in the 6-digits vertical of the Halan corpus.

Dry-run by default. Requires --execute to actually delete.

  python scripts/delete_6digits_vertical.py                 # show what would go
  python scripts/delete_6digits_vertical.py --execute       # delete for real

A full backup of the rows (all columns except the regenerable embedding vector)
must exist first: scripts/exports/kb_6digits_LIVE_BACKUP_<date>.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.config import get_settings
from embedding_service.db.manager import DatabaseManager
from embedding_service.util.uuids import hex_to_bytes

CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"
VERTICAL = "6-digits"

SELECT_SQL = """
    SELECT external_parent_id, chunk_index,
           DBMS_LOB.SUBSTR(chunk_text, 200, 1) AS preview
    FROM kb_chunk
    WHERE corpus_id = :cid
      AND JSON_VALUE(payload_json, '$.vertical') = :vertical
    ORDER BY external_parent_id, chunk_index
"""

DELETE_SQL = """
    DELETE FROM kb_chunk
    WHERE corpus_id = :cid
      AND JSON_VALUE(payload_json, '$.vertical') = :vertical
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="actually delete")
    args = parser.parse_args()

    corpus_id = hex_to_bytes(CORPUS_HEX)
    dbm = DatabaseManager(get_settings())
    dbm.init_pool()
    try:
        with dbm.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(SELECT_SQL, cid=corpus_id, vertical=VERTICAL)
                rows = cur.fetchall()

            print(f"rows matching vertical={VERTICAL!r}: {len(rows)}")
            for pid, idx, prev in rows:
                text = prev.read() if hasattr(prev, "read") else (prev or "")
                question = ""
                for line in text.splitlines():
                    if line.startswith("Question EN:"):
                        question = line[len("Question EN:"):].strip()
                        break
                print(f"  {pid} #{idx}  {question[:88] or ' '.join(text.split())[:88]}")

            if not args.execute:
                print("\nDRY RUN — nothing deleted. Re-run with --execute to delete.")
                return

            with conn.cursor() as cur:
                cur.execute(DELETE_SQL, cid=corpus_id, vertical=VERTICAL)
                deleted = cur.rowcount
            conn.commit()
            print(f"\nDELETED {deleted} rows and committed.")
    finally:
        dbm.close_pool()


if __name__ == "__main__":
    main()
