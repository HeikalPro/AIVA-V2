from __future__ import annotations

import hashlib
import json
from typing import Any

from embedding_service.adapters.base import ChunkDraft
from embedding_service.chunking.chunker import chunk_text
from embedding_service.models.corpus_config import CorpusConfig


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _deep_get(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if cur is None or not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def adapt_generic_jsonl_line(
    line: str,
    line_no: int,
    cfg: CorpusConfig,
) -> list[ChunkDraft]:
    line = line.strip()
    if not line:
        return []
    obj = json.loads(line)
    id_field = cfg.generic_id_field or "id"
    text_field = cfg.generic_text_field or "text"
    pid = _deep_get(obj, id_field) if "." in id_field else obj.get(id_field)
    if pid is None:
        raise ValueError(f"Line {line_no}: id not found at field {id_field}")
    parent_id = str(pid)
    raw_text = _deep_get(obj, text_field) if "." in text_field else obj.get(text_field)
    if raw_text is None:
        raw_text = json.dumps(obj, ensure_ascii=False)
    elif not isinstance(raw_text, str):
        raw_text = str(raw_text)

    segments = chunk_text(raw_text, cfg.chunk_max_chars, cfg.chunk_overlap)
    if not segments:
        segments = [raw_text]

    payload = {"source": "generic_jsonl_v1"}
    out: list[ChunkDraft] = []
    for idx, seg in enumerate(segments):
        body = seg
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
