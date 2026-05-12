"""Story 11.3 — middleware-level enforcement of ``must_change_password``.

A user with ``must_change_password=1`` is redirected to ``/change-password``
on every request except a finite allow-list (the change-password form itself,
login/logout, static assets, health probes). Runs before the route handler
so direct navigation to dashboard routes cannot bypass the gate.

Registered AFTER :class:`CsrfMiddleware` in ``create_app()`` — Starlette
wraps in reverse on inbound, so the last-added middleware runs FIRST on each
request. That ordering is deliberate (AC12): a user who must change their
password gets redirected before CSRF checks waste work on a request that
will never reach the route handler.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp

from open_ems.web.dependencies import _get_session_user_if_any

logger = structlog.get_logger(__name__)

# Story 11.3 AC13 — finite, exhaustive allow-list. Exact-match URL paths that
# bypass the redirect even when ``must_change_password=1``.
_MIDDLEWARE_ALLOW_LIST: Final[frozenset[str]] = frozenset(
    {
        "/change-password",
        "/login",
        "/logout",
        "/health/live",
        "/health/ready",
    }
)

# Story 11.3 AC13 — prefix-match for static assets (htmx.min.js, alpine.min.js,
# open-ems.css are all served from /static/...).
_MIDDLEWARE_ALLOW_PREFIX: Final[str] = "/static/"

_CHANGE_PASSWORD_URL: Final[str] = "/change-password"


_CallNext = Callable[[Request], Awaitable[Response]]


def _path_is_allow_listed(path: str) -> bool:
    """Return True if ``path`` is in the exact-match allow-list OR is a static asset.

    Story 11.3 review-P10: trailing slashes are stripped before the exact-match
    check so proxy/normalization variants like ``/change-password/`` resolve
    correctly. The static-prefix check operates on the raw path because the
    prefix itself ends in ``/``.
    """
    normalized = path.rstrip("/") or "/"
    if normalized in _MIDDLEWARE_ALLOW_LIST:
        return True
    return path.startswith(_MIDDLEWARE_ALLOW_PREFIX)


class MustChangePasswordMiddleware(BaseHTTPMiddleware):
    """Redirects users with ``must_change_password=1`` to ``/change-password``.

    The middleware is a pure guard — it does NOT touch ``last_active_at`` or
    renew the session cookie. Those side-effects happen in the route-level
    dependency (:func:`require_installer` / :func:`require_homeowner`) once
    the request reaches a handler.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: _CallNext) -> Response:
        path = request.url.path

        if _path_is_allow_listed(path):
            return await call_next(request)

        resolved = await _get_session_user_if_any(request)
        if resolved is None:
            # No (valid) session — route-level dependency handles auth.
            return await call_next(request)

        user_row, _session_row = resolved
        if not bool(user_row["must_change_password"]):
            return await call_next(request)

        is_htmx = bool(request.headers.get("hx-request"))
        logger.debug(
            "must_change_password_redirect",
            component="must_change_password_middleware",
            user_id=str(user_row["id"]),
            role=str(user_row["role"]),
            requested_path=path,
            is_htmx=is_htmx,
        )

        if is_htmx:
            # HTMX redirect contract: 200 + HX-Redirect header → browser does
            # a full-page navigation. A 303 alone would be followed for the
            # partial swap, which is the wrong UX here.
            return Response(
                status_code=200,
                headers={"HX-Redirect": _CHANGE_PASSWORD_URL},
            )

        return RedirectResponse(url=_CHANGE_PASSWORD_URL, status_code=303)
