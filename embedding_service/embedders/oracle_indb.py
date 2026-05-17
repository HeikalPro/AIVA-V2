from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from embedding_service.embedders.result import EmbedBatchResult
from embedding_service.models.corpus_config import EmbedderConfig
from embedding_service.util.tokens import estimate_tokens_char_heuristic

if TYPE_CHECKING:
    import oracledb

_log = logging.getLogger(__name__)

# Oracle-registered ONNX embedding model names (extend as you load models in Oracle).
_ORACLE_MODEL_WHITELIST = frozenset(
    {
        "all_minilm_l12_v2",
        "all_mpnet_base_v2",
    }
)


class OracleInDbEmbedder:
    """Embeddings via Oracle SQL VECTOR_EMBEDDING / VECTOR_EMBEDDING(... USING ... AS DATA)."""

    def __init__(self, cfg: EmbedderConfig):
        self._cfg = cfg
        self.dimension = cfg.dimension
        ident = cfg.model.strip().lower()
        if ident not in _ORACLE_MODEL_WHITELIST:
            raise ValueError(
                f"Oracle embedder model '{cfg.model}' not in whitelist {_ORACLE_MODEL_WHITELIST}. "
                "Load an ONNX model in Oracle and add its registry name here."
            )
        if not re.fullmatch(r"[a-z0-9_]+", ident):
            raise ValueError("Invalid oracle embedding model identifier")
        self._model_ident = ident

    def embed(self, texts: list[str], conn: oracledb.Connection | None) -> EmbedBatchResult:
        if conn is None:
            raise ValueError("OracleInDbEmbedder requires an active DB connection")
        out: list[list[float]] = []
        est = estimate_tokens_char_heuristic(texts)
        sql = f"""
            SELECT VECTOR_EMBEDDING({self._model_ident} USING :txt AS DATA)
            FROM DUAL
        """
        with conn.cursor() as cur:
            for t in texts:
                cur.execute(sql, txt=t)
                row = cur.fetchone()
                if row is None or row[0] is None:
                    raise RuntimeError("VECTOR_EMBEDDING returned no data")
                vec = row[0]
                out.append(list(vec))
        return EmbedBatchResult(vectors=out, api_total_tokens=None, estimated_tokens=est)
