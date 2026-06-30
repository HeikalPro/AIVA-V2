from __future__ import annotations

import logging

from backend.database import Database

_log = logging.getLogger(__name__)

# Short admin-facing notes for known SovereignEG / provider model ids.
DEFAULT_MODEL_COMMENTS: dict[str, str] = {
    "gpt-4.1-mini": "Default; fast and low-cost",
    "deepseek-v4-flash": "Fast general-purpose DeepSeek",
    "gemini-3.5-flash-2": "Fast Google Gemini flash",
    "deepseek-r1-0528": "DeepSeek R1 reasoning model",
    "glm-4.7-flash": "Fast Zhipu GLM chat",
    "claude-haiku-4-5": "Fast Anthropic; low latency",
    "gpt-oss-120b-turbo": "Large open-weight 120B turbo",
    "gemini-3.1-pro-2": "Gemini Pro; higher quality",
    "qwen-2-1.5b-instruct": "Tiny Qwen; ultra-fast",
    "nemotron-3-nano-30b-a3b-2": "NVIDIA MoE; balanced",
    "gpt-5.5": "Latest GPT flagship",
}


async def _column_exists(db: Database, table: str, column: str) -> bool:
    row = await db.fetch_one(
        """
        SELECT 1 FROM user_tab_cols
        WHERE table_name = :table_name AND column_name = :column_name
        """,
        {"table_name": table.upper(), "column_name": column.upper()},
    )
    return row is not None


async def ensure_llm_config_schema(db: Database) -> None:
    """Add optional LLM config columns introduced after initial schema deploy."""
    if not await _column_exists(db, "AIVA_LLM_CONFIGS", "MODEL_COMMENT"):
        await db.execute("ALTER TABLE AIVA_llm_configs ADD (model_comment VARCHAR2(512))")
        _log.info("Added AIVA_llm_configs.model_comment")

    for model_name, comment in DEFAULT_MODEL_COMMENTS.items():
        await db.execute(
            """
            UPDATE AIVA_llm_configs
            SET model_comment = :comment
            WHERE LOWER(model_name) = LOWER(:model_name)
              AND (model_comment IS NULL OR TRIM(model_comment) = '')
            """,
            {"model_name": model_name, "comment": comment},
        )
