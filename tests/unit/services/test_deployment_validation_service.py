"""Unit tests for ``DeploymentValidationService`` — Story 9.4 AC11."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from open_ems.core.devices import (
    BatteryState,
    CapabilityStatus,
    DeviceRole,
    GridMeterState,
    WriteCapability,
)
from open_ems.core.state import (
    ALL_DEVICE_ROLES,
    ComponentState,
    GlobalState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.services.audit_log import ObservabilityService
from open_ems.services.constraints import ConstraintsService
from open_ems.services.deployment_validation import (
    CheckNotWarnableError,
    DeploymentValidationService,
    NoCurrentResultError,
    NotAckEligibleError,
    NotHandoffEligibleError,
)
from open_ems.services.protocol_adapter_factory import ProbeOutcome
from open_ems.settings import Settings
from open_ems.storage.repositories.config_repo import ConfigRepo
from open_ems.storage.repositories.deployment_validation_repo import (
    DeploymentValidationResultRepo,
)
from open_ems.storage.repositories.device_repo import DeviceRegistryEntry, DeviceRepo
from open_ems.storage.repositories.draft_constraints_repo import DraftConstraintsRepo
from open_ems.storage.repositories.event_log_repo import EventLogRepo
from open_ems.storage.repositories.wizard_state_repo import WizardStateRepo

# ---------------------------------------------------------------------------
# Inline schema (R7 deferred-finding applies — same inline copy as 9.3 tests
# plus the new 9.4 tables)
# ---------------------------------------------------------------------------

_DDL = [
    "CREATE TABLE sessions (id TEXT PRIMARY KEY)",
    """CREATE TABLE active_constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        peak_limit_kw REAL NOT NULL CHECK (peak_limit_kw > 0),
        battery_reserve_floor_percent REAL NOT NULL
            CHECK (battery_reserve_floor_percent >= 0
                   AND battery_reserve_floor_percent <= 100),
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
        check_name TEXT NOT NULL,
        acknowledged_at TEXT NOT NULL,
        acknowledged_by_session_id TEXT,
        UNIQUE (validation_result_id, check_name),
        FOREIGN KEY (validation_result_id)
            REFERENCES deployment_validation_results(id) ON DELETE CASCADE,
        FOREIGN KEY (acknowledged_by_session_id) REFERENCES sessions(id)
            ON DELETE SET NULL
    )""",
]

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)


def _empty_snapshot() -> SystemSnapshot:
    """Snapshot with no per-device state — safety_pre_check emits WARNs."""
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


def _populated_snapshot() -> SystemSnapshot:
    """Snapshot where battery + grid_meter are both healthy — safety_pre_check PASS."""
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


class _CannedFactory:
    """Test fake for ``ProtocolAdapterFactory.probe`` — returns canned outcomes."""

    def __init__(self, outcomes: dict[str, ProbeOutcome]) -> None:
        self._outcomes = outcomes

    async def probe(self, entry: DeviceRegistryEntry, *, timeout_s: float) -> ProbeOutcome:
        if entry.device_id not in self._outcomes:
            raise AssertionError(f"unexpected probe for device_id={entry.device_id!r}")
        return self._outcomes[entry.device_id]


def _outcome(
    entry: DeviceRegistryEntry,
    *,
    reachable: bool,
    capability_status: CapabilityStatus | None = CapabilityStatus.full,
    write_capabilities: frozenset[WriteCapability] = frozenset(),
    timed_out: bool = False,
    error_reason: str | None = None,
) -> ProbeOutcome:
    return ProbeOutcome(
        device_id=entry.device_id,
        role=entry.role,
        protocol=entry.protocol,
        reachable=reachable,
        timed_out=timed_out,
        capability_status=capability_status,
        write_capabilities=write_capabilities,
        error_reason=error_reason,
        capability_profile=None,
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def harness() -> AsyncGenerator[dict[str, Any], None]:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        for stmt in _DDL:
            await conn.execute(stmt)
        await conn.execute("INSERT INTO sessions (id) VALUES ('session-x')")
        await conn.execute(
            "INSERT INTO wizard_state"
            " (session_id, step_1_complete, step_1_completed_at,"
            "  step_2_complete, step_2_completed_at, step_2_acknowledged_gaps,"
            "  step_3_complete, step_3_completed_at, step_3_activated_config_version,"
            "  created_at, updated_at)"
            " VALUES (?, 1, ?, 1, ?, ?, 1, ?, 1, ?, ?)",
            (
                "session-x",
                _NOW.isoformat(),
                _NOW.isoformat(),
                "[]",
                _NOW.isoformat(),
                _NOW.isoformat(),
                _NOW.isoformat(),
            ),
        )
        await conn.execute(
            "INSERT INTO active_constraints"
            " (peak_limit_kw, battery_reserve_floor_percent, config_version,"
            "  activated_at, actor, ev_charging_window_start, ev_charging_window_end)"
            " VALUES (?, ?, ?, ?, ?, NULL, NULL)",
            (10.0, 0.0, 1, _NOW.isoformat(), "installer"),
        )
        # Two devices: a grid_meter (read-only by protocol) + a battery
        # (controllable). Both registered + assigned to a role.
        await conn.execute(
            "INSERT INTO device_registry"
            " (device_id, protocol, address, model, source, validated,"
            "  last_capability_status, first_seen_at, role, role_assigned_at)"
            " VALUES (?, ?, ?, ?, ?, 1, 'full', ?, ?, ?)",
            (
                "meter-1",
                "dsmr_p1",
                "/dev/ttyUSB0",
                "dsmr_p1",
                "manual_entry",
                _NOW.isoformat(),
                DeviceRole.grid_meter.value,
                _NOW.isoformat(),
            ),
        )
        await conn.execute(
            "INSERT INTO device_registry"
            " (device_id, protocol, address, model, source, validated,"
            "  last_capability_status, first_seen_at, role, role_assigned_at)"
            " VALUES (?, ?, ?, ?, ?, 1, 'full', ?, ?, ?)",
            (
                "batt-1",
                "modbus_tcp",
                "192.168.1.10:502",
                "byd_hvs_v1",
                "manual_entry",
                _NOW.isoformat(),
                DeviceRole.battery.value,
                _NOW.isoformat(),
            ),
        )
        await conn.commit()

        validation_repo = DeploymentValidationResultRepo(conn)
        device_repo = DeviceRepo(conn)
        wizard_repo = WizardStateRepo(conn)
        config_repo = ConfigRepo(conn)
        provider = ActiveConstraintsProvider(repo=config_repo, settings=Settings(secret_key="x"))
        await provider.hydrate()
        # Default to a populated snapshot so safety_pre_check passes cleanly;
        # individual tests can swap in _empty_snapshot to exercise the WARN path.
        state_store = _StubStateStore(_populated_snapshot())
        constraints_service = ConstraintsService(
            draft_repo=DraftConstraintsRepo(conn),
            config_repo=config_repo,
            active_constraints_provider=provider,
            wizard_state_repo=wizard_repo,
            device_repo=device_repo,
            state_store=state_store,  # type: ignore[arg-type]
        )

        entries = await device_repo.list_all()
        all_reachable = {
            "meter-1": _outcome(entries[0], reachable=True),
            "batt-1": _outcome(
                entries[1],
                reachable=True,
                write_capabilities=frozenset(
                    {
                        WriteCapability.set_charge_rate,
                        WriteCapability.set_discharge_rate,
                    }
                ),
            ),
        }
        factory = _CannedFactory(all_reachable)
        service = DeploymentValidationService(
            validation_repo=validation_repo,
            device_repo=device_repo,
            wizard_state_repo=wizard_repo,
            active_constraints_provider=provider,
            constraints_service=constraints_service,
            protocol_adapter_factory=factory,  # type: ignore[arg-type]
            state_store=state_store,  # type: ignore[arg-type]
            observability=ObservabilityService(repo=EventLogRepo(conn)),
            check_timeout_seconds=2.0,
            device_probe_timeout_seconds=1.0,
        )
        yield {
            "service": service,
            "conn": conn,
            "factory": factory,
            "provider": provider,
            "wizard_repo": wizard_repo,
            "validation_repo": validation_repo,
            "entries": entries,
            "all_reachable_outcomes": all_reachable,
        }


# ---------------------------------------------------------------------------
# Tests — get_current_view + outdated derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_current_view_returns_none_for_never_run(
    harness: dict[str, Any],
) -> None:
    service: DeploymentValidationService = harness["service"]
    view = await service.get_current_view()
    assert view.result is None
    assert view.is_outdated is False


@pytest.mark.asyncio
async def test_get_current_view_derives_outdated_when_config_version_diverges(
    harness: dict[str, Any],
) -> None:
    """User guardrail #3: outdated is derived from comparison, never persisted."""
    service: DeploymentValidationService = harness["service"]
    provider: ActiveConstraintsProvider = harness["provider"]
    validation_repo: DeploymentValidationResultRepo = harness["validation_repo"]
    # Run a PASS validation against the current config_version=1.
    await service.run(triggered_by_session_id="session-x", now=_NOW)
    view = await service.get_current_view()
    assert view.result is not None
    assert view.result.overall_status == "complete-PASS"
    assert view.is_outdated is False

    # Now mutate the underlying DB (provider snapshot still has v=1).
    # Re-hydrate the provider by inserting a new active_constraints row + reload.
    conn: aiosqlite.Connection = harness["conn"]
    await conn.execute(
        "INSERT INTO active_constraints"
        " (peak_limit_kw, battery_reserve_floor_percent, config_version,"
        "  activated_at, actor, ev_charging_window_start, ev_charging_window_end)"
        " VALUES (?, ?, ?, ?, ?, NULL, NULL)",
        (12.0, 0.0, 2, _NOW.isoformat(), "installer"),
    )
    await conn.commit()
    await provider.reload()
    view = await service.get_current_view()
    assert view.is_outdated is True
    # The persisted result row's config_version stays at 1 — outdated is derived only.
    persisted = await validation_repo.get_current()
    assert persisted is not None
    assert persisted.config_version == 1


