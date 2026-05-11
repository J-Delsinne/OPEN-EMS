"""End-to-end integration tests for Step 3 — Constraint Configuration (Story 9.3 AC10).

Each test exercises the full request/response chain against an in-memory
SQLite (real ``DraftConstraintsRepo`` + ``ConfigRepo`` + ``ConstraintsService``
+ ``ActiveConstraintsProvider``). Covers the persistence + restart + audit
trail + live-state-shift scenarios the unit tests cannot reach.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core.constraints import ActiveConstraintsInput
from open_ems.core.devices import (
    DeviceRole,
    GridMeterState,
)
from open_ems.core.state import (
    ALL_DEVICE_ROLES,
    ComponentState,
    GlobalState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.services.constraints import ConstraintsService
from open_ems.settings import Settings
from open_ems.storage.database import get_connection
from open_ems.storage.repositories.config_repo import ConfigRepo
from open_ems.storage.repositories.device_repo import DeviceRepo, ManualDeviceEntryInput
from open_ems.storage.repositories.draft_constraints_repo import DraftConstraintsRepo
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.storage.repositories.wizard_state_repo import WizardStateRepo
from open_ems.web.csrf import CsrfMiddleware
from open_ems.web.routes.setup import router as setup_router

_CSRF = "test-csrf-constraints-e2e"
_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)


_DDL = [
    """CREATE TABLE device_registry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL UNIQUE,
        protocol TEXT NOT NULL,
        address TEXT NOT NULL,
        model TEXT,
        firmware_version TEXT,
        source TEXT NOT NULL,
        validated INTEGER NOT NULL DEFAULT 0,
        last_capability_status TEXT,
        last_limitation_reason TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT,
        installer_acknowledged_unvalidated_at TEXT,
        role TEXT,
        role_assigned_at TEXT
    )""",
    """CREATE TABLE wizard_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL UNIQUE,
        step_1_complete INTEGER NOT NULL DEFAULT 0,
        step_1_completed_at TEXT,
        last_scan_id TEXT,
        step_2_complete INTEGER NOT NULL DEFAULT 0,
        step_2_completed_at TEXT,
        step_2_acknowledged_gaps TEXT,
        step_3_complete INTEGER NOT NULL DEFAULT 0,
        step_3_completed_at TEXT,
        step_3_activated_config_version INTEGER,
        step_4_complete INTEGER NOT NULL DEFAULT 0,
        step_4_completed_at TEXT,
        step_4_completed_config_version INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE active_constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        peak_limit_kw REAL NOT NULL CHECK (peak_limit_kw > 0),
        battery_reserve_floor_percent REAL NOT NULL,
        config_version INTEGER NOT NULL UNIQUE,
        activated_at TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('system', 'installer')),
        ev_charging_window_start TEXT,
        ev_charging_window_end TEXT
    )""",
    """CREATE TABLE config_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        field TEXT NOT NULL,
        previous_value TEXT NOT NULL,
        new_value TEXT NOT NULL,
        config_version INTEGER NOT NULL
    )""",
    """CREATE TABLE draft_constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL UNIQUE,
        peak_limit_kw REAL NOT NULL,
        battery_reserve_floor_percent REAL NOT NULL,
        ev_charging_window_start TEXT,
        ev_charging_window_end TEXT,
        validation_status TEXT NOT NULL DEFAULT 'pending',
        validation_report TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    )""",
]


def _empty_snapshot() -> SystemSnapshot:
    return SystemSnapshot(
        sequence_id=0,
        captured_at=_NOW,
        global_state=GlobalState.degraded,
        operating_mode=SystemOperatingMode.degraded,
        inverter=None,
        battery=None,
        ev_charger=None,
        grid_meter=None,
        component_states=dict.fromkeys(ALL_DEVICE_ROLES, ComponentState.unavailable),
        data_age_seconds=dict.fromkeys(ALL_DEVICE_ROLES),
        system_clock_status="valid",
    )


class _MutableStateStore:
    """Minimal store the tests can reseat without going through publish()."""

    def __init__(self, snapshot: SystemSnapshot) -> None:
        self.snapshot = snapshot

    def get_snapshot(self) -> SystemSnapshot:
        return self.snapshot


@pytest_asyncio.fixture
async def app_e2e(
    session_repo: SessionRepo,
) -> AsyncGenerator[tuple[FastAPI, _MutableStateStore], None]:
    conn = get_connection()
    for stmt in _DDL:
        await conn.execute(stmt)
    await conn.commit()

    config_repo = ConfigRepo()
    device_repo = DeviceRepo()
    wizard_repo = WizardStateRepo()
    draft_repo = DraftConstraintsRepo()
    # Seed an active row so we have a stable baseline (peak=25, floor=20).
    await config_repo.activate(
        ActiveConstraintsInput(peak_limit_kw=25.0, battery_reserve_floor_percent=20.0),
        actor="installer",
    )
    provider = ActiveConstraintsProvider(repo=config_repo, settings=Settings(secret_key="x"))
    await provider.hydrate()
    state_store = _MutableStateStore(_empty_snapshot())
    svc = ConstraintsService(
        draft_repo=draft_repo,
        config_repo=config_repo,
        active_constraints_provider=provider,
        wizard_state_repo=wizard_repo,
        device_repo=device_repo,
        state_store=state_store,
    )
    app = FastAPI()
    app.state.device_repo = device_repo
    app.state.wizard_state_repo = wizard_repo
    app.state.constraints_service = svc
    app.state.draft_constraints_repo = draft_repo
    app.state.active_constraints_provider = provider
    app.include_router(setup_router)
    app.add_middleware(CsrfMiddleware)
    yield app, state_store


async def _create_session(*, step_2_complete: bool = True) -> tuple[str, str]:
    user_id = await UserRepo().create(
        username=f"installer_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role="installer",
    )
    raw = generate_session_token()
    sid = await SessionRepo().create(
        user_id=user_id,
        token_hash=hash_token(raw),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF,
    )
    wizard = WizardStateRepo()
    await wizard.get_or_create(sid, now=datetime.now(UTC))
    await wizard.set_step_1_complete(sid, now=datetime.now(UTC))
    if step_2_complete:
        await wizard.set_step_2_complete(sid, acknowledged_gaps=frozenset(), now=datetime.now(UTC))
    return raw, sid


async def _add_grid_meter() -> None:
    repo = DeviceRepo(get_connection())
    await repo.upsert_manual(
        ManualDeviceEntryInput(
            device_id="meter-1",
            protocol="dsmr_p1",
            address="/dev/ttyUSB0",
        ),
        first_seen_at=datetime.now(UTC),
    )
    await repo.assign_role("meter-1", role=DeviceRole.grid_meter, assigned_at=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Happy path + audit trail
# ---------------------------------------------------------------------------


async def test_e2e_happy_path_three_field_change_emits_three_audit_rows(
    app_e2e,
) -> None:
    """Activate with all three fields changing: 3 audit rows with the same
    config_version, distinct field names, correct previous/new JSON.
    """
    app, _ = app_e2e
    await _add_grid_meter()
    raw, _ = await _create_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "30",
            "battery_reserve_floor_percent": "25",
            "ev_charging_window_start": "09:00",
            "ev_charging_window_end": "17:00",
        },
    )
    response = client.post(
        "/installer/setup/constraints/activate",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/installer/setup/validation"

    conn = get_connection()
    async with conn.execute(
        "SELECT field, previous_value, new_value, config_version"
        " FROM config_audit_log WHERE config_version = 2 ORDER BY field"
    ) as cur:
        rows = list(await cur.fetchall())
    assert len(rows) == 3
    fields = sorted(r["field"] for r in rows)
    assert fields == [
        "battery_reserve_floor",
        "ev_charging_window",
        "peak_consumption_limit",
    ]
    versions = {r["config_version"] for r in rows}
    assert versions == {2}
    ev_row = next(r for r in rows if r["field"] == "ev_charging_window")
    assert json.loads(ev_row["new_value"]) == {"start": "09:00", "end": "17:00"}


async def test_e2e_validate_fail_then_re_edit_then_activate_succeeds(
    app_e2e,
) -> None:
    """A failing validate followed by a draft re-edit and a successful
    activate works end-to-end.
    """
    app, _ = app_e2e
    await _add_grid_meter()
    raw, _ = await _create_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    # First draft: peak below structural FAIL threshold.
    client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "0.4",
            "battery_reserve_floor_percent": "20",
        },
    )
    # Validate — must FAIL.
    validate_response = client.post(
        "/installer/setup/constraints/validate",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert validate_response.status_code == 200
    assert "FAILED" in validate_response.text
    # Re-edit with a passing value.
    client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "30",
            "battery_reserve_floor_percent": "20",
        },
    )
    validate_pass = client.post(
        "/installer/setup/constraints/validate",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert "Overall: VALID" in validate_pass.text
    activate = client.post(
        "/installer/setup/constraints/activate",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert activate.status_code == 302


async def test_e2e_persistence_draft_survives_simulated_restart(
    app_e2e,
) -> None:
    """Upsert draft → simulate restart by re-fetching state → draft restored
    + validation_status preserved.
    """
    app, _ = app_e2e
    raw, sid = await _create_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "30",
            "battery_reserve_floor_percent": "25",
        },
    )
    # Simulate restart: throw away in-memory references, re-fetch via the
    # repo (which the new GET handler would do too).
    repo = DraftConstraintsRepo()
    persisted = await repo.get(sid)
    assert persisted is not None
    assert persisted.peak_limit_kw == 30.0
    # GET also renders the persisted values back into the form.
    response = client.get("/installer/setup/constraints", cookies={"session": raw})
    assert response.status_code == 200
    assert 'value="30.00"' in response.text


async def test_e2e_grid_overload_warn_does_not_block_activation(
    app_e2e,
) -> None:
    """Validate passes with the live grid quiescent; then push a snapshot in
    which the grid is importing 50 kW (above the new 30 kW peak limit) and
    activate. The activate route's re-validation inside the write lock
    produces a fresh ``safety_pre_check`` WARN for the over-limit grid power
    — but WARNs do not abort activation. Only ``status='fail'`` rows do.
    The activation still succeeds; the WARN is informational.

    Note on naming: no ``safety_pre_check`` FAIL outcome today depends on
    snapshot state — the only structural FAIL (``peak_limit_kw < 0.5``) is a
    property of the draft itself, not the runtime snapshot. The FAIL-abort
    counterpart tests live below in
    ``test_e2e_activate_without_prior_validate_aborts_on_structural_fail``
    and ``test_e2e_activate_aborts_when_re_validation_finds_fresh_structural_fail``
    (Story 9.Y-a, closing R2P3 from the 9-3 round-2 review).
    """
    app, state = app_e2e
    await _add_grid_meter()
    raw, _ = await _create_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    # Draft + validate pass (peak=30 > current grid_meter pgrid 0.0 → no warn).
    client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "30",
            "battery_reserve_floor_percent": "20",
        },
    )
    validate_pass = client.post(
        "/installer/setup/constraints/validate",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert "Overall: VALID" in validate_pass.text
    # Now push a state where grid is importing 50 kW > 30 kW limit. Re-validate
    # inside activate produces a WARN — but WARN doesn't fail; the activation
    # still succeeds.
    snap = SystemSnapshot(
        sequence_id=1,
        captured_at=_NOW,
        global_state=GlobalState.degraded,
        operating_mode=SystemOperatingMode.degraded,
        inverter=None,
        battery=None,
        ev_charger=None,
        grid_meter=GridMeterState(
            device_id="meter-1",
            grid_power_kw=50.0,
            energy_delivered_kwh=100.0,
            energy_returned_kwh=0.0,
            received_at=_NOW,
        ),
        component_states={
            DeviceRole.grid_meter: ComponentState.active,
            DeviceRole.inverter: ComponentState.unavailable,
            DeviceRole.battery: ComponentState.unavailable,
            DeviceRole.ev_charger: ComponentState.unavailable,
        },
        data_age_seconds=dict.fromkeys(ALL_DEVICE_ROLES),
        system_clock_status="valid",
    )
    state.snapshot = snap
    activate = client.post(
        "/installer/setup/constraints/activate",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert activate.status_code == 302  # WARN ⇒ activation still succeeds


async def test_e2e_provider_snapshot_updated_after_activation(
    app_e2e,
) -> None:
    """The activate route refreshes the provider; subsequent provider.get()
    returns the new constraints.
    """
    app, _ = app_e2e
    await _add_grid_meter()
    raw, _ = await _create_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "40",
            "battery_reserve_floor_percent": "15",
        },
    )
    activate = client.post(
        "/installer/setup/constraints/activate",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert activate.status_code == 302
    provider = app.state.active_constraints_provider
    assert provider.get().peak_limit_kw == 40.0
    assert provider.get().battery_reserve_floor_percent == 15.0


# ---------------------------------------------------------------------------
# FAIL-abort coverage (Story 9.Y-a — closes R2P3 from 9-3 round-2 review)
#
# These tests anchor Story 9.3 AC6 step 5 end-to-end: "a state change between
# prior validate and activate that flips a check to FAIL aborts activation,
# leaves no `active_constraints` row, and persists the new FAIL report to the
# draft." Until 9.Y-a, only unit-level coverage existed (the WARN variant) and
# the misnamed e2e test above did not actually exercise a FAIL outcome.
#
# Today the only `safety_pre_check` FAIL outcome is the structural
# `peak_limit_kw < 0.5 kW` rule (see `_check_safety_pre_pure`). That rule is a
# property of the draft itself, not of the snapshot. The two scenarios below
# cover the two routes that can land at `activate` with such a draft:
#   (1) activate without ever calling /validate;
#   (2) validate-pass, then re-edit the draft to a FAIL value (which
#       `upsert_draft` quietly resets to `validation_status='pending'`),
#       then activate without re-validating.
# In both cases the activate route re-runs validation inside the write lock
# and discovers the fresh FAIL.
# ---------------------------------------------------------------------------


async def _count_rows(table: str) -> int:
    conn = get_connection()
    async with conn.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row["n"])


async def test_e2e_activate_without_prior_validate_aborts_on_structural_fail(
    app_e2e,
) -> None:
    """Activate directly with ``peak_limit_kw=0.4`` (below the 0.5 kW
    structural-FAIL threshold) without an intervening ``/validate`` call.
    The activate route re-runs validation inside the write lock, discovers
    the fresh structural FAIL from ``_check_safety_pre_pure``, persists
    ``validation_status='failed'`` to the draft, leaves the
    ``active_constraints`` + ``config_audit_log`` tables untouched, and
    returns HTTP 400.

    Story 9.Y-a AC1; closes the HIGH-severity FAIL-abort E2E coverage gap
    (R2P3 from 9-3 round-2 review).
    """
    app, _ = app_e2e
    await _add_grid_meter()
    raw, sid = await _create_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)

    active_before = await _count_rows("active_constraints")
    audit_before = await _count_rows("config_audit_log")

    client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "0.4",
            "battery_reserve_floor_percent": "20",
        },
    )
    # No /validate call. Drive the activate route directly so the FAIL
    # surfaces inside the activate route's re-validation, not at validate-time.
    activate = client.post(
        "/installer/setup/constraints/activate",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
    )

    assert activate.status_code == 400

    # No new active_constraints row was committed; the fixture seed at
    # config_version=1 (peak=25.0, floor=20.0) is the only row.
    assert await _count_rows("active_constraints") == active_before
    conn = get_connection()
    async with conn.execute(
        "SELECT peak_limit_kw, battery_reserve_floor_percent, config_version"
        " FROM active_constraints"
    ) as cur:
        rows = list(await cur.fetchall())
    assert len(rows) == 1
    assert rows[0]["peak_limit_kw"] == 25.0
    assert rows[0]["battery_reserve_floor_percent"] == 20.0
    assert rows[0]["config_version"] == 1

    # No new audit row was written.
    assert await _count_rows("config_audit_log") == audit_before

    # The draft carries the fresh FAIL outcome.
    persisted = await DraftConstraintsRepo().get(sid)
    assert persisted is not None
    assert persisted.validation_status == "failed"
    assert persisted.validation_report is not None
    assert persisted.validation_report.overall_status == "failed"
    fail_checks = [c for c in persisted.validation_report.checks if c.status == "fail"]
    assert len(fail_checks) == 1
    fail = fail_checks[0]
    assert fail.name == "safety_pre_check"
    assert fail.field == "peak_limit_kw"
    assert fail.message == ("Peak limit below 0.5 kW would block all grid imports indefinitely.")

    # Wizard state did not advance to step_3_complete.
    wizard = await WizardStateRepo().get(sid)
    assert wizard is not None
    assert wizard.step_3_complete is False
    assert wizard.step_3_activated_config_version is None

    # The provider snapshot is unchanged — no reload side-effect leaked from
    # the failed activation.
    provider = app.state.active_constraints_provider
    assert provider.get().peak_limit_kw == 25.0
    assert provider.get().battery_reserve_floor_percent == 20.0


async def test_e2e_activate_aborts_when_re_validation_finds_fresh_structural_fail(
    app_e2e,
) -> None:
    """Validate a passing draft (peak=30); then re-edit the draft to
    ``peak_limit_kw=0.4`` (the ``upsert_draft`` path resets
    ``validation_status='pending'``); then activate without re-validating.
    The activate route's in-lock re-validation surfaces the fresh structural
    FAIL even though the installer never saw it at validate-time. Same
    contract assertions as the direct-activate case: 400 status, no new
    active_constraints or audit rows, persisted FAIL on the draft, wizard
    state unmoved, provider snapshot unchanged.

    Story 9.Y-a AC2; exercises the validate-pass → re-edit-FAIL flow that
    the original misnamed test claimed to cover but did not.
    """
    app, _ = app_e2e
    await _add_grid_meter()
    raw, sid = await _create_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)

    # First draft + validate establishes the validate-pass precondition.
    client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "30",
            "battery_reserve_floor_percent": "20",
        },
    )
    validate_pass = client.post(
        "/installer/setup/constraints/validate",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert "Overall: VALID" in validate_pass.text

    active_before = await _count_rows("active_constraints")
    audit_before = await _count_rows("config_audit_log")

    # Re-edit the draft to a structural-FAIL value. upsert_draft resets
    # validation_status to 'pending'; the previous 'valid' status is no
    # longer authoritative for activation gating.
    client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "0.4",
            "battery_reserve_floor_percent": "20",
        },
    )
    # Activate WITHOUT re-validating — the FAIL surfaces only in the
    # activate route's in-lock re-validation.
    activate = client.post(
        "/installer/setup/constraints/activate",
        cookies={"session": raw},
        headers={"X-CSRF-Token": _CSRF},
    )

    assert activate.status_code == 400

    # The rendered banner carries both the operator-facing reason copy and
    # the per-check FAIL message from _check_safety_pre_pure.
    body = activate.text
    assert "Cannot activate constraints" in body
    assert "Validation failed" in body  # reason_message for constraints_validation_failed
    assert "safety_pre_check" in body
    assert "Peak limit below 0.5 kW would block all grid imports indefinitely." in body

    # DB-state assertions mirror the direct-activate case.
    assert await _count_rows("active_constraints") == active_before
    assert await _count_rows("config_audit_log") == audit_before
    conn = get_connection()
    async with conn.execute("SELECT peak_limit_kw, config_version FROM active_constraints") as cur:
        rows = list(await cur.fetchall())
    assert len(rows) == 1
    assert rows[0]["peak_limit_kw"] == 25.0
    assert rows[0]["config_version"] == 1

    persisted = await DraftConstraintsRepo().get(sid)
    assert persisted is not None
    assert persisted.peak_limit_kw == 0.4  # the re-edit landed
    assert persisted.validation_status == "failed"
    assert persisted.validation_report is not None
    assert persisted.validation_report.overall_status == "failed"
    fail_checks = [c for c in persisted.validation_report.checks if c.status == "fail"]
    assert len(fail_checks) == 1
    assert fail_checks[0].name == "safety_pre_check"
    assert fail_checks[0].field == "peak_limit_kw"

    wizard = await WizardStateRepo().get(sid)
    assert wizard is not None
    assert wizard.step_3_complete is False
    assert wizard.step_3_activated_config_version is None

    provider = app.state.active_constraints_provider
    assert provider.get().peak_limit_kw == 25.0
    assert provider.get().battery_reserve_floor_percent == 20.0
