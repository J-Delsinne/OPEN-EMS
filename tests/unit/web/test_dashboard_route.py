"""Story 10.1 — homeowner dashboard route + shell template (AC10 / AC16 / AC17)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core import StateStore
from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.routes.homeowner import router as homeowner_router


async def _create_session(role: str) -> str:
    user_id = await UserRepo(get_connection()).create(
        username=f"{role}_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role=role,
    )
    raw_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="test-csrf",
    )
    return raw_token


def _app_with_store(store: StateStore) -> FastAPI:
    app = FastAPI()
    app.state.state_store = store
    app.include_router(homeowner_router)
    return app


async def test_homeowner_dashboard_redirects_installer_to_their_home(
    session_repo: SessionRepo,
) -> None:
    """Installers hitting /homeowner/dashboard are redirected to their role home
    via the require_homeowner dependency. Matches the pre-existing pattern used
    by other role-gated routes."""
    store = StateStore(system_clock_status="valid")
    raw_token = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/homeowner/dashboard", cookies={"session": raw_token})
    assert response.status_code == 302


async def test_homeowner_dashboard_redirects_unauthenticated_to_login(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/homeowner/dashboard")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/login")


async def test_homeowner_dashboard_shell_renders_five_htmx_bound_sections(
    session_repo: SessionRepo,
) -> None:
    """Story 10.2 AC15: shell template contains headline + battery + solar + grid
    + EV HTMX-bound sections (10.1 had 4; 10.2 re-adds EV with the Alpine.js
    override factory)."""
    store = StateStore(system_clock_status="valid")
    raw_token = await _create_session("homeowner")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/homeowner/dashboard", cookies={"session": raw_token})

    assert response.status_code == 200
    body = response.text
    # Each of the five primary slots is HTMX-bound with the expected URL.
    assert 'hx-get="/fragments/homeowner/status-headline"' in body
    assert 'hx-get="/fragments/homeowner/battery-card"' in body
    assert 'hx-get="/fragments/homeowner/solar-card"' in body
    assert 'hx-get="/fragments/homeowner/grid-card"' in body
    assert 'hx-get="/fragments/homeowner/ev-card"' in body
    # Each slot polls every 10s.
    assert body.count('hx-trigger="load, every 10s"') == 5
    # CSS link is wired through base.html (AC19 Path B / Story 10.1).
    assert "/static/open-ems.css" in body
    # Story 10.2 AC15 — Alpine.js bundle linked and evOverride factory registered.
    assert "/static/alpine.min.js" in body
    assert "Alpine.data('evOverride'" in body or 'Alpine.data("evOverride"' in body


async def test_homeowner_dashboard_route_responds_quickly(session_repo: SessionRepo) -> None:
    """AC16: the shell route returns under 200ms on a developer-class machine.

    Generous ceiling — the route is template-only with no DB I/O.
    """
    store = StateStore(system_clock_status="valid")
    raw_token = await _create_session("homeowner")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    # Warm the test client once.
    client.get("/homeowner/dashboard", cookies={"session": raw_token})

    durations_ms = []
    for _ in range(10):
        start = time.perf_counter()
        response = client.get("/homeowner/dashboard", cookies={"session": raw_token})
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        assert response.status_code == 200
        durations_ms.append(elapsed_ms)

    average_ms = sum(durations_ms) / len(durations_ms)
    assert average_ms < 200.0, (
        f"homeowner dashboard shell route average {average_ms:.1f}ms exceeded 200ms ceiling"
    )
