"""Unit tests for POST /actions/set-strategy (Story 10.3 AC14 #12–#21)."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core import EnergyStrategy, StateStore, SystemOperatingMode
from open_ems.services.audit_log import ObservabilityService
from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.csrf import CsrfMiddleware
from open_ems.web.routes.actions import router as actions_router
from open_ems.web.routes.fragments import router as fragments_router


async def _create_session(role: str) -> tuple[str, str]:
    """Returns (session_token, csrf_token)."""
    user_id = await UserRepo(get_connection()).create(
        username=f"{role}_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role=role,
    )
    raw_token = generate_session_token()
    csrf = "test-csrf"
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=csrf,
    )
    return raw_token, csrf


def _build_app(
    *,
    store: StateStore,
    observability: ObservabilityService | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.state_store = store
    app.state.observability = observability or AsyncMock(spec=ObservabilityService)
    app.include_router(actions_router)
    # Headline fragment is rendered by the route's success path — include the
    # fragments router so /actions/set-strategy can return a headline render
    # OR a failure fragment (template paths resolved by the same Jinja env).
    app.include_router(fragments_router)
    app.add_middleware(CsrfMiddleware)
    return app


# ---------------------------------------------------------------------------
# AC14 #12 — happy path: minimize_cost updates snapshot + returns headline
# ---------------------------------------------------------------------------


async def test_post_set_strategy_minimize_cost_updates_snapshot_and_returns_headline_fragment(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(
        system_clock_status="valid",
        operating_mode=SystemOperatingMode.normal,
        active_strategy=EnergyStrategy.maximize_self_consumption,
    )
    app = _build_app(store=store)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post(
            "/actions/set-strategy",
            data={"strategy": "minimize_cost"},
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    # State store updated.
    assert store.get_snapshot().active_strategy is EnergyStrategy.minimize_cost
    # Response is the headline fragment with the new strategy.
    assert "Your home is running on solar · Minimize Cost" in resp.text
    assert 'data-active-strategy="minimize_cost"' in resp.text


# ---------------------------------------------------------------------------
# AC14 #13 — idempotent: same strategy → 200, no audit, no mutation
# ---------------------------------------------------------------------------


async def test_post_set_strategy_idempotent_when_strategy_unchanged(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(
        system_clock_status="valid",
        active_strategy=EnergyStrategy.minimize_cost,
    )
    observability = AsyncMock(spec=ObservabilityService)
    app = _build_app(store=store, observability=observability)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post(
            "/actions/set-strategy",
            data={"strategy": "minimize_cost"},  # SAME as current
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )
    assert resp.status_code == 200
    # Snapshot unchanged.
    assert store.get_snapshot().active_strategy is EnergyStrategy.minimize_cost
    # NO audit row emitted on same-strategy no-op (AC3 contract).
    observability.audit.assert_not_called()


# ---------------------------------------------------------------------------
# AC14 #14 — invalid value: failure fragment, no mutation
# ---------------------------------------------------------------------------


async def test_post_set_strategy_invalid_value_returns_failure_fragment(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(
        system_clock_status="valid",
        active_strategy=EnergyStrategy.maximize_self_consumption,
    )
    app = _build_app(store=store)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post(
            "/actions/set-strategy",
            data={"strategy": "does_not_exist"},
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )
    # 200 (HTMX-friendly), with the failure fragment as response body.
    assert resp.status_code == 200
    assert "Strategy update failed. Your previous setting is still active." in resp.text
    assert 'data-strategy-update-failed="true"' in resp.text
    # State unchanged.
    assert store.get_snapshot().active_strategy is EnergyStrategy.maximize_self_consumption


# ---------------------------------------------------------------------------
# AC14 #15 — audit row emitted on actual change
# ---------------------------------------------------------------------------


async def test_post_set_strategy_emits_homeowner_strategy_changed_audit(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(
        system_clock_status="valid",
        active_strategy=EnergyStrategy.maximize_self_consumption,
    )
    observability = AsyncMock(spec=ObservabilityService)
    app = _build_app(store=store, observability=observability)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post(
            "/actions/set-strategy",
            data={"strategy": "prioritize_ev"},
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )
    assert resp.status_code == 200
    observability.audit.assert_called_once()
    call_kwargs = observability.audit.call_args.kwargs
    assert call_kwargs["actor"] == "homeowner"
    detail = call_kwargs["detail"]
    assert detail["event"] == "homeowner_strategy_changed"
    assert detail["previous_strategy"] == "maximize_self_consumption"
    assert detail["new_strategy"] == "prioritize_ev"
    assert "user_id" in detail


# ---------------------------------------------------------------------------
# AC14 #16 — HTMX unauth → 401 (NOT 302) with WWW-Authenticate-style detail
# ---------------------------------------------------------------------------


async def test_post_set_strategy_unauthenticated_returns_401_for_htmx(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    app = _build_app(store=store)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/actions/set-strategy",
            data={"strategy": "minimize_cost"},
            headers={"X-CSRF-Token": "test-csrf", "HX-Request": "true"},
        )
    # AC14 #16 — exact 401 (NOT (401, 302)) + the project-wide
    # "Authentication required" detail body emitted by ``require_homeowner``
    # (web/dependencies.py:207). Mirrors the Story 10.1 review patch #6
    # tightening pattern: assert the specific 401 contract rather than any
    # 4xx, so a future regression that flips this back to 302 for HX-Request
    # is caught.
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Authentication required"


# ---------------------------------------------------------------------------
# AC14 #17 — installer → 403 for HTMX
# ---------------------------------------------------------------------------


async def test_post_set_strategy_installer_returns_403_for_htmx(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    app = _build_app(store=store)
    raw_token, csrf = await _create_session("installer")
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("session", raw_token)
        resp = client.post(
            "/actions/set-strategy",
            data={"strategy": "minimize_cost"},
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# AC14 #18 — missing CSRF token → 403 (CsrfMiddleware)
# ---------------------------------------------------------------------------


async def test_post_set_strategy_requires_csrf_token(session_repo: SessionRepo) -> None:
    store = StateStore(system_clock_status="valid")
    app = _build_app(store=store)
    raw_token, _csrf = await _create_session("homeowner")
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("session", raw_token)
        resp = client.post(
            "/actions/set-strategy",
            data={"strategy": "minimize_cost"},
            headers={"HX-Request": "true"},  # NO X-CSRF-Token
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# AC14 #19 — 2-second response contract (margin check; route is synchronous)
# ---------------------------------------------------------------------------


async def test_post_set_strategy_returns_within_2_seconds(session_repo: SessionRepo) -> None:
    store = StateStore(
        system_clock_status="valid",
        active_strategy=EnergyStrategy.maximize_self_consumption,
    )
    app = _build_app(store=store)
    raw_token, csrf = await _create_session("homeowner")
    durations: list[float] = []
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        # Alternate strategies so we never short-circuit on idempotent no-op.
        toggles = ["minimize_cost", "maximize_self_consumption"] * 3
        for strategy in toggles:
            t0 = time.perf_counter()
            resp = client.post(
                "/actions/set-strategy",
                data={"strategy": strategy},
                headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
            )
            durations.append(time.perf_counter() - t0)
            assert resp.status_code == 200
    avg = sum(durations) / len(durations)
    assert avg < 2.0, f"Average POST duration {avg:.3f}s exceeds 2s contract"


# ---------------------------------------------------------------------------
# AC14 #20 — response marks the new strategy as the active option
# ---------------------------------------------------------------------------


async def test_post_set_strategy_response_body_marks_new_strategy_as_active_option(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(
        system_clock_status="valid",
        active_strategy=EnergyStrategy.maximize_self_consumption,
    )
    app = _build_app(store=store)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post(
            "/actions/set-strategy",
            data={"strategy": "prioritize_ev"},
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )
    assert resp.status_code == 200
    body = resp.text
    # Exactly one option carries the --active modifier and it is prioritize_ev.
    assert body.count("strategy-selector__option--active") == 1
    active_match = re.search(
        r'<button[^>]*strategy-selector__option--active[^>]*hx-vals=\'\{"strategy": "([^"]+)"\}\'',
        body,
        re.DOTALL,
    )
    assert active_match is not None
    assert active_match.group(1) == "prioritize_ev"


# ---------------------------------------------------------------------------
# AC14 #21 — response carries CSRF token for outerHTML swap survival
# ---------------------------------------------------------------------------


async def test_post_set_strategy_response_body_contains_csrf_token_for_outerhtml_swap_survival(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(
        system_clock_status="valid",
        active_strategy=EnergyStrategy.maximize_self_consumption,
    )
    app = _build_app(store=store)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post(
            "/actions/set-strategy",
            data={"strategy": "minimize_cost"},
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )
    assert resp.status_code == 200
    body = resp.text
    # Every option button must carry hx-headers with the per-session csrf_token
    # so the NEXT strategy POST survives outerHTML replacement of the section.
    assert body.count(f'X-CSRF-Token": "{csrf}"') == 3  # one per option button
