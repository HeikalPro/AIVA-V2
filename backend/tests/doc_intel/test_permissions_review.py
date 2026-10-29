"""Permission review: doc-intel access is decided by role alone; page permissions never open it.

The role matrix itself (401 anonymous, 403 per role, 2xx for the allowed ones) is in
test_api_kb_documents.py and test_api_monitoring.py. These tests add the page-permission
angle: even a user whose resolved page permissions contain every key (including
"monitoring" and "document-import") is refused, while a nav-permission guard given the same
permissions would let that user in.
"""
from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends
from fastapi.dependencies.utils import get_dependant

from backend.auth import deps
from backend.auth.role_constants import ROLE_AGENT, ROLE_DEVELOPER, ROLE_SUPER_ADMIN
from backend.dependencies import get_db
from backend.doc_intel import guards
from backend.services.role_nav_permissions import ALL_NAV_KEYS

from .conftest import NON_PLATFORM_ROLES, PDF_BYTES, Api, doc_intel_routes
from .test_api_integrations import ADMIN_ROUTES as INTEGRATION_ADMIN_ROUTES
from .test_api_integrations import MONITOR_ROUTES as INTEGRATION_MONITOR_ROUTES

BASE = "/api/doc-intel"
EVERY_PAGE_KEY = sorted(ALL_NAV_KEYS | {"monitoring", "document-import", "integrations"})


def _dependency_calls(dependant) -> list:
    calls = []
    for dep in dependant.dependencies:
        calls.append(dep.call)
        calls.extend(_dependency_calls(dep))
    return calls


def _kwargs(method: str, path: str) -> dict:
    if method == "POST" and path == f"{BASE}/kb-documents":
        return {"data": {"account_id": "3", "queue_keys": ["HALAN"]}, "files": [("files", ("a.pdf", PDF_BYTES, "application/pdf"))]}
    if method == "PATCH":
        return {"json": {"queue_keys": ["HALAN"]}}
    return {}


def test_no_doc_intel_route_depends_on_a_page_permission_check(api: Api):
    role_guards = {guards.admin_only, guards.admin_or_developer}
    for method, path, endpoint in doc_intel_routes(api.app):
        calls = _dependency_calls(get_dependant(path=path, call=endpoint))
        checkers = [c for c in calls if getattr(c, "__name__", "") == "_checker"]
        assert checkers and set(checkers) <= role_guards, (method, path, [c.__qualname__ for c in checkers])
        assert not any("nav_permission" in getattr(c, "__qualname__", "") for c in calls), (method, path)


@pytest.mark.asyncio
async def test_page_permissions_never_open_doc_intel_routes(api: Api, monkeypatch: pytest.MonkeyPatch):
    consulted: list[int] = []

    async def every_page(db, user):
        consulted.append(user.id)
        return set(EVERY_PAGE_KEY)

    monkeypatch.setattr(deps, "_nav_permissions_for_user", every_page)

    # Control: a nav-permission guard WOULD admit an agent holding the "monitoring" page key.
    @api.app.get("/control/nav-guarded")
    async def nav_guarded(user: Annotated[deps.UserContext, Depends(deps.require_roles_or_nav_permission("monitoring"))]):
        return {"ok": True}

    api.app.dependency_overrides[get_db] = lambda: object()
    control = await api.call("GET", "/control/nav-guarded", role=ROLE_AGENT)
    assert control.status_code == 200 and consulted, "the patched page permissions must be in effect"

    consulted.clear()
    for method, path, _ in doc_intel_routes(api.app):
        url = path.replace("{document_id}", "1")
        for role in NON_PLATFORM_ROLES:
            r = await api.call(method, url, role=role, **_kwargs(method, path))
            assert r.status_code == 403, (method, path, role, r.status_code)
        if path.startswith(f"{BASE}/kb-documents") or (method, path) in INTEGRATION_ADMIN_ROUTES:
            r = await api.call(method, url, role=ROLE_DEVELOPER, **_kwargs(method, path))
            assert r.status_code == 403, (method, path, "DEVELOPER")
    assert consulted == [], "doc-intel guards must never look at page permissions"


@pytest.mark.asyncio
async def test_developer_reaches_monitoring_and_status_only(api: Api):
    allowed = {
        (m, p) for m, p, _ in doc_intel_routes(api.app)
        if (await api.call(m, p.replace("{document_id}", "1"), role=ROLE_DEVELOPER, **_kwargs(m, p))).status_code != 403
    }
    assert allowed == {
        ("GET", f"{BASE}/status"),
        ("GET", f"{BASE}/monitoring/health"),
        ("POST", f"{BASE}/monitoring/health/run"),
        ("GET", f"{BASE}/monitoring/events"),
        ("GET", f"{BASE}/monitoring/failures"),
        ("GET", f"{BASE}/monitoring/activity"),
        # Phase 2, read-only: view sources / runs / files and run the connection diagnostics.
        *INTEGRATION_MONITOR_ROUTES,
    }
    # Super Admin passes every guard (the role matrix tests check the actual responses).
    for m, p, _ in doc_intel_routes(api.app):
        r = await api.call(m, p.replace("{document_id}", "1"), role=ROLE_SUPER_ADMIN, **_kwargs(m, p))
        assert r.status_code not in (401, 403), (m, p, r.status_code)
