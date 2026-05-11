"""Route-level tests for Step 4 — Deployment Validation (Story 9.4 AC11)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core.devices import (
    BatteryState,
    CapabilityStatus,
    DeviceRole,
    GridMeterState,
    WriteCapability,
)
from open_ems.core.state import (
    ComponentState,
    GlobalState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.services.audit_log import ObservabilityService
from open_ems.services.constraints import ConstraintsService
from open_ems.services.deployment_validation import DeploymentValidationService
from open_ems.services.protocol_adapter_factory import ProbeOutcome
from open_ems.settings import Settings
from open_ems.storage.database import get_connection
from open_ems.storage.repositories.config_repo import ConfigRepo
from open_ems.storage.repositories.deployment_validation_repo import (
    DeploymentValidationResultRepo,
)
from open_ems.storage.repositories.device_repo import (
    DeviceRegistryEntry,
    DeviceRepo,
    ManualDeviceEntryInput,
)
from open_ems.storage.repositories.draft_constraints_repo import DraftConstraintsRepo
from open_ems.storage.repositories.event_log_repo import EventLogRepo
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.storage.repositories.wizard_state_repo import WizardStateRepo
from open_ems.web.csrf import CsrfMiddleware
from open_ems.web.routes.setup import router as setup_router

_CSRF = "test-csrf-validation-fixed"
_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Inline DDL (same shape as 9.3 + new 9.4 tables)
# ---------------------------------------------------------------------------

_CREATE_DEVICE_REGISTRY = """
    CREATE TABLE device_registry (
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
    )
"""
_CREATE_WIZARD_STATE = """
    CREATE TABLE wizard_state (
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
    )
"""
_CREATE_ACTIVE_CONSTRAINTS = """
    CREATE TABLE active_constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        peak_limit_kw REAL NOT NULL CHECK (peak_limit_kw > 0),
        battery_reserve_floor_percent REAL NOT NULL,
        config_version INTEGER NOT NULL UNIQUE,
        activated_at TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('system', 'installer')),
        ev_charging_window_start TEXT,
        ev_charging_window_end TEXT
    )
"""
_CREATE_CONFIG_AUDIT_LOG = """
    CREATE TABLE config_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        field TEXT NOT NULL,
        previous_value TEXT NOT NULL,
        new_value TEXT NOT NULL,
        config_version INTEGER NOT NULL
    )
"""
_CREATE_DRAFT_CONSTRAINTS = """
    CREATE TABLE draft_constraints (
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
    )
"""
_CREATE_DV_RESULTS = """
    CREATE TABLE deployment_validation_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        config_version INTEGER NOT NULL CHECK (config_version >= 0),
        overall_status TEXT NOT NULL
            CHECK (overall_status IN ('running', 'complete-PASS',
                                      'complete-WARN', 'complete-FAIL')),
        checks_json TEXT NOT NULL,
        triggered_by_session_id TEXT,
        summary_text TEXT NOT NULL,
        FOREIGN KEY (triggered_by_session_id) REFERENCES sessions(id)
            ON DELETE SET NULL
    )
"""
_CREATE_DV_ACKS = """
    CREATE TABLE deployment_validation_acks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        validation_result_id INTEGER NOT NULL,
        check_name TEXT NOT NULL
            CHECK (check_name IN ('connectivity', 'role_completeness',
                                  'capability_strategy', 'constraint_completeness',
                                  'constraint_safety_pre_check', 'control_readiness')),
        acknowledged_at TEXT NOT NULL,
        acknowledged_by_session_id TEXT,
        UNIQUE (validation_result_id, check_name),
        FOREIGN KEY (validation_result_id)
            REFERENCES deployment_validation_results(id) ON DELETE CASCADE,
        FOREIGN KEY (acknowledged_by_session_id) REFERENCES sessions(id)
            ON DELETE SET NULL
    )
