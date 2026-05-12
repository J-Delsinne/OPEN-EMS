"""Integration tests for the homeowner strategy selector end-to-end flow (Story 10.3 AC15)."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core import (
    DeviceRole,
    EnergyStrategy,
    GridMeterState,
    InverterState,
    StateStore,
    SystemOperatingMode,
)
from open_ems.engine import IntentExecutor, RetryPolicy
from open_ems.engine.control_loop import ControlLoop
from open_ems.services.audit_log import ObservabilityService
from open_ems.services.loop_liveness import LoopLiveness
from open_ems.settings import Settings
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
from tests.fixtures.active_constraints import make_active_constraints_provider


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


def _build_app(
    *,
    store: StateStore,
    observability: ObservabilityService | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.state_store = store
    app.state.observability = observability or AsyncMock(spec=ObservabilityService)
    app.include_router(homeowner_router)
    app.include_router(actions_router)
    app.include_router(fragments_router)
    app.add_middleware(CsrfMiddleware)
    return app


# ---------------------------------------------------------------------------
# AC15 #1 — happy path: idle → new strategy via outerHTML swap
# ---------------------------------------------------------------------------


async def test_strategy_selector_happy_path_idle_to_new_strategy_via_outerhtml_swap(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(
        system_clock_status="valid",
        operating_mode=SystemOperatingMode.normal,
        active_strategy=EnergyStrategy.maximize_self_consumption,
    )
    observability = AsyncMock(spec=ObservabilityService)
    app = _build_app(store=store, observability=observability)
    raw_token, csrf = await _create_homeowner_session()

    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        # Initial dashboard fragment shows the default strategy.
        initial = client.get(
            "/fragments/homeowner/status-headline",
            headers={"HX-Request": "true"},
        )
        assert initial.status_code == 200
        assert "Maximize Self-Consumption" in initial.text
        # POST a strategy change.
        post_resp = client.post(
            "/actions/set-strategy",
            data={"strategy": "minimize_cost"},
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )

    assert post_resp.status_code == 200
    # Response is the new headline fragment for outerHTML swap.
    assert "Your home is running on solar · Minimize Cost" in post_resp.text
    assert 'data-active-strategy="minimize_cost"' in post_resp.text
    # State store reflects the change.
    assert store.get_snapshot().active_strategy is EnergyStrategy.minimize_cost
    # Exactly one audit row emitted.
    observability.audit.assert_called_once()
    detail = observability.audit.call_args.kwargs["detail"]
    assert detail["event"] == "homeowner_strategy_changed"


# ---------------------------------------------------------------------------
# AC15 #2 — idempotent no-op: no audit, no mutation
# ---------------------------------------------------------------------------


async def test_strategy_selector_idempotent_no_op_does_not_emit_audit_or_advance_sequence(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(
        system_clock_status="valid",
        active_strategy=EnergyStrategy.minimize_cost,
    )
    observability = AsyncMock(spec=ObservabilityService)
    app = _build_app(store=store, observability=observability)
    raw_token, csrf = await _create_homeowner_session()

    seq_before = store.get_snapshot().sequence_id
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        # First POST: same as current strategy.
        client.post(
            "/actions/set-strategy",
            data={"strategy": "minimize_cost"},
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )
        # Second POST: still the same.
        client.post(
            "/actions/set-strategy",
            data={"strategy": "minimize_cost"},
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )

    # Sequence_id unchanged — set_active_strategy does not advance it; both
    # POSTs were no-ops; no publish() in between.
    assert store.get_snapshot().sequence_id == seq_before
    # Zero audit rows.
    observability.audit.assert_not_called()


# ---------------------------------------------------------------------------
# AC15 #3 — invalid value: failure fragment, no mutation, no audit
# ---------------------------------------------------------------------------


async def test_strategy_selector_invalid_value_returns_failure_fragment_and_does_not_mutate_state(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(
        system_clock_status="valid",
        active_strategy=EnergyStrategy.maximize_self_consumption,
    )
    observability = AsyncMock(spec=ObservabilityService)
    app = _build_app(store=store, observability=observability)
    raw_token, csrf = await _create_homeowner_session()

    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post(
            "/actions/set-strategy",
            data={"strategy": "does_not_exist"},
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "Strategy update failed. Your previous setting is still active." in resp.text
    assert 'data-strategy-update-failed="true"' in resp.text
    # State unchanged.
    assert store.get_snapshot().active_strategy is EnergyStrategy.maximize_self_consumption
    # No audit row.
    observability.audit.assert_not_called()


# ---------------------------------------------------------------------------
# AC15 #4 — change propagates to next ControlLoop tick via snapshot read
# ---------------------------------------------------------------------------


def _make_control_loop(state_store: StateStore) -> ControlLoop:
    """Construct a minimal ControlLoop instance for cross-layer propagation tests.

    Mirrors ``tests/unit/engine/test_control_loop.py::_control_loop`` (the
    canonical helper for unit-level loop instantiation); replicated inline
    so the integration test can exercise the route → StateStore → ControlLoop
    chain without importing test-private helpers. Required dependencies are
    stubbed because this test only exercises the strategy-read path through
    ``_build_evaluation_input``; the loop never ticks here.
    """
    energy_repo = AsyncMock()
    energy_repo.get_current_monthly_peak_kw = AsyncMock(return_value=0.0)
    energy_repo.write_peak_interval = AsyncMock()
    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)
    retry_policy = MagicMock(spec=RetryPolicy)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    return ControlLoop(
        state_store=state_store,
        adapters={},
        energy_repo=energy_repo,
        settings=settings,
        intent_executor=IntentExecutor(),
        retry_policy=retry_policy,
        loop_liveness=LoopLiveness(
            missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0
        ),
        observability=observability,
        active_constraints=make_active_constraints_provider(settings),
    )


def _grid_meter_state() -> GridMeterState:
    return GridMeterState(
        device_id="grid-001",
        grid_power_kw=4.0,
        energy_delivered_kwh=100.0,
        energy_returned_kwh=10.0,
        received_at=datetime.now(UTC),
    )


def _inverter_state() -> InverterState:
    return InverterState(
        device_id="inv-001",
        pv_power_kw=2.5,
        ac_power_kw=2.4,
        operating_mode="normal",
        read_at=datetime.now(UTC),
    )


async def test_strategy_selector_change_propagates_to_next_control_loop_tick(
    session_repo: SessionRepo,
) -> None:
    """End-to-end proof: route mutation → get_snapshot() reflects → ControlLoop's
    ``_build_evaluation_input`` produces an ``EvaluationInput`` carrying the new
    strategy on its next call.

    This is the cross-layer assertion the AC15 #4 spec promises ("End-to-end
    proof of the snapshot-patch propagation contract"). Without this test, a
    future refactor that drops the mutator's ``model_copy(update=...)`` snapshot
    patch — OR re-introduces a notification path into the loop — could pass the
    AC1 / AC7 unit tests yet break the end-to-end propagation.
    """
    store = StateStore(
        system_clock_status="valid",
        active_strategy=EnergyStrategy.maximize_self_consumption,
    )
    app = _build_app(store=store)
    raw_token, csrf = await _create_homeowner_session()
    loop = _make_control_loop(store)

    # Populate the required device slots so ``_build_evaluation_input`` does
    # not reject the snapshot for missing inverter/grid_meter.
    await store.publish(
        {
            DeviceRole.inverter: _inverter_state(),
            DeviceRole.grid_meter: _grid_meter_state(),
        }
    )

    # Tick 1 (BEFORE the POST): EvaluationInput carries the constructor default.
    snap1 = store.get_snapshot()
    eval1 = loop._build_evaluation_input(snap1, datetime.now(UTC))  # noqa: SLF001
    assert eval1.strategy is EnergyStrategy.maximize_self_consumption

    # Homeowner POSTs a strategy change via the route.
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post(
            "/actions/set-strategy",
            data={"strategy": "prioritize_ev"},
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )
    assert resp.status_code == 200

    # Tick 2 (AFTER the POST, no publish() in between): the SAME
    # ``_build_evaluation_input`` call site must produce an EvaluationInput
    # whose strategy is the new value — proving the route's mutator patched
    # the published snapshot and the loop's snapshot-read path observed it.
    snap2 = store.get_snapshot()
    eval2 = loop._build_evaluation_input(snap2, datetime.now(UTC))  # noqa: SLF001
    assert eval2.strategy is EnergyStrategy.prioritize_ev

    # Tick 3 (after the next real publish): the strategy is carried forward.
    next_snap = await store.publish(
        {
            DeviceRole.inverter: _inverter_state(),
            DeviceRole.grid_meter: _grid_meter_state(),
        }
    )
    eval3 = loop._build_evaluation_input(next_snap, datetime.now(UTC))  # noqa: SLF001
    assert eval3.strategy is EnergyStrategy.prioritize_ev


# ---------------------------------------------------------------------------
# AC15 #5 — serialized in SSE payload after change
# ---------------------------------------------------------------------------


async def test_strategy_selector_serialized_in_sse_payload_after_change(
    session_repo: SessionRepo,
) -> None:
    """Verifies role-aware serialization (Story 10.1 AC4) continues to surface
    active_strategy correctly after the new mutator lands.
    """
    from open_ems.web.state_serialization import (
        serialize_homeowner_snapshot,
        serialize_installer_snapshot,
    )

    store = StateStore(
        system_clock_status="valid",
        active_strategy=EnergyStrategy.maximize_self_consumption,
    )
    app = _build_app(store=store)
    raw_token, csrf = await _create_homeowner_session()
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        client.post(
            "/actions/set-strategy",
            data={"strategy": "prioritize_ev"},
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )
    snapshot = store.get_snapshot()
    assert serialize_homeowner_snapshot(snapshot)["active_strategy"] == "prioritize_ev"
    assert serialize_installer_snapshot(snapshot)["active_strategy"] == "prioritize_ev"


# ---------------------------------------------------------------------------
# AC15 #6 — a11y placeholder (xfail+skipif precedent from Story 10.1 / 10.2)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx absent — axe-core scan skipped")
@pytest.mark.xfail(reason="axe-playwright wiring deferred", strict=False)
def test_strategy_selector_a11y_placeholder() -> None:
    """Placeholder — once axe-playwright is wired the body asserts zero WCAG
    2.1 AA violations on /homeowner/dashboard with the selector expanded
    (keyboard-tab to 'Change strategy', press Enter, axe scans the panel).
    """
    raise AssertionError("axe-playwright not yet wired for the strategy selector")
