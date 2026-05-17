from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChunkDraft:
    """One embeddable chunk before persistence."""

    external_parent_id: str
    chunk_index: int
    text: str
    content_hash: str
    payload: dict[str, Any] = field(default_factory=dict)
    source_line: int | None = None
