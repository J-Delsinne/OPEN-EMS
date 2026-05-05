from __future__ import annotations

import ast
from datetime import UTC, datetime, time

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
from open_ems.engine.models import (
    BatteryControlContext,
    EVChargingWindow,
    EVSchedulingContext,
)
from open_ems.engine.rules.energy_balancing import CandidateAction, PriorityBand
from open_ems.engine.rules.ev_scheduling import (
    EVChargerIntent,
    EVChargerIntentAction,
    evaluate_ev_scheduling,
)

_NOW_UTC = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_DEFAULT_SLOT = object()


def _peak_context(
    *,
    projection_kw: float = 3.0,
    limit_kw: float = 5.0,
) -> PeakContext:
    return PeakContext(
        current_partial_window_projection_kw=projection_kw,
        configured_peak_limit_kw=limit_kw,
        current_monthly_recorded_peak_kw=4.2,
        current_interval_start=_NOW_UTC,
        current_interval_elapsed_seconds=120,
    )


def _battery_control_context() -> BatteryControlContext:
    return BatteryControlContext(reserve_floor_percent=20.0, capability_profile=None)


def _capability_profile(
    *write_capabilities: WriteCapability,
    device_id: str = "ev-001",
) -> DeviceCapabilityProfile:
    return DeviceCapabilityProfile(
        device_id=device_id,
        model="test-evse",
        write_capabilities=frozenset(write_capabilities),
    )


def _window(start_hour: int, end_hour: int) -> EVChargingWindow:
    return EVChargingWindow(
        start_local_time=time(start_hour, 0),
        end_local_time=time(end_hour, 0),
        timezone_name="UTC",
    )


def _ev_scheduling_context(
    *,
    charging_window: EVChargingWindow | None = None,
    homeowner_override_active: bool = False,
    evaluated_at: datetime = _NOW_UTC,
    target_charge_rate_kw: float | None = None,
    capability_profile: DeviceCapabilityProfile | None = None,
) -> EVSchedulingContext:
    return EVSchedulingContext(
        capability_profile=capability_profile,
        charging_window=charging_window,
        homeowner_override_active=homeowner_override_active,
        evaluated_at=evaluated_at,
        target_charge_rate_kw=target_charge_rate_kw,
    )


def _inverter() -> InverterState:
    return InverterState(
        device_id="inv-001",
        pv_power_kw=3.2,
        ac_power_kw=3.0,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )


def _battery() -> BatteryState:
    return BatteryState(
        device_id="bat-001",
        soc_percent=55.0,
        battery_power_kw=-1.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )


def _ev_charger(*, session_active: bool = True) -> EVChargerState:
    return EVChargerState(
        device_id="ev-001",
        status="charging" if session_active else "available",
        session_active=session_active,
        current_power_kw=7.0 if session_active else None,
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
    ev_charger: EVChargerState | DegradedDeviceState | None | object = _DEFAULT_SLOT,
    ev_scheduling: EVSchedulingContext | None = None,
    peak_context: PeakContext | None = None,
) -> EvaluationInput:
    return EvaluationInput(
        inverter=_inverter(),
        battery=_battery(),
        ev_charger=_ev_charger() if ev_charger is _DEFAULT_SLOT else ev_charger,
        grid_meter=_grid_meter(),
        peak_context=peak_context if peak_context is not None else _peak_context(),
        strategy=EnergyStrategy.minimize_cost,
        battery_control=_battery_control_context(),
        ev_scheduling=ev_scheduling if ev_scheduling is not None else _ev_scheduling_context(),
    )


def _candidate(
    *,
    role: DeviceRole = DeviceRole.ev_charger,
    action_type: str = "ev_charge",
) -> CandidateAction:
    return CandidateAction(
        role=role,
        action_type=action_type,
        priority_band=PriorityBand.convenience,
        priority_weight=20,
        tiebreaker_key=f"test:{action_type}",
        source_rule="test_rule",
    )


def test_ev_intent_contract_is_frozen_and_has_exact_actions() -> None:
    assert {action.value for action in EVChargerIntentAction} == {
        "hold",
        "charge",
        "stop",
    }

    intent = EVChargerIntent(
        action=EVChargerIntentAction.hold,
        target_charge_rate_kw=None,
        homeowner_override_active=False,
        reason_code="test_reason",
        source_candidate=None,
    )

    with pytest.raises(ValidationError):
        intent.action = EVChargerIntentAction.charge  # type: ignore[misc]


