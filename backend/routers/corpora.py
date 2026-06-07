from typing import Annotated

from fastapi import APIRouter, Depends

from backend.auth.deps import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_DEVELOPER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    UserContext,
    require_roles,
)
from backend.dependencies import EmbeddingServiceDep
from backend.exceptions import NotFoundError
from backend.schemas.corpora import CorpusDetailOut, CorpusSummaryOut

router = APIRouter(prefix="/corpora", tags=["corpora"])

_READ_ROLES = (ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER, ROLE_DEVELOPER)


@router.get("", response_model=list[CorpusSummaryOut])
async def list_corpora(
    _user: Annotated[UserContext, Depends(require_roles(*_READ_ROLES))],
    embedding_svc: EmbeddingServiceDep,
) -> list[CorpusSummaryOut]:
    rows = embedding_svc.list_corpora()
    return [
        CorpusSummaryOut(
            corpus_id=str(r["corpus_id"]),
            name=str(r["name"]),
            slug=str(r["slug"]),
        )
        for r in rows
    ]


@router.get("/{corpus_id}", response_model=CorpusDetailOut)
async def get_corpus(
    corpus_id: str,
    _user: Annotated[UserContext, Depends(require_roles(*_READ_ROLES))],
    embedding_svc: EmbeddingServiceDep,
) -> CorpusDetailOut:
    row = embedding_svc.get_corpus(corpus_id)
    if not row:
        raise NotFoundError("Knowledge base not found")
    return CorpusDetailOut(
        corpus_id=str(row["corpus_id"]),
        name=str(row["name"]),
        slug=str(row["slug"]),
        config=row.get("config"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )
