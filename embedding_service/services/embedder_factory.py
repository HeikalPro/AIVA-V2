from __future__ import annotations

import os

from embedding_service.config import Settings
from embedding_service.embedders.http_openai import HttpOpenAIEmbedder
from embedding_service.embedders.oracle_indb import OracleInDbEmbedder
from embedding_service.models.corpus_config import CorpusConfig


def make_embedder(cfg: CorpusConfig, settings: Settings):
    e = cfg.embedder
    if e.type == "http":
        key = e.api_key
        if key is None and e.api_key_env:
            key = os.environ.get(e.api_key_env)
        if key is None:
            key = settings.default_openai_api_key
        return HttpOpenAIEmbedder(e, key)
    if e.type == "oracle":
        return OracleInDbEmbedder(e)
    raise ValueError(f"Unknown embedder type {e.type}")