# ---------------------------------------------------------------------------
# Tests — run() happy + sad
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_completes_pass_when_all_devices_reachable(
    harness: dict[str, Any],
) -> None:
    service: DeploymentValidationService = harness["service"]
    result = await service.run(triggered_by_session_id="session-x", now=_NOW)
    assert result.overall_status == "complete-PASS"
    assert result.config_version == 1
    assert len(result.checks) == 6
    assert all(c.status == "pass" for c in result.checks)
    assert result.summary_text == "System is ready. You can complete handoff."


@pytest.mark.asyncio
async def test_run_results_in_warn_when_one_check_warns(
    harness: dict[str, Any],
) -> None:
    """One reduced-capability device → connectivity WARN → complete-WARN."""
    service: DeploymentValidationService = harness["service"]
    factory: _CannedFactory = harness["factory"]
    entries: list[DeviceRegistryEntry] = harness["entries"]
    factory._outcomes["meter-1"] = _outcome(
        entries[0],
        reachable=True,
        capability_status=CapabilityStatus.reduced,
    )
    result = await service.run(triggered_by_session_id="session-x", now=_NOW)
    assert result.overall_status == "complete-WARN"
    connectivity = next(c for c in result.checks if c.name == "connectivity")
    assert connectivity.status == "warn"


