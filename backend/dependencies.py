from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request

from backend.config import Settings, get_settings
from backend.database import Database
from embedding_service.service import EmbeddingService


@lru_cache
def _database_singleton() -> Database:
    return Database(get_settings())


def get_db() -> Database:
    return _database_singleton()


def get_settings_dep() -> Settings:
    return get_settings()


def get_embedding_service(request: Request) -> EmbeddingService:
    svc: EmbeddingService | None = getattr(request.app.state, "embedding_service", None)
    if svc is None:
        raise RuntimeError("EmbeddingService not initialized")
    return svc


EmbeddingServiceDep = Annotated[EmbeddingService, Depends(get_embedding_service)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DbDep = Annotated[Database, Depends(get_db)]
