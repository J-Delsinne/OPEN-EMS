from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, time
from types import ModuleType

import pytest
from pydantic import ValidationError

from open_ems.core import (
    BatteryState,
    DeviceCapabilityProfile,
    DeviceRole,
    EnergyStrategy,
    EVChargerState,
    GridMeterState,
    InverterState,
    SystemOperatingMode,
)
from open_ems.core.devices import WriteCapability
from open_ems.engine import EvaluationInput, PeakContext
from open_ems.engine.evaluator import (
    _reason_for_battery_intent,
    _reason_for_ev_intent,
    evaluate_cycle,
)
from open_ems.engine.models import BatteryControlContext, EVChargingWindow, EVSchedulingContext
from open_ems.engine.result import EvaluationResult
from open_ems.engine.rules.battery_control import BatteryIntent, BatteryIntentAction
from open_ems.engine.rules.energy_balancing import ActionType, CandidateAction, PriorityBand
from open_ems.engine.rules.ev_scheduling import EVChargerIntent, EVChargerIntentAction

_NOW_UTC = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_DEFAULT_SLOT = object()


def _candidate(
    *,
    role: DeviceRole = DeviceRole.battery,
    action_type: ActionType = ActionType.battery_charge_from_pv,
) -> CandidateAction:
    return CandidateAction(
        role=role,
        action_type=action_type,
        priority_band=PriorityBand.optimization,
        priority_weight=20,
        tiebreaker_key=f"test:{action_type}",
        source_rule="test_rule",
    )


