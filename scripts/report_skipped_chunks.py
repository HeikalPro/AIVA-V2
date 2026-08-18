"""Report all kb_chunk rows with answer_status=skipped in main corpus."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.config import get_settings
from embedding_service.db.manager import DatabaseManager
from embedding_service.util.uuids import hex_to_bytes

CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"

# IDs we successfully updated in this project (for cross-check)
UPDATED_IDS = {
    "233", "242", "250", "252", "221", "262", "264", "1118", "1439", "1167", "492", "523",
    "979", "1650", "1299", "1313", "1219", "1318", "1319", "1321",
    "1190", "1329", "1273", "1196", "1336", "1198", "1341", "1348", "1209", "1297",
    "581", "582", "583", "517", "519", "520", "529", "1322",
}


def main() -> None:
    corpus_id = hex_to_bytes(CORPUS_HEX)
    settings = get_settings()
    dbm = DatabaseManager(settings)
    dbm.init_pool()
    lines: list[str] = []

    with dbm.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT external_parent_id,
                       JSON_VALUE(payload_json, '$.vertical') AS vertical,
                       JSON_VALUE(payload_json, '$.interaction_type') AS itype,
                       DBMS_LOB.SUBSTR(chunk_text, 280, 1) AS preview
                FROM kb_chunk
                WHERE corpus_id = :cid
                  AND chunk_index = 0
                  AND (
                    JSON_VALUE(payload_json, '$.answer_status') = 'skipped'
                    OR DBMS_LOB.INSTR(chunk_text, 'answer.status=skipped') > 0
                  )
                ORDER BY vertical, external_parent_id
                """,
                cid=corpus_id,
            )
            rows = cur.fetchall()

        lines.append(f"TOTAL SKIPPED CHUNKS: {len(rows)}\n")

        by_vertical: dict[str, list] = {}
        for pid, vertical, itype, preview in rows:
            v = vertical or "?"
            by_vertical.setdefault(v, []).append((pid, itype, preview))

        for vertical in sorted(by_vertical.keys()):
            lines.append(f"\n=== {vertical} ({len(by_vertical[vertical])}) ===")
            for pid, itype, preview in by_vertical[vertical]:
                text = preview.read() if hasattr(preview, "read") else (preview or "")
                # extract Question EN if present
                qen = ""
                for part in text.split("\n"):
                    if part.startswith("Question EN:"):
                        qen = part.replace("Question EN:", "").strip()[:80]
                        break
                marker = " [we-updated-other]" if pid in UPDATED_IDS else ""
                lines.append(f"  {pid} | {itype or '?'} | {qen}{marker}")

    out = Path(__file__).resolve().parent / "skipped_report.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out} ({len(rows)} skipped)")
    dbm.close_pool()


if __name__ == "__main__":
    main()
