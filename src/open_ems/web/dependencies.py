from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import structlog
from fastapi import Request
from fastapi.exceptions import HTTPException

from open_ems.settings import get_settings
from open_ems.storage.repositories.session_repo import SessionRepo, hash_token
from open_ems.storage.repositories.user_repo import UserRepo

logger = structlog.get_logger(__name__)

_COOKIE_NAME = "session"
_ROLE_HOME: dict[str, str] = {
    "installer": "/installer/dashboard",
    "homeowner": "/homeowner/dashboard",
}


@dataclass
class AuthenticatedUser:
    user_id: str
    username: str
    role: str
    session_id: str
    csrf_token: str


InstallerUser = AuthenticatedUser
HomeownerUser = AuthenticatedUser


def _next_url(request: Request) -> str:
    """Build the `next` redirect value: path + query string, URL-encoded."""
    path = request.url.path
    if request.url.query:
        path += f"?{request.url.query}"
    return quote(path, safe="/:@!$&'()*+,;=")


def _is_htmx_or_api(request: Request) -> bool:
    return bool(request.headers.get("hx-request")) or (
        "application/json" in request.headers.get("accept", "")
    )


async def _resolve_session(request: Request) -> AuthenticatedUser | None:
    raw_token = request.cookies.get(_COOKIE_NAME)
    if not raw_token:
        return None

    session_repo = SessionRepo()
    session_row = await session_repo.get_by_token_hash(hash_token(raw_token))
    if session_row is None:
        return None

    raw_expires = str(session_row["expires_at"])
    expires_at = datetime.fromisoformat(raw_expires)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    now = datetime.now(UTC)

    if now > expires_at:
        await session_repo.delete_by_id(str(session_row["id"]))
        user_row = await UserRepo().get_by_id(str(session_row["user_id"]))
        logger.info(
            "session_expired",
            component="auth",
            user_id=str(session_row["user_id"]),
            role=str(user_row["role"]) if user_row else "unknown",
        )
        request.state.session_expired = True
        return None

    user_row = await UserRepo().get_by_id(str(session_row["user_id"]))
    if user_row is None:
        return None

    role = str(user_row["role"])
    settings = get_settings()

    if role == "installer":
        timeout = timedelta(hours=settings.installer_session_timeout_hours)
    else:
        timeout = timedelta(days=settings.homeowner_session_timeout_days)
    new_expires_at = now + timeout
    try:
        await session_repo.touch(str(session_row["id"]), new_expires_at)
    except ValueError:
        # Concurrent logout/cleanup deleted the session between get_by_token_hash and touch
        return None

    await session_repo.delete_expired_for_user(str(session_row["user_id"]))

    return AuthenticatedUser(
        user_id=str(user_row["id"]),
        username=str(user_row["username"]),
        role=role,
        session_id=str(session_row["id"]),
        csrf_token=str(session_row["csrf_token"]),
    )


async def require_installer(request: Request) -> InstallerUser:
    user = await _resolve_session(request)
    if user is None:
        if _is_htmx_or_api(request):
            raise HTTPException(status_code=401, detail="Authentication required")
        headers: dict[str, str] = {"Location": f"/login?next={_next_url(request)}"}
        if getattr(request.state, "session_expired", False):
            headers["Set-Cookie"] = (
                f"{_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=strict"
            )
        raise HTTPException(status_code=302, headers=headers)
    if user.role != "installer":
        if _is_htmx_or_api(request):
            logger.warning(
                "role_access_denied",
                component="auth",
                role=user.role,
                user_id=user.user_id,
                attempted_route=request.url.path,
            )
            raise HTTPException(status_code=403, detail="Forbidden")
        raise HTTPException(
            status_code=302,
            headers={"Location": _ROLE_HOME.get(user.role, "/")},
        )
    return user


async def require_homeowner(request: Request) -> HomeownerUser:
    user = await _resolve_session(request)
    if user is None:
        if _is_htmx_or_api(request):
            raise HTTPException(status_code=401, detail="Authentication required")
        headers: dict[str, str] = {"Location": f"/login?next={_next_url(request)}"}
        if getattr(request.state, "session_expired", False):
            headers["Set-Cookie"] = (
                f"{_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=strict"
            )
        raise HTTPException(status_code=302, headers=headers)
    if user.role != "homeowner":
        if _is_htmx_or_api(request):
            logger.warning(
                "role_access_denied",
                component="auth",
                role=user.role,
                user_id=user.user_id,
                attempted_route=request.url.path,
            )
            raise HTTPException(status_code=403, detail="Forbidden")
        raise HTTPException(
            status_code=302,
            headers={"Location": _ROLE_HOME.get(user.role, "/")},
        )
    return user
