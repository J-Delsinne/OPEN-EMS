from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from open_ems.core import (
    BatteryState,
    ComponentState,
    DegradedDeviceState,
    DeviceRole,
    EnergyStrategy,
    EVChargerState,
    GridMeterState,
    InverterState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.core.state import DeviceSlot

HomeownerCard = Literal["battery", "solar", "grid", "ev"]

# AC5 / AC14 — plain-language labels mapped 1:1 from EnergyStrategy enum values.
# Per the structural contract, the headline reads ONLY from operating_mode +
# active_strategy; this label table is the sole source of the user-visible
# strategy text. Story 10.3 may extend this mapping when the write path lands;
# any new EnergyStrategy member added without a label here will fail the
# discovery test in test_state_serialization.
_STRATEGY_LABELS: Mapping[EnergyStrategy, str] = {
    EnergyStrategy.minimize_cost: "Minimize Cost",
    EnergyStrategy.maximize_self_consumption: "Maximize Self-Consumption",
    EnergyStrategy.prioritize_ev: "Prioritize EV",
}

# AC5 / AC7 — degraded-mode explanation lines. The UX spec mandates a calm,
# plain-language one-liner derived from the operating_mode value. `normal` has
# no explanation (the headline alone is sufficient). The three non-normal
# modes each get a stable string; UX spec §"Status Headline Card" forbids
# alarming wording (no amber, no exclamation marks, no error codes).
_DEGRADED_EXPLANATIONS: Mapping[SystemOperatingMode, str] = {
    SystemOperatingMode.normal: "",
    SystemOperatingMode.degraded: "Some optimization features are reduced.",
    SystemOperatingMode.conservative: (
        "Some optimization features are paused while the system recovers."
    ),
    SystemOperatingMode.fail_safe: "Active control is paused for safety.",
}


def serialize_homeowner_snapshot(snapshot: SystemSnapshot) -> dict[str, object]:
    """Serialize a SystemSnapshot using only homeowner-safe allowlisted fields."""
    return {
        "sequence_id": snapshot.sequence_id,
        "captured_at": _datetime_to_iso(snapshot.captured_at),
        "global_state": snapshot.global_state.value,
        "operating_mode": snapshot.operating_mode.value,
        "active_strategy": snapshot.active_strategy.value,
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
        "active_strategy": snapshot.active_strategy.value,
        "system_clock_status": snapshot.system_clock_status,
        "component_states": _role_map(snapshot.component_states),
        "data_age_seconds": _role_map(snapshot.data_age_seconds),
        "inverter": _serialize_installer_device(snapshot.inverter),
        "battery": _serialize_installer_device(snapshot.battery),
        "ev_charger": _serialize_installer_device(snapshot.ev_charger),
        "grid_meter": _serialize_installer_device(snapshot.grid_meter),
    }


def build_homeowner_headline_context(snapshot: SystemSnapshot) -> dict[str, object]:
    """Build the homeowner status-headline template context (AC5 / AC14).

    Reads ONLY ``snapshot.operating_mode`` and ``snapshot.active_strategy``;
    never inspects device slots, component_states, or data_age_seconds. This
    is the structural enforcement of Epic 10's cross-story constraint that
    the headline is derived exclusively from SystemOperatingMode + active
    strategy — verified by test_state_serialization::test_headline_ignores_device_state.
    """
    operating_mode = snapshot.operating_mode
    active_strategy = snapshot.active_strategy
    presentation_mode = "normal" if operating_mode is SystemOperatingMode.normal else "degraded"
    # Defensive fallbacks: if a future enum member ships ahead of the label table
    # the exhaustiveness tests will fail at test time, but production must
    # degrade gracefully rather than 500 the homeowner status-headline endpoint.
    strategy_label = _STRATEGY_LABELS.get(
        active_strategy, active_strategy.value.replace("_", " ").title()
    )
    if presentation_mode == "normal":
        headline_text = f"Your home is running on solar · {strategy_label}"
        explanation_text = ""
    else:
        headline_text = "Running with limited functionality"
        explanation_text = _DEGRADED_EXPLANATIONS.get(operating_mode, "")
    return {
        "operating_mode": operating_mode.value,
        "active_strategy": active_strategy.value,
        "presentation_mode": presentation_mode,
        "strategy_label": strategy_label,
        "headline_text": headline_text,
        "explanation_text": explanation_text,
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


def _homeowner_card_rows(card: HomeownerCard, device_payload: object) -> list[dict[str, str]]:
    """Build the single primary metric row per card (AC8 / AC9).

    UX spec §"Metric Card" mandates a single body value with a plain-language
    label — no auxiliary metric rows in v1. Returns an empty list when the
    device payload is missing or marks itself "unavailable"; the template's
    ``unavailable`` branch then renders the "Unavailable" body text.

    Exception: the EV branch (preserved intact for Story 10.2's override card)
    may return a row whose value field is the literal string "Unavailable" when
    the device is reachable but has no live ``current_power_kw`` reading. In
    that case the template renders the value through the normal body slot
    rather than the ``unavailable`` branch. Story 10.2 will reconcile this
    surface when the override flow lands.
    """
    if not isinstance(device_payload, dict) or device_payload.get("state") == "unavailable":
        return []
    if card == "battery":
        return [
            {"label": "Charge", "value": _format_percent(device_payload["soc_percent"])},
        ]
    if card == "solar":
        return [
            {"label": "PV output", "value": _format_kw(device_payload["pv_power_kw"])},
        ]
    if card == "grid":
        return [
            {"label": "Grid power", "value": _format_grid_power(device_payload["grid_power_kw"])},
        ]
    # EV card — left for Story 10.2 to overhaul. Single-row body for now so the
    # contract (rows: list[{label, value}]) is consistent across cards.
    current_power = device_payload["current_power_kw"]
    return [
        {
            "label": "Power",
            "value": _format_kw(current_power) if current_power is not None else "Unavailable",
        },
    ]


def _format_grid_power(value: object) -> str:
    """AC9 grid card formatting — magnitude + plain-language direction suffix.

    Sign convention per architecture.md §"Energy sign convention":
    positive = importing from grid, negative = exporting to grid.
    Rounded magnitude of 0.0 kW renders as "idle" rather than a directional label.
    """
    kw = _number(value)
    if math.isnan(kw) or math.isinf(kw):
        return "Unavailable"
    rounded = round(kw, 1)
    if rounded > 0:
        direction = "importing"
    elif rounded < 0:
        direction = "exporting"
    else:
        direction = "idle"
    # abs() is load-bearing: round(-0.04, 1) returns -0.0; without abs() the
    # f-string would render "-0.0 kW idle". Keep abs() even if the branch above
    # appears to collapse the sign — it does not for the -0.0 special case.
    return f"{abs(rounded):.1f} kW {direction}"


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
