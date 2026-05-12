"""Unit tests for POST /actions/ev-override (Story 10.2 AC18 #34–#42)."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core import (
    CommandResult,
    CommandStatus,
    DeviceRole,
    EVChargerState,
    EVOverrideState,
    StateStore,
)
from open_ems.engine.policy_guard import PolicyGuard
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

_NOW_UTC = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


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


def _ev_charger(
    *,
    session_active: bool = False,
    read_at: datetime | None = None,
) -> EVChargerState:
    at = read_at or datetime.now(UTC)
    return EVChargerState(
        device_id="ev-001",
        status="charging" if session_active else "available",
        session_active=session_active,
        current_power_kw=7.0 if session_active else 0.0,
        power_source="meter_values",
        power_measured_at=at,
        read_at=at,
    )


async def _seed_snapshot(store: StateStore, *, ev_session_active: bool = False) -> None:
    await store.publish({DeviceRole.ev_charger: _ev_charger(session_active=ev_session_active)})


def _build_app(
    *,
    store: StateStore,
    policy_guard: Any,
    observability: ObservabilityService | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.state_store = store
    app.state.policy_guard = policy_guard
    app.state.observability = observability or AsyncMock(spec=ObservabilityService)
    app.include_router(actions_router)
    app.add_middleware(CsrfMiddleware)
    return app


def _make_success_result(correlation_id: uuid.UUID) -> CommandResult:
    return CommandResult(
        correlation_id=correlation_id,
        device_id="ev-001",
        status=CommandStatus.success,
        applied=True,
        reason="ok",
    )


def _make_rejected_result(
    correlation_id: uuid.UUID, reason: str = "fail_safe_mode_active"
) -> CommandResult:
    return CommandResult(
        correlation_id=correlation_id,
        device_id="ev-001",
        status=CommandStatus.rejected,
        applied=False,
        reason=reason,
    )


def _make_failed_result(
    correlation_id: uuid.UUID, reason: str = "capability_check_failed"
) -> CommandResult:
    return CommandResult(
        correlation_id=correlation_id,
        device_id="ev-001",
        status=CommandStatus.failed,
        applied=False,
        reason=reason,
    )


def _stub_policy_guard(dispatch_result: CommandResult | Exception) -> Any:
    pg = MagicMock(spec=PolicyGuard)
    if isinstance(dispatch_result, Exception):
        pg.authorize_and_dispatch = AsyncMock(side_effect=dispatch_result)
    else:
        # Pass-through correlation_id round-trip.
        async def _dispatch(command: Any) -> CommandResult:
            return dispatch_result.model_copy(update={"correlation_id": command.correlation_id})

        pg.authorize_and_dispatch = AsyncMock(side_effect=_dispatch)
    return pg


async def _wait_for_terminal_status(store: StateStore, *, max_wait_s: float = 2.0) -> None:
    """Poll the snapshot until the dispatch task settles (or timeout).

    NOTE: with the synchronous Starlette ``TestClient`` the route runs inside a
    portal event loop. Background tasks spawned by the route run on the same
    portal loop and continue to tick as long as the portal is open (i.e. while
    we are still inside ``with TestClient(app) as client:``). Calling this
    helper INSIDE the ``with`` block keeps the portal loop alive long enough
    for the dispatch task to settle.
    """
    deadline = time.perf_counter() + max_wait_s
    while time.perf_counter() < deadline:
        ov = store.get_snapshot().active_ev_override
        if ov is None or ov.dispatch_status != "pending":
            return
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# AC18 #34 — no EV charger
# ---------------------------------------------------------------------------


async def test_post_ev_override_returns_400_when_no_ev_charger(session_repo: SessionRepo) -> None:
    store = StateStore(system_clock_status="valid")
    # No EV charger published — store.get_snapshot().ev_charger is None.
    app = _build_app(store=store, policy_guard=_stub_policy_guard(MagicMock()))
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400
    body = resp.json()
    assert body["status"] == "failed"
    assert "EV charger" in body["message"]
    assert body["action_id"] is None


# ---------------------------------------------------------------------------
# AC18 #35 — first POST installs pending override
# ---------------------------------------------------------------------------


async def test_post_ev_override_installs_pending_override_on_first_call(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    pg = _stub_policy_guard(_make_success_result(uuid.uuid4()))
    app = _build_app(store=store, policy_guard=pg)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        # Read the snapshot WHILE the portal loop is still alive so the
        # dispatch task's potential override-mutation has settled but does
        # not vanish along with the portal at __exit__.
        override = store.get_snapshot().active_ev_override
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["action_id"]
    assert override is not None
    assert str(override.correlation_id) == body["action_id"]


# ---------------------------------------------------------------------------
# AC18 #36 — idempotent replay within window
# ---------------------------------------------------------------------------


async def test_post_ev_override_idempotent_replay_within_window(session_repo: SessionRepo) -> None:
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    pg = _stub_policy_guard(_make_success_result(uuid.uuid4()))
    app = _build_app(store=store, policy_guard=pg)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        first = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        second = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        first_action = first.json()["action_id"]
        second_action = second.json()["action_id"]
        second_details = second.json()["details"]
        await_count = pg.authorize_and_dispatch.await_count
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_action == second_action
    assert second_details.get("idempotent_replay") is True
    # PolicyGuard should have been invoked only once.
    assert await_count == 1


# ---------------------------------------------------------------------------
# AC18 #37 — new POST after previous failed
# ---------------------------------------------------------------------------


async def test_post_ev_override_new_command_after_previous_failed(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    # Install a prior failed override directly.
    prior_cid = uuid.uuid4()
    prior_failed = EVOverrideState(
        correlation_id=prior_cid,
        requested_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        dispatch_status="failed",
        failure_reason="capability_check_failed",
    )
    await store.set_ev_override(prior_failed)
    pg = _stub_policy_guard(_make_success_result(uuid.uuid4()))
    app = _build_app(store=store, policy_guard=pg)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    new_cid = resp.json()["action_id"]
    assert new_cid != str(prior_cid)


# ---------------------------------------------------------------------------
# AC18 #38 — new POST after expiry
# ---------------------------------------------------------------------------


async def test_post_ev_override_new_command_after_expiry(session_repo: SessionRepo) -> None:
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    # Install a prior pending override that is already expired.
    far_past = datetime.now(UTC) - timedelta(hours=3)
    expired = EVOverrideState(
        correlation_id=uuid.uuid4(),
        requested_at=far_past,
        expires_at=far_past + timedelta(seconds=60),
        dispatch_status="pending",
    )
    await store.set_ev_override(expired)
    # Re-publish so StateStore.publish clears the expired override per AC4.
    await _seed_snapshot(store)
    assert store.get_snapshot().active_ev_override is None
    pg = _stub_policy_guard(_make_success_result(uuid.uuid4()))
    app = _build_app(store=store, policy_guard=pg)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# AC18 #39 — CSRF enforcement
# ---------------------------------------------------------------------------


async def test_post_ev_override_requires_csrf_token(session_repo: SessionRepo) -> None:
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    pg = _stub_policy_guard(_make_success_result(uuid.uuid4()))
    app = _build_app(store=store, policy_guard=pg)
    raw_token, _csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post("/actions/ev-override")  # no CSRF header
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# AC18 #40/41 — auth/role enforcement
# ---------------------------------------------------------------------------


async def test_post_ev_override_unauth_returns_401_for_htmx_request(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    pg = _stub_policy_guard(_make_success_result(uuid.uuid4()))
    app = _build_app(store=store, policy_guard=pg)
    with TestClient(app) as client:
        resp = client.post(
            "/actions/ev-override",
            headers={"X-CSRF-Token": "x", "HX-Request": "true"},
        )
    assert resp.status_code == 401


async def test_post_ev_override_installer_returns_403_for_htmx_request(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    pg = _stub_policy_guard(_make_success_result(uuid.uuid4()))
    app = _build_app(store=store, policy_guard=pg)
    raw_token, csrf = await _create_session("installer")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post(
            "/actions/ev-override",
            headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# AC18 #42 — 2-second response contract
# ---------------------------------------------------------------------------


async def test_post_ev_override_returns_within_2_seconds(session_repo: SessionRepo) -> None:
    """UX contract line 1840 — every action endpoint returns within 2s."""
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    pg = _stub_policy_guard(_make_success_result(uuid.uuid4()))

    # Make the dispatch take 5 seconds (longer than the contract): the route
    # MUST still return promptly because dispatch is fire-and-forget.
    async def _slow_dispatch(command: Any) -> CommandResult:
        await asyncio.sleep(5.0)
        return _make_success_result(command.correlation_id)

    pg.authorize_and_dispatch = AsyncMock(side_effect=_slow_dispatch)
    app = _build_app(store=store, policy_guard=pg)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        start = time.perf_counter()
        resp = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        elapsed = time.perf_counter() - start
    assert resp.status_code == 200
    assert elapsed < 2.0, f"Route took {elapsed:.2f}s; must be < 2s (UX contract)"


# ---------------------------------------------------------------------------
# Dispatch-task settlement scenarios
# ---------------------------------------------------------------------------


async def test_dispatch_task_settles_override_to_failed_on_dispatch_failure(
    session_repo: SessionRepo,
) -> None:
    """AC7 step 4: dispatch failure → dispatch_status="failed" + failure_reason."""
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    pg = _stub_policy_guard(_make_failed_result(uuid.uuid4(), reason="capability_check_failed"))
    app = _build_app(store=store, policy_guard=pg)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        await _wait_for_terminal_status(store)
        ov = store.get_snapshot().active_ev_override
    assert resp.status_code == 200
    assert ov is not None
    assert ov.dispatch_status == "failed"
    assert ov.failure_reason == "capability_check_failed"


async def test_dispatch_task_settles_override_to_rejected_on_policy_guard_p0(
    session_repo: SessionRepo,
) -> None:
    """AC7 step 4: P0 fail_safe → dispatch_status="rejected"."""
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    pg = _stub_policy_guard(_make_rejected_result(uuid.uuid4(), reason="fail_safe_mode_active"))
    app = _build_app(store=store, policy_guard=pg)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        await _wait_for_terminal_status(store)
        ov = store.get_snapshot().active_ev_override
    assert ov is not None
    assert ov.dispatch_status == "rejected"
    assert ov.failure_reason == "fail_safe_mode_active"


async def test_dispatch_task_leaves_override_pending_on_success(session_repo: SessionRepo) -> None:
    """AC7 step 3: success → dispatch_status stays pending until session_active=True."""
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    pg = _stub_policy_guard(_make_success_result(uuid.uuid4()))
    app = _build_app(store=store, policy_guard=pg)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        # Give the dispatch task time to complete its success path.
        await asyncio.sleep(0.2)
        ov = store.get_snapshot().active_ev_override
    assert ov is not None
    assert ov.dispatch_status == "pending"  # Confirmed needs session_active=True observed


async def test_dispatch_task_skips_settlement_when_override_correlation_id_drifts(
    session_repo: SessionRepo,
) -> None:
    """AC7 step 2: orphan dispatch results must NOT clobber a newer override."""
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    # The PolicyGuard stub blocks until released; we swap the override before
    # the dispatch result lands so the task detects orphan-and-exit.
    swap_event = asyncio.Event()

    async def _slow_dispatch(command: Any) -> CommandResult:
        await swap_event.wait()
        return _make_failed_result(command.correlation_id, reason="capability_check_failed")

    pg = MagicMock(spec=PolicyGuard)
    pg.authorize_and_dispatch = AsyncMock(side_effect=_slow_dispatch)
    app = _build_app(store=store, policy_guard=pg)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        first_cid = resp.json()["action_id"]
        # Replace the override with a newer correlation_id.
        newer = EVOverrideState(
            correlation_id=uuid.uuid4(),
            requested_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            dispatch_status="pending",
        )
        await store.set_ev_override(newer)
        swap_event.set()
        await asyncio.sleep(0.2)
        ov = store.get_snapshot().active_ev_override
    assert ov is not None
    # The orphan dispatch result for first_cid did NOT mutate the newer override.
    assert ov.dispatch_status == "pending"
    assert str(ov.correlation_id) != first_cid
    assert ov.failure_reason is None


async def test_dispatch_task_skips_settlement_when_override_cleared_to_none(
    session_repo: SessionRepo,
) -> None:
    """Review patch: orphan-protection must also cover the override-cleared-to-None path.

    Companion to the correlation-id-drift case: if a concurrent ``publish()``
    expires or naturally-completes the override between the dispatch's
    PolicyGuard await and the settlement write, the dispatch result must NOT
    re-install the cleared override (which would resurrect it with a terminal
    ``failure_reason``). The atomic compare-and-set should reject the write,
    leaving the snapshot's ``active_ev_override`` as ``None``.
    """
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    swap_event = asyncio.Event()

    async def _slow_dispatch(command: Any) -> CommandResult:
        await swap_event.wait()
        return _make_failed_result(command.correlation_id, reason="capability_check_failed")

    pg = MagicMock(spec=PolicyGuard)
    pg.authorize_and_dispatch = AsyncMock(side_effect=_slow_dispatch)
    app = _build_app(store=store, policy_guard=pg)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200
        # Clear the override (simulating an expiry/natural-completion sweep
        # from ``publish()``) BEFORE the dispatch result lands.
        await store.set_ev_override(None)
        assert store.get_snapshot().active_ev_override is None
        swap_event.set()
        await asyncio.sleep(0.2)
        ov = store.get_snapshot().active_ev_override
    # The dispatch result did NOT resurrect the cleared override.
    assert ov is None


async def test_post_ev_override_emits_audit_row_on_happy_path(
    session_repo: SessionRepo,
) -> None:
    """Review patch: assert ``_safe_audit`` actually invokes ``observability.audit``.

    Catches a class of bug where a future code change passes an invalid
    ``event_type`` (or other validator-rejected field) and ``_safe_audit``
    silently swallows the ``ValueError`` via its ``except Exception``,
    leaving production with no audit emissions despite green tests.
    """
    store = StateStore(system_clock_status="valid")
    await _seed_snapshot(store)
    observability = AsyncMock(spec=ObservabilityService)
    pg = _stub_policy_guard(_make_success_result(uuid.uuid4()))
    app = _build_app(store=store, policy_guard=pg, observability=observability)
    raw_token, csrf = await _create_session("homeowner")
    with TestClient(app) as client:
        client.cookies.set("session", raw_token)
        resp = client.post("/actions/ev-override", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    # At least one audit row was emitted; on the happy path the route emits
    # the ``homeowner_ev_override_requested`` row before returning.
    assert observability.audit.await_count >= 1
    call_kwargs = observability.audit.await_args_list[0].kwargs
    assert call_kwargs["actor"] == "homeowner"
    # event_type must be a member of the valid set so ``ObservabilityService.audit``
    # does not raise ValueError that ``_safe_audit`` would silently swallow.
    assert call_kwargs["event_type"] in {
        "DECISION",
        "DEVICE",
        "SYSTEM",
        "CONSTRAINT",
        "INSTALLER",
    }