@pytest.mark.parametrize("ev_slot", [None, _degraded(DeviceRole.ev_charger)])
def test_ev_charger_unavailable_returns_hold(ev_slot: DegradedDeviceState | None) -> None:
    intent = evaluate_ev_scheduling(_input_with(ev_charger=ev_slot), _candidate())

    assert intent.action is EVChargerIntentAction.hold
    assert intent.target_charge_rate_kw is None
    assert intent.reason_code == "ev_charger_unavailable"


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        _candidate(role=DeviceRole.battery, action_type="battery_charge_from_pv"),
        _candidate(action_type="unknown_ev_action"),
    ],
)
def test_absent_non_ev_or_unknown_candidate_returns_hold(
    candidate: CandidateAction | None,
) -> None:
    intent = evaluate_ev_scheduling(_input_with(), candidate)

    assert intent.action is EVChargerIntentAction.hold
    assert intent.target_charge_rate_kw is None
    assert intent.source_candidate is candidate


@pytest.mark.parametrize("action_type", ["permit_ev_charge", "ev_charge"])
def test_charge_inside_configured_window_returns_charge(action_type: str) -> None:
    intent = evaluate_ev_scheduling(
        _input_with(ev_scheduling=_ev_scheduling_context(charging_window=_window(10, 14))),
        _candidate(action_type=action_type),
    )

    assert intent.action is EVChargerIntentAction.charge
    assert intent.target_charge_rate_kw is None
    assert intent.reason_code == "ev_charge_allowed"


@pytest.mark.parametrize(
    "session_active,expected_action",
    [(False, EVChargerIntentAction.hold), (True, EVChargerIntentAction.stop)],
)
def test_charge_outside_window_is_suppressed_without_override(
    session_active: bool,
    expected_action: EVChargerIntentAction,
) -> None:
    intent = evaluate_ev_scheduling(
        _input_with(
            ev_charger=_ev_charger(session_active=session_active),
            ev_scheduling=_ev_scheduling_context(charging_window=_window(8, 10)),
        ),
        _candidate(),
    )

    assert intent.action is expected_action
    assert intent.target_charge_rate_kw is None
    assert intent.reason_code == "ev_charging_window_inactive"


def test_homeowner_override_bypasses_window_preference_only() -> None:
    intent = evaluate_ev_scheduling(
        _input_with(
            ev_scheduling=_ev_scheduling_context(
                charging_window=_window(8, 10),
                homeowner_override_active=True,
            )
        ),
        _candidate(),
    )

    assert intent.action is EVChargerIntentAction.charge
    assert intent.homeowner_override_active is True


@pytest.mark.parametrize(
    "evaluated_at,expected_action",
    [
        (datetime(2026, 5, 5, 10, 0, 0, tzinfo=UTC), EVChargerIntentAction.charge),
        (datetime(2026, 5, 5, 14, 0, 0, tzinfo=UTC), EVChargerIntentAction.stop),
    ],
)
def test_same_day_window_is_start_inclusive_and_end_exclusive(
    evaluated_at: datetime,
    expected_action: EVChargerIntentAction,
) -> None:
    intent = evaluate_ev_scheduling(
        _input_with(
            ev_scheduling=_ev_scheduling_context(
                charging_window=_window(10, 14),
                evaluated_at=evaluated_at,
            )
        ),
        _candidate(),
    )

    assert intent.action is expected_action


@pytest.mark.parametrize(
    "evaluated_at,expected_action",
    [
        (datetime(2026, 5, 5, 22, 0, 0, tzinfo=UTC), EVChargerIntentAction.charge),
        (datetime(2026, 5, 5, 5, 59, 0, tzinfo=UTC), EVChargerIntentAction.charge),
        (datetime(2026, 5, 5, 6, 0, 0, tzinfo=UTC), EVChargerIntentAction.stop),
    ],
)
def test_overnight_window_is_start_inclusive_and_end_exclusive(
    evaluated_at: datetime,
    expected_action: EVChargerIntentAction,
) -> None:
    intent = evaluate_ev_scheduling(
        _input_with(
            ev_scheduling=_ev_scheduling_context(
                charging_window=_window(22, 6),
                evaluated_at=evaluated_at,
            )
        ),
        _candidate(),
    )

    assert intent.action is expected_action


