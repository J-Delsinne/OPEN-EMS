from __future__ import annotations

import ast
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from open_ems.core import (
    BatteryState,
    DegradedDeviceState,
    DeviceCapabilityProfile,
    DeviceRole,
    EnergyStrategy,
    EVChargerState,
    GridMeterState,
    InverterState,
)
from open_ems.core.devices import WriteCapability
from open_ems.engine import EvaluationInput, PeakContext
from open_ems.engine.models import BatteryControlContext, EVSchedulingContext
from open_ems.engine.rules.battery_control import (
    BatteryIntent,
    BatteryIntentAction,
    evaluate_battery_control,
)
from open_ems.engine.rules.energy_balancing import CandidateAction, PriorityBand

_NOW_UTC = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_DEFAULT_SLOT = object()


def _peak_context() -> PeakContext:
    return PeakContext(
        current_partial_window_projection_kw=3.8,
        configured_peak_limit_kw=5.0,
        current_monthly_recorded_peak_kw=4.2,
        current_interval_start=_NOW_UTC,
        current_interval_elapsed_seconds=120,
    )


def _capability_profile(
    *write_capabilities: WriteCapability,
    device_id: str = "bat-001",
) -> DeviceCapabilityProfile:
    return DeviceCapabilityProfile(
        device_id=device_id,
        model="test-battery",
        write_capabilities=frozenset(write_capabilities),
    )


def _battery_control_context(
    *,
    reserve_floor_percent: float = 20.0,
    capability_profile: DeviceCapabilityProfile | None = None,
) -> BatteryControlContext:
    return BatteryControlContext(
        reserve_floor_percent=reserve_floor_percent,
        capability_profile=capability_profile,
    )


def _ev_scheduling_context() -> EVSchedulingContext:
    return EVSchedulingContext(
        capability_profile=None,
        charging_window=None,
        homeowner_override_active=False,
        evaluated_at=_NOW_UTC,
        target_charge_rate_kw=None,
    )


def _inverter() -> InverterState:
    return InverterState(
        device_id="inv-001",
        pv_power_kw=3.2,
        ac_power_kw=3.0,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )


def _battery(*, soc_percent: float = 55.0) -> BatteryState:
    return BatteryState(
        device_id="bat-001",
        soc_percent=soc_percent,
        battery_power_kw=-1.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )


def _ev_charger() -> EVChargerState:
    return EVChargerState(
        device_id="ev-001",
        status="charging",
        session_active=True,
        current_power_kw=7.0,
        read_at=_NOW_UTC,
    )


def _grid_meter() -> GridMeterState:
    return GridMeterState(
        device_id="grid-001",
        grid_power_kw=1.2,
        energy_delivered_kwh=100.0,
        energy_returned_kwh=20.0,
        received_at=_NOW_UTC,
    )


def _degraded(role: DeviceRole) -> DegradedDeviceState:
    return DegradedDeviceState(
        device_id=f"{role.value}-001",
        role=role,
        reason="reconnecting",
        occurred_at=_NOW_UTC,
    )


def _input_with(
    *,
    battery: BatteryState | DegradedDeviceState | None | object = _DEFAULT_SLOT,
    battery_control: BatteryControlContext | None = None,
) -> EvaluationInput:
    return EvaluationInput(
        inverter=_inverter(),
        battery=_battery() if battery is _DEFAULT_SLOT else battery,
        ev_charger=_ev_charger(),
        grid_meter=_grid_meter(),
        peak_context=_peak_context(),
        strategy=EnergyStrategy.minimize_cost,
        battery_control=battery_control
        if battery_control is not None
        else _battery_control_context(),
        ev_scheduling=_ev_scheduling_context(),
    )


def _candidate(
    *,
    role: DeviceRole = DeviceRole.battery,
    action_type: str = "battery_charge_from_pv",
) -> CandidateAction:
    return CandidateAction(
        role=role,
        action_type=action_type,
        priority_band=PriorityBand.optimization,
        priority_weight=20,
        tiebreaker_key=f"test:{action_type}",
        source_rule="test_rule",
    )


def test_battery_intent_contract_is_frozen_and_has_exact_actions() -> None:
    assert {action.value for action in BatteryIntentAction} == {
        "hold",
        "charge",
        "discharge",
    }

    intent = BatteryIntent(
        action=BatteryIntentAction.hold,
        target_power_kw=None,
        reserve_floor_percent=20.0,
        reason_code="test_reason",
        source_candidate=None,
    )

    with pytest.raises(ValidationError):
        intent.action = BatteryIntentAction.charge  # type: ignore[misc]