"""


def _populated_snapshot() -> SystemSnapshot:
    return SystemSnapshot(
        sequence_id=1,
        captured_at=_NOW,
        global_state=GlobalState.normal,
        operating_mode=SystemOperatingMode.normal,
        inverter=None,
        battery=BatteryState(
            device_id="batt-1",
            soc_percent=80.0,
            battery_power_kw=0.0,
            capacity_kwh=10.0,
            operating_mode="idle",
            read_at=_NOW,
        ),
        ev_charger=None,
        grid_meter=GridMeterState(
            device_id="meter-1",
            grid_power_kw=2.0,
            energy_delivered_kwh=100.0,
            energy_returned_kwh=50.0,
            received_at=_NOW,
        ),
        component_states={
            DeviceRole.inverter: ComponentState.unavailable,
            DeviceRole.battery: ComponentState.active,
            DeviceRole.ev_charger: ComponentState.unavailable,
            DeviceRole.grid_meter: ComponentState.active,
        },
        data_age_seconds={
            DeviceRole.inverter: None,
            DeviceRole.battery: 0,
            DeviceRole.ev_charger: None,
            DeviceRole.grid_meter: 0,
        },
        system_clock_status="valid",
    )


class _StubStateStore:
    def __init__(self, snapshot: SystemSnapshot) -> None:
        self._snapshot = snapshot

    def get_snapshot(self) -> SystemSnapshot:
        return self._snapshot


class _AllReachableFactory:
    """Synthesises reachable outcomes for every device, with FULL capabilities."""

    async def probe(self, entry: DeviceRegistryEntry, *, timeout_s: float) -> ProbeOutcome:
        write_caps: frozenset[WriteCapability]
        if entry.role is DeviceRole.battery:
            write_caps = frozenset(
                {WriteCapability.set_charge_rate, WriteCapability.set_discharge_rate}
            )
        elif entry.role is DeviceRole.ev_charger:
            write_caps = frozenset({WriteCapability.set_ev_charge_current})
        else:
            write_caps = frozenset()
        return ProbeOutcome(
            device_id=entry.device_id,
            role=entry.role,
            protocol=entry.protocol,
            reachable=True,
            timed_out=False,
            capability_status=CapabilityStatus.full,
            write_capabilities=write_caps,
            error_reason=None,
            capability_profile=None,
        )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_with_validation(
    session_repo: SessionRepo,
) -> AsyncGenerator[FastAPI, None]:
    conn = get_connection()
    for ddl in (
        _CREATE_DEVICE_REGISTRY,
        _CREATE_WIZARD_STATE,
        _CREATE_ACTIVE_CONSTRAINTS,
        _CREATE_CONFIG_AUDIT_LOG,
        _CREATE_DRAFT_CONSTRAINTS,
        _CREATE_DV_RESULTS,
        _CREATE_DV_ACKS,
    ):
        await conn.execute(ddl)
    await conn.commit()

    config_repo = ConfigRepo()
    device_repo = DeviceRepo()
    wizard_state_repo = WizardStateRepo()
    draft_repo = DraftConstraintsRepo()
    validation_repo = DeploymentValidationResultRepo()

    await config_repo.activate(_make_input(25.0, 20.0), actor="installer")
    provider = ActiveConstraintsProvider(repo=config_repo, settings=Settings(secret_key="x"))
    await provider.hydrate()
    state_store = _StubStateStore(_populated_snapshot())
    constraints_service = ConstraintsService(
        draft_repo=draft_repo,
        config_repo=config_repo,
        active_constraints_provider=provider,
        wizard_state_repo=wizard_state_repo,
        device_repo=device_repo,
        state_store=state_store,
    )
    factory = _AllReachableFactory()
    validation_service = DeploymentValidationService(
        validation_repo=validation_repo,
        device_repo=device_repo,
        wizard_state_repo=wizard_state_repo,
        active_constraints_provider=provider,
        constraints_service=constraints_service,
        protocol_adapter_factory=factory,  # type: ignore[arg-type]
        state_store=state_store,  # type: ignore[arg-type]
        observability=ObservabilityService(repo=EventLogRepo(conn)),
        check_timeout_seconds=2.0,
        device_probe_timeout_seconds=1.0,
    )

    app = FastAPI()
    app.state.device_repo = device_repo
    app.state.wizard_state_repo = wizard_state_repo
    app.state.constraints_service = constraints_service
    app.state.draft_constraints_repo = draft_repo
    app.state.deployment_validation_repo = validation_repo
    app.state.deployment_validation_service = validation_service
    app.include_router(setup_router)
    app.add_middleware(CsrfMiddleware)
    yield app


def _make_input(peak: float, floor: float):
    from open_ems.core.constraints import ActiveConstraintsInput

    return ActiveConstraintsInput(
        peak_limit_kw=peak,
        battery_reserve_floor_percent=floor,
    )


async def _create_installer_session(
    *,
    step_1: bool = True,
    step_2: bool = True,
    step_3: bool = True,
) -> tuple[str, str]:
    user_id = await UserRepo(get_connection()).create(
        username=f"installer_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role="installer",
    )
    raw_token = generate_session_token()
    session_id = await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF,
    )
    wizard_repo = WizardStateRepo()
    await wizard_repo.get_or_create(session_id, now=datetime.now(UTC))
    if step_1:
        await wizard_repo.set_step_1_complete(session_id, now=datetime.now(UTC))
    if step_2:
        await wizard_repo.set_step_2_complete(
            session_id, acknowledged_gaps=frozenset(), now=datetime.now(UTC)
        )
    if step_3:
        await wizard_repo.set_step_3_complete(
            session_id, activated_config_version=1, now=datetime.now(UTC)
        )
    return raw_token, session_id


async def _create_homeowner_session() -> str:
    user_id = await UserRepo(get_connection()).create(
        username=f"homeowner_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role="homeowner",
    )
    raw_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF,
    )
    return raw_token


async def _seed_grid_meter_and_battery() -> None:
    """Register the two devices used by every test (grid_meter + battery)."""
    repo = DeviceRepo(get_connection())
    await repo.upsert_manual(
        ManualDeviceEntryInput(
            device_id="meter-1",
            protocol="dsmr_p1",
            address="/dev/ttyUSB0",
            model="dsmr_p1",
        ),
        first_seen_at=datetime.now(UTC),
    )
    await repo.mark_validated(
        "meter-1",
        last_capability_status="full",
        last_limitation_reason=None,
        last_seen_at=datetime.now(UTC),
    )
    await repo.assign_role("meter-1", role=DeviceRole.grid_meter, assigned_at=datetime.now(UTC))
    await repo.upsert_manual(
        ManualDeviceEntryInput(
            device_id="batt-1",
            protocol="modbus_tcp",
            address="192.168.1.10:502",
            model="byd_hvs_v1",
        ),
        first_seen_at=datetime.now(UTC),
    )
    await repo.mark_validated(
        "batt-1",
        last_capability_status="full",
        last_limitation_reason=None,
        last_seen_at=datetime.now(UTC),
    )
    await repo.assign_role("batt-1", role=DeviceRole.battery, assigned_at=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Auth + CSRF
# ---------------------------------------------------------------------------


def test_unauthenticated_get_validation_redirects_to_login(
    app_with_validation: FastAPI,
) -> None:
    client = TestClient(app_with_validation, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/validation")
    assert response.status_code == 302
    assert "/login" in response.headers["location"]


async def test_homeowner_rejected_on_validation_get(
    app_with_validation: FastAPI,
) -> None:
    raw_token = await _create_homeowner_session()
    client = TestClient(app_with_validation, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/validation", cookies={"session": raw_token})
    assert response.status_code == 302


async def test_run_without_csrf_returns_403(
    app_with_validation: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_validation, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Step-gate enforcement
# ---------------------------------------------------------------------------


async def test_get_without_step_1_redirects_to_discovery(
    app_with_validation: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session(step_1=False, step_2=False, step_3=False)
    client = TestClient(app_with_validation, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/validation", cookies={"session": raw_token})
    assert response.status_code == 302
    assert response.headers["location"].endswith("/installer/setup/discovery")


async def test_get_without_step_2_redirects_to_roles(
    app_with_validation: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session(step_2=False, step_3=False)
    client = TestClient(app_with_validation, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/validation", cookies={"session": raw_token})
    assert response.status_code == 302
    assert response.headers["location"].endswith("/installer/setup/roles")


async def test_get_without_step_3_redirects_to_constraints(
    app_with_validation: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session(step_3=False)
    client = TestClient(app_with_validation, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/validation", cookies={"session": raw_token})
    assert response.status_code == 302
    assert response.headers["location"].endswith("/installer/setup/constraints")


# ---------------------------------------------------------------------------
# GET — never_run / page rendering
# ---------------------------------------------------------------------------


async def test_get_renders_never_run_state(
    app_with_validation: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_validation, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/validation", cookies={"session": raw_token})
    assert response.status_code == 200
    body = response.text
    assert "Run validation to check deployment readiness" in body
    # Handoff button must be disabled in the never_run state.
    assert "disabled" in body
    assert "Complete handoff" in body


# ---------------------------------------------------------------------------
# POST /run
# ---------------------------------------------------------------------------


async def test_run_completes_pass_and_emits_hx_trigger(
    app_with_validation: FastAPI,
) -> None:
    await _seed_grid_meter_and_battery()
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_validation, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "validation-complete"
    assert "System is ready. You can complete handoff." in response.text


# ---------------------------------------------------------------------------
# POST /handoff
# ---------------------------------------------------------------------------


async def test_handoff_succeeds_after_pass(
    app_with_validation: FastAPI,
) -> None:
    await _seed_grid_meter_and_battery()
    raw_token, session_id = await _create_installer_session()
    client = TestClient(app_with_validation, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    response = client.post(
        "/installer/setup/validation/handoff",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 302
    assert response.headers["location"].endswith("/installer/handoff")
    state = await WizardStateRepo(get_connection()).get(session_id)
    assert state is not None
    assert state.step_4_complete is True
    assert state.step_4_completed_config_version == 1


async def test_handoff_rejects_when_never_run(
    app_with_validation: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_validation, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/validation/handoff",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 400
    assert "never_run" in response.text


# ---------------------------------------------------------------------------
# Acknowledge / revoke
# ---------------------------------------------------------------------------


async def test_acknowledge_unknown_check_returns_400(
    app_with_validation: FastAPI,
) -> None:
    # P27 — unknown check_name is a 400 (bad request payload), consistent with
    # every other validation rejection in this route module.
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_validation, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/validation/acknowledge/nonexistent",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 400
    assert "unknown_check_name" in response.json()["detail"]


async def test_acknowledge_when_no_current_result_returns_400(
    app_with_validation: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_validation, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/validation/acknowledge/connectivity",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 400
