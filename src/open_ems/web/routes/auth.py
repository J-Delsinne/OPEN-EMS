from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from open_ems.services import rate_limiter
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import get_settings
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import (
    PasswordTooLongError,
    UserRepo,
    hash_password,
    validate_password_complexity,
    verify_password,
)
from open_ems.web.csrf import generate_csrf_token
from open_ems.web.dependencies import (
    _is_htmx_or_api,
    _resolve_session,
    get_observability_service,
    get_session_repo,
    get_user_repo,
)
from open_ems.web.state_serialization import build_change_password_page_context

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


def _unauth_change_password_response(request: Request, *, post: bool) -> Response:
    """Story 11.3 review-P5/P11 — branch on HTMX vs browser for unauth requests.

    HTMX/API clients get a 401 per AC15 step 1; browsers get a redirect to the
    login page. POST handlers use 303 so the next request is a GET (avoids the
    browser re-POSTing form data); GET handlers stay on 302.
    """
    if _is_htmx_or_api(request):
        return Response(status_code=401)
    status = 303 if post else 302
    return RedirectResponse(url="/login?next=/change-password", status_code=status)


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request,
    user_repo: UserRepo = Depends(get_user_repo),  # noqa: B008
) -> Response:
    """Story 11.3 AC14 — GET /change-password.

    Renders the change-password form for the authenticated session (any role).
    Unauthenticated requests redirect to ``/login?next=/change-password`` (or
    401 for HTMX). Story 2.1's forced-change UX has been latent since
    2026-05-02; this is the first surface that enforces it.
    """
    user = await _resolve_session(request)
    if user is None:
        return _unauth_change_password_response(request, post=False)
    user_row = await user_repo.get_by_id(user.user_id)
    if user_row is None:
        return _unauth_change_password_response(request, post=False)
    is_forced = bool(user_row["must_change_password"])
    context = build_change_password_page_context(
        username=user.username,
        csrf_token=user.csrf_token,
        is_forced=is_forced,
        error_message=None,
    )
    return _templates.TemplateResponse(request, "change_password.html", context)


def _render_change_password_error(
    request: Request,
    *,
    username: str,
    csrf_token: str,
    is_forced: bool,
    error_message: str,
) -> HTMLResponse:
    context = build_change_password_page_context(
        username=username,
        csrf_token=csrf_token,
        is_forced=is_forced,
        error_message=error_message,
    )
    return _templates.TemplateResponse(request, "change_password.html", context, status_code=200)


@router.post("/change-password")
async def change_password_submit(
    request: Request,
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    confirm_new_password: Annotated[str, Form()] = "",
    user_repo: UserRepo = Depends(get_user_repo),  # noqa: B008
    session_repo: SessionRepo = Depends(get_session_repo),  # noqa: B008
    observability: ObservabilityService = Depends(get_observability_service),  # noqa: B008
) -> Response:
    """Story 11.3 AC15 — POST /change-password.

    Validates current_password, new-password complexity, confirm-match, and
    differs-from-current; on success updates the hash + clears the flag,
    invalidates all sessions for the user, clears the cookie, and redirects
    to ``/login`` so the user re-authenticates with the new credentials.
    """
    user = await _resolve_session(request)
    if user is None:
        return _unauth_change_password_response(request, post=True)

    user_row = await user_repo.get_by_id(user.user_id)
    if user_row is None:
        return _unauth_change_password_response(request, post=True)
    is_forced = bool(user_row["must_change_password"])

    # Step 2 — current_password verifies. Debug-level structured log captures
    # the byte length of the submitted current_password (NOT the value) so a
    # recurrence of the body-consumption bug (where the field arrives empty
    # because middleware consumed the ASGI stream) is trivially diagnosable
    # from logs: a byte-length of 0 paired with a wrong-password failure is
    # the smoking gun.
    if not verify_password(current_password, str(user_row["hashed_password"])):
        logger.debug(
            "change_password_current_verify_failed",
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            is_forced=is_forced,
            current_password_byte_length=len(current_password.encode("utf-8")),
            component="auth",
        )
        return _render_change_password_error(
            request,
            username=user.username,
            csrf_token=user.csrf_token,
            is_forced=is_forced,
            error_message="Current password is incorrect.",
        )

    # Step 3 — complexity
    complexity_error = validate_password_complexity(new_password)
    if complexity_error is not None:
        return _render_change_password_error(
            request,
            username=user.username,
            csrf_token=user.csrf_token,
            is_forced=is_forced,
            error_message=complexity_error,
        )

    # Step 4 — confirm matches
    if new_password != confirm_new_password:
        return _render_change_password_error(
            request,
            username=user.username,
            csrf_token=user.csrf_token,
            is_forced=is_forced,
            error_message="Passwords do not match.",
        )

    # Step 5 — new must differ from current
    if verify_password(new_password, str(user_row["hashed_password"])):
        return _render_change_password_error(
            request,
            username=user.username,
            csrf_token=user.csrf_token,
            is_forced=is_forced,
            error_message="New password must differ from the current password.",
        )

    # Step 6 — bcrypt 72-byte ceiling (PasswordTooLongError narrows the catch)
    try:
        new_hash = hash_password(new_password)
    except PasswordTooLongError:
        return _render_change_password_error(
            request,
            username=user.username,
            csrf_token=user.csrf_token,
            is_forced=is_forced,
            error_message="Password is too long. Use 12–72 bytes.",
        )

    # Step 7 — persist + clear flag
    await user_repo.update_password(user.user_id, new_hash, must_change_password=False)

    # Step 8 — invalidate all sessions for this user (Story 2.2 contract)
    await session_repo.delete_all_for_user(user.user_id)

    # Step 9 — audit + structured log. Role is constrained to installer|homeowner
    # by the users-table CHECK constraint; pass it through directly per
    # AC15 step 9 + Resolved decision #7.
    await observability.audit(
        actor=user.role,
        event_type="SYSTEM",
        summary="Password changed",
    )
    logger.info(
        "password_changed",
        user_id=user.user_id,
        role=user.role,
        component="auth",
    )

    # Step 10 — clear cookie + redirect to /login
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(
        key=_COOKIE_NAME,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    raw_token = request.cookies.get(_COOKIE_NAME)
    if raw_token:
        session_repo = SessionRepo()
        session_row = await session_repo.get_by_token_hash(hash_token(raw_token))
        if session_row is not None:
            user_id = str(session_row["user_id"])
            session_id = str(session_row["id"])
            user_row = await UserRepo().get_by_id(user_id)
            role = str(user_row["role"]) if user_row else "unknown"
            await session_repo.delete_by_id(session_id)
            logger.info(
                "logout",
                component="auth",
                user_id=user_id,
                role=role,
            )
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(
        key=_COOKIE_NAME,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response
