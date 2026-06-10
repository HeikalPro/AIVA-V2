"""Find kb_chunk rows with scripts (answer_status=ok) matching skipped question text."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.config import get_settings
from embedding_service.db.manager import DatabaseManager
from embedding_service.util.uuids import hex_to_bytes

CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"

SKIPPED_IDS = [
    "1259", "525", "546", "550", "559", "491", "1637", "1581",
    "1258", "862", "699", "1005", "1083",
]

_QUESTION_EN_RE = re.compile(r"^Question EN:\s*(.+)$", re.MULTILINE)
_QUESTION_AR_RE = re.compile(r"^Question AR:\s*(.+)$", re.MULTILINE)
_SCRIPT_RE = re.compile(r"^Script:\s*\n([\s\S]+)$", re.MULTILINE)


def _read_lob(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "read"):
        return value.read() or ""
    return str(value)


def _extract(chunk_text: str) -> tuple[str | None, str | None, str | None]:
    ar = _QUESTION_AR_RE.search(chunk_text)
    en = _QUESTION_EN_RE.search(chunk_text)
    script_m = _SCRIPT_RE.search(chunk_text)
    q_ar = ar.group(1).strip() if ar else None
    q_en = en.group(1).strip() if en else None
    script = script_m.group(1).strip() if script_m else None
    if script and "(missing" in script:
        script = None
    return q_en, q_ar, script


def main() -> None:
    corpus_id = hex_to_bytes(CORPUS_HEX)
    settings = get_settings()
    dbm = DatabaseManager(settings)
    dbm.init_pool()
    lines: list[str] = []

    with dbm.connection() as conn:
        skipped: list[dict] = []
        with conn.cursor() as cur:
            for pid in SKIPPED_IDS:
                cur.execute(
                    """
                    SELECT TO_CHAR(external_parent_id), chunk_text,
                           JSON_VALUE(payload_json, '$.answer_status')
                    FROM kb_chunk
                    WHERE corpus_id = :cid AND external_parent_id = :pid AND chunk_index = 0
                    """,
                    cid=corpus_id,
                    pid=pid,
                )
                row = cur.fetchone()
                if not row:
                    lines.append(f"{pid}: NOT IN DB")
                    continue
                text = _read_lob(row[1])
                q_en, q_ar, script = _extract(text)
                skipped.append({
                    "pid": pid,
                    "q_en": q_en,
                    "q_ar": q_ar,
                    "has_script": bool(script),
                })

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TO_CHAR(external_parent_id), chunk_text,
                       JSON_VALUE(payload_json, '$.answer_status')
                FROM kb_chunk
                WHERE corpus_id = :cid
                  AND chunk_index = 0
                  AND JSON_VALUE(payload_json, '$.answer_status') = 'ok'
                """,
                cid=corpus_id,
            )
            ok_rows = cur.fetchall()

        ok_by_ar: dict[str, list[tuple[str, str]]] = {}
        ok_by_en: dict[str, list[tuple[str, str]]] = {}
        for pid, text_lob, _st in ok_rows:
            text = _read_lob(text_lob)
            q_en, q_ar, script = _extract(text)
            if not script:
                continue
            if q_ar:
                ok_by_ar.setdefault(q_ar, []).append((pid, script[:120]))
            if q_en:
                ok_by_en.setdefault(q_en, []).append((pid, script[:120]))

        lines.append("=== FILL FROM EXISTING OK CHUNKS (same question) ===\n")
        for item in skipped:
            pid = item["pid"]
            matches: list[tuple[str, str, str]] = []
            if item["q_ar"] and item["q_ar"] in ok_by_ar:
                for src, preview in ok_by_ar[item["q_ar"]]:
                    if src != pid:
                        matches.append(("AR", src, preview))
            if item["q_en"] and item["q_en"] in ok_by_en:
                for src, preview in ok_by_en[item["q_en"]]:
                    if src != pid and not any(m[1] == src for m in matches):
                        matches.append(("EN", src, preview))

            if matches:
                lines.append(f"{pid}: CAN FILL from same question")
                for how, src, preview in matches:
                    lines.append(f"  match ({how}) -> parent_id {src}: {preview}...")
            else:
                lines.append(f"{pid}: no ok chunk with same question")
            lines.append("")

        lines.append("=== RELATED: same Question EN, different AR (share script?) ===\n")
        by_en: dict[str, list[dict]] = {}
        for item in skipped:
            if item["q_en"]:
                by_en.setdefault(item["q_en"], []).append(item)
        for q_en, group in by_en.items():
            if len(group) > 1:
                lines.append(f"Question EN: {q_en[:100]}")
                for g in group:
                    lines.append(f"  {g['pid']}: AR={g['q_ar'] or '(none)'}")
                # check if any in group or ok corpus has script for this EN
                ok_sources = ok_by_en.get(q_en, [])
                if ok_sources:
                    lines.append(f"  OK script exists on: {', '.join(s[0] for s in ok_sources)}")
                else:
                    lines.append("  No OK script in DB for this Question EN yet")
                lines.append("")

    out = Path(__file__).resolve().parent / "fill_missing_report.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}")
    dbm.close_pool()


if __name__ == "__main__":
    main()
