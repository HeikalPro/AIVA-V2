"""Read-only: compare live 6-digits kb_chunk rows to the v4 export and the v5 delta.

Run from AIVA-V2/:  python scripts/check_6digits_live_vs_exports.py
Each record prints "= v4", "= v5", or "?? matches neither", plus whether it is embedded.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import oracledb
from dotenv import dotenv_values

CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"
V4 = Path("scripts/exports/kb_6digits_v4.jsonl")
V5 = Path("scripts/exports/kb_6digits_v5_delta.jsonl")


def load(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            c = json.loads(line)["canonical"]
            out[str(c["id"])] = c["answer"]["text_ocr"].strip()
    return out


def main() -> None:
    cfg = {**dotenv_values(".env"), **dotenv_values("embedding_service/.env")}
    v4, v5 = load(V4), load(V5)
    print("DSN:", cfg["ORACLE_DSN"])
    conn = oracledb.connect(
        user=cfg["ORACLE_USER"], password=cfg["ORACLE_PASSWORD"], dsn=cfg["ORACLE_DSN"]
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT external_parent_id, chunk_index, chunk_text,
                   CASE WHEN embedding IS NULL THEN 'NO' ELSE 'yes' END, updated_at
              FROM kb_chunk
             WHERE corpus_id = :cid
               AND JSON_VALUE(payload_json, '$.vertical') = '6-digits'
             ORDER BY TO_NUMBER(external_parent_id DEFAULT NULL ON CONVERSION ERROR), chunk_index
            """,
            cid=bytes.fromhex(CORPUS_HEX),
        )
        rows = cur.fetchall()
    conn.close()

    print(f"live 6-digits chunks: {len(rows)}")
    for pid, idx, text, embedded, updated in rows:
        text = text.read() if hasattr(text, "read") else text
        if pid in v5 and v5[pid] in text:
            state = "= v5"
        elif pid in v4 and v4[pid] in text:
            state = "= v4"
        else:
            state = "?? matches neither"
        print(f"  {pid} idx={idx} embedded={embedded} updated={updated:%Y-%m-%d %H:%M}  {state}")


if __name__ == "__main__":
    main()
