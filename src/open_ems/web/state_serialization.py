from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

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

HomeownerCard = Literal["battery", "solar", "grid", "ev"]


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


def build_homeowner_card_context(
    snapshot: SystemSnapshot, card: HomeownerCard
) -> dict[str, object]:
    """Build display-ready fragment context from the homeowner-safe SSE payload."""
    payload = serialize_homeowner_snapshot(snapshot)
    component_key = _card_component_key(card)
    component_state = str(_mapping_value(payload["components"], component_key))
    data_age_seconds = _age_value(payload["data_age_seconds"], component_key)
    device_payload = payload[component_key]

    title = _card_title(card)
    rows = _homeowner_card_rows(card, device_payload)
    is_unavailable = (
        component_state == ComponentState.unavailable.value
        or not isinstance(device_payload, dict)
        or device_payload.get("state") == "unavailable"
    )
    return {
        "card": card,
        "title": title,
        "component_key": component_key,
        "component_state": component_state,
        "data_age_seconds": data_age_seconds,
        "stale_caption": _stale_caption(title, data_age_seconds)
        if component_state == ComponentState.stale.value
        else "",
        "system_clock_status": payload["system_clock_status"],
        "rows": rows,
        "unavailable": is_unavailable,
    }


def _datetime_to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _role_map(values: Mapping[DeviceRole, object]) -> dict[str, object]:
    return {
        role.value: value.value if isinstance(value, ComponentState) else value
        for role, value in values.items()
    }


def _mapping_value(value: object, key: str) -> object:
    if not isinstance(value, Mapping):
        raise TypeError("Expected role mapping in serialized snapshot")
    return value[key]


def _age_value(value: object, key: str) -> int | None:
    age = _mapping_value(value, key)
    return age if isinstance(age, int) else None


def _card_component_key(card: HomeownerCard) -> str:
    return {"battery": "battery", "solar": "inverter", "grid": "grid_meter", "ev": "ev_charger"}[
        card
    ]


def _card_title(card: HomeownerCard) -> str:
    return {
        "battery": "Battery",
        "solar": "Solar",
        "grid": "Grid",
        "ev": "EV charger",
    }[card]


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("Expected numeric value in serialized snapshot")
    return float(value)


def _format_kw(value: object) -> str:
    return f"{_number(value):.1f} kW"


def _format_percent(value: object) -> str:
    return f"{_number(value):.0f}%"


def _format_kwh(value: object) -> str:
    return f"{_number(value):.1f} kWh"


def _homeowner_card_rows(card: HomeownerCard, device_payload: object) -> list[dict[str, str]]:
    if not isinstance(device_payload, dict) or device_payload.get("state") == "unavailable":
        return []
    if card == "battery":
        return [
            {"label": "Charge", "value": _format_percent(device_payload["soc_percent"])},
            {"label": "Power", "value": _format_kw(device_payload["battery_power_kw"])},
            {"label": "Mode", "value": str(device_payload["operating_mode"])},
        ]
    if card == "solar":
        return [
            {"label": "PV output", "value": _format_kw(device_payload["pv_power_kw"])},
            {"label": "AC output", "value": _format_kw(device_payload["ac_power_kw"])},
            {"label": "Mode", "value": str(device_payload["operating_mode"])},
        ]
    if card == "grid":
        return [
            {"label": "Grid power", "value": _format_kw(device_payload["grid_power_kw"])},
            {
                "label": "Imported",
                "value": _format_kwh(device_payload["energy_delivered_kwh"]),
            },
            {
                "label": "Exported",
                "value": _format_kwh(device_payload["energy_returned_kwh"]),
            },
        ]
    return [
        {"label": "Status", "value": str(device_payload["status"])},
        {"label": "Session", "value": "Active" if device_payload["session_active"] else "Idle"},
        {
            "label": "Power",
            "value": _format_kw(device_payload["current_power_kw"])
            if device_payload["current_power_kw"] is not None
            else "Unavailable",
        },
    ]


def _stale_caption(title: str, data_age_seconds: int | None) -> str:
    if data_age_seconds is None:
        return f"{title} data is stale"
    if data_age_seconds < 60:
        age = f"{data_age_seconds} sec"
    else:
        age = f"{data_age_seconds // 60} min"
    return f"{title} data last updated {age} ago"


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
