from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone

import pytest
from pydantic import ValidationError

from open_ems.core import (
    BatteryState,
    ComponentState,
    DeviceRole,
    EnergyStrategy,
    EVChargerState,
    GlobalState,
    GridMeterState,
    InverterState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.core.devices import DeviceCapabilityProfile
from open_ems.engine import EvaluationInput, PeakContext
from open_ems.engine.models import (
    BatteryControlContext,
    EVChargingWindow,
    EVSchedulingContext,
)
from open_ems.engine.rules.battery_control import (
    BatteryIntent,
    BatteryIntentAction,
    evaluate_battery_control,
)
from open_ems.engine.rules.ev_scheduling import (
    EVChargerIntent,
    EVChargerIntentAction,
    evaluate_ev_scheduling,
)

_NOW_UTC = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


def _peak_context() -> PeakContext:
    return PeakContext(
        current_partial_window_projection_kw=3.8,
        configured_peak_limit_kw=5.0,
        current_monthly_recorded_peak_kw=4.2,
        current_interval_start=_NOW_UTC,
        current_interval_elapsed_seconds=120,
    )


def _battery_context() -> BatteryControlContext:
    return BatteryControlContext(reserve_floor_percent=20.0, capability_profile=None)


def _ev_context() -> EVSchedulingContext:
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


def _snapshot() -> SystemSnapshot:
    return SystemSnapshot(
        sequence_id=1,
        captured_at=_NOW_UTC,
        global_state=GlobalState.normal,
        operating_mode=SystemOperatingMode.normal,
        inverter=_inverter(),
        battery=_battery(),
        ev_charger=_ev_charger(),
        grid_meter=_grid_meter(),
        component_states={
            DeviceRole.inverter: ComponentState.active,
            DeviceRole.battery: ComponentState.active,
            DeviceRole.ev_charger: ComponentState.active,
            DeviceRole.grid_meter: ComponentState.active,
        },
        data_age_seconds={
            DeviceRole.inverter: 0,
            DeviceRole.battery: 0,
            DeviceRole.ev_charger: 0,
            DeviceRole.grid_meter: 0,
        },
        system_clock_status="valid",
    )


def test_battery_control_context_accepts_floor_and_optional_capability_profile() -> None:
    profile = DeviceCapabilityProfile(device_id="bat-001", model="test-battery")
    context = BatteryControlContext(reserve_floor_percent=20.0, capability_profile=profile)

    assert context.reserve_floor_percent == 20.0
    assert context.capability_profile is profile


@pytest.mark.parametrize("reserve_floor_percent", [-0.1, 100.1])
def test_battery_control_context_rejects_out_of_range_floor(
    reserve_floor_percent: float,
) -> None:
    with pytest.raises(ValidationError):
        BatteryControlContext(
            reserve_floor_percent=reserve_floor_percent,
            capability_profile=None,
        )


def test_ev_charging_window_accepts_same_day_and_overnight_windows() -> None:
    same_day = EVChargingWindow(
        start_local_time=time(9, 0),
        end_local_time=time(17, 0),
        timezone_name="Europe/Brussels",
    )
    overnight = EVChargingWindow(
        start_local_time=time(22, 0),
        end_local_time=time(6, 0),
        timezone_name="Europe/Brussels",
    )

    assert same_day.start_local_time < same_day.end_local_time
    assert overnight.start_local_time > overnight.end_local_time


def test_ev_charging_window_rejects_equal_times_and_unknown_timezone() -> None:
    with pytest.raises(ValidationError):
        EVChargingWindow(
            start_local_time=time(9, 0),
            end_local_time=time(9, 0),
            timezone_name="Europe/Brussels",
        )

    with pytest.raises(ValidationError):
        EVChargingWindow(
            start_local_time=time(9, 0),
            end_local_time=time(17, 0),
            timezone_name="Mars/OlympusMons",
        )


def test_ev_scheduling_context_requires_utc_evaluated_at_and_positive_target_rate() -> None:
    context = EVSchedulingContext(
        capability_profile=None,
        charging_window=None,
        homeowner_override_active=True,
        evaluated_at=_NOW_UTC,
        target_charge_rate_kw=7.4,
    )

    assert context.evaluated_at is _NOW_UTC
    assert context.target_charge_rate_kw == 7.4

    for invalid_dt in (
        datetime(2026, 5, 5, 12, 0, 0),
        datetime(2026, 5, 5, 14, 0, 0, tzinfo=timezone(timedelta(hours=2))),
    ):
        with pytest.raises(ValidationError):
            EVSchedulingContext(
                capability_profile=None,
                charging_window=None,
                homeowner_override_active=False,
                evaluated_at=invalid_dt,
                target_charge_rate_kw=None,
            )

    with pytest.raises(ValidationError):
        EVSchedulingContext(
            capability_profile=None,
            charging_window=None,
            homeowner_override_active=False,
            evaluated_at=_NOW_UTC,
            target_charge_rate_kw=0.0,
        )


@pytest.mark.parametrize("missing_field", ["battery_control", "ev_scheduling"])
def test_evaluation_input_requires_battery_and_ev_contexts(missing_field: str) -> None:
    data = {
        "inverter": _inverter(),
        "battery": _battery(),
        "ev_charger": _ev_charger(),
        "grid_meter": _grid_meter(),
        "peak_context": _peak_context(),
        "strategy": EnergyStrategy.minimize_cost,
        "battery_control": _battery_context(),
        "ev_scheduling": _ev_context(),
    }
    data.pop(missing_field)

    with pytest.raises(ValidationError):
        EvaluationInput(**data)


def test_from_snapshot_requires_battery_and_ev_contexts_as_keyword_only() -> None:
    snapshot = _snapshot()

    with pytest.raises(TypeError):
        EvaluationInput.from_snapshot(
            snapshot,
            _peak_context(),  # type: ignore[misc]
            EnergyStrategy.minimize_cost,
            _battery_context(),
            _ev_context(),
        )

    with pytest.raises(TypeError):
        EvaluationInput.from_snapshot(
            snapshot,
            peak_context=_peak_context(),
            strategy=EnergyStrategy.minimize_cost,
            battery_control=_battery_context(),
        )

    evaluation_input = EvaluationInput.from_snapshot(
        snapshot,
        peak_context=_peak_context(),
        strategy=EnergyStrategy.prioritize_ev,
        battery_control=_battery_context(),
        ev_scheduling=_ev_context(),
    )

    assert evaluation_input.battery_control == _battery_context()
    assert evaluation_input.ev_scheduling == _ev_context()
    assert evaluation_input.strategy is EnergyStrategy.prioritize_ev


def test_engine_package_exports_new_battery_and_ev_contracts() -> None:
    import open_ems.engine as engine

    assert engine.BatteryControlContext is BatteryControlContext
    assert engine.BatteryIntent is BatteryIntent
    assert engine.BatteryIntentAction is BatteryIntentAction
    assert engine.EVChargingWindow is EVChargingWindow
    assert engine.EVSchedulingContext is EVSchedulingContext
    assert engine.EVChargerIntent is EVChargerIntent
    assert engine.EVChargerIntentAction is EVChargerIntentAction
    assert engine.evaluate_battery_control is evaluate_battery_control
    assert engine.evaluate_ev_scheduling is evaluate_ev_scheduling
