from __future__ import annotations

import logging

import oracledb

from backend.database import Database

_log = logging.getLogger(__name__)

DEFAULT_SYSTEM_TEMPLATE = """You are AIVA, an AI assistant helping call center agents during live calls.
Answer using ONLY the knowledge base context below. If the answer is not in the context, say you do not have that information and suggest escalating.

Knowledge base context:
{context}
"""


async def ensure_system_prompt_storage(db: Database) -> None:
    exists = await db.fetch_one(
        "SELECT 1 FROM user_tables WHERE table_name = 'AIVA_SYSTEM_PROMPT'",
    )
    if not exists:
        await db.execute(
            """
            CREATE TABLE AIVA_system_prompt (
                singleton_id   NUMBER(1) DEFAULT 1 NOT NULL,
                prompt_text    CLOB NOT NULL,
                updated_by     NUMBER,
                updated_at     TIMESTAMP(6) DEFAULT SYSTIMESTAMP,
                CONSTRAINT pk_aiva_system_prompt PRIMARY KEY (singleton_id),
                CONSTRAINT ck_aiva_system_prompt_singleton CHECK (singleton_id = 1)
            )
            """
        )
        _log.info("Created AIVA_system_prompt table")

    row = await db.fetch_one(
        "SELECT 1 FROM AIVA_system_prompt WHERE singleton_id = 1",
    )
    if not row:
        await db.execute(
            """
            INSERT INTO AIVA_system_prompt (singleton_id, prompt_text)
            VALUES (1, :prompt_text)
            """,
            {"prompt_text": DEFAULT_SYSTEM_TEMPLATE},
        )
        _log.info("Seeded default system prompt")


async def get_system_prompt_text(db: Database) -> str:
    try:
        await ensure_system_prompt_storage(db)
        row = await db.fetch_one(
            "SELECT prompt_text FROM AIVA_system_prompt WHERE singleton_id = 1",
        )
        if row and row.get("prompt_text") is not None:
            text = str(row["prompt_text"]).strip()
            if text:
                return text
    except oracledb.Error:
        _log.debug("System prompt unavailable; using built-in default", exc_info=True)
    return DEFAULT_SYSTEM_TEMPLATE


async def set_system_prompt_text(db: Database, prompt_text: str, *, user_id: int) -> None:
    await ensure_system_prompt_storage(db)
    await db.execute(
        """
        MERGE INTO AIVA_system_prompt t
        USING (SELECT 1 AS singleton_id FROM dual) s
        ON (t.singleton_id = s.singleton_id)
        WHEN MATCHED THEN
            UPDATE SET
                prompt_text = :prompt_text,
                updated_by = :updated_by,
                updated_at = SYSTIMESTAMP
        WHEN NOT MATCHED THEN
            INSERT (singleton_id, prompt_text, updated_by)
            VALUES (1, :prompt_text, :updated_by)
        """,
        {"prompt_text": prompt_text, "updated_by": user_id},
    )