@pytest.mark.parametrize("battery_slot", [None, _degraded(DeviceRole.battery)])
def test_battery_unavailable_returns_hold(battery_slot: DegradedDeviceState | None) -> None:
    intent = evaluate_battery_control(
        _input_with(battery=battery_slot),
        _candidate(action_type="battery_discharge_to_avoid_import"),
    )

    assert intent.action is BatteryIntentAction.hold
    assert intent.target_power_kw is None
    assert intent.reason_code == "battery_unavailable"


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        _candidate(role=DeviceRole.ev_charger, action_type="ev_charge"),
        _candidate(action_type="unknown_battery_action"),
    ],
)
def test_absent_non_battery_or_unknown_candidate_returns_hold(
    candidate: CandidateAction | None,
) -> None:
    intent = evaluate_battery_control(_input_with(), candidate)

    assert intent.action is BatteryIntentAction.hold
    assert intent.target_power_kw is None
    assert intent.source_candidate is candidate


def test_charge_intent_requires_matching_charge_capability() -> None:
    candidate = _candidate(action_type="battery_charge_from_pv")
    without_capability = evaluate_battery_control(_input_with(), candidate)
    with_capability = evaluate_battery_control(
        _input_with(
            battery_control=_battery_control_context(
                capability_profile=_capability_profile(WriteCapability.set_charge_rate)
            )
        ),
        candidate,
    )

    assert without_capability.action is BatteryIntentAction.hold
    assert without_capability.target_power_kw is None
    assert with_capability.action is BatteryIntentAction.charge
    assert with_capability.reserve_floor_percent == 20.0
    assert with_capability.source_candidate is candidate


@pytest.mark.parametrize("soc_percent", [20.0, 19.9])
def test_discharge_at_or_below_reserve_floor_returns_hold(soc_percent: float) -> None:
    intent = evaluate_battery_control(
        _input_with(
            battery=_battery(soc_percent=soc_percent),
            battery_control=_battery_control_context(
                reserve_floor_percent=20.0,
                capability_profile=_capability_profile(WriteCapability.set_discharge_rate),
            ),
        ),
        _candidate(action_type="battery_discharge_to_avoid_import"),
    )

    assert intent.action is BatteryIntentAction.hold
    assert intent.target_power_kw is None
    assert intent.reason_code == "battery_reserve_floor_reached"


@pytest.mark.parametrize(
    "action_type",
    ["battery_discharge_to_avoid_import", "battery_support_ev"],
)
def test_discharge_intent_requires_matching_discharge_capability(action_type: str) -> None:
    candidate = _candidate(action_type=action_type)
    without_capability = evaluate_battery_control(_input_with(), candidate)
    with_capability = evaluate_battery_control(
        _input_with(
            battery_control=_battery_control_context(
                capability_profile=_capability_profile(WriteCapability.set_discharge_rate)
            )
        ),
        candidate,
    )

    assert without_capability.action is BatteryIntentAction.hold
    assert with_capability.action is BatteryIntentAction.discharge
    assert with_capability.target_power_kw is None


def test_mismatched_battery_capability_profile_is_treated_as_missing() -> None:
    intent = evaluate_battery_control(
        _input_with(
            battery_control=_battery_control_context(
                capability_profile=_capability_profile(
                    WriteCapability.set_charge_rate,
                    device_id="other-battery",
                )
            )
        ),
        _candidate(action_type="battery_charge_from_pv"),
    )

    assert intent.action is BatteryIntentAction.hold
    assert intent.target_power_kw is None


def test_repeated_calls_with_same_input_return_equal_intents() -> None:
    evaluation_input = _input_with(
        battery_control=_battery_control_context(
            capability_profile=_capability_profile(WriteCapability.set_charge_rate)
        )
    )
    candidate = _candidate(action_type="battery_charge_from_pv")

    assert [evaluate_battery_control(evaluation_input, candidate) for _ in range(5)] == [
        evaluate_battery_control(evaluation_input, candidate)
    ] * 5


def test_rule_module_avoids_runtime_infrastructure_imports() -> None:
    import open_ems.engine.rules.battery_control as battery_control

    imported_modules = set()
    tree = ast.parse(battery_control.__loader__.get_source(battery_control.__name__) or "")  # type: ignore[union-attr]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    forbidden_modules = (
        "open_ems.storage",
        "open_ems.web",
        "open_ems.adapters",
        "open_ems.services",
    )

    assert not any(
        name == module or name.startswith(f"{module}.")
        for module in forbidden_modules
        for name in imported_modules
    )
