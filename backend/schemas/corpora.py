from typing import Any

from pydantic import BaseModel


class CorpusSummaryOut(BaseModel):
    corpus_id: str
    name: str
    slug: str


class CorpusDetailOut(BaseModel):
    corpus_id: str
    name: str
    slug: str
    config: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None
