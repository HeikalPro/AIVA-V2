from __future__ import annotations

import hashlib
import json
from typing import Any

from embedding_service.adapters.base import ChunkDraft
from embedding_service.chunking.chunker import chunk_text
from embedding_service.models.corpus_config import CorpusConfig


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _build_prefix(cat: dict[str, Any], cfg: CorpusConfig) -> str:
    return cfg.passage_prefix_template.format(
        vertical=cat.get("vertical") or "",
        interaction_type=cat.get("interaction_type") or "",
        issue_type=cat.get("issue_type") or "",
        escalation=cat.get("escalation") or "",
    )


def _question_block(q: dict[str, Any], raw: dict[str, Any]) -> str:
    parts: list[str] = []
    en = (q.get("en") or "").strip()
    ar = (q.get("ar") or "").strip()
    notes = (q.get("notes") or "").strip()
    body = (raw.get("q_body") or "").strip()
    if en:
        parts.append(f"Question EN: {en}")
    if ar:
        parts.append(f"Question AR: {ar}")
    if notes:
        parts.append(f"Notes: {notes}")
    if body:
        parts.append(f"Body: {body}")
    return "\n".join(parts)


def adapt_halan_records_line(
    line: str,
    line_no: int,
    cfg: CorpusConfig,
) -> list[ChunkDraft]:
    line = line.strip()
    if not line:
        return []
    obj = json.loads(line)
    can = obj.get("canonical") or {}
    raw = obj.get("raw_row") or {}
    cid = can.get("id")
    if cid is None:
        raise ValueError(f"Line {line_no}: missing canonical.id")
    parent_id = str(cid)
    cat = can.get("category") or {}
    ans = can.get("answer") or {}
    q = can.get("question") or {}

    prefix = _build_prefix(cat, cfg)
    qblock = _question_block(q, raw)
    ocr = (ans.get("text_ocr") or "").strip()
    status = (ans.get("status") or "").strip()

    payload = {
        "vertical": cat.get("vertical"),
        "interaction_type": cat.get("interaction_type"),
        "issue_type": cat.get("issue_type"),
        "escalation": cat.get("escalation"),
        "queue": cat.get("queue"),
        "answer_status": status,
        "canonical_id": int(cid) if str(cid).isdigit() else cid,
    }

    if not ocr and status != "ok":
        body = f"{prefix}\n\n{qblock}\n\nScript: (missing — answer.status={status})"
        h = _sha256(body)
        return [
            ChunkDraft(
                external_parent_id=parent_id,
                chunk_index=0,
                text=body,
                content_hash=h,
                payload=payload,
                source_line=line_no,
            )
        ]

    full_script = ocr if ocr else "(empty OCR)"
    segments = chunk_text(full_script, cfg.chunk_max_chars, cfg.chunk_overlap)
    if not segments:
        segments = [full_script]

    out: list[ChunkDraft] = []
    for idx, seg in enumerate(segments):
        body = f"{prefix}\n\n{qblock}\n\nScript:\n{seg}"
        out.append(
            ChunkDraft(
                external_parent_id=parent_id,
                chunk_index=idx,
                text=body,
                content_hash=_sha256(body),
                payload=payload,
                source_line=line_no,
            )
        )
    return out
