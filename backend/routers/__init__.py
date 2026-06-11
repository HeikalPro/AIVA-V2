from fastapi import APIRouter

from backend.routers import (
    accounts,
    analytics,
    auth,
    auth_flow,
    chat,
    corpora,
    ingestion,
    llm_configs,
    organizations,
    prompts,
    tickets,
    users,
)


def build_api_router() -> APIRouter:
    router = APIRouter(prefix="/api")
    router.include_router(auth.router)
    router.include_router(auth_flow.router)
    router.include_router(organizations.router)
    router.include_router(accounts.router)
    router.include_router(corpora.router)
    router.include_router(users.router)
    router.include_router(chat.router)
    router.include_router(prompts.router)
    router.include_router(tickets.router)
    router.include_router(ingestion.router)
    router.include_router(analytics.router)
    router.include_router(llm_configs.router)
    return router
