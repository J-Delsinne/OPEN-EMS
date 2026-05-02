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
        _user: InstallerUser = Depends(require_installer),
    ) -> dict[str, str]:
        return {"role": _user.role}

    @app.get("/homeowner/dashboard")
    async def homeowner_route(
        _user: HomeownerUser = Depends(require_homeowner),
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
    response = client.get(
        "/installer/dashboard", cookies={"session": "totally-invalid-token"}
    )
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

