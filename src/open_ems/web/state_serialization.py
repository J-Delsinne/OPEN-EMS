from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from open_ems.core import (
    BatteryState,
    ComponentState,
    DegradedDeviceState,
    DeviceRole,
    EVChargerState,
    GridMeterState,
    InverterState,
    SystemSnapshot,
)
from open_ems.core.state import DeviceSlot


def serialize_homeowner_snapshot(snapshot: SystemSnapshot) -> dict[str, object]:
    """Serialize a SystemSnapshot using only homeowner-safe allowlisted fields."""
    return {
        "sequence_id": snapshot.sequence_id,
        "captured_at": _datetime_to_iso(snapshot.captured_at),
        "global_state": snapshot.global_state.value,
        "operating_mode": snapshot.operating_mode.value,
        "system_clock_status": snapshot.system_clock_status,
        "data_age_seconds": _role_map(snapshot.data_age_seconds),
        "components": _role_map(snapshot.component_states),
        "inverter": _serialize_homeowner_inverter(snapshot.inverter),
        "battery": _serialize_homeowner_battery(snapshot.battery),
        "ev_charger": _serialize_homeowner_ev_charger(snapshot.ev_charger),
        "grid_meter": _serialize_homeowner_grid_meter(snapshot.grid_meter),
    }


def serialize_installer_snapshot(snapshot: SystemSnapshot) -> dict[str, object]:
    """Serialize a SystemSnapshot with installer diagnostics, excluding auth/session internals."""
    return {
        "sequence_id": snapshot.sequence_id,
        "captured_at": _datetime_to_iso(snapshot.captured_at),
        "global_state": snapshot.global_state.value,
        "operating_mode": snapshot.operating_mode.value,
        "system_clock_status": snapshot.system_clock_status,
        "component_states": _role_map(snapshot.component_states),
        "data_age_seconds": _role_map(snapshot.data_age_seconds),
        "inverter": _serialize_installer_device(snapshot.inverter),
        "battery": _serialize_installer_device(snapshot.battery),
        "ev_charger": _serialize_installer_device(snapshot.ev_charger),
        "grid_meter": _serialize_installer_device(snapshot.grid_meter),
    }


def _datetime_to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _role_map(values: Mapping[DeviceRole, object]) -> dict[str, object]:
    return {
        role.value: value.value if isinstance(value, ComponentState) else value
        for role, value in values.items()
    }


def _public_unavailable(state: DeviceSlot) -> dict[str, object] | None:
    if state is None:
        return None
    if isinstance(state, DegradedDeviceState):
        return {"state": "unavailable"}
    return None


def _serialize_homeowner_inverter(state: DeviceSlot) -> dict[str, object] | None:
    public_state = _public_unavailable(state)
    if public_state is not None or state is None:
        return public_state
    if not isinstance(state, InverterState):
        return {"state": "unavailable"}
    return {
        "pv_power_kw": state.pv_power_kw,
        "ac_power_kw": state.ac_power_kw,
        "operating_mode": state.operating_mode,
    }


def _serialize_homeowner_battery(state: DeviceSlot) -> dict[str, object] | None:
    public_state = _public_unavailable(state)
    if public_state is not None or state is None:
        return public_state
    if not isinstance(state, BatteryState):
        return {"state": "unavailable"}
    return {
        "soc_percent": state.soc_percent,
        "battery_power_kw": state.battery_power_kw,
        "capacity_kwh": state.capacity_kwh,
        "operating_mode": state.operating_mode,
    }


def _serialize_homeowner_ev_charger(state: DeviceSlot) -> dict[str, object] | None:
    public_state = _public_unavailable(state)
    if public_state is not None or state is None:
        return public_state
    if not isinstance(state, EVChargerState):
        return {"state": "unavailable"}
    return {
        "status": state.status,
        "session_active": state.session_active,
        "current_power_kw": state.current_power_kw,
    }


def _serialize_homeowner_grid_meter(state: DeviceSlot) -> dict[str, object] | None:
    public_state = _public_unavailable(state)
    if public_state is not None or state is None:
        return public_state
    if not isinstance(state, GridMeterState):
        return {"state": "unavailable"}
    return {
        "grid_power_kw": state.grid_power_kw,
        "energy_delivered_kwh": state.energy_delivered_kwh,
        "energy_returned_kwh": state.energy_returned_kwh,
    }


def _serialize_installer_device(state: DeviceSlot) -> dict[str, Any] | None:
    if state is None:
        return None
    if isinstance(state, DegradedDeviceState):
        return {
            "state": "degraded",
            "device_id": state.device_id,
            "role": state.role.value,
            "reason": state.reason,
            "occurred_at": _datetime_to_iso(state.occurred_at),
        }
    return state.model_dump(mode="json")
