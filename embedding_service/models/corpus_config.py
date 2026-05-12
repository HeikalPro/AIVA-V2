from typing import Literal

from pydantic import BaseModel, Field


class EmbedderConfig(BaseModel):
    type: Literal["http", "oracle"] = "http"
    model: str = "text-embedding-3-small"
    base_url: str | None = Field(
        default=None,
        description="OpenAI-compatible API base URL; default https://api.openai.com/v1",
    )
    api_key: str | None = Field(
        default=None,
        description="Optional inline API key; prefer env OPENAI_API_KEY or embedder.api_key_env",
    )
    api_key_env: str | None = Field(
        default="OPENAI_API_KEY",
        description="Environment variable name holding the API key for http embedder",
    )
    dimension: int = Field(default=1536, ge=64, le=8192)
    pricing_usd_per_million_tokens: float | None = Field(
        default=None,
        description=(
            "USD per 1M input tokens for cost estimates (commercial APIs). "
            "If unset, falls back to Settings.embedding_default_usd_per_million_tokens."
        ),
    )


class CorpusConfig(BaseModel):
    adapter: Literal["halan_records_v1", "generic_jsonl_v1"] = "halan_records_v1"
    chunker_version: str = Field(default="1", max_length=32)
    chunk_max_chars: int = Field(default=2000, ge=200, le=32000)
    chunk_overlap: int = Field(default=200, ge=0, le=4000)
    passage_prefix_template: str = (
        "Vertical: {vertical} | Type: {interaction_type} | "
        "Issue: {issue_type} | Escalation: {escalation}"
    )
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    distance_metric: Literal["COSINE", "EUCLIDEAN", "DOT"] = "COSINE"
    generic_id_field: str | None = Field(
        default=None,
        description="For generic_jsonl_v1: dot path to id, e.g. 'id' or 'meta.id'",
    )
    generic_text_field: str | None = Field(
        default=None,
        description="For generic_jsonl_v1: dot path to main text field",
    )


def default_corpus_config() -> dict:
    return CorpusConfig().model_dump()


def parse_corpus_config(data: dict) -> CorpusConfig:
    return CorpusConfig.model_validate(data)
