"""Story 11.3 AC22 #20-30 — MustChangePasswordMiddleware unit tests.

Exercises the middleware against a minimal FastAPI app that:
- has no business logic of its own (the middleware is the system under test)
- registers a handful of representative routes (installer dashboard subpath,
  homeowner dashboard subpath, fragments, actions, allow-list paths)
- mounts a static directory so the ``/static/`` prefix branch is reachable

The middleware decision is the only thing under test. Route-level
dependencies (require_installer / require_homeowner) are NOT used here —
the middleware should redirect before any route handler runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.must_change_password_middleware import MustChangePasswordMiddleware

# ── App fixture ───────────────────────────────────────────────────────────────


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/installer/dashboard")
    async def installer_dashboard() -> dict[str, str]:
        return {"ok": "installer/dashboard"}

    @app.get("/installer/event-log")
    async def installer_event_log() -> dict[str, str]:
        return {"ok": "installer/event-log"}

    @app.get("/homeowner/dashboard")
    async def homeowner_dashboard() -> dict[str, str]:
        return {"ok": "homeowner/dashboard"}

    @app.get("/fragments/homeowner/battery-card")
    async def homeowner_fragment() -> dict[str, str]:
        return {"ok": "fragment"}

    @app.post("/actions/ev-override")
    async def homeowner_action() -> dict[str, str]:
        return {"ok": "action"}

    @app.get("/change-password")
    async def change_password_get() -> dict[str, str]:
        return {"ok": "change-password"}

    @app.post("/change-password")
    async def change_password_post() -> dict[str, str]:
        return {"ok": "change-password-post"}

    @app.post("/login")
    async def login_post() -> dict[str, str]:
        return {"ok": "login"}

    @app.post("/logout")
    async def logout_post() -> dict[str, str]:
        return {"ok": "logout"}

    @app.get("/static/htmx.min.js")
    async def static_asset() -> dict[str, str]:
        return {"ok": "static"}

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"ok": "live"}

    app.add_middleware(MustChangePasswordMiddleware)
    return app


@pytest.fixture
def client(session_repo: SessionRepo) -> TestClient:
    del session_repo  # fixture wires DB; not used directly
    return TestClient(_build_app(), base_url="https://test", follow_redirects=False)


# ── Session helpers ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session_with_flag_true(session_repo: SessionRepo) -> AsyncIterator[str]:
    """Yield a raw session cookie token for a user with must_change_password=1."""
    del session_repo
    user_id = await UserRepo(get_connection()).create(
        username="forced-user",
        hashed_password=hash_password("password1234"),
        role="installer",
        must_change_password=True,
    )
    raw = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="c" * 64,
    )
    yield raw


@pytest_asyncio.fixture
async def session_with_flag_false(session_repo: SessionRepo) -> AsyncIterator[str]:
    """Yield a raw session cookie for a user with must_change_password=0."""
    del session_repo
    user_id = await UserRepo(get_connection()).create(
        username="normal-user",
        hashed_password=hash_password("password1234"),
        role="installer",
        must_change_password=False,
    )
    raw = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="c" * 64,
    )
    yield raw


@pytest_asyncio.fixture
async def expired_session_with_flag_true(session_repo: SessionRepo) -> AsyncIterator[str]:
    """Yield a raw session cookie whose session row is expired."""
    del session_repo
    user_id = await UserRepo(get_connection()).create(
        username="expired-user",
        hashed_password=hash_password("password1234"),
        role="installer",
        must_change_password=True,
    )
    raw = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw),
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        csrf_token="c" * 64,
    )
    yield raw


# ── Tests (AC22 #20-30) ───────────────────────────────────────────────────────


def test_no_session_cookie_middleware_is_noop(client: TestClient) -> None:
    """AC22 #20: a request without a session cookie passes through unchanged."""
    resp = client.get("/installer/dashboard")
    assert resp.status_code == 200
    assert resp.json() == {"ok": "installer/dashboard"}