def _peak_context(
    *,
    projection_kw: float = 3.8,
    limit_kw: float = 5.0,
) -> PeakContext:
    return PeakContext(
        current_partial_window_projection_kw=projection_kw,
        configured_peak_limit_kw=limit_kw,
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
        model="test-device",
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


def _window() -> EVChargingWindow:
    return EVChargingWindow(
        start_local_time=time(22, 0),
        end_local_time=time(6, 0),
        timezone_name="Europe/Brussels",
    )


def _ev_scheduling_context(
    *,
    charging_window: EVChargingWindow | None = None,
    homeowner_override_active: bool = False,
    target_charge_rate_kw: float | None = None,
    capability_profile: DeviceCapabilityProfile | None = None,
) -> EVSchedulingContext:
    return EVSchedulingContext(
        capability_profile=capability_profile,
        charging_window=charging_window,
        homeowner_override_active=homeowner_override_active,
        evaluated_at=_NOW_UTC,
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


def _battery(*, soc_percent: float = 55.0) -> BatteryState:
    return BatteryState(
        device_id="bat-001",
        soc_percent=soc_percent,
        battery_power_kw=-1.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )


def _ev_charger(
    *,
    session_active: bool = True,
    current_power_kw: float | None = 7.0,
) -> EVChargerState:
    return EVChargerState(
        device_id="ev-001",
        status="charging" if session_active else "available",
        session_active=session_active,
        current_power_kw=current_power_kw,
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


def _full_input(
    *,
    battery: BatteryState | object = _DEFAULT_SLOT,
    peak_context: PeakContext | None = None,
    strategy: EnergyStrategy = EnergyStrategy.minimize_cost,
    battery_control: BatteryControlContext | None = None,
    ev_scheduling: EVSchedulingContext | None = None,
) -> EvaluationInput:
    return EvaluationInput(
        inverter=_inverter(),
        battery=_battery() if battery is _DEFAULT_SLOT else battery,
        ev_charger=_ev_charger(),
        grid_meter=_grid_meter(),
        peak_context=peak_context if peak_context is not None else _peak_context(),
        strategy=strategy,
        battery_control=battery_control
        if battery_control is not None
        else _battery_control_context(),
        ev_scheduling=ev_scheduling if ev_scheduling is not None else _ev_scheduling_context(),
    )


def _battery_intent() -> BatteryIntent:
    return BatteryIntent(
        action=BatteryIntentAction.charge,
        target_power_kw=None,
        reserve_floor_percent=20.0,
        reason_code="battery_charge_allowed",
        source_candidate=_candidate(),
    )


def _ev_intent() -> EVChargerIntent:
    return EVChargerIntent(
        action=EVChargerIntentAction.charge,
        target_charge_rate_kw=None,
        homeowner_override_active=False,
        reason_code="ev_charge_allowed",
        source_candidate=_candidate(role=DeviceRole.ev_charger, action_type=ActionType.ev_charge),
    )


def _battery_intent_with(
    reason_code: str,
    *,
    action: BatteryIntentAction = BatteryIntentAction.hold,
) -> BatteryIntent:
    return BatteryIntent(
        action=action,
        target_power_kw=None,
        reserve_floor_percent=25.0,
        reason_code=reason_code,
        source_candidate=_candidate(action_type=ActionType.battery_discharge_to_avoid_import),
    )


def _ev_intent_with(
    reason_code: str,
    *,
    action: EVChargerIntentAction = EVChargerIntentAction.hold,
    homeowner_override_active: bool = False,
) -> EVChargerIntent:
    return EVChargerIntent(
        action=action,
        target_charge_rate_kw=None,
        homeowner_override_active=homeowner_override_active,
        reason_code=reason_code,
        source_candidate=_candidate(role=DeviceRole.ev_charger, action_type=ActionType.ev_charge),
    )


def test_evaluation_result_model_requires_reason_per_intent() -> None:
    with pytest.raises(ValidationError):
        EvaluationResult(
            cycle_id=uuid.uuid4(),
            evaluated_at=_NOW_UTC,
            recommended_operating_mode=SystemOperatingMode.normal,
            intents=(_battery_intent(), _ev_intent()),
            decision_reasons=("Battery charge approved",),
            cycle_duration_ms=0,
        )


def test_evaluation_result_model_rejects_duplicate_device_roles() -> None:
    with pytest.raises(ValidationError):
        EvaluationResult(
            cycle_id=uuid.uuid4(),
            evaluated_at=_NOW_UTC,
            recommended_operating_mode=SystemOperatingMode.normal,
            intents=(_battery_intent(), _battery_intent()),
            decision_reasons=("Battery charge approved", "Battery charge approved"),
            cycle_duration_ms=0,
        )


def test_decision_reason_generation_for_all_reason_codes() -> None:
    evaluation_input = _full_input(
        battery=_battery(soc_percent=22.0),
        peak_context=_peak_context(projection_kw=5.2, limit_kw=5.0),
        strategy=EnergyStrategy.maximize_self_consumption,
        battery_control=_battery_control_context(reserve_floor_percent=25.0),
        ev_scheduling=_ev_scheduling_context(
            charging_window=_window(),
            target_charge_rate_kw=1.5,
        ),
    )

    battery_cases = {
        "battery_unavailable": ("Battery device",),
        "battery_candidate_unavailable": ("No battery candidate",),
        "battery_charge_capability_missing": ("set_charge_rate", "bat-001"),
        "battery_discharge_capability_missing": ("set_discharge_rate", "bat-001"),
        "battery_reserve_floor_reached": ("22%", "25%"),
        "battery_charge_allowed": ("maximize_self_consumption",),
        "battery_discharge_allowed": ("22%", "25%"),
    }
    for reason_code, expected_fragments in battery_cases.items():
        reason = _reason_for_battery_intent(
            _battery_intent_with(reason_code),
            evaluation_input,
        )
        assert reason.strip() == reason
        assert len(reason.split()) > 2
        for fragment in expected_fragments:
            assert fragment in reason

    ev_cases = {
        "ev_charger_unavailable": ("EV charger",),
        "ev_candidate_unavailable": ("No EV charger candidate",),
        "ev_charging_window_inactive": ("22:00", "06:00", "Europe/Brussels"),
        "peak_limit_blocks_ev_charge": ("5.20 kW", "5.00 kW"),
        "peak_headroom_insufficient": ("-0.20 kW", "1.5 kW"),
        "ev_charge_allowed": ("maximize_self_consumption", "False"),
    }
    for reason_code, expected_fragments in ev_cases.items():
        reason = _reason_for_ev_intent(_ev_intent_with(reason_code), evaluation_input)
        assert reason.strip() == reason
        assert len(reason.split()) > 2
        for fragment in expected_fragments:
            assert fragment in reason


def test_reason_generation_uses_specific_unknown_code_fallbacks() -> None:
    evaluation_input = _full_input()

    battery_reason = _reason_for_battery_intent(
        _battery_intent_with("new_battery_reason"),
        evaluation_input,
    )
    ev_reason = _reason_for_ev_intent(
        _ev_intent_with("new_ev_reason"),
        evaluation_input,
    )

    assert "new_battery_reason" in battery_reason
    assert BatteryIntentAction.hold.value in battery_reason
    assert "new_ev_reason" in ev_reason
    assert EVChargerIntentAction.hold.value in ev_reason


def test_evaluation_result_contains_all_required_fields() -> None:
    result = evaluate_cycle(_full_input())

    assert isinstance(result, EvaluationResult)
    assert isinstance(result.cycle_id, uuid.UUID)
    assert result.evaluated_at.tzinfo is not None
    assert result.evaluated_at.utcoffset() is not None
    assert isinstance(result.recommended_operating_mode, SystemOperatingMode)
    assert isinstance(result.intents, tuple)
    assert isinstance(result.decision_reasons, tuple)
    assert isinstance(result.cycle_duration_ms, int)


def test_cycle_id_is_uuid() -> None:
    result = evaluate_cycle(_full_input())

    assert isinstance(result.cycle_id, uuid.UUID)
    assert result.cycle_id.version == 4


def test_evaluated_at_is_timezone_aware_utc() -> None:
    result = evaluate_cycle(_full_input())

    assert result.evaluated_at.tzinfo is not None
    assert result.evaluated_at.utcoffset() == _NOW_UTC.utcoffset()


def test_recommended_operating_mode_is_system_operating_mode_enum() -> None:
    result = evaluate_cycle(_full_input())

    assert isinstance(result.recommended_operating_mode, SystemOperatingMode)


def test_intents_contains_at_most_one_per_role() -> None:
    result = evaluate_cycle(_full_input())

    assert sum(isinstance(intent, BatteryIntent) for intent in result.intents) == 1
    assert sum(isinstance(intent, EVChargerIntent) for intent in result.intents) == 1


def test_each_intent_has_corresponding_reason() -> None:
    result = evaluate_cycle(_full_input())

    assert len(result.intents) == len(result.decision_reasons)
    assert all(isinstance(reason, str) and reason for reason in result.decision_reasons)


def test_cycle_duration_recorded_and_reflects_elapsed_time() -> None:
    result = evaluate_cycle(_full_input())

    assert isinstance(result.cycle_duration_ms, int)
    assert result.cycle_duration_ms >= 0


def test_no_command_objects_in_result() -> None:
    result = evaluate_cycle(_full_input())
    forbidden_names = {"Command", "DeviceCommand", "CommandResult"}

    assert all(type(intent).__name__ not in forbidden_names for intent in result.intents)


def test_decision_reasons_are_specific_not_generic() -> None:
    result = evaluate_cycle(
        _full_input(
            battery=_battery(soc_percent=22.0),
            strategy=EnergyStrategy.prioritize_ev,
            battery_control=_battery_control_context(
                reserve_floor_percent=25.0,
                capability_profile=_capability_profile(WriteCapability.set_discharge_rate),
            ),
        )
    )

    battery_reason = result.decision_reasons[0]
    ev_reason = result.decision_reasons[1]

    assert "22%" in battery_reason
    assert "25%" in battery_reason
    assert ev_reason
    assert len(ev_reason.split()) > 2


def test_peak_limit_candidate_wins_over_strategy_candidate() -> None:
    result = evaluate_cycle(
        _full_input(
            peak_context=_peak_context(projection_kw=6.0, limit_kw=5.0),
            battery_control=_battery_control_context(
                reserve_floor_percent=20.0,
                capability_profile=_capability_profile(WriteCapability.set_discharge_rate),
            ),
        )
    )

    battery_intent = next(intent for intent in result.intents if isinstance(intent, BatteryIntent))

    assert battery_intent.action is BatteryIntentAction.discharge
    assert "Battery discharge approved" in result.decision_reasons[0]


def test_two_calls_with_same_input_produce_equal_resolved_fields() -> None:
    evaluation_input = _full_input()

    first = evaluate_cycle(evaluation_input)
    second = evaluate_cycle(evaluation_input)

    assert first.recommended_operating_mode == second.recommended_operating_mode
    assert first.intents == second.intents
    assert first.decision_reasons == second.decision_reasons
    assert first.cycle_id != second.cycle_id


def test_repeated_identical_inputs_produce_equal_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import open_ems.engine.evaluator as evaluator_module

    class FixedDateTime:
        @staticmethod
        def now(tz: object) -> datetime:
            return _NOW_UTC

    monkeypatch.setattr(evaluator_module, "datetime", FixedDateTime)
    evaluation_input = _full_input()

    first = evaluate_cycle(evaluation_input)
    second = evaluate_cycle(evaluation_input)

    normalized_first = first.model_copy(
        update={
            "cycle_id": second.cycle_id,
            "cycle_duration_ms": second.cycle_duration_ms,
        }
    )
    assert normalized_first == second
    assert first.cycle_id != second.cycle_id


def test_engine_public_exports_include_evaluation_result_and_cycle() -> None:
    import open_ems.engine as engine

    assert engine.EvaluationResult is EvaluationResult
    assert engine.evaluate_cycle is evaluate_cycle


def test_evaluator_does_not_import_forbidden_modules() -> None:
    import open_ems.engine.evaluator as evaluator

    assert not _has_forbidden_import(evaluator)


def test_result_does_not_import_forbidden_modules() -> None:
    import open_ems.engine.result as result

    assert not _has_forbidden_import(result)


def test_evaluation_result_rejects_whitespace_only_reason() -> None:
    with pytest.raises(ValidationError):
        EvaluationResult(
            cycle_id=uuid.uuid4(),
            evaluated_at=_NOW_UTC,
            recommended_operating_mode=SystemOperatingMode.normal,
            intents=(_battery_intent(), _ev_intent()),
            decision_reasons=("Battery charge approved", "   "),
            cycle_duration_ms=0,
        )


def test_evaluation_result_rejects_empty_intents() -> None:
    with pytest.raises(ValidationError):
        EvaluationResult(
            cycle_id=uuid.uuid4(),
            evaluated_at=_NOW_UTC,
            recommended_operating_mode=SystemOperatingMode.normal,
            intents=(),
            decision_reasons=(),
            cycle_duration_ms=0,
        )


def _has_forbidden_import(module: ModuleType) -> bool:
    imported_modules = set()
    tree = ast.parse(module.__loader__.get_source(module.__name__) or "")  # type: ignore[union-attr]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    forbidden_modules = (
        "open_ems.adapters",
        "open_ems.core.commands",
        "open_ems.services",
        "open_ems.storage",
        "open_ems.web",
    )

    return any(
        name == module_name or name.startswith(f"{module_name}.")
        for module_name in forbidden_modules
        for name in imported_modules
    )
