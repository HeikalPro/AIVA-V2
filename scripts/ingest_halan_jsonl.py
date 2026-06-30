"""Ingest halan_records_v1 JSONL into the Hallan corpus (append, not reindex).

Usage:
  python scripts/ingest_halan_jsonl.py path/to/halan_cash_scripts.jsonl
  python scripts/ingest_halan_jsonl.py path/to/file.jsonl --dry-run
  python scripts/ingest_halan_jsonl.py path/to/file.jsonl --no-fix-ocr

Expects one JSON object per line (adapter halan_records_v1). Default corpus is Hallan KB.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.service import EmbeddingService

DEFAULT_CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"

# Known Gemini/PDF OCR glitches — applied unless --no-fix-ocr
_OCR_REPLACEMENTS: dict[int, list[tuple[str, str]]] = {
    1735: [
        ("ليها ستع و ف\n", ""),
        ("ليها ستع و ف", ""),
    ],
    1766: [
        ("الذهاب الى المهاست ع وفي", "الذهاب الى فروع تساهيل"),
        ("موقع \"ميزة\"", "موقع ميزة"),
    ],
}


def _apply_ocr_fixes(obj: dict) -> list[str]:
    changes: list[str] = []
    cid = (obj.get("canonical") or {}).get("id")
    if not isinstance(cid, int):
        return changes
    answer = (obj.get("canonical") or {}).get("answer") or {}
    text = answer.get("text_ocr")
    if not isinstance(text, str):
        return changes
    for old, new in _OCR_REPLACEMENTS.get(cid, []):
        if old in text:
            text = text.replace(old, new)
            changes.append(f"id {cid}: replaced {old!r} -> {new!r}")
    if changes:
        answer["text_ocr"] = text
    return changes


def _load_lines(path: Path, *, fix_ocr: bool) -> tuple[list[str], list[str]]:
    raw_lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    out: list[str] = []
    fixes: list[str] = []
    ids: list[int] = []
    for i, line in enumerate(raw_lines, start=1):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Line {i}: invalid JSON: {exc}") from exc
        if "canonical" not in obj:
            raise SystemExit(f"Line {i}: missing 'canonical' (halan_records_v1 required)")
        cid = obj["canonical"].get("id")
        if cid is not None:
            ids.append(int(cid))
        if fix_ocr:
            fixes.extend(_apply_ocr_fixes(obj))
        out.append(json.dumps(obj, ensure_ascii=False))
    if ids:
        print(f"Records: {len(ids)} | IDs {min(ids)}..{max(ids)}")
    if fixes:
        print("OCR fixes applied:")
        for f in fixes:
            print(f"  - {f}")
    return out, fixes


def _wait_job(svc: EmbeddingService, job_id: str, *, timeout_sec: int = 3600) -> dict:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        job = svc.get_job(job_id)
        status = (job.get("status") or "").upper()
        if status in {"COMPLETED", "FAILED"}:
            return job
        time.sleep(1.0)
    raise TimeoutError(f"Job {job_id} did not finish within {timeout_sec}s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest halan_records_v1 JSONL into Hallan KB")
    parser.add_argument("jsonl", type=Path, help="Path to .jsonl file")
    parser.add_argument("--corpus-id", default=DEFAULT_CORPUS_HEX, help="Corpus hex id")
    parser.add_argument("--dry-run", action="store_true", help="Validate only, do not ingest")
    parser.add_argument("--no-fix-ocr", action="store_true", help="Skip known OCR patches")
    parser.add_argument("--wait", action="store_true", help="Poll until job completes (inline mode)")
    args = parser.parse_args()

    if not args.jsonl.is_file():
        raise SystemExit(f"File not found: {args.jsonl}")

    lines, _ = _load_lines(args.jsonl, fix_ocr=not args.no_fix_ocr)
    if args.dry_run:
        print(f"Dry run OK — {len(lines)} lines ready for corpus {args.corpus_id}")
        return

    svc = EmbeddingService()
    try:
        result = svc.ingest(args.corpus_id, lines=lines)
        print(f"Ingest started: job_id={result['job_id']} mode={result.get('mode')}")
        if args.wait or result.get("mode") == "inline":
            job = _wait_job(svc, result["job_id"])
            print(f"Status: {job.get('status')}")
            if job.get("error_msg"):
                print(f"Error: {job['error_msg']}")
                raise SystemExit(1)
            stats = job.get("stats_json")
            if stats:
                if isinstance(stats, str):
                    stats = json.loads(stats)
                print(f"Stats: {json.dumps(stats, ensure_ascii=False)[:500]}")
    finally:
        svc.close()


if __name__ == "__main__":
    main()
