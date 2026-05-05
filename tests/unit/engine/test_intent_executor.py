"""Unit tests for IntentExecutor — typed intent → candidate DeviceCommand translation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from open_ems.core import (
    BatteryState,
    DeviceRole,
    EVChargerState,
    SystemOperatingMode,
)
from open_ems.core.commands import (
    CommandOrigin,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.core.state import (
    ALL_DEVICE_ROLES,
    ComponentState,
    GlobalState,
    SystemSnapshot,
)
from open_ems.engine import EvaluationResult, IntentExecutor
from open_ems.engine.rules.battery_control import BatteryIntent, BatteryIntentAction
from open_ems.engine.rules.ev_scheduling import EVChargerIntent, EVChargerIntentAction

_NOW = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


def _battery_state() -> BatteryState:
    return BatteryState(
        device_id="bat-001",
        soc_percent=50.0,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW,
    )


def _ev_state() -> EVChargerState:
    return EVChargerState(
        device_id="ev-001",
        status="charging",
        session_active=True,
        read_at=_NOW,
    )


def _snapshot(
    *,
    battery: BatteryState | None = None,
    ev_charger: EVChargerState | None = None,
) -> SystemSnapshot:
    return SystemSnapshot(
        sequence_id=1,
        captured_at=_NOW,
        global_state=GlobalState.normal,
        operating_mode=SystemOperatingMode.normal,
        inverter=None,
        battery=battery,
        ev_charger=ev_charger,
        grid_meter=None,
        component_states=dict.fromkeys(ALL_DEVICE_ROLES, ComponentState.active),
        data_age_seconds=dict.fromkeys(ALL_DEVICE_ROLES, 0),
        system_clock_status="valid",
    )


def _battery_intent(action: BatteryIntentAction) -> BatteryIntent:
    return BatteryIntent(
        action=action,
        target_power_kw=2.5 if action is not BatteryIntentAction.hold else None,
        reserve_floor_percent=20.0,
        reason_code="test",
        source_candidate=None,
    )


def _ev_intent(action: EVChargerIntentAction) -> EVChargerIntent:
    return EVChargerIntent(
        action=action,
        target_charge_rate_kw=7.4 if action is EVChargerIntentAction.charge else None,
        homeowner_override_active=False,
        reason_code="test",
        source_candidate=None,
    )


def _result_with(*intents: BatteryIntent | EVChargerIntent) -> EvaluationResult:
    return EvaluationResult(
        cycle_id=uuid.uuid4(),
        evaluated_at=_NOW,
        recommended_operating_mode=SystemOperatingMode.normal,
        intents=intents,
        decision_reasons=tuple(f"reason_{i}" for i in range(len(intents))),
        cycle_duration_ms=1,
    )


def test_battery_hold_produces_no_command() -> None:
    executor = IntentExecutor()
    result = _result_with(_battery_intent(BatteryIntentAction.hold))

    commands = executor.translate(result, _snapshot(battery=_battery_state()))

    assert commands == []


def test_ev_hold_produces_no_command() -> None:
    executor = IntentExecutor()
    result = _result_with(_ev_intent(EVChargerIntentAction.hold))

    commands = executor.translate(result, _snapshot(ev_charger=_ev_state()))

    assert commands == []


def test_battery_charge_produces_command() -> None:
    executor = IntentExecutor()
    result = _result_with(_battery_intent(BatteryIntentAction.charge))

    commands = executor.translate(result, _snapshot(battery=_battery_state()))

    assert len(commands) == 1
    cmd = commands[0]
    assert isinstance(cmd, SetBatteryChargeRateCommand)
    assert cmd.device_id == "bat-001"
    assert cmd.device_role is DeviceRole.battery
    assert cmd.origin is CommandOrigin.decision_engine
    assert cmd.rate_kw == 2.5


def test_battery_discharge_produces_command() -> None:
    executor = IntentExecutor()
    result = _result_with(_battery_intent(BatteryIntentAction.discharge))

    commands = executor.translate(result, _snapshot(battery=_battery_state()))

    assert len(commands) == 1
    cmd = commands[0]
    assert isinstance(cmd, SetBatteryDischargeRateCommand)
    assert cmd.device_id == "bat-001"
    assert cmd.rate_kw == 2.5


def test_ev_charge_produces_command() -> None:
    executor = IntentExecutor()
    result = _result_with(_ev_intent(EVChargerIntentAction.charge))

    commands = executor.translate(result, _snapshot(ev_charger=_ev_state()))

    assert len(commands) == 1
    cmd = commands[0]
    assert isinstance(cmd, SetEVChargingRateCommand)
    assert cmd.device_id == "ev-001"
    assert cmd.device_role is DeviceRole.ev_charger
    assert cmd.origin is CommandOrigin.decision_engine
    assert cmd.rate_kw == 7.4


def test_ev_stop_produces_command() -> None:
    executor = IntentExecutor()
    result = _result_with(_ev_intent(EVChargerIntentAction.stop))

    commands = executor.translate(result, _snapshot(ev_charger=_ev_state()))

    assert len(commands) == 1
    cmd = commands[0]
    assert isinstance(cmd, StopEVChargingCommand)
    assert cmd.device_id == "ev-001"
    assert cmd.device_role is DeviceRole.ev_charger
    assert cmd.origin is CommandOrigin.decision_engine


def test_battery_charge_with_no_battery_state_skips() -> None:
    """If snapshot.battery is not BatteryState, the intent is skipped (no command)."""
    executor = IntentExecutor()
    result = _result_with(_battery_intent(BatteryIntentAction.charge))

    commands = executor.translate(result, _snapshot(battery=None))

    assert commands == []


def test_battery_intent_with_none_target_uses_zero_rate() -> None:
    executor = IntentExecutor()
    intent = BatteryIntent(
        action=BatteryIntentAction.charge,
        target_power_kw=None,
        reserve_floor_percent=20.0,
        reason_code="test",
        source_candidate=None,
    )
    result = _result_with(intent)

    commands = executor.translate(result, _snapshot(battery=_battery_state()))

    assert len(commands) == 1
    cmd = commands[0]
    assert isinstance(cmd, SetBatteryChargeRateCommand)
    assert cmd.rate_kw == 0.0
