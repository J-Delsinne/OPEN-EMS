from __future__ import annotations

from collections.abc import Generator

import pytest
import pytest_asyncio
import structlog.testing
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.services import rate_limiter as rl
from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import SessionRepo, hash_token
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.routes.auth import router as auth_router

# ── App fixture (no lifespan — DB already initialised by session_repo fixture) ──


@pytest.fixture
def auth_app(session_repo: SessionRepo) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    return app


@pytest.fixture
def https_client(auth_app: FastAPI) -> TestClient:
    return TestClient(auth_app, base_url="https://test", follow_redirects=False)


@pytest.fixture
def http_client(auth_app: FastAPI) -> TestClient:
    return TestClient(auth_app, base_url="http://test", follow_redirects=False)


@pytest_asyncio.fixture
async def test_user(session_repo: SessionRepo) -> str:
    """Create a test installer user; return user_id."""
    repo = UserRepo(get_connection())
    return await repo.create(
        username="admin",
        hashed_password=hash_password("correcthorse"),
        role="installer",
    )


@pytest.fixture(autouse=True)
def reset_rate_limiter() -> Generator[None, None, None]:
    rl._reset_all()
    yield
    rl._reset_all()


# ── GET /login ────────────────────────────────────────────────────────────────


def test_get_login_returns_200(https_client: TestClient) -> None:
    resp = https_client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_get_login_contains_form(https_client: TestClient) -> None:
    resp = https_client.get("/login")
    assert '<form method="POST"' in resp.text
    assert 'name="username"' in resp.text
    assert 'name="password"' in resp.text


def test_get_login_preserves_next_param(https_client: TestClient) -> None:
    resp = https_client.get("/login?next=/installer/dashboard")
    assert resp.status_code == 200
    assert "/installer/dashboard" in resp.text


# ── POST /login — valid credentials ──────────────────────────────────────────


def test_post_login_valid_redirects(https_client: TestClient, test_user: str) -> None:
    resp = https_client.post("/login", data={"username": "admin", "password": "correcthorse"})
    assert resp.status_code == 303


def test_post_login_sets_session_cookie(https_client: TestClient, test_user: str) -> None:
    resp = https_client.post("/login", data={"username": "admin", "password": "correcthorse"})
    assert "session" in resp.cookies
    cookie_header = resp.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie_header
    assert "Secure" in cookie_header
    assert "samesite=strict" in cookie_header.lower()


def test_post_login_cookie_path_is_root(https_client: TestClient, test_user: str) -> None:
    resp = https_client.post("/login", data={"username": "admin", "password": "correcthorse"})
    assert "Path=/" in resp.headers.get("set-cookie", "")


async def test_post_login_stores_hash_not_raw_token(
    https_client: TestClient, test_user: str
) -> None:
    resp = https_client.post("/login", data={"username": "admin", "password": "correcthorse"})
    raw_cookie = resp.cookies["session"]
    session_repo = SessionRepo(get_connection())
    row = await session_repo.get_by_token_hash(hash_token(raw_cookie))
    assert row is not None
    assert row["token_hash"] != raw_cookie


def test_post_login_redirects_to_next_if_valid(https_client: TestClient, test_user: str) -> None:
    resp = https_client.post(
        "/login?next=/installer/dashboard",
        data={"username": "admin", "password": "correcthorse"},
    )
    assert resp.headers["location"] == "/installer/dashboard"


def test_post_login_ignores_unsafe_next(https_client: TestClient, test_user: str) -> None:
    resp = https_client.post(
        "/login?next=https://evil.com",
        data={"username": "admin", "password": "correcthorse"},
    )
    location = resp.headers["location"]
    assert not location.startswith("https://evil.com")
    assert location.startswith("/")


def test_post_login_ignores_protocol_relative_next(
    https_client: TestClient, test_user: str
) -> None:
    resp = https_client.post(
        "/login?next=//evil.com",
        data={"username": "admin", "password": "correcthorse"},
    )
    assert not resp.headers["location"].startswith("//")


def test_post_login_ignores_backslash_relative_next(
    https_client: TestClient, test_user: str
) -> None:
    resp = https_client.post(
        "/login?next=/\\evil.com",
        data={"username": "admin", "password": "correcthorse"},
    )
    assert not resp.headers["location"].startswith("/\\")