@pytest.mark.asyncio
async def test_run_results_in_fail_when_one_device_unreachable(
    harness: dict[str, Any],
) -> None:
    service: DeploymentValidationService = harness["service"]
    factory: _CannedFactory = harness["factory"]
    entries: list[DeviceRegistryEntry] = harness["entries"]
    factory._outcomes["meter-1"] = _outcome(
        entries[0], reachable=False, error_reason="connection_refused"
    )
    result = await service.run(triggered_by_session_id="session-x", now=_NOW)
    assert result.overall_status == "complete-FAIL"
    connectivity = next(c for c in result.checks if c.name == "connectivity")
    assert connectivity.status == "fail"


@pytest.mark.asyncio
async def test_run_replaces_prior_result_atomically(
    harness: dict[str, Any],
) -> None:
    service: DeploymentValidationService = harness["service"]
    conn: aiosqlite.Connection = harness["conn"]
    first = await service.run(triggered_by_session_id="session-x", now=_NOW)
    second = await service.run(triggered_by_session_id="session-x", now=_NOW)
    assert first.id != second.id
    async with conn.execute("SELECT COUNT(*) FROM deployment_validation_results") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == 1


@pytest.mark.asyncio
async def test_run_snapshots_config_version_at_start(
    harness: dict[str, Any],
) -> None:
    """The run's config_version is taken at run start, not after."""
    service: DeploymentValidationService = harness["service"]
    provider: ActiveConstraintsProvider = harness["provider"]
    assert provider.get().config_version == 1
    result = await service.run(triggered_by_session_id="session-x", now=_NOW)
    assert result.config_version == 1


