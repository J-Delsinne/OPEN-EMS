"""Tests for system state models and deterministic derivation (Story 5.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

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
from open_ems.core.state import (
    OPERATING_MODE_GLOBAL_STATE,
    derive_component_state,
    derive_data_age_seconds,
    derive_global_state,
)

_NOW_UTC = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


def _inverter(read_at: datetime = _NOW_UTC) -> InverterState:
    return InverterState(
        device_id="inv-001",
        pv_power_kw=3.2,
        ac_power_kw=3.0,
        operating_mode="normal",
        read_at=read_at,
    )


def _battery(read_at: datetime = _NOW_UTC) -> BatteryState:
    return BatteryState(
        device_id="bat-001",
        soc_percent=55.0,
        battery_power_kw=1.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=read_at,
    )


def _ev_charger(read_at: datetime = _NOW_UTC) -> EVChargerState:
    return EVChargerState(
        device_id="ev-001",
        status="charging",
        session_active=True,
        current_power_kw=7.0,
        read_at=read_at,
    )


def _grid_meter(received_at: datetime = _NOW_UTC) -> GridMeterState:
    return GridMeterState(
        device_id="grid-001",
        grid_power_kw=1.2,
        energy_delivered_kwh=100.0,
        energy_returned_kwh=20.0,
        received_at=received_at,
    )


def _degraded(
    role: DeviceRole, reason: str, occurred_at: datetime = _NOW_UTC
) -> DegradedDeviceState:
    return DegradedDeviceState(
        device_id=f"{role.value}-001",
        role=role,
        reason=reason,
        occurred_at=occurred_at,
    )


def _snapshot(**overrides: object) -> SystemSnapshot:
    data: dict[str, object] = {
        "sequence_id": 1,
        "captured_at": _NOW_UTC,
        "global_state": GlobalState.normal,
        "operating_mode": SystemOperatingMode.normal,
        "inverter": _inverter(),
        "battery": None,
        "ev_charger": None,
        "grid_meter": _grid_meter(),
        "component_states": {
            DeviceRole.inverter: ComponentState.active,
            DeviceRole.battery: ComponentState.unavailable,
            DeviceRole.ev_charger: ComponentState.unavailable,
            DeviceRole.grid_meter: ComponentState.active,
        },
        "data_age_seconds": {
            DeviceRole.inverter: 0,
            DeviceRole.battery: None,
            DeviceRole.ev_charger: None,
            DeviceRole.grid_meter: 0,
        },
        "system_clock_status": "valid",
    }
    data.update(overrides)
    return SystemSnapshot(**data)


def test_state_enum_values_and_operating_mode_mapping() -> None:
    assert {state.value for state in GlobalState} == {"NORMAL", "DEGRADED", "STALE", "FAILED"}
    assert {state.value for state in ComponentState} == {
        "IDLE",
        "PENDING",
        "ACTIVE",
        "ERROR",
        "UNAVAILABLE",
        "STALE",
    }
    assert {mode.value for mode in SystemOperatingMode} == {
        "normal",
        "degraded",
        "conservative",
        "fail_safe",
    }
    assert OPERATING_MODE_GLOBAL_STATE == {
        SystemOperatingMode.normal: GlobalState.normal,
        SystemOperatingMode.degraded: GlobalState.degraded,
        SystemOperatingMode.conservative: GlobalState.degraded,
        SystemOperatingMode.fail_safe: GlobalState.failed,
    }


def test_snapshot_is_frozen_and_rejects_extra_fields() -> None:
    snapshot = _snapshot()

    with pytest.raises(ValidationError):
        snapshot.sequence_id = 2  # type: ignore[misc]

    with pytest.raises(ValidationError):
        _snapshot(extra_field="not allowed")


def test_snapshot_mapping_fields_are_not_mutable_or_caller_owned() -> None:
    component_states = {
        DeviceRole.inverter: ComponentState.active,
        DeviceRole.battery: ComponentState.unavailable,
        DeviceRole.ev_charger: ComponentState.unavailable,
        DeviceRole.grid_meter: ComponentState.active,
    }
    data_age_seconds = {
        DeviceRole.inverter: 1,
        DeviceRole.battery: None,
        DeviceRole.ev_charger: None,
        DeviceRole.grid_meter: 1,
    }
    snapshot = _snapshot(
        component_states=component_states,
        data_age_seconds=data_age_seconds,
    )

    component_states[DeviceRole.inverter] = ComponentState.error
    data_age_seconds[DeviceRole.inverter] = 99

    assert snapshot.component_states[DeviceRole.inverter] == ComponentState.active
    assert snapshot.data_age_seconds[DeviceRole.inverter] == 1

    with pytest.raises(TypeError):
        snapshot.component_states[DeviceRole.inverter] = ComponentState.error  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.data_age_seconds[DeviceRole.inverter] = 99  # type: ignore[index]


def test_snapshot_rejects_incomplete_role_maps() -> None:
    with pytest.raises(ValidationError):
        _snapshot(component_states={DeviceRole.inverter: ComponentState.active})

    with pytest.raises(ValidationError):
        _snapshot(data_age_seconds={DeviceRole.inverter: 0})


def test_snapshot_mapping_fields_are_json_serializable() -> None:
    snapshot = _snapshot()

    dumped = snapshot.model_dump(mode="json")

    assert dumped["component_states"]["inverter"] == "ACTIVE"
    assert dumped["data_age_seconds"]["inverter"] == 0
    assert "component_states" in snapshot.model_dump_json()


def test_snapshot_requires_utc_captured_at() -> None:
    with pytest.raises(ValidationError):
        _snapshot(captured_at=datetime(2024, 1, 1, 12, 0, 0))

    with pytest.raises(ValidationError):
        _snapshot(captured_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=1))))


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (None, ComponentState.unavailable),
        (_inverter(), ComponentState.active),
        (_degraded(DeviceRole.inverter, "reconnecting"), ComponentState.error),
        (_degraded(DeviceRole.grid_meter, "dsmr_stale"), ComponentState.error),
    ],
)
def test_component_state_derivation(
    state: InverterState | DegradedDeviceState | None,
    expected: ComponentState,
) -> None:
    assert derive_component_state(state) == expected


def test_global_state_derivation_priority_failed_degraded_normal() -> None:
    assert (
        derive_global_state(
            {
                DeviceRole.inverter: _degraded(DeviceRole.inverter, "device_id_mismatch"),
                DeviceRole.grid_meter: _degraded(DeviceRole.grid_meter, "dsmr_stale"),
            }
        )
        == GlobalState.failed
    )
    assert (
        derive_global_state(
            {
                DeviceRole.inverter: _degraded(DeviceRole.inverter, "reconnecting"),
                DeviceRole.grid_meter: _grid_meter(),
            }
        )
        == GlobalState.degraded
    )
    assert (
        derive_global_state(
            {
                DeviceRole.inverter: _inverter(),
                DeviceRole.grid_meter: _grid_meter(),
            }
        )
        == GlobalState.normal
    )


def test_required_and_optional_absence_rules() -> None:
    assert (
        derive_global_state(
            {
                DeviceRole.inverter: _inverter(),
                DeviceRole.grid_meter: _grid_meter(),
                DeviceRole.battery: None,
                DeviceRole.ev_charger: None,
            }
        )
        == GlobalState.normal
    )
    assert (
        derive_global_state({DeviceRole.inverter: _inverter(), DeviceRole.grid_meter: None})
        == GlobalState.degraded
    )
    assert (
        derive_global_state({DeviceRole.inverter: None, DeviceRole.grid_meter: _grid_meter()})
        == GlobalState.degraded
    )


@pytest.mark.parametrize(
    "reason",
    ["device_id_mismatch", "validation_error:bad value", "unexpected_raw_state_type"],
)
def test_failed_class_degraded_reasons(reason: str) -> None:
    assert (
        derive_global_state(
            {
                DeviceRole.inverter: _degraded(DeviceRole.inverter, reason),
                DeviceRole.grid_meter: _grid_meter(),
            }
        )
        == GlobalState.failed
    )


def test_unknown_degraded_reason_remains_degraded() -> None:
    assert (
        derive_global_state(
            {
                DeviceRole.inverter: _inverter(),
                DeviceRole.grid_meter: _degraded(DeviceRole.grid_meter, "future_reason"),
            }
        )
        == GlobalState.degraded
    )


def test_dsmr_stale_reason_remains_degraded_adapter_input() -> None:
    assert (
        derive_global_state(
            {
                DeviceRole.inverter: _inverter(),
                DeviceRole.grid_meter: _degraded(DeviceRole.grid_meter, "dsmr_stale"),
            }
        )
        == GlobalState.degraded
    )
    assert (
        derive_global_state(
            {
                DeviceRole.inverter: _inverter(),
                DeviceRole.grid_meter: _grid_meter(),
                DeviceRole.battery: _degraded(DeviceRole.battery, "dsmr_stale"),
            }
        )
        == GlobalState.degraded
    )


def test_known_optional_unavailable_does_not_degrade_global_state() -> None:
    assert (
        derive_global_state(
            {
                DeviceRole.inverter: _inverter(),
                DeviceRole.grid_meter: _grid_meter(),
                DeviceRole.battery: _degraded(DeviceRole.battery, "unavailable"),
            }
        )
        == GlobalState.normal
    )


def test_data_age_derivation_from_each_timestamp_type() -> None:
    captured_at = _NOW_UTC
    measured_at = _NOW_UTC - timedelta(seconds=12.5)

    assert derive_data_age_seconds(_inverter(measured_at), captured_at) == 12
    assert derive_data_age_seconds(_battery(measured_at), captured_at) == 12
    assert derive_data_age_seconds(_ev_charger(measured_at), captured_at) == 12
    assert derive_data_age_seconds(_grid_meter(measured_at), captured_at) == 12
    assert (
        derive_data_age_seconds(
            _degraded(DeviceRole.grid_meter, "reconnecting", measured_at),
            captured_at,
        )
        == 12
    )
    assert derive_data_age_seconds(None, captured_at) is None
