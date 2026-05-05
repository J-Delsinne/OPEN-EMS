from __future__ import annotations

import ast
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from open_ems.core import (
    BatteryState,
    DegradedDeviceState,
    DeviceRole,
    EnergyStrategy,
    EVChargerState,
    GridMeterState,
    InverterState,
    SystemOperatingMode,
)
from open_ems.engine import EvaluationInput, PeakContext
from open_ems.engine.models import BatteryControlContext, EVSchedulingContext
from open_ems.engine.rules.peak_limiting import (
    LoadReductionAction,
    evaluate_peak_limiting,
)

_NOW_UTC = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
_DEFAULT_SLOT = object()
_DEFAULT_STRATEGY = EnergyStrategy.minimize_cost


def _peak_context(
    *,
    projection_kw: float = 6.2,
    limit_kw: float = 5.0,
    monthly_peak_kw: float = 4.8,
    interval_start: datetime = _NOW_UTC,
    elapsed_seconds: int = 300,
) -> PeakContext:
    return PeakContext(
        current_partial_window_projection_kw=projection_kw,
        configured_peak_limit_kw=limit_kw,
        current_monthly_recorded_peak_kw=monthly_peak_kw,
        current_interval_start=interval_start,
        current_interval_elapsed_seconds=elapsed_seconds,
    )


def _battery_control_context() -> BatteryControlContext:
    return BatteryControlContext(reserve_floor_percent=20.0, capability_profile=None)


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


def _battery() -> BatteryState:
    return BatteryState(
        device_id="bat-001",
        soc_percent=55.0,
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
        grid_power_kw=4.8,
        energy_delivered_kwh=100.0,
        energy_returned_kwh=20.0,
        received_at=_NOW_UTC,
    )


def _degraded(role: DeviceRole, reason: str = "reconnecting") -> DegradedDeviceState:
    return DegradedDeviceState(
        device_id=f"{role.value}-001",
        role=role,
        reason=reason,
        occurred_at=_NOW_UTC,
    )


def _input_with(
    degraded_roles: Iterable[DeviceRole] = (),
    *,
    battery: BatteryState | DegradedDeviceState | None | object = _DEFAULT_SLOT,
    ev_charger: EVChargerState | DegradedDeviceState | None | object = _DEFAULT_SLOT,
    peak_context: PeakContext | None = None,
) -> EvaluationInput:
    degraded = set(degraded_roles)
    return EvaluationInput(
        inverter=_degraded(DeviceRole.inverter) if DeviceRole.inverter in degraded else _inverter(),
        battery=(
            _degraded(DeviceRole.battery)
            if DeviceRole.battery in degraded
            else battery
            if battery is not _DEFAULT_SLOT
            else _battery()
        ),
        ev_charger=(
            _degraded(DeviceRole.ev_charger)
            if DeviceRole.ev_charger in degraded
            else ev_charger
            if ev_charger is not _DEFAULT_SLOT
            else _ev_charger()
        ),
        grid_meter=_degraded(DeviceRole.grid_meter, "dsmr_stale")
        if DeviceRole.grid_meter in degraded
        else _grid_meter(),
        peak_context=peak_context if peak_context is not None else _peak_context(),
        strategy=_DEFAULT_STRATEGY,
        battery_control=_battery_control_context(),
        ev_scheduling=_ev_scheduling_context(),
    )


def test_peak_context_accepts_clock_aligned_utc_interval_start() -> None:
    context = _peak_context(interval_start=datetime(2026, 5, 4, 12, 15, tzinfo=UTC))

    assert context.current_interval_start.minute == 15
    assert context.current_interval_elapsed_seconds == 300