@pytest.mark.asyncio
async def test_session_with_must_change_password_false_middleware_is_noop(
    client: TestClient, session_with_flag_false: str
) -> None:
    """AC22 #21: middleware is a no-op when ``must_change_password=0``."""
    resp = client.get("/installer/dashboard", cookies={"session": session_with_flag_false})
    assert resp.status_code == 200
    assert resp.json() == {"ok": "installer/dashboard"}


@pytest.mark.asyncio
async def test_session_with_must_change_password_true_redirects_to_change_password(
    client: TestClient, session_with_flag_true: str
) -> None:
    """AC22 #22: a request to a protected page redirects 303 to /change-password."""
    resp = client.get("/installer/dashboard", cookies={"session": session_with_flag_true})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/change-password"


@pytest.mark.asyncio
async def test_change_password_path_itself_allowed_when_flag_is_true(
    client: TestClient, session_with_flag_true: str
) -> None:
    """AC22 #23: the change-password form itself is in the allow-list."""
    resp = client.get("/change-password", cookies={"session": session_with_flag_true})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_login_and_logout_paths_allowed_when_flag_is_true(
    client: TestClient, session_with_flag_true: str
) -> None:
    """AC22 #24: /login and /logout bypass the redirect."""
    # /login (POST) — re-authenticate path
    resp = client.post("/login", cookies={"session": session_with_flag_true})
    assert resp.status_code == 200
    # /logout (POST) — escape hatch
    resp = client.post("/logout", cookies={"session": session_with_flag_true})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_static_assets_allowed_when_flag_is_true(
    client: TestClient, session_with_flag_true: str
) -> None:
    """AC22 #25: /static/* prefix is in the allow-list."""
    resp = client.get("/static/htmx.min.js", cookies={"session": session_with_flag_true})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_htmx_request_returns_200_with_hx_redirect_header(
    client: TestClient, session_with_flag_true: str
) -> None:
    """AC22 #26: HTMX requests get 200 + HX-Redirect, not a 303 (UX contract)."""
    resp = client.get(
        "/fragments/homeowner/battery-card",
        cookies={"session": session_with_flag_true},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert resp.headers["hx-redirect"] == "/change-password"


@pytest.mark.asyncio
async def test_subpath_under_installer_redirects_when_flag_is_true(
    client: TestClient, session_with_flag_true: str
) -> None:
    """AC22 #27: installer sub-pages (with query params) redirect to /change-password.

    The redirect destination is plain ``/change-password`` — query params from
    the originally-requested URL are intentionally dropped.
    """
    resp = client.get(
        "/installer/event-log?type=DECISION",
        cookies={"session": session_with_flag_true},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/change-password"


@pytest.mark.asyncio
async def test_subpath_under_homeowner_redirects_when_flag_is_true(
    client: TestClient, session_with_flag_true: str
) -> None:
    """AC22 #28: homeowner sub-pages redirect to /change-password."""
    resp = client.get("/homeowner/dashboard", cookies={"session": session_with_flag_true})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/change-password"


@pytest.mark.asyncio
async def test_post_actions_path_redirects_when_flag_is_true(
    client: TestClient, session_with_flag_true: str
) -> None:
    """AC22 #29: POST /actions/* redirects even without CSRF.

    Confirms the middleware ordering: MustChangePasswordMiddleware runs FIRST
    inbound; CSRF is never reached on the redirect path.
    """
    resp = client.post("/actions/ev-override", cookies={"session": session_with_flag_true})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/change-password"


@pytest.mark.asyncio
async def test_expired_session_no_redirect(
    client: TestClient, expired_session_with_flag_true: str
) -> None:
    """AC22 #30: an expired session is treated as "no session" — middleware is a no-op.

    The route-level dependency handles the expiry redirect later in the
    request lifecycle; the middleware does not double up.
    """
    resp = client.get(
        "/installer/dashboard",
        cookies={"session": expired_session_with_flag_true},
    )
    # Middleware noop → route handler executes (returns 200 in this test
    # harness because no require_installer dependency is wired).
    assert resp.status_code == 200
