"""Human-readable HTTP access logs with handler names and user context."""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
import uuid
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from backend.auth.jwt import decode_token
from backend.database import Database
from backend.services.auth_audit import client_ip
from backend.services.error_log import persist_error_log
from backend.services.http_request_log import RequestActor, persist_http_request_log
from backend.services.notifications import notify_error_admins_developers

if TYPE_CHECKING:
    from backend.auth.deps import UserContext

_log = logging.getLogger("aiva.http")

# Keep strong references to fire-and-forget alert tasks so they aren't GC'd mid-flight.
_alert_tasks: set[asyncio.Task] = set()


def _schedule_error_alert(**kwargs) -> None:
    """Send the admin/developer error alert without delaying the error response."""
    try:
        task = asyncio.create_task(notify_error_admins_developers(**kwargs))
    except RuntimeError:
        return  # No running loop (shouldn't happen inside middleware).
    _alert_tasks.add(task)
    task.add_done_callback(_alert_tasks.discard)


def _handler_label(request: Request) -> str:
    route = request.scope.get("route")
    if route is None:
        return "unmatched"
    name = getattr(route, "name", None) or "unknown"
    tags = getattr(route, "tags", None) or []
    tag = tags[0] if tags else None
    if tag and not name.startswith(f"{tag}."):
        return f"{tag}.{name}"
    return str(name)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    if route and getattr(route, "path", None):
        return str(route.path)
    return request.url.path


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    return token or None


def _resolve_actor(request: Request) -> RequestActor:
    user: UserContext | None = getattr(request.state, "user", None)
    if user is not None:
        roles = ",".join(sorted(user.role_names)) if user.role_names else None
        return RequestActor(
            label=(
                f"user_id={user.id} email={user.email} org_id={user.organization_id}"
                + (f" roles={roles}" if roles else "")
            ),
            user_id=user.id,
            user_email=user.email,
            org_id=user.organization_id,
            user_roles=roles,
        )

    token = _bearer_token(request)
    if token:
        try:
            payload = decode_token(token, expected_type="access")
            uid = payload.get("uid")
            email = payload.get("email") or payload.get("sub")
            org = payload.get("org_id")
            parts: list[str] = []
            if uid is not None:
                parts.append(f"user_id={uid}")
            if email:
                parts.append(f"email={email}")
            if org is not None:
                parts.append(f"org_id={org}")
            label = " ".join(parts) if parts else "token_present"
            return RequestActor(
                label=label,
                user_id=int(uid) if uid is not None else None,
                user_email=str(email) if email else None,
                org_id=int(org) if org is not None else None,
            )
        except Exception:
            return RequestActor(label="token_invalid")

    return RequestActor(label="user=anonymous")


def _database(request: Request) -> Database | None:
    return getattr(request.app.state, "database", None)


async def request_logging_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    if request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    if path == "/health":
        return await call_next(request)

    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        # Unhandled error → capture the full traceback for the Errors view, record
        # a 500 access-log row, then re-raise so normal error handling proceeds.
        elapsed_ms = (time.perf_counter() - start) * 1000
        template = _route_template(request)
        actor = _resolve_actor(request)
        ip = client_ip(request) or "-"
        request_id = uuid.uuid4().hex
        db = _database(request)
        await persist_error_log(
            db,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            stack_trace="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            source="HTTP",
            http_method=request.method,
            path=path,
            route_template=template,
            status_code=500,
            request_id=request_id,
            user_id=actor.user_id,
            user_email=actor.user_email,
            org_id=actor.org_id,
            client_ip=ip if ip != "-" else None,
        )
        _schedule_error_alert(
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            stack_trace="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            http_method=request.method,
            path=path,
            route_template=template,
            status_code=500,
            request_id=request_id,
            user_email=actor.user_email,
            organization_id=actor.org_id,
        )
        await persist_http_request_log(
            db,
            http_method=request.method,
            path=path,
            query_string=request.url.query or None,
            handler_name=_handler_label(request),
            route_template=template,
            status_code=500,
            duration_ms=elapsed_ms,
            actor=actor,
            client_ip=ip if ip != "-" else None,
        )
        _log.exception("Unhandled error in %s %s (request_id=%s)", request.method, path, request_id)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000

    handler = _handler_label(request)
    template = _route_template(request)
    query = request.url.query
    path_display = f"{path}?{query}" if query else path
    actor = _resolve_actor(request)
    ip = client_ip(request) or "-"

    _log.info(
        "%s %s | handler=%s route=%s | %s | ip=%s | status=%s %.0fms",
        request.method,
        path_display,
        handler,
        template,
        actor.label,
        ip,
        response.status_code,
        elapsed_ms,
    )

    await persist_http_request_log(
        _database(request),
        http_method=request.method,
        path=path,
        query_string=query or None,
        handler_name=handler,
        route_template=template,
        status_code=response.status_code,
        duration_ms=elapsed_ms,
        actor=actor,
        client_ip=ip if ip != "-" else None,
    )

    return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        return await request_logging_middleware(request, call_next)
