"""End-to-end integration tests for Story 9.5 — installer handoff guide.

Exercises the full request flow against a real in-memory SQLite using the
same fixture shape as ``tests/unit/web/test_handoff_guide_route.py``. Focused
on multi-step user journeys that the route-level unit tests do not cover:

* WARN path: run → ack → handoff → success page inlines the guide.
* Cross-AC asymmetry: outdated config makes the guide route still serve 200
  (with inline notice) while the validation page hides the download button.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core.constraints import ActiveConstraintsInput
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

_CSRF = "test-csrf-handoff-guide-e2e"
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
    """CREATE TABLE deployment_validation_results (
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
    )""",
    """CREATE TABLE deployment_validation_acks (
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
    )""",
    """CREATE TABLE event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schema_version INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        actor TEXT NOT NULL,
        event_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        detail TEXT,
        device_id TEXT,
        config_version TEXT
    )""",
]


def _snapshot() -> SystemSnapshot:
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


class _MutableFactory:
    """Same shape as the 9.4 e2e fixture — tweak reachability between runs."""

    def __init__(self) -> None:
        self._reduced: set[str] = set()

    def make_reduced(self, device_id: str) -> None:
        self._reduced.add(device_id)

    async def probe(self, entry: DeviceRegistryEntry, *, timeout_s: float) -> ProbeOutcome:
        capability_status = (
            CapabilityStatus.reduced if entry.device_id in self._reduced else CapabilityStatus.full
        )
        if entry.role is DeviceRole.battery:
            write_caps: frozenset[WriteCapability] = frozenset(
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
            capability_status=capability_status,
            write_capabilities=write_caps,
            error_reason=None,
            capability_profile=None,
        )


@pytest_asyncio.fixture
async def app_with_validation(
    session_repo: SessionRepo,
) -> AsyncGenerator[tuple[FastAPI, _MutableFactory, ActiveConstraintsProvider, ConfigRepo], None]:
    conn = get_connection()
    for ddl in _DDL:
        await conn.execute(ddl)
    await conn.commit()

    config_repo = ConfigRepo()
    device_repo = DeviceRepo()
    wizard_state_repo = WizardStateRepo()
    draft_repo = DraftConstraintsRepo()
    validation_repo = DeploymentValidationResultRepo()

    await config_repo.activate(
        ActiveConstraintsInput(peak_limit_kw=25.0, battery_reserve_floor_percent=20.0),
        actor="installer",
    )
    provider = ActiveConstraintsProvider(repo=config_repo, settings=Settings(secret_key="x"))
    await provider.hydrate()
    state_store = _StubStateStore(_snapshot())
    constraints_service = ConstraintsService(
        draft_repo=draft_repo,
        config_repo=config_repo,
        active_constraints_provider=provider,
        wizard_state_repo=wizard_state_repo,
        device_repo=device_repo,
        state_store=state_store,
    )
    factory = _MutableFactory()
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
    yield app, factory, provider, config_repo


async def _create_installer_session() -> tuple[str, str]:
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
    await wizard_repo.set_step_1_complete(session_id, now=datetime.now(UTC))
    await wizard_repo.set_step_2_complete(
        session_id, acknowledged_gaps=frozenset(), now=datetime.now(UTC)
    )
    await wizard_repo.set_step_3_complete(
        session_id, activated_config_version=1, now=datetime.now(UTC)
    )
    return raw_token, session_id


async def _seed_devices() -> None:
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
# WARN happy path: run → ack → handoff → success page inlines guide
# ---------------------------------------------------------------------------


async def test_warn_path_through_ack_and_handoff_renders_inlined_guide(
    app_with_validation: tuple[FastAPI, _MutableFactory, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, factory, _, _ = app_with_validation
    await _seed_devices()
    factory.make_reduced("meter-1")  # → connectivity WARN
    raw_token, session_id = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)

    # Run → complete-WARN.
    client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )

    # Download link IS visible on WARN with handoff-button still gated on ack.
    page = client.get("/installer/setup/validation", cookies={"session": raw_token})
    assert page.status_code == 200
    assert 'href="/installer/handoff/guide"' in page.text
    assert "Download handoff guide" in page.text

    # Standalone guide page also accessible during WARN (before ack/handoff).
    guide = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    assert guide.status_code == 200
    assert "OPEN-EMS Homeowner Quick-Start Guide" in guide.text

    # Ack the WARN.
    client.post(
        "/installer/setup/validation/acknowledge/connectivity",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )

    # Handoff now succeeds.
    handoff = client.post(
        "/installer/setup/validation/handoff",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert handoff.status_code == 302

    # Success page inlines the guide.
    success = client.get("/installer/handoff", cookies={"session": raw_token})
    assert success.status_code == 200
    body = success.text
    assert "Handoff complete" in body
    assert "OPEN-EMS Homeowner Quick-Start Guide" in body
    assert "Installer contact" in body

    # Sanity: wizard advanced.
    state = await WizardStateRepo(get_connection()).get(session_id)
    assert state is not None
    assert state.step_4_complete is True


# ---------------------------------------------------------------------------
# Cross-AC asymmetry: outdated tolerated by route, hidden by button (AC1 / AC2)
# ---------------------------------------------------------------------------


async def test_outdated_route_serves_but_validation_page_hides_button(
    app_with_validation: tuple[FastAPI, _MutableFactory, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    """AC1 + AC2 cross-AC consistency:
       - GET /installer/handoff/guide tolerates outdated → 200 + inline notice.
       - GET /installer/setup/validation HIDES the download button on outdated.
    Both halves asserted in one test so future refactors cannot regress one
    without the other.
    """
    app, _, provider, config_repo = app_with_validation
    await _seed_devices()
    raw_token, _ = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)

    # PASS at config_version=1.
    client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )

    # Bump config_version → outdated.
    await config_repo.activate(
        ActiveConstraintsInput(peak_limit_kw=27.0, battery_reserve_floor_percent=20.0),
        actor="installer",
    )
    await provider.reload()

    # Route TOLERATES outdated.
    guide = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    assert guide.status_code == 200
    assert "Configuration changed since validation" in guide.text

    # Validation page HIDES the button.
    page = client.get("/installer/setup/validation", cookies={"session": raw_token})
    assert page.status_code == 200
    assert "Download handoff guide" not in page.text
    assert 'href="/installer/handoff/guide"' not in page.text
