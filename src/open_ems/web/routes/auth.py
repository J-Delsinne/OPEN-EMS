from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from open_ems.services import rate_limiter
from open_ems.settings import get_settings
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, verify_password
from open_ems.web.csrf import generate_csrf_token

logger = structlog.get_logger(__name__)

router = APIRouter()

_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_COOKIE_NAME = "session"
_ROLE_HOME: dict[str, str] = {
    "installer": "/installer/dashboard",
    "homeowner": "/homeowner/dashboard",
}


def _safe_next(next_url: str | None) -> str | None:
    """Validate next URL to prevent open redirect. Returns None if unsafe."""
    if next_url is None:
        return None
    if not next_url.startswith("/"):
        return None
    # Block protocol-relative (//) and backslash-relative (/\ or /%5C) URLs
    second = next_url[1:2]
    if second in ("/", "\\") or next_url[1:5].lower().startswith("%5c"):
        return None
    return next_url


def _render_login(
    request: Request,
    *,
    next: str | None,
    username: str,
    error: str,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": error, "username": username},
        status_code=200,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: str | None = None,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": None, "username": ""},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: str | None = None,
) -> Response:
    settings = get_settings()

    ip = request.client.host if request.client else "unknown"

    # AC3: Refuse login over HTTP.
    # Only trust X-Forwarded-Proto from configured trusted proxy IPs (TRUSTED_PROXY_IPS).
    # Normalise multi-value header to the first comma-separated segment.
    raw_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if raw_proto and ip in settings.trusted_proxy_ips:
        scheme = raw_proto
    else:
        scheme = request.url.scheme
    if scheme != "https":
        https_url = str(request.url.replace(scheme="https"))
        return RedirectResponse(url=https_url, status_code=302)

    # AC5: Rate limit check before credential verification (prevents timing oracle).
    # Rate limiting is by username only — per-IP limiting is unreliable behind a proxy.
    if rate_limiter.is_rate_limited(username=username):
        logger.warning(
            "auth_rate_limited",
            component="auth",
            source_ip=ip,
            username=username,
        )
        return _render_login(
            request, next=next, username=username, error="Invalid username or password"
        )

    # AC2/AC4: Credential verification
    user_row = await UserRepo().get_by_username(username)
    if user_row is None or not verify_password(password, user_row["hashed_password"]):
        rate_limiter.record_failure(username=username)
        logger.info(
            "login_failed",
            component="auth",
            source_ip=ip,
        )
        return _render_login(
            request, next=next, username=username, error="Invalid username or password"
        )

    user_id: str = user_row["id"]
    role: str = user_row["role"]

    # Compute session_lifetime once — used for both expires_at and cookie max_age
    # to avoid clock-skew between the two datetime.now(UTC) calls.
    if role == "installer":
        session_lifetime = timedelta(hours=settings.installer_session_timeout_hours)
    else:
        session_lifetime = timedelta(days=settings.homeowner_session_timeout_days)
    expires_at = datetime.now(UTC) + session_lifetime

    session_repo = SessionRepo()

    # AC2: Invalidate all existing sessions for this user before creating new one.
    # NOTE: Story 2.5 will relax this to allow multiple concurrent sessions per user.
    await session_repo.delete_all_for_user(user_id)

    raw_token = generate_session_token()
    token_hash_value = hash_token(raw_token)
    csrf_tok = generate_csrf_token()
    await session_repo.create(
        user_id=user_id,
        token_hash=token_hash_value,
        expires_at=expires_at,
        csrf_token=csrf_tok,
    )

    rate_limiter.reset_for_key(username=username)

    logger.info(
        "login_success",
        component="auth",
        user_id=user_id,
        role=role,
    )

    redirect_to = _safe_next(next) or _ROLE_HOME.get(role, "/")
    response = RedirectResponse(url=redirect_to, status_code=303)
    max_age = int(session_lifetime.total_seconds())
    response.set_cookie(
        key=_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
        max_age=max_age,
    )
    return response
