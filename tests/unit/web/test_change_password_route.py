"""Story 11.3 AC22 #50-59 — GET/POST /change-password route tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.storage.database import get_connection
from open_ems.storage.repositories.event_log_repo import EventLogRepo
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import (
    UserRepo,
    hash_password,
    verify_password,
)
from open_ems.web.csrf import CsrfMiddleware
from open_ems.web.routes.auth import router as auth_router

_CSRF_TOKEN = "c" * 64
_CURRENT_PASSWORD = "OldPasswordX12"
_NEW_PASSWORD = "FreshNewPass1!"


_CREATE_EVENT_LOG_TABLE = """
    CREATE TABLE IF NOT EXISTS event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schema_version INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        actor TEXT NOT NULL,
        event_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        detail TEXT,
        device_id TEXT,
        config_version TEXT
    )
"""


async def _ensure_event_log_table() -> None:
    conn = get_connection()
    await conn.execute(_CREATE_EVENT_LOG_TABLE)
    await conn.commit()


@pytest_asyncio.fixture
async def authenticated_session(
    session_repo: SessionRepo,
) -> AsyncIterator[tuple[str, str]]:
    """Yield (raw_session_token, user_id) for an installer with the current password."""
    del session_repo
    await _ensure_event_log_table()
    user_repo = UserRepo(get_connection())
    sess_repo = SessionRepo(get_connection())

    user_id = await user_repo.create(
        username="alice",
        hashed_password=hash_password(_CURRENT_PASSWORD),
        role="installer",
        must_change_password=False,
    )
    raw = generate_session_token()
    await sess_repo.create(
        user_id=user_id,
        token_hash=hash_token(raw),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF_TOKEN,
    )
    yield raw, user_id


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    app.add_middleware(CsrfMiddleware)
    return app


def _client() -> TestClient:
    return TestClient(_app(), base_url="https://test", follow_redirects=False)


def _post_data(
    *, current: str = _CURRENT_PASSWORD, new: str = _NEW_PASSWORD, confirm: str | None = None
) -> dict[str, str]:
    """Form body for the change-password POST.

    The CSRF token is passed via X-CSRF-Token header in the test (not in the
    form body) because Starlette's BaseHTTPMiddleware-based CsrfMiddleware
    reads the form via ``await request.form()``, which prevents FastAPI's
    ``Form()`` parameters from re-reading the body (pre-existing issue
    documented in deferred-work.md). Production HTMX requests use the header
    path natively (htmx auto-adds X-CSRF-Token from base.html).
    """
    return {
        "current_password": current,
        "new_password": new,
        "confirm_new_password": confirm if confirm is not None else new,
    }


def _csrf_headers() -> dict[str, str]:
    return {"X-CSRF-Token": _CSRF_TOKEN}


# ── AC22 #50: GET renders form for authenticated user ────────────────────────


@pytest.mark.asyncio
async def test_get_change_password_renders_form_for_authenticated_user(
    authenticated_session: tuple[str, str],
) -> None:
    raw, _uid = authenticated_session
    resp = _client().get("/change-password", cookies={"session": raw})
    assert resp.status_code == 200
    assert 'name="current_password"' in resp.text
    assert 'name="new_password"' in resp.text
    assert 'name="confirm_new_password"' in resp.text


# ── AC22 #51: GET unauthenticated redirects to /login ────────────────────────


@pytest.mark.asyncio
async def test_get_change_password_redirects_unauthenticated_to_login(
    session_repo: SessionRepo,
) -> None:
    """The DB fixture is required because the route now uses
    ``Depends(get_user_repo)``, which constructs ``UserRepo()`` eagerly per
    request even when the handler returns early on missing session."""
    del session_repo
    resp = _client().get("/change-password")
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]


# ── AC22 #52: POST success clears must_change_password ───────────────────────


@pytest.mark.asyncio
async def test_post_change_password_success_clears_must_change_password_flag(
    session_repo: SessionRepo,
) -> None:
    del session_repo
    await _ensure_event_log_table()
    user_repo = UserRepo(get_connection())
    sess_repo = SessionRepo(get_connection())

    # User with must_change_password=True (forced path)
    user_id = await user_repo.create(
        username="forced",
        hashed_password=hash_password(_CURRENT_PASSWORD),
        role="installer",
        must_change_password=True,
    )
    raw = generate_session_token()
    await sess_repo.create(
        user_id=user_id,
        token_hash=hash_token(raw),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF_TOKEN,
    )

    resp = _client().post(
        "/change-password",
        cookies={"session": raw},
        headers=_csrf_headers(),
        data=_post_data(),
    )
    # Success → 303 redirect to /login.
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"

    row = await user_repo.get_by_id(user_id)
    assert row is not None
    assert row["must_change_password"] == 0
    assert verify_password(_NEW_PASSWORD, row["hashed_password"]) is True


# ── AC22 #53: POST success invalidates all user sessions ──────────────────────


@pytest.mark.asyncio
async def test_post_change_password_success_invalidates_all_user_sessions_and_redirects_to_login(
    authenticated_session: tuple[str, str],
) -> None:
    raw, user_id = authenticated_session

    resp = _client().post(
        "/change-password",
        cookies={"session": raw},
        headers=_csrf_headers(),
        data=_post_data(),
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"

    # Session deleted — original token no longer resolves.
    sess_repo = SessionRepo(get_connection())
    assert await sess_repo.get_by_token_hash(hash_token(raw)) is None

    # Cookie cleared on the response.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "session=" in set_cookie
    assert "max-age=0" in set_cookie.lower() or "expires=" in set_cookie.lower()
    del user_id


# ── AC22 #54: wrong current_password ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_change_password_wrong_current_password_renders_error(
    authenticated_session: tuple[str, str],
) -> None:
    raw, user_id = authenticated_session

    resp = _client().post(
        "/change-password",
        cookies={"session": raw},
        headers=_csrf_headers(),
        data=_post_data(current="WRONG-current12"),
    )
    assert resp.status_code == 200
    assert "Current password is incorrect" in resp.text

    # Password unchanged.
    row = await UserRepo(get_connection()).get_by_id(user_id)
    assert row is not None
    assert verify_password(_CURRENT_PASSWORD, row["hashed_password"]) is True


# ── AC22 #55: short new_password ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_change_password_short_new_password_renders_error(
    authenticated_session: tuple[str, str],
) -> None:
    raw, _uid = authenticated_session
    resp = _client().post(
        "/change-password",
        cookies={"session": raw},
        headers=_csrf_headers(),
        data=_post_data(new="short", confirm="short"),
    )
    assert resp.status_code == 200
    assert "12 characters" in resp.text


# ── AC22 #56: mismatched confirm ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_change_password_mismatched_confirm_renders_error(
    authenticated_session: tuple[str, str],
) -> None:
    raw, _uid = authenticated_session
    resp = _client().post(
        "/change-password",
        cookies={"session": raw},
        headers=_csrf_headers(),
        data=_post_data(new="FreshNewPass1!", confirm="MismatchPass1!"),
    )
    assert resp.status_code == 200
    assert "Passwords do not match" in resp.text


# ── AC22 #57: same as current ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_change_password_same_as_current_renders_error(
    authenticated_session: tuple[str, str],
) -> None:
    raw, _uid = authenticated_session
    resp = _client().post(
        "/change-password",
        cookies={"session": raw},
        headers=_csrf_headers(),
        data=_post_data(new=_CURRENT_PASSWORD, confirm=_CURRENT_PASSWORD),
    )
    assert resp.status_code == 200
    assert "must differ" in resp.text


# ── AC22 #58: emits audit event with exact summary ────────────────────────────


@pytest.mark.asyncio
async def test_post_change_password_emits_audit_event(
    authenticated_session: tuple[str, str],
) -> None:
    raw, _uid = authenticated_session
    resp = _client().post(
        "/change-password",
        cookies={"session": raw},
        headers=_csrf_headers(),
        data=_post_data(),
    )
    assert resp.status_code == 303

    rows = await EventLogRepo().list_filtered(
        event_types=frozenset({"SYSTEM"}),
        device_id=None,
        since=None,
        until=None,
        keyword="",
        limit=10,
        offset=0,
    )
    summaries = [r.summary for r in rows]
    assert "Password changed" in summaries


# ── Regression: form-body csrf_token (no header) works end-to-end ────────────


@pytest.mark.asyncio
async def test_post_change_password_with_form_body_csrf_token_succeeds(
    authenticated_session: tuple[str, str],
) -> None:
    """Regression for the production bug filed against Story 11.3:

    A non-HTMX browser form submit on ``/change-password`` sends the CSRF
    token in the FORM BODY (the hidden ``<input name="csrf_token">`` field),
    NOT in the ``X-CSRF-Token`` header. The CSRF middleware previously called
    ``await request.form()`` to read the token, which consumed the ASGI
    receive stream — leaving FastAPI's ``Form()`` parameters as their default
    empty strings on the downstream route. ``verify_password("", admin_hash)``
    returned False, surfacing "Current password is incorrect" even when the
    user typed the password that had just succeeded at ``/login``.

    The other change-password POST tests (#52–#59) all bypass this code path
    by sending the token via ``X-CSRF-Token`` header, so the bug slipped
    through unit coverage.

    This test sends the token via the form body, no header.
    """
    raw, user_id = authenticated_session

    resp = _client().post(
        "/change-password",
        cookies={"session": raw},
        # No X-CSRF-Token header — middleware MUST find the token in the form body.
        data={
            "csrf_token": _CSRF_TOKEN,
            "current_password": _CURRENT_PASSWORD,
            "new_password": _NEW_PASSWORD,
            "confirm_new_password": _NEW_PASSWORD,
        },
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/login"

    # The password actually rotated, proving the route saw the form body.
    row = await UserRepo(get_connection()).get_by_id(user_id)
    assert row is not None
    assert verify_password(_NEW_PASSWORD, row["hashed_password"]) is True


# ── AC22 #59: CSRF token missing returns 403 ─────────────────────────────────


@pytest.mark.asyncio
async def test_post_change_password_csrf_token_missing_returns_403(
    authenticated_session: tuple[str, str],
) -> None:
    raw, _uid = authenticated_session
    # Send data WITHOUT csrf_token form field and WITHOUT X-CSRF-Token header.
    resp = _client().post(
        "/change-password",
        cookies={"session": raw},
        data={
            "current_password": _CURRENT_PASSWORD,
            "new_password": _NEW_PASSWORD,
            "confirm_new_password": _NEW_PASSWORD,
        },
    )
    assert resp.status_code == 403
