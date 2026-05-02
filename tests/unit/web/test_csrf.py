from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.csrf import CsrfMiddleware

_CSRF_TOKEN = "a" * 64  # fixed known token for tests


# ── App fixture ───────────────────────────────────────────────────────────────


@pytest.fixture
def csrf_app(session_repo: SessionRepo) -> FastAPI:
    """Minimal FastAPI app with CsrfMiddleware and a POST endpoint."""
    app = FastAPI()

    @app.post("/state")
    async def mutate() -> dict[str, str]:
        return {"ok": "true"}

    @app.put("/state")
    async def mutate_put() -> dict[str, str]:
        return {"ok": "true"}

    @app.delete("/state")
    async def mutate_delete() -> dict[str, str]:
        return {"ok": "true"}

    @app.patch("/state")
    async def mutate_patch() -> dict[str, str]:
        return {"ok": "true"}

    @app.get("/state")
    async def read() -> dict[str, str]:
        return {"ok": "true"}

    @app.post("/login")
    async def login_post() -> dict[str, str]:
        return {"ok": "true"}

    @app.get("/api/stream/state")
    async def sse_get() -> dict[str, str]:
        return {"ok": "true"}

    @app.post("/api/stream/state")
    async def sse_post() -> dict[str, str]:
        return {"ok": "true"}

    app.add_middleware(CsrfMiddleware)
    return app


@pytest.fixture
def client(csrf_app: FastAPI) -> TestClient:
    return TestClient(csrf_app, base_url="https://test", follow_redirects=False)


# ── Session helper ────────────────────────────────────────────────────────────


async def _make_session(csrf_token: str = _CSRF_TOKEN) -> str:
    """Create user + session; return raw session token."""
    user_repo = UserRepo(get_connection())
    user_id = await user_repo.create(
        username="csrf_test_user",
        hashed_password=hash_password("secret"),
        role="installer",
    )
    raw = generate_session_token()
    expires_at = datetime.now(UTC) + timedelta(hours=4)
    repo = SessionRepo(get_connection())
    await repo.create(
        user_id=user_id,
        token_hash=hash_token(raw),
        expires_at=expires_at,
        csrf_token=csrf_token,
    )
    return raw


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_get_request_exempt(client: TestClient) -> None:
    """Safe methods bypass CSRF validation."""
    resp = client.get("/state")
    assert resp.status_code == 200


def test_post_no_session_exempt(client: TestClient) -> None:
    """POST with no session cookie passes through (route dependency handles auth)."""
    resp = client.post("/state")
    assert resp.status_code == 200


def test_post_invalid_session_exempt(client: TestClient) -> None:
    """POST with a cookie that doesn't match any session passes through."""
    resp = client.post("/state", cookies={"session": "no-such-token"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_valid_token_header_passes(
    client: TestClient, session_repo: SessionRepo
) -> None:
    """POST with correct X-CSRF-Token header is accepted."""
    raw = await _make_session()
    resp = client.post("/state", cookies={"session": raw}, headers={"X-CSRF-Token": _CSRF_TOKEN})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_missing_token_rejected(client: TestClient, session_repo: SessionRepo) -> None:
    """POST with valid session but no CSRF token is rejected with 403."""
    raw = await _make_session()
    resp = client.post("/state", cookies={"session": raw})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_wrong_token_rejected(client: TestClient, session_repo: SessionRepo) -> None:
    """POST with valid session but wrong X-CSRF-Token is rejected with 403."""
    raw = await _make_session()
    resp = client.post("/state", cookies={"session": raw}, headers={"X-CSRF-Token": "wrong" * 13})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_form_valid_token_passes(client: TestClient, session_repo: SessionRepo) -> None:
    """POST with correct csrf_token in form body is accepted."""
    raw = await _make_session()
    resp = client.post(
        "/state",
        cookies={"session": raw},
        data={"csrf_token": _CSRF_TOKEN},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_form_missing_token_rejected(
    client: TestClient, session_repo: SessionRepo
) -> None:
    """POST with valid session but no csrf_token form field is rejected with 403."""
    raw = await _make_session()
    resp = client.post(
        "/state",
        cookies={"session": raw},
        data={"other_field": "value"},
    )
    assert resp.status_code == 403


def test_login_path_exempt(client: TestClient) -> None:
    """POST to /login is exempt from CSRF validation."""
    resp = client.post("/login")
    assert resp.status_code == 200


def test_sse_path_exempt(client: TestClient) -> None:
    """GET to /api/stream/state is exempt (also a safe method)."""
    resp = client.get("/api/stream/state")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_sse_path_exempt_with_session(
    client: TestClient, session_repo: SessionRepo
) -> None:
    """POST to /api/stream/state with a valid session but no token is exempt via path guard."""
    raw = await _make_session()
    resp = client.post("/api/stream/state", cookies={"session": raw})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_put_missing_token_rejected(client: TestClient, session_repo: SessionRepo) -> None:
    """PUT with valid session but no CSRF token is rejected with 403."""
    raw = await _make_session()
    resp = client.put("/state", cookies={"session": raw})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_missing_token_rejected(client: TestClient, session_repo: SessionRepo) -> None:
    """DELETE with valid session but no CSRF token is rejected with 403."""
    raw = await _make_session()
    resp = client.delete("/state", cookies={"session": raw})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_patch_missing_token_rejected(client: TestClient, session_repo: SessionRepo) -> None:
    """PATCH with valid session but no CSRF token is rejected with 403."""
    raw = await _make_session()
    resp = client.patch("/state", cookies={"session": raw})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_expired_session_bypasses_csrf_check(
    client: TestClient, session_repo: SessionRepo
) -> None:
    """POST with an expired session skips CSRF validation (route dependency handles redirect)."""
    user_repo = UserRepo(get_connection())
    user_id = await user_repo.create(
        username="expired_csrf_user",
        hashed_password=hash_password("secret"),
        role="installer",
    )
    raw = generate_session_token()
    past = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    conn = get_connection()
    session_id = str(__import__("uuid").uuid4())
    await conn.execute(
        "INSERT INTO sessions"
        " (id, user_id, token_hash, created_at, last_active_at, expires_at, csrf_token)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, user_id, hash_token(raw), past, past, past, _CSRF_TOKEN),
    )
    await conn.commit()
    # POST without CSRF token — expired session should bypass CSRF (no 403)
    resp = client.post("/state", cookies={"session": raw})
    assert resp.status_code != 403