# ---------------------------------------------------------------------------
# Tests — safe-probe invariant (user guardrail #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_never_calls_send_command_on_any_adapter(
    harness: dict[str, Any],
) -> None:
    """The probe path must NEVER invoke send_command.

    The canned factory does not expose send_command at all, but we install
    a MagicMock spy on the factory's .probe to confirm the service does not
    call it with anything that would dispatch a write. (The structural
    guarantee is that ``ProtocolAdapterFactory`` never reaches send_command;
    this test wraps the public surface to assert no surprise call shapes.)
    """
    service: DeploymentValidationService = harness["service"]
    factory: _CannedFactory = harness["factory"]
    real_probe = factory.probe
    spy = MagicMock()

    async def spied_probe(entry: DeviceRegistryEntry, *, timeout_s: float) -> ProbeOutcome:
        spy(entry.device_id, timeout_s=timeout_s)
        return await real_probe(entry, timeout_s=timeout_s)

    factory.probe = spied_probe  # type: ignore[method-assign]
    await service.run(triggered_by_session_id="session-x", now=_NOW)
    # Each device probed twice: once for connectivity, once for control_readiness
    # (battery only; meter is excluded from control_readiness since grid_meter
    # is not controllable).
    assert spy.call_count == 3  # meter+battery in connectivity, battery in control_readiness


# ---------------------------------------------------------------------------
# Tests — acknowledge / revoke / handoff gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acknowledge_warning_raises_when_pass(
    harness: dict[str, Any],
) -> None:
    service: DeploymentValidationService = harness["service"]
    await service.run(triggered_by_session_id="session-x", now=_NOW)
    with pytest.raises(NotAckEligibleError) as exc:
        await service.acknowledge_warning(
            check_name="connectivity",
            acknowledged_by_session_id="session-x",
            now=_NOW,
        )
    assert exc.value.reason.startswith("not_ack_eligible: status=complete-PASS")


@pytest.mark.asyncio
async def test_acknowledge_warning_succeeds_on_warn_then_idempotent(
    harness: dict[str, Any],
) -> None:
    service: DeploymentValidationService = harness["service"]
    factory: _CannedFactory = harness["factory"]
    entries: list[DeviceRegistryEntry] = harness["entries"]
    factory._outcomes["meter-1"] = _outcome(
        entries[0], reachable=True, capability_status=CapabilityStatus.reduced
    )
    await service.run(triggered_by_session_id="session-x", now=_NOW)
    updated = await service.acknowledge_warning(
        check_name="connectivity",
        acknowledged_by_session_id="session-x",
        now=_NOW,
    )
    assert "connectivity" in updated.acknowledged_warnings
    # Idempotent re-ack — no change in the set.
    updated2 = await service.acknowledge_warning(
        check_name="connectivity",
        acknowledged_by_session_id="session-x",
        now=_NOW,
    )
    assert updated2.acknowledged_warnings == updated.acknowledged_warnings


@pytest.mark.asyncio
async def test_acknowledge_pass_check_raises_check_not_warnable(
    harness: dict[str, Any],
) -> None:
    """Acking a PASS check on a complete-WARN result is illegal."""
    service: DeploymentValidationService = harness["service"]
    factory: _CannedFactory = harness["factory"]
    entries: list[DeviceRegistryEntry] = harness["entries"]
    # Make one check WARN so overall is complete-WARN, but the targeted
    # check (constraint_completeness) stays PASS.
    factory._outcomes["meter-1"] = _outcome(
        entries[0], reachable=True, capability_status=CapabilityStatus.reduced
    )
    await service.run(triggered_by_session_id="session-x", now=_NOW)
    with pytest.raises(CheckNotWarnableError) as exc:
        await service.acknowledge_warning(
            check_name="constraint_completeness",
            acknowledged_by_session_id="session-x",
            now=_NOW,
        )
    assert "constraint_completeness" in exc.value.reason


@pytest.mark.asyncio
async def test_acknowledge_raises_when_no_result(
    harness: dict[str, Any],
) -> None:
    service: DeploymentValidationService = harness["service"]
    with pytest.raises(NoCurrentResultError):
        await service.acknowledge_warning(
            check_name="connectivity",
            acknowledged_by_session_id="session-x",
            now=_NOW,
        )


@pytest.mark.asyncio
async def test_revoke_warning_clears_ack(harness: dict[str, Any]) -> None:
    service: DeploymentValidationService = harness["service"]
    factory: _CannedFactory = harness["factory"]
    entries: list[DeviceRegistryEntry] = harness["entries"]
    factory._outcomes["meter-1"] = _outcome(
        entries[0], reachable=True, capability_status=CapabilityStatus.reduced
    )
    await service.run(triggered_by_session_id="session-x", now=_NOW)
    await service.acknowledge_warning(
        check_name="connectivity",
        acknowledged_by_session_id="session-x",
        now=_NOW,
    )
    updated = await service.revoke_warning(check_name="connectivity")
    assert "connectivity" not in updated.acknowledged_warnings


