"""End-to-end decision-engine simulation tests (Story 8.5 AC4-AC8, AC12 #8-#13).

Composes the production engine + execution layers (``evaluate_cycle`` →
``IntentExecutor`` → ``RetryPolicy`` → ``PolicyGuard`` → adapter) for a single
deterministic cycle per ``SimulationScenario``. The control loop, watchdog,
and ``PartialIntervalTracker`` are intentionally out of scope here — those are
exercised by Story 8.4's ``test_fail_safe_lifecycle.py``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from open_ems.core import DeviceRole, EnergyStrategy
from open_ems.core.commands import (
    CommandOrigin,
    CommandResult,
    CommandStatus,
    DeviceCommand,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
)
from open_ems.engine import (
    IntentExecutor,
    PolicyGuard,
    RetryPolicy,
    evaluate_cycle,
)
from open_ems.engine.result import EvaluationResult
from open_ems.engine.rules.battery_control import BatteryIntent, BatteryIntentAction
from open_ems.services.audit_log import ObservabilityService
from open_ems.storage.repositories.event_log_repo import EventLogRepo
from tests.fixtures.scenarios import (
    SimulationScenario,
    _StubBatteryAdapter,
    _StubEVChargerAdapter,
    _StubGridMeterAdapter,
    _StubInverterAdapter,
    build_scenario_adapters,
    build_scenario_evaluation_input,
    build_scenario_settings,
    build_scenario_state_store,
)

# Fixed module-level "now" so peak_context.current_interval_start is deterministic
# (12:00 UTC is already on a 15-minute boundary — no floor adjustment).
_NOW: datetime = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


# ── Module-level scenario constants ───────────────────────────────────────────

PEAK_TARIFF_CURTAIL = SimulationScenario(
    name="peak_tariff_curtail_load",
    grid_power_kw=8.0,
    pv_power_kw=0.0,
    inverter_ac_power_kw=0.0,
    battery_soc_percent=80.0,
    battery_capacity_kwh=10.0,
    ev_session_active=True,
    ev_current_power_kw=4.0,
    strategy=EnergyStrategy.maximize_self_consumption,
    peak_limit_kw=5.0,
    battery_reserve_floor_percent=20.0,
    homeowner_override_active=True,
    ev_charging_window=None,
)

EXCESS_SOLAR = SimulationScenario(
    name="excess_solar_charge_battery",
    grid_power_kw=-2.0,
    pv_power_kw=6.0,
    inverter_ac_power_kw=4.0,
    battery_soc_percent=50.0,
    battery_capacity_kwh=10.0,
    ev_session_active=False,
    ev_current_power_kw=None,
    strategy=EnergyStrategy.maximize_self_consumption,
    peak_limit_kw=25.0,
    battery_reserve_floor_percent=20.0,
)

BATTERY_BELOW_RESERVE = SimulationScenario(
    name="battery_below_reserve_rejection",
    grid_power_kw=8.0,
    pv_power_kw=0.0,
    inverter_ac_power_kw=0.0,
    battery_soc_percent=20.0,  # AT reserve floor → discharge is unsafe
    battery_capacity_kwh=10.0,
    ev_session_active=False,
    ev_current_power_kw=None,
    strategy=EnergyStrategy.maximize_self_consumption,
    peak_limit_kw=25.0,  # no peak overshoot — discharge candidate from STRATEGY band
    battery_reserve_floor_percent=20.0,
)


# ── SimulationCycleResult + run_simulation_cycle ──────────────────────────────


@dataclass(frozen=True)
class SimulationCycleResult:
    """Captured outputs of one ``run_simulation_cycle()`` call."""

    evaluation_result: EvaluationResult
    commands: list[DeviceCommand]
    command_results: list[CommandResult]
    audit_calls: list[dict[str, object]]
    adapter_calls: dict[DeviceRole, list[DeviceCommand]]


def _audit_spy_observability() -> tuple[ObservabilityService, list[dict[str, object]]]:
    """Real ObservabilityService + mocked EventLogRepo + audit() call recording.

    Mirrors the spy pattern from ``tests/integration/engine/test_fail_safe_lifecycle.py``
    (lines 164-180). Every ``audit()`` invocation is recorded as a kwargs dict
    AND forwarded to the underlying real audit method (which writes to the
    mocked repo) so validation/serialization is exercised.
    """
    repo = AsyncMock(spec=EventLogRepo)
    repo.append = AsyncMock(return_value=None)
    obs = ObservabilityService(repo=repo)
    original_audit = obs.audit
    audit_calls: list[dict[str, object]] = []

    async def spy_audit(**kwargs: object) -> None:
        audit_calls.append(dict(kwargs))
        await original_audit(**kwargs)  # type: ignore[arg-type]

    obs.audit = spy_audit  # type: ignore[method-assign]
    return obs, audit_calls


async def run_simulation_cycle(
    scenario: SimulationScenario, *, at: datetime = _NOW
) -> SimulationCycleResult:
    """Run one decision-engine cycle end-to-end for ``scenario``.

    Composes the full pipeline:
    1. Build StateStore + adapters + settings from scenario.
    2. Build EvaluationInput from snapshot + scenario.
    3. Call ``evaluate_cycle()`` to produce ``EvaluationResult``.
    4. Translate intents to commands via ``IntentExecutor``.
    5. Execute each command through ``RetryPolicy`` → ``PolicyGuard`` → adapter.
    6. Return all results + audit calls + adapter dispatch records.
    """
    state_store = await build_scenario_state_store(scenario, at=at)
    adapters = build_scenario_adapters(scenario, at=at)
    settings = build_scenario_settings(scenario)
    obs, audit_calls = _audit_spy_observability()

    snapshot = state_store.get_snapshot()
    eval_input = build_scenario_evaluation_input(scenario, snapshot, at=at)
    evaluation_result = evaluate_cycle(eval_input)

    executor = IntentExecutor()
    guard = PolicyGuard(
        state_store=state_store,
        adapters=adapters,
        observability=obs,
        settings=settings,
    )
    retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

    commands = executor.translate(evaluation_result, snapshot)
    command_results: list[CommandResult] = []
    for cmd in commands:
        command_results.append(await retry.execute(cmd))

    adapter_calls: dict[DeviceRole, list[DeviceCommand]] = {
        role: list(getattr(adapter, "send_command_calls", [])) for role, adapter in adapters.items()
    }
    return SimulationCycleResult(
        evaluation_result=evaluation_result,
        commands=commands,
        command_results=command_results,
        audit_calls=audit_calls,
        adapter_calls=adapter_calls,
    )


# ── AC12 #8: peak-tariff curtail-load scenario ────────────────────────────────


async def test_peak_tariff_curtail_load_dispatches_battery_discharge() -> None:
    """AC5: peak overshoot triggers battery discharge via the safety band."""
    result = await run_simulation_cycle(PEAK_TARIFF_CURTAIL)

    # Operating mode stays normal — peak overshoot is a control signal, not a fail-safe trigger
    from open_ems.core import SystemOperatingMode

    assert result.evaluation_result.recommended_operating_mode is SystemOperatingMode.normal

    # Engine emitted a discharge intent for the battery (peak-limiting safety candidate)
    battery_intents = [i for i in result.evaluation_result.intents if isinstance(i, BatteryIntent)]
    assert len(battery_intents) == 1
    assert battery_intents[0].action is BatteryIntentAction.discharge

    # Exactly one SetBatteryDischargeRateCommand was translated and dispatched
    assert len(result.commands) == 1
    cmd = result.commands[0]
    assert isinstance(cmd, SetBatteryDischargeRateCommand)
    assert cmd.device_id == "bat-001"

    assert len(result.command_results) == 1
    assert result.command_results[0].status is CommandStatus.success
    assert result.command_results[0].applied is True

    # Adapter saw exactly one dispatch on the battery role; other roles untouched
    assert result.adapter_calls[DeviceRole.battery] == [cmd]
    assert result.adapter_calls[DeviceRole.ev_charger] == []
    assert result.adapter_calls[DeviceRole.inverter] == []
    assert result.adapter_calls[DeviceRole.grid_meter] == []

    # Exactly ONE DECISION audit, ZERO CONSTRAINT, ZERO DEVICE
    decision_audits = [c for c in result.audit_calls if c["event_type"] == "DECISION"]
    constraint_audits = [c for c in result.audit_calls if c["event_type"] == "CONSTRAINT"]
    device_audits = [c for c in result.audit_calls if c["event_type"] == "DEVICE"]
    assert len(decision_audits) == 1
    assert len(constraint_audits) == 0
    assert len(device_audits) == 0

    audit = decision_audits[0]
    assert audit["device_id"] == "bat-001"
    assert "SetBatteryDischargeRateCommand" in str(audit["summary"])
    assert "success" in str(audit["summary"])
    detail = audit["detail"]
    assert isinstance(detail, dict)
    assert detail["correlation_id"] == str(cmd.correlation_id)


# ── AC12 #9: excess-solar charges battery ─────────────────────────────────────


async def test_excess_solar_charges_battery() -> None:
    """AC6: PV surplus triggers battery charge via maximize_self_consumption."""
    result = await run_simulation_cycle(EXCESS_SOLAR)

    battery_intents = [i for i in result.evaluation_result.intents if isinstance(i, BatteryIntent)]
    assert len(battery_intents) == 1
    assert battery_intents[0].action is BatteryIntentAction.charge

    assert len(result.commands) == 1
    cmd = result.commands[0]
    assert isinstance(cmd, SetBatteryChargeRateCommand)

    assert result.command_results[0].status is CommandStatus.success
    assert result.command_results[0].applied is True
    assert result.adapter_calls[DeviceRole.battery] == [cmd]

    decision_audits = [c for c in result.audit_calls if c["event_type"] == "DECISION"]
    assert len(decision_audits) == 1
    audit = decision_audits[0]
    assert audit["device_id"] == "bat-001"
    assert "SetBatteryChargeRateCommand" in str(audit["summary"])
    detail = audit["detail"]
    assert isinstance(detail, dict)
    assert detail["correlation_id"] == str(cmd.correlation_id)


# ── AC12 #10: AC7 Path A (engine-side hold) — no adapter call, no audits ──────


async def test_battery_below_reserve_rejection_no_adapter_call() -> None:
    """AC7 Path A: SoC at reserve floor → engine-side hold short-circuits dispatch.

    Per Story 8.5 dev notes: ``evaluate_battery_control`` rejects discharge BEFORE
    a command is translated when ``battery.soc_percent <= reserve_floor_percent``.
    The result is ZERO commands, ZERO audit events, ZERO adapter calls. The
    PolicyGuard rejection contract is exercised in isolation by test #11 below.
    """
    result = await run_simulation_cycle(BATTERY_BELOW_RESERVE)

    battery_intents = [i for i in result.evaluation_result.intents if isinstance(i, BatteryIntent)]
    assert len(battery_intents) == 1
    assert battery_intents[0].action is BatteryIntentAction.hold
    assert battery_intents[0].reason_code == "battery_reserve_floor_reached"

    assert result.commands == []
    assert result.command_results == []
    assert result.adapter_calls[DeviceRole.battery] == []
    assert result.audit_calls == []


# ── AC12 #11: PolicyGuard rejection in isolation ──────────────────────────────


async def test_below_reserve_policy_guard_rejection_isolated() -> None:
    """AC7 Path B: directly construct a discharge command, bypassing the engine-side hold,
    and verify PolicyGuard rejects it with a CONSTRAINT audit carrying correlation_id.
    """
    state_store = await build_scenario_state_store(BATTERY_BELOW_RESERVE, at=_NOW)
    adapters = build_scenario_adapters(BATTERY_BELOW_RESERVE, at=_NOW)
    settings = build_scenario_settings(BATTERY_BELOW_RESERVE)
    obs, audit_calls = _audit_spy_observability()

    guard = PolicyGuard(
        state_store=state_store,
        adapters=adapters,
        observability=obs,
        settings=settings,
    )
    retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

    discharge = SetBatteryDischargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )
    cmd_result = await retry.execute(discharge)

    assert cmd_result.status is CommandStatus.rejected
    assert cmd_result.reason == "battery_soc_at_or_below_reserve_floor"

    battery_adapter = adapters[DeviceRole.battery]
    assert isinstance(battery_adapter, _StubBatteryAdapter)
    assert battery_adapter.send_command_calls == []

    constraint_audits = [c for c in audit_calls if c["event_type"] == "CONSTRAINT"]
    assert len(constraint_audits) == 1
    audit = constraint_audits[0]
    assert audit["device_id"] == "bat-001"
    assert "battery_soc_at_or_below_reserve_floor" in str(audit["summary"])
    detail = audit["detail"]
    assert isinstance(detail, dict)
    assert detail["correlation_id"] == str(discharge.correlation_id)
    assert detail["rejection_reason"] == "battery_soc_at_or_below_reserve_floor"


# ── AC12 #12: every dispatched command produces an event_log row ──────────────


@pytest.mark.parametrize(
    "scenario",
    [PEAK_TARIFF_CURTAIL, EXCESS_SOLAR],
    ids=["peak_tariff_curtail_load", "excess_solar_charge_battery"],
)
async def test_all_commands_produce_event_log_rows(scenario: SimulationScenario) -> None:
    """AC8: for every dispatched command, exactly ONE matching audit row exists
    with detail.correlation_id == str(command.correlation_id).
    """
    result = await run_simulation_cycle(scenario)
    assert result.commands, "scenario must dispatch at least one command for this AC"

    for cmd in result.commands:
        matching: list[dict[str, object]] = []
        for entry in result.audit_calls:
            detail = entry.get("detail")
            if isinstance(detail, dict) and detail.get("correlation_id") == str(cmd.correlation_id):
                matching.append(entry)
        assert len(matching) == 1, (
            f"expected exactly one audit row per command (got {len(matching)} for "
            f"{type(cmd).__name__} {cmd.correlation_id})"
        )
        audit = matching[0]
        assert audit["device_id"] == cmd.device_id
        assert audit["event_type"] in {"DECISION", "CONSTRAINT", "DEVICE"}


# ── AC12 #13: simulation does not import real protocol libraries ──────────────


async def test_simulation_does_not_touch_real_adapters() -> None:
    """Sentinel: ``run_simulation_cycle`` must not import or instantiate any
    real protocol library (``pymodbus``, ``ocpp``, ``dsmr_parser``).

    The scenario adapters are stubs; verify each role is wired to its stub
    type AND no protocol library is loaded into ``sys.modules`` as a side
    effect of running the cycle.
    """
    forbidden_modules = ("pymodbus", "ocpp", "dsmr_parser")
    before = {name for name in sys.modules if name.startswith(forbidden_modules)}

    adapters = build_scenario_adapters(EXCESS_SOLAR, at=_NOW)
    assert isinstance(adapters[DeviceRole.inverter], _StubInverterAdapter)
    assert isinstance(adapters[DeviceRole.battery], _StubBatteryAdapter)
    assert isinstance(adapters[DeviceRole.ev_charger], _StubEVChargerAdapter)
    assert isinstance(adapters[DeviceRole.grid_meter], _StubGridMeterAdapter)

    await run_simulation_cycle(EXCESS_SOLAR)

    after = {name for name in sys.modules if name.startswith(forbidden_modules)}
    newly_loaded = after - before
    assert newly_loaded == set(), (
        f"simulation must not load real protocol libraries; newly loaded: {newly_loaded}"
    )