@pytest.mark.parametrize(
    "interval_start",
    [
        datetime(2026, 5, 4, 12, 0),
        datetime(2026, 5, 4, 14, 0, tzinfo=timezone(timedelta(hours=2))),
        datetime(2026, 5, 4, 12, 10, tzinfo=UTC),
        datetime(2026, 5, 4, 12, 15, 1, tzinfo=UTC),
        datetime(2026, 5, 4, 12, 15, 0, 1, tzinfo=UTC),
    ],
)
def test_peak_context_rejects_invalid_interval_start(interval_start: datetime) -> None:
    with pytest.raises(ValidationError):
        _peak_context(interval_start=interval_start)


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("configured_peak_limit_kw", 0.0),
        ("configured_peak_limit_kw", -0.1),
        ("current_monthly_recorded_peak_kw", -0.1),
        ("current_interval_elapsed_seconds", -1),
        ("current_interval_elapsed_seconds", 901),
    ],
)
def test_peak_context_rejects_invalid_numeric_values(field_name: str, value: float) -> None:
    data = _peak_context().model_dump()
    data[field_name] = value

    with pytest.raises(ValidationError):
        PeakContext(**data)


def test_projected_overshoot_returns_required_reduction_and_ordered_candidates() -> None:
    decision = evaluate_peak_limiting(_input_with())

    assert decision.action_required is True
    assert decision.required_reduction_kw == pytest.approx(1.2)
    assert decision.candidate_actions == (
        LoadReductionAction.reduce_ev_charge_rate,
        LoadReductionAction.discharge_battery,
    )
    assert decision.suppressed_by_operating_mode is None


@pytest.mark.parametrize("projection_kw", [5.0, 4.9])
def test_projection_at_or_below_limit_returns_no_action(projection_kw: float) -> None:
    decision = evaluate_peak_limiting(
        _input_with(peak_context=_peak_context(projection_kw=projection_kw))
    )

    assert decision.action_required is False
    assert decision.required_reduction_kw == 0.0
    assert decision.candidate_actions == ()


def test_degraded_battery_excludes_battery_candidate() -> None:
    decision = evaluate_peak_limiting(_input_with([DeviceRole.battery]))

    assert decision.action_required is True
    assert decision.candidate_actions == (LoadReductionAction.reduce_ev_charge_rate,)


def test_degraded_ev_charger_excludes_ev_candidate() -> None:
    decision = evaluate_peak_limiting(_input_with([DeviceRole.ev_charger]))

    assert decision.action_required is True
    assert decision.candidate_actions == (LoadReductionAction.discharge_battery,)


def test_absent_optional_slots_are_not_candidate_levers() -> None:
    decision = evaluate_peak_limiting(_input_with(battery=None, ev_charger=None))

    assert decision.action_required is True
    assert decision.candidate_actions == ()


def test_inverter_degradation_does_not_disable_peak_limiting() -> None:
    decision = evaluate_peak_limiting(_input_with([DeviceRole.inverter]))

    assert decision.action_required is True
    assert decision.candidate_actions == (
        LoadReductionAction.reduce_ev_charge_rate,
        LoadReductionAction.discharge_battery,
    )


@pytest.mark.parametrize(
    "roles,expected_mode",
    [
        ((DeviceRole.grid_meter,), SystemOperatingMode.conservative),
        ((DeviceRole.grid_meter, DeviceRole.battery), SystemOperatingMode.fail_safe),
    ],
)
def test_grid_meter_degradation_suppresses_peak_rule_candidates(
    roles: tuple[DeviceRole, ...],
    expected_mode: SystemOperatingMode,
) -> None:
    decision = evaluate_peak_limiting(_input_with(roles))

    assert decision.action_required is False
    assert decision.required_reduction_kw == 0.0
    assert decision.candidate_actions == ()
    assert decision.suppressed_by_operating_mode == expected_mode


def test_repeated_calls_return_equal_results() -> None:
    evaluation_input = _input_with()

    assert [evaluate_peak_limiting(evaluation_input) for _ in range(5)] == [
        evaluate_peak_limiting(evaluation_input)
    ] * 5


def test_rule_module_avoids_runtime_infrastructure_imports() -> None:
    import open_ems.engine.rules.peak_limiting as peak_limiting

    imported_modules = set()
    tree = ast.parse(peak_limiting.__loader__.get_source(peak_limiting.__name__) or "")
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
