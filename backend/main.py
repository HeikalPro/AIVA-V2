from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LLM_SERVICE = _ROOT / "llm_service"
for _path in (_ROOT, _LLM_SERVICE):
    _s = str(_path)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from backend.config import get_settings
from backend.dependencies import _database_singleton
from backend.services.audit_schema import ensure_audit_schema
from backend.services.account_schema import ensure_account_schema
from backend.services.account_updates_schema import ensure_account_updates_schema
from backend.services.auth_schema import ensure_auth_schema
from backend.services.bootstrap import ensure_bootstrap_superadmin
from backend.services.chat_schema import ensure_chat_schema
from backend.services.agent_queue_access_schema import ensure_agent_queue_access_schema
from backend.services.ai_request_schema import ensure_ai_request_schema
from backend.services.http_request_log_schema import ensure_http_request_log_schema
from backend.services.rag_retrieval_schema import ensure_rag_retrieval_schema
from backend.services.error_log import ensure_error_log_schema
from backend.services.llm_config_schema import ensure_llm_config_schema
from backend.services.role_nav_permissions import ensure_role_nav_permissions_schema
from backend.services.sovereign_catalog import start_catalog_scheduler
from backend.services.system_prompt import ensure_system_prompt_storage
from backend.limiter import limiter
from backend.exceptions import HTTPException
from backend.middleware import RequestLoggingMiddleware
from backend.routers import build_api_router
from embedding_service.service import EmbeddingService

_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = _database_singleton()
    await db.init_pool()
    await ensure_bootstrap_superadmin(db, settings)
    await ensure_account_schema(db)
    await ensure_audit_schema(db)
    await ensure_account_updates_schema(db)
    await ensure_auth_schema(db)
    await ensure_chat_schema(db)
    await ensure_agent_queue_access_schema(db)
    await ensure_ai_request_schema(db)
    await ensure_http_request_log_schema(db)
    await ensure_rag_retrieval_schema(db)
    await ensure_error_log_schema(db)
    await ensure_llm_config_schema(db)
    await ensure_role_nav_permissions_schema(db)
    await ensure_system_prompt_storage(db)

    embedding_svc = EmbeddingService()
    app.state.embedding_service = embedding_svc
    app.state.database = db

    # Refresh SovereignEG model prices daily at 22:00 Africa/Cairo (warms cache on startup too).
    catalog_task = start_catalog_scheduler()

    _log.info("AIVA backend started on %s:%s", settings.backend_host, settings.backend_port)
    try:
        yield
    finally:
        catalog_task.cancel()
        embedding_svc.close()
        await db.close_pool()
        _log.info("AIVA backend shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=logging.DEBUG if settings.backend_debug else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    app = FastAPI(
        title="AIVA Backend API",
        description="AI Virtual Assistant for Call Centers",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(build_api_router())
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=s.backend_host,
        port=s.backend_port,
        reload=s.backend_debug,
        # Behind a TLS-terminating reverse proxy: without these, every request
        # is attributed to the proxy's IP (rate limiting and request logs).
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
