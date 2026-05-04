from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from open_ems.core import (
    BatteryState,
    ComponentState,
    DegradedDeviceState,
    DeviceRole,
    EVChargerState,
    GlobalState,
    GridMeterState,
    InverterState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.engine import EvaluationInput, derive_recommended_operating_mode

_NOW_UTC = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)


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


def _degraded(role: DeviceRole, reason: str = "reconnecting") -> DegradedDeviceState:
    return DegradedDeviceState(
        device_id=f"{role.value}-001",
        role=role,
        reason=reason,
        occurred_at=_NOW_UTC,
    )


def _input_with(degraded_roles: Iterable[DeviceRole] = ()) -> EvaluationInput:
    degraded = set(degraded_roles)
    return EvaluationInput(
        inverter=_degraded(DeviceRole.inverter) if DeviceRole.inverter in degraded else _inverter(),
        battery=_degraded(DeviceRole.battery) if DeviceRole.battery in degraded else _battery(),
        ev_charger=_degraded(DeviceRole.ev_charger)
        if DeviceRole.ev_charger in degraded
        else _ev_charger(),
        grid_meter=_degraded(DeviceRole.grid_meter, "dsmr_stale")
        if DeviceRole.grid_meter in degraded
        else _grid_meter(),
    )


def test_healthy_required_roles_with_absent_optional_roles_recommend_normal() -> None:
    evaluation_input = EvaluationInput(
        inverter=_inverter(),
        battery=None,
        ev_charger=None,
        grid_meter=_grid_meter(),
    )

    assert derive_recommended_operating_mode(evaluation_input) == SystemOperatingMode.normal


@pytest.mark.parametrize(
    "role",
    [DeviceRole.inverter, DeviceRole.battery, DeviceRole.ev_charger],
)
def test_single_non_grid_meter_degradation_recommends_degraded(role: DeviceRole) -> None:
    assert derive_recommended_operating_mode(_input_with([role])) == SystemOperatingMode.degraded


@pytest.mark.parametrize(
    "roles",
    [
        (DeviceRole.inverter, DeviceRole.battery),
        (DeviceRole.inverter, DeviceRole.ev_charger),
        (DeviceRole.battery, DeviceRole.ev_charger),
        (DeviceRole.inverter, DeviceRole.battery, DeviceRole.ev_charger),
    ],
)
def test_non_grid_meter_degradation_combinations_recommend_degraded(
    roles: tuple[DeviceRole, ...],
) -> None:
    assert derive_recommended_operating_mode(_input_with(roles)) == SystemOperatingMode.degraded


def test_grid_meter_stale_degradation_recommends_conservative() -> None:
    assert (
        derive_recommended_operating_mode(_input_with([DeviceRole.grid_meter]))
        == SystemOperatingMode.conservative
    )


@pytest.mark.parametrize(
    "second_role",
    [DeviceRole.inverter, DeviceRole.battery, DeviceRole.ev_charger],
)
def test_grid_meter_plus_any_second_degraded_role_recommends_fail_safe(
    second_role: DeviceRole,
) -> None:
    assert (
        derive_recommended_operating_mode(_input_with([DeviceRole.grid_meter, second_role]))
        == SystemOperatingMode.fail_safe
    )


def test_all_roles_degraded_recommends_fail_safe() -> None:
    assert (
        derive_recommended_operating_mode(_input_with(DeviceRole)) == SystemOperatingMode.fail_safe
    )


@pytest.mark.parametrize("missing_field", ["inverter", "grid_meter"])
def test_required_slots_reject_none(missing_field: str) -> None:
    slots: dict[str, object] = {
        "inverter": _inverter(),
        "battery": _battery(),
        "ev_charger": _ev_charger(),
        "grid_meter": _grid_meter(),
    }
    slots[missing_field] = None
    with pytest.raises(ValidationError):
        EvaluationInput(**slots)  # type: ignore[arg-type]


def test_derivation_is_deterministic_across_repeated_calls_and_input_construction() -> None:
    roles_a = [DeviceRole.ev_charger, DeviceRole.inverter]
    roles_b = [DeviceRole.inverter, DeviceRole.ev_charger]
    input_a = _input_with(roles_a)
    input_b = EvaluationInput(
        grid_meter=_grid_meter(),
        ev_charger=_degraded(DeviceRole.ev_charger),
        battery=_battery(),
        inverter=_degraded(DeviceRole.inverter),
    )

    expected = SystemOperatingMode.degraded

    assert derive_recommended_operating_mode(input_a) == expected
    assert derive_recommended_operating_mode(input_b) == expected
    assert [derive_recommended_operating_mode(input_a) for _ in range(10)] == [expected] * 10
    assert roles_a != roles_b


def test_from_snapshot_copies_slots_and_does_not_echo_snapshot_operating_mode() -> None:
    snapshot = SystemSnapshot(
        sequence_id=42,
        captured_at=_NOW_UTC,
        global_state=GlobalState.failed,
        operating_mode=SystemOperatingMode.fail_safe,
        inverter=_inverter(),
        battery=None,
        ev_charger=None,
        grid_meter=_grid_meter(),
        component_states={
            DeviceRole.inverter: ComponentState.active,
            DeviceRole.battery: ComponentState.unavailable,
            DeviceRole.ev_charger: ComponentState.unavailable,
            DeviceRole.grid_meter: ComponentState.active,
        },
        data_age_seconds={
            DeviceRole.inverter: 0,
            DeviceRole.battery: None,
            DeviceRole.ev_charger: None,
            DeviceRole.grid_meter: 0,
        },
        system_clock_status="valid",
    )

    evaluation_input = EvaluationInput.from_snapshot(snapshot)

    assert evaluation_input.inverter is snapshot.inverter
    assert evaluation_input.battery is snapshot.battery
    assert evaluation_input.ev_charger is snapshot.ev_charger
    assert evaluation_input.grid_meter is snapshot.grid_meter
    assert derive_recommended_operating_mode(evaluation_input) == SystemOperatingMode.normal
