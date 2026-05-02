from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import structlog.testing
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.dependencies import (
    HomeownerUser,
    InstallerUser,
    require_homeowner,
    require_installer,
)

# ── Test app fixture ─────────────────────────────────────────────────────────


@pytest.fixture
def protected_app(session_repo: SessionRepo) -> FastAPI:
    app = FastAPI()

    @app.get("/installer/dashboard")
    async def installer_route(
        _user: InstallerUser = Depends(require_installer),  # noqa: B008
    ) -> dict[str, str]:
        return {"role": _user.role}

    @app.get("/homeowner/dashboard")
    async def homeowner_route(
        _user: HomeownerUser = Depends(require_homeowner),  # noqa: B008
    ) -> dict[str, str]:
        return {"role": _user.role}

    return app


@pytest.fixture
def client(protected_app: FastAPI) -> TestClient:
    return TestClient(protected_app, base_url="https://test", follow_redirects=False)


# ── Session helpers ───────────────────────────────────────────────────────────


async def _create_session(role: str) -> str:
    """Create user + session in the DB; return raw token for cookie."""
    user_repo = UserRepo(get_connection())
    user_id = await user_repo.create(
        username=f"{role}_user",
        hashed_password=hash_password("secret"),
        role=role,
    )
    raw_token = generate_session_token()
    expires_at = datetime.now(UTC) + timedelta(hours=4)
    repo = SessionRepo(get_connection())
    await repo.create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
        csrf_token="test-csrf-token",
    )
    return raw_token


@pytest_asyncio.fixture
async def installer_token(session_repo: SessionRepo) -> str:
    return await _create_session("installer")


@pytest_asyncio.fixture
async def homeowner_token(session_repo: SessionRepo) -> str:
    return await _create_session("homeowner")


# ── require_installer tests ──────────────────────────────────────────────────


def test_require_installer_no_cookie_browser_redirects_to_login(
    client: TestClient,
) -> None:
    response = client.get("/installer/dashboard")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/login?next=")


def test_require_installer_no_cookie_htmx_returns_401(
    client: TestClient,
) -> None:
    response = client.get("/installer/dashboard", headers={"HX-Request": "true"})
    assert response.status_code == 401


def test_require_installer_invalid_token_browser_redirects_to_login(
    client: TestClient,
) -> None:
    response = client.get("/installer/dashboard", cookies={"session": "totally-invalid-token"})
    assert response.status_code == 302
    assert "/login" in response.headers["location"]


def test_require_installer_valid_installer_session_grants_access(
    client: TestClient,
    installer_token: str,
) -> None:
    response = client.get("/installer/dashboard", cookies={"session": installer_token})
    assert response.status_code == 200
    assert response.json() == {"role": "installer"}


def test_require_installer_homeowner_session_browser_redirects_to_homeowner_home(
    client: TestClient,
    homeowner_token: str,
) -> None:
    response = client.get("/installer/dashboard", cookies={"session": homeowner_token})
    assert response.status_code == 302
    assert response.headers["location"] == "/homeowner/dashboard"


