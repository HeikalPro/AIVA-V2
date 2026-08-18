"""Parse batch2 standardized markdown into JSON records."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SECTION_RE = re.compile(
    r"##\s*(\d+)\.\s*\n+"
    r"(?:_\([^)]+\)_\s*\n+)?"
    r"\*\*Vertical:\*\*\s*([^|]+)\|\s*\*\*Type:\*\*\s*([^|]+)\|\s*"
    r"\*\*Issue:\*\*\s*([^|]+)\|\s*\*\*Escalation:\*\*\s*(\w+)\s*\n+"
    r"\*\*Question EN:\*\*\s*(.+?)\s*\n+"
    r"(?:\*\*Question AR:\*\*\s*(.+?)\s*\n+)?"
    r"\*\*Script:\*\*\s*\n"
    r"([\s\S]*?)"
    r"(?=\n---|\n##\s*\d+\.|\Z)",
    re.MULTILINE,
)


def parse_md(text: str) -> list[dict]:
    records = []
    for m in SECTION_RE.finditer(text):
        n, vertical, typ, issue, esc, q_en, q_ar, script = m.groups()
        script = script.strip()
        if "لا يوجد محتوى" in script or script.startswith("_(لا"):
            continue
        if "(missing" in script:
            continue
        # strip markdown bullets
        script = re.sub(r"^\* ", "", script, flags=re.MULTILINE)
        records.append({
            "n": int(n),
            "vertical": vertical.strip(),
            "type": typ.strip(),
            "issue": issue.strip(),
            "escalation": esc.strip(),
            "q_en": q_en.strip(),
            "q_ar": (q_ar or "").strip() or None,
            "script": script,
        })
    return records


def main() -> None:
    md_path = Path(__file__).resolve().parent / "batch2_standardized.md"
    if not md_path.exists():
        raise SystemExit(f"Missing {md_path}")
    records = parse_md(md_path.read_text(encoding="utf-8"))
    out = Path(__file__).resolve().parent / "batch2_scripts.json"
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Parsed {len(records)} scripts -> {out}")


if __name__ == "__main__":
    main()
