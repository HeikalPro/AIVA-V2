"""HTTP API of the document intelligence module, mounted under ``/api/doc-intel``.

Every route is guarded by a role-only dependency from ``backend.doc_intel.guards``
(never a page permission): Super Admin for document import, source administration and
the CRM entities; Super Admin + Developer for monitoring and the read-only SharePoint views.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from backend.doc_intel.settings import DocIntelSettings, get_doc_intel_settings

_log = logging.getLogger(__name__)


def build_doc_intel_router(settings: DocIntelSettings | None = None) -> APIRouter | None:
    """The ``/doc-intel`` router, or None when ``DOC_INTEL_ENABLED=false``.

    Never raises: a settings or import problem only leaves these routes unmounted.
    """
    try:
        cfg = settings if settings is not None else get_doc_intel_settings()
        if not cfg.enabled:
            _log.info("doc_intel: routes not mounted (DOC_INTEL_ENABLED=false)")
            return None
        from backend.doc_intel.routers import integrations, kb_documents, monitoring

        router = APIRouter(prefix="/doc-intel")
        router.include_router(kb_documents.router)
        router.include_router(monitoring.router)
        router.include_router(integrations.router)
        return router
    except Exception:
        _log.exception("doc_intel: routes not mounted")
        return None