def test_require_installer_homeowner_session_htmx_returns_403(
    client: TestClient,
    homeowner_token: str,
) -> None:
    response = client.get(
        "/installer/dashboard",
        cookies={"session": homeowner_token},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 403


def test_require_installer_role_denied_event_logged(
    client: TestClient,
    homeowner_token: str,
) -> None:
    with structlog.testing.capture_logs() as logs:
        client.get(
            "/installer/dashboard",
            cookies={"session": homeowner_token},
            headers={"HX-Request": "true"},
        )
    role_events = [e for e in logs if e.get("event") == "role_access_denied"]
    assert len(role_events) == 1
    event = role_events[0]
    assert event["component"] == "auth"
    assert event["role"] == "homeowner"
    assert "user_id" in event
    assert "attempted_route" in event


# ── require_homeowner tests ──────────────────────────────────────────────────


def test_require_homeowner_valid_homeowner_session_grants_access(
    client: TestClient,
    homeowner_token: str,
) -> None:
    response = client.get("/homeowner/dashboard", cookies={"session": homeowner_token})
    assert response.status_code == 200
    assert response.json() == {"role": "homeowner"}


def test_require_homeowner_installer_session_browser_redirects_to_installer_home(
    client: TestClient,
    installer_token: str,
) -> None:
    response = client.get("/homeowner/dashboard", cookies={"session": installer_token})
    assert response.status_code == 302
    assert response.headers["location"] == "/installer/dashboard"


def test_require_homeowner_installer_session_htmx_returns_403(
    client: TestClient,
    installer_token: str,
) -> None:
    response = client.get(
        "/homeowner/dashboard",
        cookies={"session": installer_token},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 403


# ── Expiry enforcement tests ──────────────────────────────────────────────────


async def _create_expired_session(role: str) -> str:
    """Create user + expired session in the DB; return raw token."""
    user_repo = UserRepo(get_connection())
    user_id = await user_repo.create(
        username=f"{role}_expired_user",
        hashed_password=hash_password("secret"),
        role=role,
    )
    raw_token = generate_session_token()
    past = datetime.now(UTC) - timedelta(hours=5)
    conn = get_connection()
    session_id = str(__import__("uuid").uuid4())
    await conn.execute(
        "INSERT INTO sessions"
        " (id, user_id, token_hash, created_at, last_active_at, expires_at, csrf_token)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            user_id,
            hash_token(raw_token),
            past.isoformat(),
            past.isoformat(),
            past.isoformat(),
            "dummy_csrf",
        ),
    )
    await conn.commit()
    return raw_token


def test_expired_session_returns_redirect(client: TestClient, session_repo: SessionRepo) -> None:
    import asyncio

    raw_token = asyncio.get_event_loop().run_until_complete(_create_expired_session("installer"))
    response = client.get("/installer/dashboard", cookies={"session": raw_token})
    assert response.status_code == 302
    assert "/login" in response.headers["location"]


async def test_expired_session_is_deleted(client: TestClient, session_repo: SessionRepo) -> None:
    raw_token = await _create_expired_session("installer")
    client.get("/installer/dashboard", cookies={"session": raw_token})
    repo = SessionRepo(get_connection())
    row = await repo.get_by_token_hash(hash_token(raw_token))
    assert row is None


async def test_expired_session_logs_event(client: TestClient, session_repo: SessionRepo) -> None:
    import structlog.testing

    raw_token = await _create_expired_session("installer")
    with structlog.testing.capture_logs() as logs:
        client.get("/installer/dashboard", cookies={"session": raw_token})
    assert any(log.get("event") == "session_expired" for log in logs)


async def test_valid_session_updates_expires_at(
    client: TestClient, session_repo: SessionRepo
) -> None:
    raw_token = await _create_session("installer")
    repo = SessionRepo(get_connection())
    row_before = await repo.get_by_token_hash(hash_token(raw_token))
    assert row_before is not None
    old_expires = row_before["expires_at"]

    client.get("/installer/dashboard", cookies={"session": raw_token})

    row_after = await repo.get_by_token_hash(hash_token(raw_token))
    assert row_after is not None
    assert row_after["expires_at"] > old_expires


async def test_valid_session_updates_last_active_at(
    client: TestClient, session_repo: SessionRepo
) -> None:
    raw_token = await _create_session("installer")
    repo = SessionRepo(get_connection())
    row_before = await repo.get_by_token_hash(hash_token(raw_token))
    assert row_before is not None
    old_last_active = row_before["last_active_at"]

    client.get("/installer/dashboard", cookies={"session": raw_token})

    row_after = await repo.get_by_token_hash(hash_token(raw_token))
    assert row_after is not None
    assert row_after["last_active_at"] >= old_last_active


async def test_expired_sessions_for_user_pruned_on_valid_request(
    client: TestClient, session_repo: SessionRepo
) -> None:
    """On a valid request, other expired sessions for that user are pruned."""
    user_repo = UserRepo(get_connection())
    user_id = await user_repo.create(
        username="pruning_user",
        hashed_password=hash_password("secret"),
        role="installer",
    )
    # Two expired sessions
    past = datetime.now(UTC) - timedelta(hours=5)
    conn = get_connection()
    for _i in range(2):
        sid = str(__import__("uuid").uuid4())
        th = hash_token(generate_session_token())
        await conn.execute(
            "INSERT INTO sessions"
            " (id, user_id, token_hash, created_at, last_active_at, expires_at, csrf_token)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, user_id, th, past.isoformat(), past.isoformat(), past.isoformat(), "dummy"),
        )
    await conn.commit()

    # One valid session
    raw_token = generate_session_token()
    future = datetime.now(UTC) + timedelta(hours=4)
    await session_repo.create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=future,
        csrf_token="valid_csrf",
    )

    client.get("/installer/dashboard", cookies={"session": raw_token})

    # The two expired sessions should be deleted
    async with conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE user_id = ? AND expires_at < ?",
        (user_id, datetime.now(UTC).isoformat()),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0


def test_expired_session_clears_session_cookie(
    client: TestClient, session_repo: SessionRepo
) -> None:
    import asyncio

    raw_token = asyncio.get_event_loop().run_until_complete(_create_expired_session("installer"))
    response = client.get("/installer/dashboard", cookies={"session": raw_token})
    assert response.status_code == 302
    set_cookie = response.headers.get("set-cookie", "")
    assert "session=" in set_cookie
    assert "Max-Age=0" in set_cookie


async def test_touch_race_returns_redirect(client: TestClient, session_repo: SessionRepo) -> None:
    """If touch() raises ValueError (concurrent delete), treat as unauthenticated."""
    from unittest.mock import AsyncMock, patch

    raw_token = await _create_session("installer")
    with patch.object(SessionRepo, "touch", new_callable=AsyncMock, side_effect=ValueError):
        response = client.get("/installer/dashboard", cookies={"session": raw_token})
    assert response.status_code == 302
    assert "/login" in response.headers["location"]