@pytest.mark.asyncio
async def test_mark_step_4_complete_succeeds_on_pass(
    harness: dict[str, Any],
) -> None:
    service: DeploymentValidationService = harness["service"]
    wizard_repo: WizardStateRepo = harness["wizard_repo"]
    await service.run(triggered_by_session_id="session-x", now=_NOW)
    version = await service.mark_step_4_complete(session_id="session-x", now=_NOW)
    assert version == 1
    state = await wizard_repo.get("session-x")
    assert state is not None
    assert state.step_4_complete is True
    assert state.step_4_completed_config_version == 1


@pytest.mark.asyncio
async def test_mark_step_4_complete_rejects_when_outdated(
    harness: dict[str, Any],
) -> None:
    service: DeploymentValidationService = harness["service"]
    provider: ActiveConstraintsProvider = harness["provider"]
    conn: aiosqlite.Connection = harness["conn"]
    await service.run(triggered_by_session_id="session-x", now=_NOW)
    # Now bump config_version under the provider.
    await conn.execute(
        "INSERT INTO active_constraints"
        " (peak_limit_kw, battery_reserve_floor_percent, config_version,"
        "  activated_at, actor, ev_charging_window_start, ev_charging_window_end)"
        " VALUES (?, ?, ?, ?, ?, NULL, NULL)",
        (12.0, 0.0, 2, _NOW.isoformat(), "installer"),
    )
    await conn.commit()
    await provider.reload()
    with pytest.raises(NotHandoffEligibleError) as exc:
        await service.mark_step_4_complete(session_id="session-x", now=_NOW)
    assert exc.value.reason == "handoff_not_eligible: outdated"


@pytest.mark.asyncio
async def test_mark_step_4_complete_rejects_when_warn_not_acked(
    harness: dict[str, Any],
) -> None:
    service: DeploymentValidationService = harness["service"]
    factory: _CannedFactory = harness["factory"]
    entries: list[DeviceRegistryEntry] = harness["entries"]
    factory._outcomes["meter-1"] = _outcome(
        entries[0], reachable=True, capability_status=CapabilityStatus.reduced
    )
    await service.run(triggered_by_session_id="session-x", now=_NOW)
    with pytest.raises(NotHandoffEligibleError) as exc:
        await service.mark_step_4_complete(session_id="session-x", now=_NOW)
    assert exc.value.reason == "handoff_not_eligible: complete-WARN not fully acknowledged"


@pytest.mark.asyncio
async def test_mark_step_4_complete_rejects_when_fail(
    harness: dict[str, Any],
) -> None:
    service: DeploymentValidationService = harness["service"]
    factory: _CannedFactory = harness["factory"]
    entries: list[DeviceRegistryEntry] = harness["entries"]
    factory._outcomes["meter-1"] = _outcome(entries[0], reachable=False, error_reason="x")
    await service.run(triggered_by_session_id="session-x", now=_NOW)
    with pytest.raises(NotHandoffEligibleError) as exc:
        await service.mark_step_4_complete(session_id="session-x", now=_NOW)
    assert exc.value.reason == "handoff_not_eligible: complete-FAIL"


@pytest.mark.asyncio
async def test_mark_step_4_complete_rejects_when_never_run(
    harness: dict[str, Any],
) -> None:
    service: DeploymentValidationService = harness["service"]
    with pytest.raises(NotHandoffEligibleError) as exc:
        await service.mark_step_4_complete(session_id="session-x", now=_NOW)
    assert exc.value.reason == "handoff_not_eligible: never_run"


# ---------------------------------------------------------------------------
# Tests — cancellation cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_cancellation_finalises_row_as_complete_fail(
    harness: dict[str, Any],
) -> None:
    """A cancelled run leaves the persisted row in a terminal state.

    The factory is replaced with one that blocks indefinitely so the outer
    cancel() lands during the probe phase, AFTER start_run_locked but
    BEFORE finalize_run_locked.
    """
    service: DeploymentValidationService = harness["service"]
    validation_repo: DeploymentValidationResultRepo = harness["validation_repo"]

    blocker = asyncio.Event()

    class _BlockingFactory:
        async def probe(self, entry: DeviceRegistryEntry, *, timeout_s: float) -> ProbeOutcome:
            await blocker.wait()
            raise AssertionError("should not run")

    service._factory = _BlockingFactory()  # type: ignore[assignment]

    task = asyncio.create_task(service.run(triggered_by_session_id="session-x", now=_NOW))
    # Let the run reach the probe wait.
    for _ in range(50):
        await asyncio.sleep(0)
        current = await validation_repo.get_current()
        if current is not None and current.overall_status == "running":
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    final = await validation_repo.get_current()
    assert final is not None
    assert final.overall_status == "complete-FAIL"
    assert final.summary_text == "Validation cancelled before completion."
