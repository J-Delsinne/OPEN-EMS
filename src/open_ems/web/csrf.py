from __future__ import annotations

import secrets
import urllib.parse
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message

from open_ems.storage.repositories.session_repo import SessionRepo, hash_token

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_CSRF_EXEMPT_PATHS = frozenset({"/login", "/api/stream/state"})

# Must match _COOKIE_NAME in dependencies.py
_COOKIE_NAME = "session"  # must match dependencies.py


def _parse_expires_at(raw_expires: str) -> datetime | None:
    try:
        expires_at = datetime.fromisoformat(raw_expires)
    except ValueError:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at


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
        expires_at = _parse_expires_at(str(session_row["expires_at"]))
        if expires_at is None or datetime.now(UTC) > expires_at:
            return await call_next(request)

        # HTMX path (primary): check header first
        submitted: str | None = request.headers.get("x-csrf-token")

        # Fallback: read from form body for traditional form submits (urlencoded
        # or multipart). The naive ``await request.form()`` here consumed the
        # ASGI receive stream, leaving FastAPI's ``Form()`` parameters empty on
        # the downstream route handler (this surfaced as a real bug on
        # ``/change-password`` — the only non-HTMX CSRF-protected form in v1:
        # the route's ``current_password`` arrived as ``""`` and verification
        # failed with "Current password is incorrect" even when the user
        # typed the right password). Fix: read the raw body once via
        # :meth:`Request.body`, parse it manually for ``csrf_token``, then
        # replace ``request._receive`` with a closure that replays the cached
        # body — so the inner application can re-parse the same body via
        # ``await request.form()``. Resolves the deferred-work entry at
        # ``csrf.py:71-79`` (Story 9.1 deferred → closed by 11.3 bugfix).
        if submitted is None:
            content_type = request.headers.get("content-type", "").lower()
            is_urlencoded = "application/x-www-form-urlencoded" in content_type
            is_multipart = "multipart/form-data" in content_type
            if is_urlencoded or is_multipart:
                raw_body = await request.body()

                async def _replay() -> Message:
                    return {
                        "type": "http.request",
                        "body": raw_body,
                        "more_body": False,
                    }

                # Patch _receive so the downstream app's Form() / request.form()
                # call replays the same body instead of getting an empty stream.
                request._receive = _replay

                if is_urlencoded:
                    # Manual parse — avoids re-invoking request.form() in the
                    # middleware itself, which would mutate FormData caching.
                    try:
                        decoded = raw_body.decode("utf-8")
                    except UnicodeDecodeError:
                        decoded = ""
                    submitted = next(
                        (
                            value
                            for key, value in urllib.parse.parse_qsl(
                                decoded, keep_blank_values=True
                            )
                            if key == "csrf_token"
                        ),
                        None,
                    )
                else:
                    # Multipart: defer to request.form() since manual multipart
                    # parsing is non-trivial. _body is now cached so this is a
                    # read-from-cache, not a stream consume.
                    form_data = await request.form()
                    value = form_data.get("csrf_token")
                    submitted = value if isinstance(value, str) else None

        if submitted is None:
            return Response("CSRF token missing", status_code=403, media_type="text/plain")

        expected = str(session_row["csrf_token"])
        if not secrets.compare_digest(submitted, expected):
            return Response("CSRF token invalid", status_code=403, media_type="text/plain")

        return await call_next(request)
