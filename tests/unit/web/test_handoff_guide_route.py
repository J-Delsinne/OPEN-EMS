"""Route-level tests for Story 9.5 — `GET /installer/handoff/guide`.

Covers AC7 unit cases:
- Auth + step-gate redirects
- Validation-state gates (400 on never_run / running / complete-FAIL with
  exact-match detail; 200 on PASS / WARN including outdated)
- Body content assertions (section headings, outdated notice)
- Structlog emission of `installer_handoff_guide_rendered`
- Side-effect-free invariant (no `event_log` insert, no state mutation)

Mirrors the fixture pattern in `tests/unit/web/test_setup_validation_routes.py`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

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
    EnergyStrategy,
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

_CSRF = "test-csrf-handoff-guide"
_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Inline DDL (mirrors 9.4 route-test fixture; intentional duplication per
# the cross-fixture deferred-finding tracked in deferred-work.md)
# ---------------------------------------------------------------------------

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
    # event_log present so the AC1 side-effect-free row-count assertion can be
    # made against a real table (matches the schema in
    # migrations/versions/0005_add_event_log_table.py).
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
        active_strategy=EnergyStrategy.maximize_self_consumption,
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
    """Reachable, FULL-capability probe outcomes for every device."""

    async def probe(self, entry: DeviceRegistryEntry, *, timeout_s: float) -> ProbeOutcome:
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
            capability_status=CapabilityStatus.full,
            write_capabilities=write_caps,
            error_reason=None,
            capability_profile=None,
        )


class _UnreachableFactory:
    """All-unreachable probe outcomes — drives connectivity FAIL."""

    async def probe(self, entry: DeviceRegistryEntry, *, timeout_s: float) -> ProbeOutcome:
        return ProbeOutcome(
            device_id=entry.device_id,
            role=entry.role,
            protocol=entry.protocol,
            reachable=False,
            timed_out=False,
            capability_status=None,
            write_capabilities=frozenset(),
            error_reason="injected",
            capability_profile=None,
        )


@pytest_asyncio.fixture
async def app_with_validation(
    session_repo: SessionRepo,
) -> AsyncGenerator[tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo], None]:
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
    yield app, provider, config_repo


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
# Auth
# ---------------------------------------------------------------------------


def test_unauthenticated_get_handoff_guide_redirects_to_login(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, _, _ = app_with_validation
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/handoff/guide")
    assert response.status_code == 302
    assert "/login" in response.headers["location"]


async def test_homeowner_rejected_on_handoff_guide(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, _, _ = app_with_validation
    raw_token = await _create_homeowner_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    assert response.status_code == 302


# ---------------------------------------------------------------------------
# Step-gate enforcement (mirrors get_validation_page chain)
# ---------------------------------------------------------------------------


async def test_get_without_step_1_redirects_to_discovery(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, _, _ = app_with_validation
    raw_token, _ = await _create_installer_session(step_1=False, step_2=False, step_3=False)
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    assert response.status_code == 302
    assert response.headers["location"].endswith("/installer/setup/discovery")


async def test_get_without_step_2_redirects_to_roles(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, _, _ = app_with_validation
    raw_token, _ = await _create_installer_session(step_2=False, step_3=False)
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    assert response.status_code == 302
    assert response.headers["location"].endswith("/installer/setup/roles")


async def test_get_without_step_3_redirects_to_constraints(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, _, _ = app_with_validation
    raw_token, _ = await _create_installer_session(step_3=False)
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    assert response.status_code == 302
    assert response.headers["location"].endswith("/installer/setup/constraints")


# ---------------------------------------------------------------------------
# Validation-state gate — 400 paths (exact-match detail)
# ---------------------------------------------------------------------------


async def test_get_when_never_run_returns_400_with_exact_reason(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, _, _ = app_with_validation
    raw_token, _ = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    assert response.status_code == 400
    # D3 (2026-05-11 code-review resolution): the response is now an HTML 400 page
    # rather than the FastAPI-default JSON exception body. The exact-match AC1 token
    # is still required to appear verbatim in the body.
    assert "handoff_guide_not_available: never_run" in response.text


async def test_get_when_running_returns_400_with_exact_reason(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    """P4 (2026-05-11 code-review resolution): AC1's three rejection reasons are
    ``never_run``, ``running``, and ``complete-FAIL``. The first and third are
    covered by sibling tests; this test closes the gap by directly persisting a
    ``running`` row (rather than racing the validation runner) and asserting the
    exact-match detail token surfaces in the rendered HTML 400 body."""
    app, _, _ = app_with_validation
    await _seed_devices()
    raw_token, _ = await _create_installer_session()

    # Insert a `running` row directly so we deterministically hit the running gate.
    conn = get_connection()
    await conn.execute(
        "INSERT INTO deployment_validation_results "
        "(started_at, completed_at, config_version, overall_status, checks_json, summary_text) "
        "VALUES (?, NULL, ?, 'running', '[]', 'running')",
        (datetime.now(UTC).isoformat(), 1),
    )
    await conn.commit()

    client = TestClient(app, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    assert response.status_code == 400
    assert "handoff_guide_not_available: running" in response.text


async def test_get_when_complete_fail_returns_400_with_exact_reason(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    """Drive a complete-FAIL result by swapping in an _UnreachableFactory then
    running validation. The 400 detail must match exactly."""
    app, provider, _ = app_with_validation
    # Swap to a factory that drives FAIL on connectivity.
    app.state.deployment_validation_service._factory = _UnreachableFactory()  # noqa: SLF001
    await _seed_devices()
    raw_token, _ = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    response = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    assert response.status_code == 400
    # D3: HTML 400 page with the AC1 exact-match token in the body.
    assert "handoff_guide_not_available: complete-FAIL" in response.text


# ---------------------------------------------------------------------------
# Validation-state gate — 200 happy paths
# ---------------------------------------------------------------------------


async def test_get_when_complete_pass_renders_200_with_section_headings(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, _, _ = app_with_validation
    await _seed_devices()
    raw_token, _ = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    # PASS run.
    client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    response = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # AC4 section headings (subset — full content is the source of truth in the partial).
    assert "OPEN-EMS Homeowner Quick-Start Guide" in body
    assert "First-time access" in body
    assert "The dashboard at a glance" in body
    assert "How to change your energy strategy" in body
    assert "Understanding " in body  # section 6
    assert "Installer note" in body  # section 7 — v1 control-commands limitation
    assert "Installer contact" in body
    # Outdated notice must NOT appear when result is current.
    assert "Configuration changed since validation" not in body


async def test_get_when_outdated_pass_renders_200_with_outdated_notice(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, provider, config_repo = app_with_validation
    await _seed_devices()
    raw_token, _ = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    # Constraint activation bumps config_version → 2 → result is outdated.
    await config_repo.activate(
        ActiveConstraintsInput(peak_limit_kw=27.0, battery_reserve_floor_percent=20.0),
        actor="installer",
    )
    await provider.reload()
    response = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    # AC1 outdated-PASS: 200, not 400 — the route tolerates outdated.
    assert response.status_code == 200
    body = response.text
    assert "Configuration changed since validation" in body


# ---------------------------------------------------------------------------
# Structlog emission
# ---------------------------------------------------------------------------


async def test_get_emits_handoff_guide_rendered_structlog_event(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, _, _ = app_with_validation
    await _seed_devices()
    raw_token, session_id = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    # Reconfigure structlog so capture_logs sees the route's emission. The
    # session-wide autouse `_reset_structlog_config` fixture in conftest puts
    # the library at its defaults at test entry; we re-init the test-friendly
    # config here so the route's `logger.info(...)` actually flows to capture.
    structlog.configure(
        processors=[structlog.testing.LogCapture()],
    )
    with capture_logs() as logs:
        response = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    assert response.status_code == 200
    matching = [ev for ev in logs if ev.get("event") == "installer_handoff_guide_rendered"]
    assert len(matching) == 1
    event = matching[0]
    assert event["session_id"] == session_id
    assert event["overall_status"] == "complete-PASS"
    assert event["is_outdated"] is False


# ---------------------------------------------------------------------------
# Side-effect-free contract (AC1)
# ---------------------------------------------------------------------------


async def test_get_does_not_mutate_validation_or_wizard_state(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    """AC1 — the handler must NOT mutate `deployment_validation_results`,
    `deployment_validation_acks`, `wizard_state`, or insert an `event_log` row.
    Asserts by row-count + value comparison pre/post GET."""
    app, _, _ = app_with_validation
    await _seed_devices()
    raw_token, session_id = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    conn = get_connection()
    # Snapshot state BEFORE the guide GET.

    async def _row_counts() -> tuple[int, int, int, int]:
        # dv_results, dv_acks, wizard_state, event_log
        async with conn.execute("SELECT COUNT(*) FROM deployment_validation_results") as cur:
            (dv_results,) = await cur.fetchone()  # type: ignore[misc]
        async with conn.execute("SELECT COUNT(*) FROM deployment_validation_acks") as cur:
            (dv_acks,) = await cur.fetchone()  # type: ignore[misc]
        async with conn.execute("SELECT COUNT(*) FROM wizard_state") as cur:
            (wizard,) = await cur.fetchone()  # type: ignore[misc]
        async with conn.execute("SELECT COUNT(*) FROM event_log") as cur:
            (events,) = await cur.fetchone()  # type: ignore[misc]
        return dv_results, dv_acks, wizard, events

    async def _wizard_step_4() -> tuple[int, str | None, int | None]:
        async with conn.execute(
            "SELECT step_4_complete, step_4_completed_at, step_4_completed_config_version "
            "FROM wizard_state WHERE session_id = ?",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        return row[0], row[1], row[2]

    before_counts = await _row_counts()
    before_wizard = await _wizard_step_4()

    response = client.get("/installer/handoff/guide", cookies={"session": raw_token})
    assert response.status_code == 200

    after_counts = await _row_counts()
    after_wizard = await _wizard_step_4()

    # Row counts MUST be unchanged. Especially: zero new event_log rows.
    assert before_counts == after_counts
    # Wizard step_4 triple MUST be unchanged.
    assert before_wizard == after_wizard


# ---------------------------------------------------------------------------
# Conditional "Download handoff guide" link on the validation actions
# fragment (AC2 visibility rules, exact-match exercise)
# ---------------------------------------------------------------------------

_LINK_FRAGMENT = 'href="/installer/handoff/guide"'
_LINK_TARGET_NEW = 'target="_blank"'
_LINK_REL_NOOPENER = 'rel="noopener"'


async def test_download_link_absent_when_never_run(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, _, _ = app_with_validation
    raw_token, _ = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/validation", cookies={"session": raw_token})
    assert response.status_code == 200
    body = response.text
    assert _LINK_FRAGMENT not in body
    assert "Download handoff guide" not in body


async def test_download_link_present_when_complete_pass(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, _, _ = app_with_validation
    await _seed_devices()
    raw_token, _ = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    response = client.get("/installer/setup/validation", cookies={"session": raw_token})
    assert response.status_code == 200
    body = response.text
    # Link present with the exact href / target / rel attributes per AC2.
    assert _LINK_FRAGMENT in body
    assert _LINK_TARGET_NEW in body
    assert _LINK_REL_NOOPENER in body
    assert "Download handoff guide" in body


async def test_download_link_absent_when_complete_fail(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    app, _, _ = app_with_validation
    app.state.deployment_validation_service._factory = _UnreachableFactory()  # noqa: SLF001
    await _seed_devices()
    raw_token, _ = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    response = client.get("/installer/setup/validation", cookies={"session": raw_token})
    assert response.status_code == 200
    body = response.text
    assert _LINK_FRAGMENT not in body
    assert "Download handoff guide" not in body


async def test_download_link_absent_when_outdated_pass(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    """AC2 — the button HIDES on outdated even though the route TOLERATES it.

    This is the UX-contract assertion that proves the asymmetry between the
    button visibility (hides) and the route's 200 response (renders inline
    notice) is consistent with the spec.
    """
    app, provider, config_repo = app_with_validation
    await _seed_devices()
    raw_token, _ = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    # Bump config_version so the persisted result becomes outdated.
    await config_repo.activate(
        ActiveConstraintsInput(peak_limit_kw=27.0, battery_reserve_floor_percent=20.0),
        actor="installer",
    )
    await provider.reload()
    response = client.get("/installer/setup/validation", cookies={"session": raw_token})
    assert response.status_code == 200
    body = response.text
    assert _LINK_FRAGMENT not in body
    assert "Download handoff guide" not in body


# ---------------------------------------------------------------------------
# Handoff success page (GET /installer/handoff) inlines the guide
# (Story 9.5 AC3 — the 9.4 placeholder body is replaced)
# ---------------------------------------------------------------------------


async def test_handoff_success_page_inlines_guide_after_handoff(
    app_with_validation: tuple[FastAPI, ActiveConstraintsProvider, ConfigRepo],
) -> None:
    """End-to-end: PASS run → POST /handoff → 302 → GET /installer/handoff
    renders the success confirmation AND inlines the guide body via the same
    `_handoff_guide_body.html` partial as the standalone page."""
    app, _, _ = app_with_validation
    await _seed_devices()
    raw_token, _ = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/validation/run",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    handoff = client.post(
        "/installer/setup/validation/handoff",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert handoff.status_code == 302
    assert handoff.headers["location"].endswith("/installer/handoff")

    success = client.get("/installer/handoff", cookies={"session": raw_token})
    assert success.status_code == 200
    body = success.text
    # Confirmation block (continuity with 9.4 placeholder copy).
    assert "Handoff complete" in body
    # Inlined guide body (shared partial).
    assert "OPEN-EMS Homeowner Quick-Start Guide" in body
    assert "First-time access" in body
    # P5 (2026-05-11 code-review resolution): button label is "Print handoff guide"
    # per AC3 (was "Print this page" — locked in the divergence before the fix).
    assert "Print handoff guide" in body
    assert _LINK_FRAGMENT in body
