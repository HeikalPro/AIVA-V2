from __future__ import annotations

import hashlib

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from backend.config import get_settings


def _client_ip(request: Request) -> str:
    """Real client IP, honouring X-Forwarded-For when behind a reverse proxy.

    Without this, every request appears to come from the proxy, so all users
    share a single rate-limit bucket.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Left-most entry is the original client; the rest are proxy hops.
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


def rate_limit_key(request: Request) -> str:
    """Rate-limit per authenticated user, falling back to client IP.

    Keying by IP alone breaks two ways at scale: many users behind one NAT
    share a bucket, and behind a reverse proxy *everyone* shares one bucket.
    The bearer token is a stable per-session identifier, so hash it rather than
    decoding the JWT on every request. Unauthenticated traffic (login, signup)
    still keys by IP, which is what brute-force protection needs.
    """
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        if token:
            return "user:" + hashlib.sha256(token.encode()).hexdigest()[:32]
    return "ip:" + _client_ip(request)


limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=[get_settings().rate_limit_default],
)