# ── POST /login — invalid credentials ────────────────────────────────────────


def test_post_login_invalid_password_returns_200(https_client: TestClient, test_user: str) -> None:
    resp = https_client.post("/login", data={"username": "admin", "password": "wrongpass"})
    assert resp.status_code == 200


def test_post_login_unknown_user_returns_200(https_client: TestClient) -> None:
    resp = https_client.post("/login", data={"username": "nobody", "password": "anything"})
    assert resp.status_code == 200


def test_post_login_error_message_is_generic(https_client: TestClient, test_user: str) -> None:
    resp = https_client.post("/login", data={"username": "admin", "password": "wrongpass"})
    assert "Invalid username or password" in resp.text


def test_post_login_username_preserved_on_error(https_client: TestClient, test_user: str) -> None:
    resp = https_client.post("/login", data={"username": "admin", "password": "wrongpass"})
    assert 'value="admin"' in resp.text


def test_post_login_no_cookie_on_failure(https_client: TestClient, test_user: str) -> None:
    resp = https_client.post("/login", data={"username": "admin", "password": "wrongpass"})
    assert "session" not in resp.cookies


# ── POST /login — HTTP enforcement ───────────────────────────────────────────


def test_post_login_over_http_redirects_to_https(http_client: TestClient, test_user: str) -> None:
    resp = http_client.post("/login", data={"username": "admin", "password": "correcthorse"})
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://")


def test_post_login_no_cookie_over_http(http_client: TestClient, test_user: str) -> None:
    resp = http_client.post("/login", data={"username": "admin", "password": "correcthorse"})
    assert "session" not in resp.cookies


# ── POST /login — rate limiting ───────────────────────────────────────────────


def test_post_login_rate_limited_after_threshold(https_client: TestClient, test_user: str) -> None:
    for _ in range(rl._MAX_FAILURES):
        https_client.post("/login", data={"username": "admin", "password": "wrong"})
    resp = https_client.post("/login", data={"username": "admin", "password": "correcthorse"})
    # Rate limited — returns 200 with error, not 303 redirect
    assert resp.status_code == 200
    assert "Invalid username or password" in resp.text


# ── POST /login — session fixation prevention ────────────────────────────────


async def test_post_login_clears_old_sessions(
    https_client: TestClient, test_user: str
) -> None:
    resp1 = https_client.post("/login", data={"username": "admin", "password": "correcthorse"})
    token1 = resp1.cookies["session"]

    # Login again — old session should be gone
    https_client.post("/login", data={"username": "admin", "password": "correcthorse"})

    repo = SessionRepo(get_connection())
    row = await repo.get_by_token_hash(hash_token(token1))
    assert row is None


# ── Structured logging events ─────────────────────────────────────────────────


def test_login_success_event_logged(https_client: TestClient, test_user: str) -> None:
    with structlog.testing.capture_logs() as logs:
        https_client.post("/login", data={"username": "admin", "password": "correcthorse"})
    assert any(log.get("event") == "login_success" for log in logs)


def test_login_success_logs_user_id(https_client: TestClient, test_user: str) -> None:
    with structlog.testing.capture_logs() as logs:
        https_client.post("/login", data={"username": "admin", "password": "correcthorse"})
    success_logs = [log for log in logs if log.get("event") == "login_success"]
    assert success_logs
    assert "user_id" in success_logs[0]


def test_login_failed_event_logged(https_client: TestClient, test_user: str) -> None:
    with structlog.testing.capture_logs() as logs:
        https_client.post("/login", data={"username": "admin", "password": "wrongpass"})
    assert any(log.get("event") == "login_failed" for log in logs)


def test_auth_rate_limited_event_logged(https_client: TestClient, test_user: str) -> None:
    for _ in range(rl._MAX_FAILURES):
        https_client.post("/login", data={"username": "admin", "password": "wrong"})
    with structlog.testing.capture_logs() as logs:
        https_client.post("/login", data={"username": "admin", "password": "wrong"})
    assert any(log.get("event") == "auth_rate_limited" for log in logs)
