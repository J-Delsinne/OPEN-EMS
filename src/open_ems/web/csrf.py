from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from open_ems.storage.repositories.session_repo import SessionRepo, hash_token

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_CSRF_EXEMPT_PATHS = frozenset({"/login", "/api/stream/state"})

# Must match _COOKIE_NAME in dependencies.py
_COOKIE_NAME = "session"  # must match dependencies.py


def generate_csrf_token() -> str:
    """Generate a cryptographically random CSRF token (64-char hex string)."""
    return secrets.token_hex(32)


_CallNext = Callable[[Request], Awaitable[Response]]


class CsrfMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: _CallNext) -> Response:
        # Safe methods are exempt
        if request.method.upper() in _SAFE_METHODS:
            return await call_next(request)

        # Explicitly exempt paths
        if request.url.path in _CSRF_EXEMPT_PATHS:
            return await call_next(request)

        # No session cookie → exempt (route dependency handles auth)
        raw_token = request.cookies.get(_COOKIE_NAME)
        if raw_token is None:
            return await call_next(request)

        # Invalid/unknown session → exempt (route dependency handles auth)
        session_row = await SessionRepo().get_by_token_hash(hash_token(raw_token))
        if session_row is None:
            return await call_next(request)

        # Expired session → skip CSRF, let route dependency handle expiry and redirect
        raw_expires = str(session_row["expires_at"])
        expires_at = datetime.fromisoformat(raw_expires)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if datetime.now(UTC) > expires_at:
            return await call_next(request)

        # HTMX path (primary): check header first
        submitted: str | None = request.headers.get("x-csrf-token")

        # Fallback: read from form body for traditional form submits (urlencoded or multipart)
        if submitted is None:
            content_type = request.headers.get("content-type", "").lower()
            if (
                "application/x-www-form-urlencoded" in content_type
                or "multipart/form-data" in content_type
            ):
                form_data = await request.form()
                value = form_data.get("csrf_token")
                submitted = value if isinstance(value, str) else None

        if submitted is None:
            return Response("CSRF token missing", status_code=403, media_type="text/plain")

        expected = str(session_row["csrf_token"])
        if not secrets.compare_digest(submitted, expected):
            return Response("CSRF token invalid", status_code=403, media_type="text/plain")

        return await call_next(request)
