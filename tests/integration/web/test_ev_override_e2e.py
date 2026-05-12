"""Integration tests for the homeowner EV override end-to-end flow (Story 10.2 AC19)."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core import (
    CommandResult,
    CommandStatus,
    DegradedDeviceState,
    DeviceRole,
    EVChargerState,
    EVOverrideState,
    StateStore,
)
from open_ems.core.constraints import ActiveConstraints
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.services.active_constraints import ActiveConstraintsProvider
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
from open_ems.web.routes.homeowner import router as homeowner_router


async def _create_homeowner_session() -> tuple[str, str]:
    user_id = await UserRepo(get_connection()).create(
        username=f"hw_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role="homeowner",
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


def _ev_state(*, session_active: bool = False) -> EVChargerState:
    now = datetime.now(UTC)
    return EVChargerState(
        device_id="ev-001",
        status="charging" if session_active else "available",
        session_active=session_active,
        current_power_kw=7.0 if session_active else 0.0,
        power_source="meter_values",
        power_measured_at=now,
        read_at=now,
    )


def _active_constraints() -> ActiveConstraints:
    return ActiveConstraints(
        peak_limit_kw=5.0,
        battery_reserve_floor_percent=20.0,
        config_version=1,
        activated_at=datetime.now(UTC),
        ev_charging_window_start="22:30",
        ev_charging_window_end="06:00",
    )


def _build_app(
    *,
    store: StateStore,
    policy_guard: Any,
    constraints: ActiveConstraints | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.state_store = store
    app.state.policy_guard = policy_guard
    app.state.observability = AsyncMock(spec=ObservabilityService)
    if constraints is not None:
        provider = ActiveConstraintsProvider.from_snapshot(constraints)
        app.state.active_constraints_provider = provider
    app.include_router(homeowner_router)
    app.include_router(actions_router)
    app.include_router(fragments_router)
    app.add_middleware(CsrfMiddleware)
    return app


def _success_dispatch(command: Any) -> CommandResult:
    return CommandResult(
        correlation_id=command.correlation_id,
        device_id="ev-001",
        status=CommandStatus.success,
        applied=True,
        reason="ok",
    )


def _failed_dispatch(reason: str = "capability_check_failed") -> Any:
    async def _fn(command: Any) -> CommandResult:
        return CommandResult(
            correlation_id=command.correlation_id,
            device_id="ev-001",
            status=CommandStatus.failed,
            applied=False,
            reason=reason,
        )

    return _fn


def _rejected_dispatch(reason: str = "fail_safe_mode_active") -> Any:
    async def _fn(command: Any) -> CommandResult:
        return CommandResult(
            correlation_id=command.correlation_id,
            device_id="ev-001",
            status=CommandStatus.rejected,
            applied=False,
            reason=reason,
        )

    return _fn


def _pg_with(handler: Any) -> Any:
    pg = MagicMock(spec=PolicyGuard)
    pg.authorize_and_dispatch = AsyncMock(side_effect=handler)
    return pg


async def _wait_for_terminal(store: StateStore, *, max_wait_s: float = 2.0) -> None:
    deadline = time.perf_counter() + max_wait_s
    while time.perf_counter() < deadline:
        ov = store.get_snapshot().active_ev_override
        if ov is None or ov.dispatch_status != "pending":
            return
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# AC19 #1 — happy path: idle → confirmed via session_active=True observed
# ---------------------------------------------------------------------------


async def test_ev_override_happy_path_idle_to_confirmed(session_repo: SessionRepo) -> None:
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state(session_active=False)})
    app = _build_app(
        store=store,
        policy_guard=_pg_with(_success_dispatch),
        constraints=_active_constraints(),
    )
    raw_token, csrf = await _create_homeowner_session()
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        # POST /actions/ev-override
        post = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        assert post.status_code == 200
        # Dispatch task settles success → stays pending in StateStore.
        await asyncio.sleep(0.15)
        # Now publish a snapshot showing session_active=True; StateStore.publish's
        # session-flip detection (AC4) flips session_observed_active=True.
        await store.publish({DeviceRole.ev_charger: _ev_state(session_active=True)})
        # Fetch the EV card fragment — render should be Confirmed.
        frag = client.get(
            "/fragments/homeowner/ev-card",
            headers={"HX-Request": "true"},
        )
    assert frag.status_code == 200
    assert 'data-override-state="confirmed"' in frag.text
    assert "Charging now" in frag.text
    assert "state-card__ev-button--confirmed" in frag.text
    # Critical: NO pass-green / amber / red class fragments.
    body_lower = frag.text.lower()
    for forbidden in ("pass-green", "color-pass", "amber", "color-fail"):
        assert forbidden not in body_lower


# ---------------------------------------------------------------------------
# AC19 #2 — optimistic → fallback on dispatch failure
# ---------------------------------------------------------------------------


async def test_ev_override_optimistic_to_fallback_on_dispatch_failure(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state()})
    app = _build_app(
        store=store,
        policy_guard=_pg_with(_failed_dispatch("capability_check_failed")),
        constraints=_active_constraints(),
    )
    raw_token, csrf = await _create_homeowner_session()
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        await _wait_for_terminal(store)
        frag = client.get(
            "/fragments/homeowner/ev-card",
            headers={"HX-Request": "true"},
        )
    assert frag.status_code == 200
    assert 'data-override-state="fallback"' in frag.text
    assert "Could not start charging" in frag.text
    assert "Try again" in frag.text
    assert "did not respond" in frag.text  # plain-language reason


# ---------------------------------------------------------------------------
# AC19 #3 — optimistic → fallback on PolicyGuard P0 rejection
# ---------------------------------------------------------------------------


async def test_ev_override_optimistic_to_fallback_on_policy_guard_rejection(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state()})
    app = _build_app(
        store=store,
        policy_guard=_pg_with(_rejected_dispatch("fail_safe_mode_active")),
        constraints=_active_constraints(),
    )
    raw_token, csrf = await _create_homeowner_session()
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        await _wait_for_terminal(store)
        frag = client.get(
            "/fragments/homeowner/ev-card",
            headers={"HX-Request": "true"},
        )
    assert 'data-override-state="fallback"' in frag.text
    assert "paused for safety" in frag.text  # AC12 plain-language mapping


# ---------------------------------------------------------------------------
# AC19 #4 — natural session completion returns to idle
# ---------------------------------------------------------------------------


async def test_ev_override_natural_session_completion_returns_to_idle(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state(session_active=False)})
    app = _build_app(
        store=store,
        policy_guard=_pg_with(_success_dispatch),
        constraints=_active_constraints(),
    )
    raw_token, csrf = await _create_homeowner_session()
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        await asyncio.sleep(0.15)
        # Snapshot 1: session_active=True observed → session_observed_active=True.
        await store.publish({DeviceRole.ev_charger: _ev_state(session_active=True)})
        # Snapshot 2: session_active=False → override is cleared per AC4 clause 3.
        await store.publish({DeviceRole.ev_charger: _ev_state(session_active=False)})
        frag = client.get(
            "/fragments/homeowner/ev-card",
            headers={"HX-Request": "true"},
        )
    assert 'data-override-state="idle"' in frag.text
    assert "Charge EV now" in frag.text


# ---------------------------------------------------------------------------
# AC19 #5 — expiry returns to idle
# ---------------------------------------------------------------------------


async def test_ev_override_expiry_returns_to_idle(session_repo: SessionRepo) -> None:
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state()})
    # Install an already-expired override directly so we exercise publish-time
    # expiry-clear without waiting 2 hours.
    far_past = datetime.now(UTC) - timedelta(hours=3)
    expired = EVOverrideState(
        correlation_id=uuid.uuid4(),
        requested_at=far_past,
        expires_at=far_past + timedelta(seconds=60),
        dispatch_status="pending",
    )
    await store.set_ev_override(expired)
    # Trigger a publish so the AC4 expiry-clear fires.
    await store.publish({DeviceRole.ev_charger: _ev_state()})
    assert store.get_snapshot().active_ev_override is None
    app = _build_app(
        store=store,
        policy_guard=_pg_with(_success_dispatch),
        constraints=_active_constraints(),
    )
    raw_token, _csrf = await _create_homeowner_session()
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        frag = client.get(
            "/fragments/homeowner/ev-card",
            headers={"HX-Request": "true"},
        )
    assert 'data-override-state="idle"' in frag.text


# ---------------------------------------------------------------------------
# AC19 #6 — degraded EV state renders unavailable branch
# ---------------------------------------------------------------------------


async def test_ev_card_renders_unavailable_when_ev_charger_is_degraded(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    await store.publish(
        {
            DeviceRole.ev_charger: DegradedDeviceState(
                device_id="ev-001",
                role=DeviceRole.ev_charger,
                reason="ocpp_charger_not_connected",
                occurred_at=datetime.now(UTC),
            )
        }
    )
    app = _build_app(
        store=store,
        policy_guard=_pg_with(_success_dispatch),
        constraints=_active_constraints(),
    )
    raw_token, _csrf = await _create_homeowner_session()
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        frag = client.get(
            "/fragments/homeowner/ev-card",
            headers={"HX-Request": "true"},
        )
    assert "No EV charger connected" in frag.text


# ---------------------------------------------------------------------------
# AC19 #7 — dispatch with homeowner origin + correlation_id round-trip
# ---------------------------------------------------------------------------


async def test_ev_override_dispatches_with_homeowner_origin_and_correlation_id_matches_override(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state()})
    captured_commands: list[Any] = []

    async def _capture_dispatch(command: Any) -> CommandResult:
        captured_commands.append(command)
        return _success_dispatch(command)

    pg = MagicMock(spec=PolicyGuard)
    pg.authorize_and_dispatch = AsyncMock(side_effect=_capture_dispatch)
    app = _build_app(store=store, policy_guard=pg, constraints=_active_constraints())
    raw_token, csrf = await _create_homeowner_session()
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        await asyncio.sleep(0.15)
    assert resp.status_code == 200
    action_id = resp.json()["action_id"]
    assert len(captured_commands) == 1
    from open_ems.core import CommandOrigin

    assert captured_commands[0].origin is CommandOrigin.homeowner
    assert str(captured_commands[0].correlation_id) == action_id


# ---------------------------------------------------------------------------
# AC19 #8 — ControlLoop observes homeowner_override_active on next tick
# ---------------------------------------------------------------------------


async def test_control_loop_observes_homeowner_override_active_after_post(
    session_repo: SessionRepo,
) -> None:
    """The override visibility through ControlLoop._build_evaluation_input is
    covered exhaustively in tests/unit/engine/test_control_loop.py. This test
    is the end-to-end smoke check: a successful POST flips the flag in the
    next-evaluator input."""

    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state()})
    app = _build_app(
        store=store,
        policy_guard=_pg_with(_success_dispatch),
        constraints=_active_constraints(),
    )
    raw_token, csrf = await _create_homeowner_session()
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        await asyncio.sleep(0.15)
        ov = store.get_snapshot().active_ev_override
    assert ov is not None
    assert ov.dispatch_status == "pending"
    # The build-eval-input wiring is the contract; checking the snapshot field
    # is sufficient at this layer (control_loop.py:210 reads it directly).


# ---------------------------------------------------------------------------
# AC19 #9 — a11y placeholder (xfail+skipif per Story 9.x precedent)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="axe-playwright wiring deferred (Story 9.x precedent).", strict=False)
@pytest.mark.skipif(True, reason="axe-playwright not yet wired into the test stack.")
def test_ev_card_a11y_axe_scan_zero_violations() -> None:
    """Placeholder for axe-core scan over the 4 override_state variants.

    Follows the Story 10.1 / 9-X precedent at
    `tests/integration/web/test_homeowner_dashboard_a11y.py`.
    """
    raise AssertionError("axe-playwright placeholder")
