"""Story 11.3 AC22 #31-42 — POST /actions/create-homeowner route tests."""

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
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.csrf import CsrfMiddleware
from open_ems.web.routes.actions import router as actions_router

_CSRF_TOKEN = "c" * 64
_VALID_PASSWORD = "Sup3rSecret1!23"
_TOO_SHORT_PASSWORD = "short"  # 5 chars; below MIN_PASSWORD_LENGTH=12


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
async def installer_session(session_repo: SessionRepo) -> AsyncIterator[str]:
    """Yield an installer's raw session cookie with a known CSRF token."""
    del session_repo
    await _ensure_event_log_table()
    user_id = await UserRepo(get_connection()).create(
        username="installer1",
        hashed_password=hash_password("password1234"),
        role="installer",
        must_change_password=False,
    )
    raw = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF_TOKEN,
    )
    yield raw


@pytest_asyncio.fixture
async def homeowner_session(session_repo: SessionRepo) -> AsyncIterator[str]:
    """Yield a homeowner's raw session cookie (rejected by the route)."""
    del session_repo
    await _ensure_event_log_table()
    user_id = await UserRepo(get_connection()).create(
        username="home1",
        hashed_password=hash_password("password1234"),
        role="homeowner",
        must_change_password=False,
    )
    raw = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF_TOKEN,
    )
    yield raw


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(actions_router)
    app.add_middleware(CsrfMiddleware)
    return app


def _client() -> TestClient:
    return TestClient(_app(), base_url="https://test", follow_redirects=False)


# ── AC22 #31: success creates user with must_change_password=true ─────────────


@pytest.mark.asyncio
async def test_create_homeowner_success_creates_user_with_must_change_password_true(
    installer_session: str,
) -> None:
    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": installer_session},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"username": "newhome", "password": _VALID_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    row = await UserRepo(get_connection()).get_by_username("newhome")
    assert row is not None
    assert row["role"] == "homeowner"
    assert row["must_change_password"] == 1


# ── AC22 #32: audit event emitted with exact summary ─────────────────────────


@pytest.mark.asyncio
async def test_create_homeowner_emits_audit_event_with_exact_summary(
    installer_session: str,
) -> None:
    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": installer_session},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"username": "newhome", "password": _VALID_PASSWORD},
    )
    assert resp.status_code == 200
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
    assert "Homeowner credentials created" in summaries


# ── AC22 #33: rejects when homeowner already exists ──────────────────────────


@pytest.mark.asyncio
async def test_create_homeowner_rejects_when_homeowner_already_exists(
    installer_session: str,
) -> None:
    # Seed an existing homeowner.
    await UserRepo(get_connection()).create(
        username="existing-home",
        hashed_password=hash_password(_VALID_PASSWORD),
        role="homeowner",
    )

    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": installer_session},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"username": "anotherhome", "password": _VALID_PASSWORD},
    )
    assert resp.status_code == 200
    assert "homeowner account already exists" in resp.text.lower()
    # The unwanted user must NOT have been created.
    assert await UserRepo(get_connection()).get_by_username("anotherhome") is None


# ── AC22 #34: rejects empty username ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_homeowner_rejects_empty_username(installer_session: str) -> None:
    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": installer_session},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"username": "   ", "password": _VALID_PASSWORD},
    )
    assert resp.status_code == 200
    assert "1–64 characters" in resp.text


# ── AC22 #35: rejects username over 64 chars ─────────────────────────────────


@pytest.mark.asyncio
async def test_create_homeowner_rejects_username_over_64_chars(
    installer_session: str,
) -> None:
    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": installer_session},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"username": "x" * 65, "password": _VALID_PASSWORD},
    )
    assert resp.status_code == 200
    assert "1–64 characters" in resp.text


# ── AC22 #36: rejects duplicate username with neutral message ────────────────


@pytest.mark.asyncio
async def test_create_homeowner_rejects_duplicate_username_with_neutral_message(
    installer_session: str,
) -> None:
    # "installer1" is the installer fixture's username — taken across roles.
    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": installer_session},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"username": "installer1", "password": _VALID_PASSWORD},
    )
    assert resp.status_code == 200
    # The neutral error message itself must not disclose the existing user's role.
    # Extract the literal text inside the inline error <p role="alert">...</p>.
    import re

    match = re.search(
        r'<p[^>]*role="alert"[^>]*>([^<]*)</p>',
        resp.text,
    )
    assert match is not None, "expected an inline error <p role='alert'>"
    error_text = match.group(1).strip().lower()
    assert "already in use" in error_text
    # The neutral phrasing — no "homeowner" or "installer" role hint.
    assert "homeowner" not in error_text
    assert "installer" not in error_text


# ── AC22 #37: rejects password under 12 chars server-side ────────────────────


@pytest.mark.asyncio
async def test_create_homeowner_rejects_password_under_12_chars_server_side(
    installer_session: str,
) -> None:
    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": installer_session},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"username": "newhome", "password": _TOO_SHORT_PASSWORD},
    )
    assert resp.status_code == 200
    assert "12 characters" in resp.text
    # Must NOT have persisted the user.
    assert await UserRepo(get_connection()).get_by_username("newhome") is None


# ── AC22 #38: rejects password over 72 bytes with byte-specific message ──────


@pytest.mark.asyncio
async def test_create_homeowner_rejects_password_over_72_bytes_with_byte_message(
    installer_session: str,
) -> None:
    # 80-char ASCII password = 80 bytes — exceeds bcrypt's 72-byte ceiling
    # while satisfying the >= 12 char complexity rule.
    long_password = "A" * 80
    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": installer_session},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"username": "newhome", "password": long_password},
    )
    assert resp.status_code == 200
    assert "12–72 bytes" in resp.text


# ── AC22 #39: unauthenticated redirects to login ─────────────────────────────


def test_create_homeowner_unauthenticated_redirects_to_login() -> None:
    resp = _client().post(
        "/actions/create-homeowner",
        data={"username": "x", "password": _VALID_PASSWORD},
    )
    # CSRF middleware short-circuits on "no session cookie"; route's
    # require_installer then 302s to /login.
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]


# ── AC22 #40: homeowner role bounced via require_installer's 302 redirect ────


@pytest.mark.asyncio
async def test_create_homeowner_homeowner_role_redirects_to_homeowner_home(
    homeowner_session: str,
) -> None:
    """Review-D3: ``require_installer`` redirects wrong-role browser requests
    (302) rather than returning 403. The 403 path is reserved for HTMX/API
    callers (see ``dependencies._is_htmx_or_api``). Test name aligned to
    actual behavior; the underlying contract is unchanged."""
    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": homeowner_session},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"username": "x", "password": _VALID_PASSWORD},
    )
    assert resp.status_code == 302
    assert "/homeowner" in resp.headers["location"]


# ── AC22 #41: CSRF token missing returns 403 ─────────────────────────────────


@pytest.mark.asyncio
async def test_create_homeowner_csrf_token_missing_returns_403(
    installer_session: str,
) -> None:
    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": installer_session},
        data={"username": "x", "password": _VALID_PASSWORD},
    )
    assert resp.status_code == 403


# ── AC22 #42: success response includes View-in-event-log link ───────────────


@pytest.mark.asyncio
async def test_create_homeowner_response_includes_view_in_event_log_link(
    installer_session: str,
) -> None:
    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": installer_session},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"username": "newhome", "password": _VALID_PASSWORD},
    )
    assert resp.status_code == 200
    assert "/installer/event-log?type=SYSTEM" in resp.text
