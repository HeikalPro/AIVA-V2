from fastapi import APIRouter

from backend.routers import (
    account_updates,
    accounts,
    agents,
    analytics,
    auth,
    auth_flow,
    chat,
    corpora,
    http_logs,
    ingestion,
    llm_configs,
    logs,
    organizations,
    prompts,
    roles,
    system,
    tickets,
    users,
    widget,
)


def build_api_router() -> APIRouter:
    router = APIRouter(prefix="/api")
    router.include_router(auth.router)
    router.include_router(auth_flow.router)
    router.include_router(organizations.router)
    router.include_router(accounts.router)
    router.include_router(agents.router)
    router.include_router(corpora.router)
    router.include_router(users.router)
    router.include_router(roles.router)
    router.include_router(chat.router)
    router.include_router(prompts.router)
    router.include_router(tickets.router)
    router.include_router(account_updates.router)
    router.include_router(ingestion.router)
    router.include_router(analytics.router)
    router.include_router(http_logs.router)
    router.include_router(llm_configs.router)
    router.include_router(logs.router)
    router.include_router(system.router)
    router.include_router(widget.router)
    return router
