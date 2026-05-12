"""Story 11.3 AC22 #43-49 — POST /actions/reset-homeowner-password tests."""

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
from open_ems.web.routes.actions import router as actions_router
from open_ems.web.routes.fragments import router as fragments_router

_CSRF_TOKEN = "c" * 64
_VALID_PASSWORD = "BrandNewPasswd1!"


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
async def installer_session_and_homeowner(
    session_repo: SessionRepo,
) -> AsyncIterator[tuple[str, str, str, str]]:
    """Yield (installer_session_token, installer_session_id, homeowner_id,
    homeowner_session_token).
    """
    del session_repo
    await _ensure_event_log_table()
    user_repo = UserRepo(get_connection())
    sess_repo = SessionRepo(get_connection())

    installer_uid = await user_repo.create(
        username="installer1",
        hashed_password=hash_password("password1234"),
        role="installer",
        must_change_password=False,
    )
    homeowner_uid = await user_repo.create(
        username="home1",
        hashed_password=hash_password("oldpassword12"),
        role="homeowner",
        must_change_password=False,
    )

    installer_raw = generate_session_token()
    installer_sid = await sess_repo.create(
        user_id=installer_uid,
        token_hash=hash_token(installer_raw),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF_TOKEN,
    )
    homeowner_raw = generate_session_token()
    await sess_repo.create(
        user_id=homeowner_uid,
        token_hash=hash_token(homeowner_raw),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="h" * 64,
    )

    yield installer_raw, installer_sid, homeowner_uid, homeowner_raw


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(actions_router)
    app.include_router(fragments_router)
    app.add_middleware(CsrfMiddleware)
    return app


def _client() -> TestClient:
    return TestClient(_app(), base_url="https://test", follow_redirects=False)


# ── AC22 #43: success updates hash + sets must_change_password ───────────────


@pytest.mark.asyncio
async def test_reset_homeowner_password_success_updates_hash_and_sets_must_change_password_true(
    installer_session_and_homeowner: tuple[str, str, str, str],
) -> None:
    installer_raw, _sid, homeowner_uid, _homeowner_raw = installer_session_and_homeowner

    resp = _client().post(
        "/actions/reset-homeowner-password",
        cookies={"session": installer_raw},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"new_password": _VALID_PASSWORD},
    )
    assert resp.status_code == 200, resp.text

    row = await UserRepo(get_connection()).get_by_id(homeowner_uid)
    assert row is not None
    assert row["must_change_password"] == 1
    assert verify_password(_VALID_PASSWORD, row["hashed_password"]) is True
    # Old password no longer verifies.
    assert verify_password("oldpassword12", row["hashed_password"]) is False


# ── AC22 #44: invalidates all homeowner sessions ─────────────────────────────


@pytest.mark.asyncio
async def test_reset_homeowner_password_invalidates_all_homeowner_sessions(
    installer_session_and_homeowner: tuple[str, str, str, str],
) -> None:
    installer_raw, _sid, _homeowner_uid, homeowner_raw = installer_session_and_homeowner

    resp = _client().post(
        "/actions/reset-homeowner-password",
        cookies={"session": installer_raw},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"new_password": _VALID_PASSWORD},
    )
    assert resp.status_code == 200

    # Homeowner session no longer resolvable.
    sess_repo = SessionRepo(get_connection())
    assert await sess_repo.get_by_token_hash(hash_token(homeowner_raw)) is None


# ── AC22 #45: does NOT invalidate installer session ──────────────────────────


@pytest.mark.asyncio
async def test_reset_homeowner_password_does_not_invalidate_installer_session(
    installer_session_and_homeowner: tuple[str, str, str, str],
) -> None:
    installer_raw, installer_sid, _homeowner_uid, _homeowner_raw = installer_session_and_homeowner

    resp = _client().post(
        "/actions/reset-homeowner-password",
        cookies={"session": installer_raw},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"new_password": _VALID_PASSWORD},
    )
    assert resp.status_code == 200

    sess_repo = SessionRepo(get_connection())
    installer_row = await sess_repo.get_by_token_hash(hash_token(installer_raw))
    assert installer_row is not None
    assert str(installer_row["id"]) == installer_sid


# ── AC22 #46: emits audit event with exact summary ───────────────────────────


@pytest.mark.asyncio
async def test_reset_homeowner_password_emits_audit_event_with_exact_summary(
    installer_session_and_homeowner: tuple[str, str, str, str],
) -> None:
    installer_raw, _sid, _homeowner_uid, _homeowner_raw = installer_session_and_homeowner

    resp = _client().post(
        "/actions/reset-homeowner-password",
        cookies={"session": installer_raw},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"new_password": _VALID_PASSWORD},
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
    assert "Homeowner credentials reset" in summaries


# ── AC22 #47: rejects when no homeowner exists ───────────────────────────────


@pytest.mark.asyncio
async def test_reset_homeowner_password_rejects_when_no_homeowner_exists(
    session_repo: SessionRepo,
) -> None:
    del session_repo
    await _ensure_event_log_table()
    user_repo = UserRepo(get_connection())
    sess_repo = SessionRepo(get_connection())

    installer_uid = await user_repo.create(
        username="installer-only",
        hashed_password=hash_password("password1234"),
        role="installer",
        must_change_password=False,
    )
    installer_raw = generate_session_token()
    await sess_repo.create(
        user_id=installer_uid,
        token_hash=hash_token(installer_raw),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF_TOKEN,
    )

    resp = _client().post(
        "/actions/reset-homeowner-password",
        cookies={"session": installer_raw},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"new_password": _VALID_PASSWORD},
    )
    assert resp.status_code == 200
    assert "no homeowner account exists" in resp.text.lower()


# ── AC22 #48: rejects short password ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_homeowner_password_rejects_short_password(
    installer_session_and_homeowner: tuple[str, str, str, str],
) -> None:
    installer_raw, _sid, homeowner_uid, _homeowner_raw = installer_session_and_homeowner

    resp = _client().post(
        "/actions/reset-homeowner-password",
        cookies={"session": installer_raw},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"new_password": "shortpass"},
    )
    assert resp.status_code == 200
    assert "12 characters" in resp.text
    # Password unchanged.
    row = await UserRepo(get_connection()).get_by_id(homeowner_uid)
    assert row is not None
    assert verify_password("oldpassword12", row["hashed_password"]) is True


# ── AC22 #49: CSRF token missing returns 403 ─────────────────────────────────


@pytest.mark.asyncio
async def test_reset_homeowner_password_csrf_token_missing_returns_403(
    installer_session_and_homeowner: tuple[str, str, str, str],
) -> None:
    installer_raw, _sid, _homeowner_uid, _homeowner_raw = installer_session_and_homeowner

    resp = _client().post(
        "/actions/reset-homeowner-password",
        cookies={"session": installer_raw},
        data={"new_password": _VALID_PASSWORD},
    )
    assert resp.status_code == 403
