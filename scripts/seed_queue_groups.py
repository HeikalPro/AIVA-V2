"""Seed queue_groups on the main KB corpus config."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.config import get_settings
from embedding_service.models.corpus_config import parse_corpus_config
from embedding_service.service import EmbeddingService
from backend.services.kb_queue_groups import DEFAULT_QUEUE_GROUPS

MAIN_CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"


def main() -> None:
    svc = EmbeddingService(get_settings())
    try:
        corpus = svc.get_corpus(MAIN_CORPUS_HEX)
        if not corpus:
            raise SystemExit(f"Corpus not found: {MAIN_CORPUS_HEX}")
        config = parse_corpus_config(corpus.get("config") or {}).model_dump()
        if config.get("queue_groups") == DEFAULT_QUEUE_GROUPS:
            print("queue_groups already seeded")
            return
        config["queue_groups"] = DEFAULT_QUEUE_GROUPS
        updated = svc.patch_corpus(MAIN_CORPUS_HEX, config=config)
        print(f"Seeded queue_groups on corpus {updated['name']} ({updated['corpus_id']})")
    finally:
        svc.close()


if __name__ == "__main__":
    main()
