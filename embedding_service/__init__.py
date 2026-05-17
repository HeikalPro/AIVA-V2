"""Configurable embedding KB library (Oracle 23ai): ingest, search, jobs."""

from embedding_service.config import Settings, get_settings
from embedding_service.db.manager import DatabaseManager
from embedding_service.db.repository import CorpusRepository
from embedding_service.models.corpus_config import CorpusConfig, parse_corpus_config
from embedding_service.service import EmbeddingService

__all__ = [
    "EmbeddingService",
    "DatabaseManager",
    "CorpusRepository",
    "Settings",
    "get_settings",
    "CorpusConfig",
    "parse_corpus_config",
]