@pytest.mark.parametrize(
    "override_active,expected_action",
    [(False, EVChargerIntentAction.stop), (True, EVChargerIntentAction.charge)],
)
def test_no_configured_window_requires_override_for_charge(
    override_active: bool,
    expected_action: EVChargerIntentAction,
) -> None:
    intent = evaluate_ev_scheduling(
        _input_with(
            ev_scheduling=_ev_scheduling_context(homeowner_override_active=override_active)
        ),
        _candidate(),
    )

    assert intent.action is expected_action


def test_homeowner_override_does_not_bypass_current_peak_overshoot() -> None:
    intent = evaluate_ev_scheduling(
        _input_with(
            peak_context=_peak_context(projection_kw=5.0, limit_kw=5.0),
            ev_scheduling=_ev_scheduling_context(homeowner_override_active=True),
        ),
        _candidate(),
    )

    assert intent.action is EVChargerIntentAction.stop
    assert intent.target_charge_rate_kw is None
    assert intent.reason_code == "peak_limit_blocks_ev_charge"


def test_dynamic_target_rate_requires_matching_ev_capability() -> None:
    without_capability = evaluate_ev_scheduling(
        _input_with(
            ev_scheduling=_ev_scheduling_context(
                homeowner_override_active=True,
                target_charge_rate_kw=1.5,
            )
        ),
        _candidate(),
    )
    mismatched_capability = evaluate_ev_scheduling(
        _input_with(
            ev_scheduling=_ev_scheduling_context(
                homeowner_override_active=True,
                target_charge_rate_kw=1.5,
                capability_profile=_capability_profile(
                    WriteCapability.set_ev_charge_current,
                    device_id="other-ev",
                ),
            )
        ),
        _candidate(),
    )
    with_capability = evaluate_ev_scheduling(
        _input_with(
            ev_scheduling=_ev_scheduling_context(
                homeowner_override_active=True,
                target_charge_rate_kw=1.5,
                capability_profile=_capability_profile(WriteCapability.set_ev_charge_current),
            )
        ),
        _candidate(),
    )

    assert without_capability.action is EVChargerIntentAction.charge
    assert without_capability.target_charge_rate_kw is None
    assert mismatched_capability.action is EVChargerIntentAction.charge
    assert mismatched_capability.target_charge_rate_kw is None
    assert with_capability.action is EVChargerIntentAction.charge
    assert with_capability.target_charge_rate_kw == 1.5


def test_target_rate_above_peak_headroom_suppresses_charge() -> None:
    intent = evaluate_ev_scheduling(
        _input_with(
            peak_context=_peak_context(projection_kw=4.0, limit_kw=5.0),
            ev_scheduling=_ev_scheduling_context(
                homeowner_override_active=True,
                target_charge_rate_kw=1.1,
                capability_profile=_capability_profile(WriteCapability.set_ev_charge_current),
            ),
        ),
        _candidate(),
    )

    assert intent.action is EVChargerIntentAction.stop
    assert intent.target_charge_rate_kw is None
    assert intent.reason_code == "peak_headroom_insufficient"


def test_repeated_calls_with_same_input_return_equal_intents() -> None:
    evaluation_input = _input_with(
        ev_scheduling=_ev_scheduling_context(
            homeowner_override_active=True,
            target_charge_rate_kw=1.5,
            capability_profile=_capability_profile(WriteCapability.set_ev_charge_current),
        )
    )
    candidate = _candidate()

    assert [evaluate_ev_scheduling(evaluation_input, candidate) for _ in range(5)] == [
        evaluate_ev_scheduling(evaluation_input, candidate)
    ] * 5


def test_rule_module_avoids_runtime_infrastructure_imports() -> None:
    import open_ems.engine.rules.ev_scheduling as ev_scheduling

    imported_modules = set()
    tree = ast.parse(ev_scheduling.__loader__.get_source(ev_scheduling.__name__) or "")  # type: ignore[union-attr]
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
