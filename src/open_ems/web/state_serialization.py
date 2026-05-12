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
    EVOverrideState,
    GridMeterState,
    InverterState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.core.constraints import ActiveConstraints
from open_ems.core.energy import WeeklyEnergySummaryRow
from open_ems.core.state import DeviceSlot
from open_ems.services.installer_anomaly import (
    AnomalySignature,
    detect_anomaly_from_snapshot,
    evaluate_dismiss_state,
)
from open_ems.storage.repositories.device_repo import DeviceRegistryEntry
from open_ems.storage.repositories.event_log_repo import EventLogEntry

OverrideRenderState = Literal["idle", "optimistic", "confirmed", "fallback"]

HomeownerCard = Literal["battery", "solar", "grid", "ev"]

# Story 11.1 — installer dashboard surface.

# AC3: SystemOperatingMode → installer-facing health display label. Direct
# mapping with no interpretation layer. Three distinct display strings cover
# the four enum members (degraded + conservative both → DEGRADED).
_INSTALLER_HEALTH_DISPLAY: Mapping[SystemOperatingMode, str] = {
    SystemOperatingMode.normal: "NORMAL",
    SystemOperatingMode.degraded: "DEGRADED",
    SystemOperatingMode.conservative: "DEGRADED",
    SystemOperatingMode.fail_safe: "FAILED",
}

# AC4 / Q1: ComponentState → installer-facing per-device-row display label.
# The engine-internal enum name `error` stays; the installer surface presents
# it as DEGRADED (the existing `derive_component_state` function maps any
# DegradedDeviceState with reason != "unavailable" to ComponentState.error,
# which is exactly the "device is reachable but reporting trouble" surface
# epic AC line 2278-2279 calls DEGRADED).
_INSTALLER_COMPONENT_STATE_DISPLAY: Mapping[ComponentState, str] = {
    ComponentState.active: "ACTIVE",
    ComponentState.error: "DEGRADED",
    ComponentState.unavailable: "UNAVAILABLE",
    ComponentState.stale: "UNAVAILABLE",  # stale treated as unavailable for installer
    ComponentState.pending: "UNAVAILABLE",
    ComponentState.idle: "UNAVAILABLE",
}

# AC4: device_registry.last_capability_status → installer-facing badge label.
_INSTALLER_CAPABILITY_BADGE_DISPLAY: Mapping[str, str] = {
    "full": "FULL",
    "reduced": "REDUCED",
    "unsupported": "UNSUPPORTED",
}

# AC4: device-row display order. Required roles first (grid_meter, inverter),
# then optional roles (battery, ev_charger).
_INSTALLER_DEVICE_ROW_ORDER: tuple[DeviceRole, ...] = (
    DeviceRole.grid_meter,
    DeviceRole.inverter,
    DeviceRole.battery,
    DeviceRole.ev_charger,
)

# AC4: friendly role labels for the leftmost column of each device row.
_INSTALLER_DEVICE_ROLE_LABELS: Mapping[DeviceRole, str] = {
    DeviceRole.grid_meter: "Grid meter",
    DeviceRole.inverter: "Inverter",
    DeviceRole.battery: "Battery",
    DeviceRole.ev_charger: "EV charger",
}

# AC5: peak tracker thresholds — single source of truth.
_PEAK_APPROACH_RATIO = 0.9

# AC6: event-log preview summary truncation length.
_EVENT_LOG_PREVIEW_SUMMARY_MAX = 80

# AC6: server-side fallback timezone for the event-log preview. Story 11.2
# will replace with client-side Intl.DateTimeFormat. The DST-aware Brussels
# zone is the deployment target for OPEN-EMS v1.
_EVENT_LOG_DISPLAY_TIMEZONE = "Europe/Brussels"

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

# Story 10.2 AC12 — plain-language failure-reason mapping. Each key is the EXACT
# rejection-reason string emitted by ``engine/policy_guard.py`` (P0–P5 paths)
# or the dispatch-timeout / correlation-broken synthetic results. The
# ``capability_missing`` entry is matched via prefix because PolicyGuard P4
# returns ``capability_missing: <WriteCapability value>`` with a dynamic suffix.
# Any new PolicyGuard rejection reason MUST be added here; the exhaustiveness
# test in test_state_serialization.py asserts coverage by scanning
# engine/policy_guard.py source for literal reason strings.
_OVERRIDE_FAILURE_REASONS: Mapping[str, str] = {
    "fail_safe_mode_active": "The system is paused for safety right now.",
    "adapter_not_registered": "The charger is not connected to the system.",
    "capability_check_failed": "The charger did not respond. Try again or check the connection.",
    "unknown_command_type": "The charger did not respond. Try again or check the connection.",
    "capability_missing": "Your charger does not support remote charging.",
    "battery_state_unavailable_for_safety_check": (
        "The system is currently checking battery status. Try again shortly."
    ),
    "battery_soc_at_or_below_reserve_floor": (
        "Battery is at the reserved level — charging will resume from grid only."
    ),
    "commanded_rate_exceeds_peak_limit": (
        "Charging rate would exceed your peak limit. Lower the limit or wait."
    ),
    "conservative_mode_blocks_load_increase": (
        "The system is in recovery mode. Try again shortly."
    ),
    "command_timeout": "The charger did not respond in time. Check that it is powered on.",
    "command_dispatch_failed": "The charger did not respond. Try again or check the connection.",
    "adapter_correlation_id_mismatch": "The charger response could not be verified. Try again.",
}

_OVERRIDE_FAILURE_FALLBACK = "Could not start charging. Try again."

_EV_CARD_BUTTON_LABELS: Mapping[OverrideRenderState, str] = {
    "idle": "Charge EV now",
    "optimistic": "Starting charging…",
    "confirmed": "Charging now",
    "fallback": "Could not start charging",
}

_EV_CARD_ARIA_LIVE: Mapping[OverrideRenderState, str] = {
    "idle": "",
    "optimistic": "Starting EV charging.",
    "confirmed": "EV charging started.",
    "fallback": "Could not start charging.",  # extended with reason at call site
}

_EV_CARD_CONSTRAINT_NOTICE = (
    "Peak limit still active — charge rate may be adjusted if household load is high."
)


def _resolve_failure_reason_plain(reason: str | None) -> str:
    """Story 10.2 AC12: map PolicyGuard reason string → plain-language UX text."""
    if reason is None:
        return ""
    direct = _OVERRIDE_FAILURE_REASONS.get(reason)
    if direct is not None:
        return direct
    if reason.startswith("capability_missing:"):
        return _OVERRIDE_FAILURE_REASONS["capability_missing"]
    return _OVERRIDE_FAILURE_FALLBACK


def _serialize_homeowner_ev_override_state(
    override: EVOverrideState | None,
) -> dict[str, object] | None:
    if override is None:
        return None
    return {
        "correlation_id": str(override.correlation_id),
        "requested_at": _datetime_to_iso(override.requested_at),
        "expires_at": _datetime_to_iso(override.expires_at),
        "dispatch_status": override.dispatch_status,
        "failure_reason": override.failure_reason,
        "session_observed_active": override.session_observed_active,
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
        "active_ev_override": _serialize_homeowner_ev_override_state(snapshot.active_ev_override),
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
        "active_ev_override": _serialize_homeowner_ev_override_state(snapshot.active_ev_override),
    }


def build_homeowner_ev_card_context(
    snapshot: SystemSnapshot,
    active_constraints: ActiveConstraints | None,
    *,
    confirmation_timeout_seconds: int | None = None,
) -> dict[str, object]:
    """Build the homeowner EV-card fragment context (Story 10.2 AC11).

    Reads ONLY ``snapshot.active_ev_override`` and ``snapshot.ev_charger`` for
    ``override_state`` derivation. Does NOT consult ``operating_mode``,
    ``component_states``, or ``data_age_seconds`` for the state machine. The
    ``constraint_notice`` MAY consult ``operating_mode`` for content but does
    not influence ``override_state``. A structural test enforces the
    device-state independence invariant.

    ``confirmation_timeout_seconds`` is the UI-side budget after which the
    Alpine optimistic layer flips to Fallback if the next HTMX poll has not
    yet observed ``session_active=True``. It surfaces as a ``data-*`` hint on
    the EV section so the client factory can pick it up without a second
    server round-trip. ``None`` disables the client-side timer.
    """
    ev = snapshot.ev_charger
    override = snapshot.active_ev_override
    unavailable = ev is None or isinstance(ev, DegradedDeviceState)

    if unavailable:
        return {
            "card": "ev",
            "title": "EV charger",
            "override_state": "idle",
            "button_label": _EV_CARD_BUTTON_LABELS["idle"],
            "button_disabled": True,
            "aria_live_message": "",
            "constraint_notice": "",
            "retry_visible": False,
            "failure_reason_plain": "",
            "next_session_summary": "",
            "unavailable": True,
            "active_ev_override": None,
            "confirmation_timeout_seconds": confirmation_timeout_seconds,
        }

    override_state: OverrideRenderState
    if override is None:
        override_state = "idle"
    elif override.dispatch_status in ("failed", "timeout", "rejected"):
        override_state = "fallback"
    elif isinstance(ev, EVChargerState) and ev.session_active:
        override_state = "confirmed"
    else:
        override_state = "optimistic"

    failure_reason_plain = (
        _resolve_failure_reason_plain(override.failure_reason)
        if (override is not None and override_state == "fallback")
        else ""
    )

    aria_live = _EV_CARD_ARIA_LIVE[override_state]
    if override_state == "fallback" and failure_reason_plain:
        aria_live = f"Could not start charging — {failure_reason_plain}"

    constraint_notice = (
        _EV_CARD_CONSTRAINT_NOTICE
        if override_state == "confirmed"
        and snapshot.operating_mode is not SystemOperatingMode.fail_safe
        else ""
    )

    next_session_summary = (
        _format_next_session_summary(active_constraints) if override_state == "idle" else ""
    )

    return {
        "card": "ev",
        "title": "EV charger",
        "override_state": override_state,
        "button_label": _EV_CARD_BUTTON_LABELS[override_state],
        "button_disabled": override_state != "idle",
        "aria_live_message": aria_live,
        "constraint_notice": constraint_notice,
        "retry_visible": override_state == "fallback",
        "failure_reason_plain": failure_reason_plain,
        "next_session_summary": next_session_summary,
        "unavailable": False,
        "active_ev_override": _serialize_homeowner_ev_override_state(override),
        "confirmation_timeout_seconds": confirmation_timeout_seconds,
    }


def _format_next_session_summary(constraints: ActiveConstraints | None) -> str:
    """AC11 next_session_summary — derived from active EV charging window.

    UX spec line 836 example: ``"Charging tonight at 22:30"``. When no window
    is configured: ``"No charging session scheduled"``.
    """
    if constraints is None:
        return "No charging session scheduled"
    start = getattr(constraints, "ev_charging_window_start", None)
    if start is None:
        return "No charging session scheduled"
    # ``start`` is stored as an HH:MM string per the constraints schema.
    if isinstance(start, str) and ":" in start:
        return f"Charging tonight at {start}"
    return "Charging window active"


def build_homeowner_headline_context(snapshot: SystemSnapshot) -> dict[str, object]:
    """Build the homeowner status-headline template context (AC5 / AC14).

    Reads ONLY ``snapshot.operating_mode`` and ``snapshot.active_strategy``;
    never inspects device slots, component_states, or data_age_seconds. This
    is the structural enforcement of Epic 10's cross-story constraint that
    the headline is derived exclusively from SystemOperatingMode + active
    strategy — verified by test_state_serialization::test_headline_ignores_device_state.

    Story 10.3 AC5: emits ``strategy_options`` — one entry per ``EnergyStrategy``
    enum member in declaration order — for the inline selector panel. The
    labels are sourced from ``_STRATEGY_LABELS`` (single source of truth);
    each option carries an ``active`` boolean matching the current strategy.
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
    strategy_options: list[dict[str, object]] = [
        {
            "value": option.value,
            "label": _STRATEGY_LABELS.get(option, option.value.replace("_", " ").title()),
            "active": option is active_strategy,
        }
        for option in EnergyStrategy
    ]
    return {
        "operating_mode": operating_mode.value,
        "active_strategy": active_strategy.value,
        "presentation_mode": presentation_mode,
        "strategy_label": strategy_label,
        "headline_text": headline_text,
        "explanation_text": explanation_text,
        "strategy_options": strategy_options,
    }


def build_homeowner_weekly_summary_context(
    row: WeeklyEnergySummaryRow | None,
) -> dict[str, object]:
    """Story 10.4 AC8: build the homeowner weekly-summary fragment context.

    Pure-functional over ``WeeklyEnergySummaryRow | None``. The ``None`` branch
    handles the cold-start window before the aggregator's first pass runs.
    Both ``None`` and ``insufficient_history=True`` rows render the same
    "Data is still being collected" message — they are indistinguishable to the
    homeowner.
    """
    if row is None or row.insufficient_history:
        return {
            "insufficient_history": True,
            "data_complete_days_count": row.data_complete_days_count if row is not None else 0,
            "computed_at": row.computed_at.isoformat() if row is not None else None,
        }
    # row.self_consumption_ratio is non-None when insufficient_history is False
    # (enforced by WeeklyEnergySummaryRow._terminal_fields_iff_history_sufficient).
    assert row.self_consumption_ratio is not None
    assert row.estimated_cost_savings_eur is not None
    return {
        "insufficient_history": False,
        "peaks_avoided_count": row.peaks_avoided_count,
        "self_consumption_percent": round(row.self_consumption_ratio * 100),
        "estimated_cost_savings_eur": row.estimated_cost_savings_eur,
        "window_start_utc": row.window_start_utc.isoformat(),
        "window_end_utc": row.window_end_utc.isoformat(),
        "window_end_utc_date": row.window_end_utc.date().isoformat(),
        "computed_at": row.computed_at.isoformat(),
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


# ── Story 11.1 — installer dashboard fragment builders ───────────────────────


def build_installer_health_indicator_context(snapshot: SystemSnapshot) -> dict[str, object]:
    """AC3: derive the installer-facing system health display directly from
    ``snapshot.operating_mode`` via the four-to-three mapping.

    Pure-functional. Reads ONLY ``operating_mode``. Defensive ``.get(...)``
    fallback on the display table so a future SystemOperatingMode member that
    ships without an entry degrades gracefully instead of 500ing the route;
    the exhaustiveness test catches the missing entry at test time.
    """
    operating_mode = snapshot.operating_mode
    display = _INSTALLER_HEALTH_DISPLAY.get(operating_mode, operating_mode.value.upper())
    return {
        "operating_mode": operating_mode.value,
        "display": display,
        # CSS-class suffix: --normal / --degraded / --failed
        "css_modifier": display.lower(),
    }


def build_installer_device_row_context(
    snapshot: SystemSnapshot,
    registry_entries: list[DeviceRegistryEntry],
) -> dict[str, object]:
    """AC4: build one row per role with component_state + capability_badge as
    independently-derived fields.

    The two values are kept STRUCTURALLY DISTINCT — they live in different
    dict keys (``component_state_display`` and ``capability_badge_display``)
    so the template renders them as separate DOM elements. The "never
    conflated" invariant (epic AC line 2276-2279) is enforced both by the
    builder's output shape and by the template's class names.
    """
    # Build a role-keyed map of registry entries (one per role). For roles
    # with multiple entries (shouldn't happen in v1 single-device-per-role
    # contract; defensive), prefer the validated one.
    role_to_entry: dict[DeviceRole, DeviceRegistryEntry] = {}
    for entry in registry_entries:
        if entry.role is None:
            continue
        existing = role_to_entry.get(entry.role)
        if existing is None or (entry.validated and not existing.validated):
            role_to_entry[entry.role] = entry

    rows: list[dict[str, object]] = []
    for role in _INSTALLER_DEVICE_ROW_ORDER:
        component_state = snapshot.component_states.get(role, ComponentState.unavailable)
        component_state_display = _INSTALLER_COMPONENT_STATE_DISPLAY.get(
            component_state, "UNAVAILABLE"
        )

        row_entry = role_to_entry.get(role)
        if row_entry is not None and row_entry.last_capability_status is not None:
            capability_badge_display = _INSTALLER_CAPABILITY_BADGE_DISPLAY.get(
                row_entry.last_capability_status, "UNKNOWN"
            )
            device_id: str | None = row_entry.device_id
        else:
            capability_badge_display = "UNKNOWN"
            device_id = None

        age_seconds = snapshot.data_age_seconds.get(role)
        age_caption = _device_row_age_caption(age_seconds)

        rows.append(
            {
                "role": role.value,
                "role_label": _INSTALLER_DEVICE_ROLE_LABELS[role],
                "component_state_display": component_state_display,
                "component_state_css_modifier": component_state_display.lower(),
                "capability_badge_display": capability_badge_display,
                "capability_badge_css_modifier": capability_badge_display.lower(),
                "age_caption": age_caption,
                "device_id": device_id,
                # "View in event log" link target — emitted only when device_id
                # is known; AC4 last-paragraph contract.
                "event_log_link": (
                    f"/installer/event-log?device_id={device_id}" if device_id is not None else None
                ),
            }
        )

    return {"rows": rows}


def _device_row_age_caption(age_seconds: int | None) -> str:
    """Renders ``data_age_seconds[role]`` as a short caption under each device
    row. Reused homeowner-card pattern (state_serialization.py:_stale_caption)
    but with installer-tuned copy ("Updated" instead of "data last updated")."""
    if age_seconds is None:
        return "Updated long ago"
    if age_seconds < 60:
        return f"Updated {age_seconds}s ago"
    return f"Updated {age_seconds // 60} min ago"


def build_installer_peak_tracker_context(
    current_month_peak_kw: float,
    peak_limit_kw: float | None,
) -> dict[str, object]:
    """AC5: build the peak tracker context with magnitude + plain-language label.

    Branches:
    - no peak data yet (peak_kw == 0.0 AND no rows ever written) → "no data"
    - constraints unavailable (peak_limit_kw is None) → "not configured"
    - peak_kw / limit ratio < 0.9 → simple label
    - 0.9 <= ratio < 1.0 → "(approaching)" suffix
    - ratio >= 1.0 → "(exceeded)" suffix
    """
    peak_kw_rounded = round(current_month_peak_kw, 1)

    # Constraints unavailable branch — pre-installer-wizard-complete state.
    if peak_limit_kw is None:
        if peak_kw_rounded <= 0.0:
            return {
                "peak_kw_display": "0.0",
                "limit_display": None,
                "label": "Peak this month: 0.0 kW · No data yet",
                "status": "no_data",
            }
        return {
            "peak_kw_display": f"{peak_kw_rounded:.1f}",
            "limit_display": None,
            "label": f"Peak this month: {peak_kw_rounded:.1f} kW · Limit: not configured",
            "status": "not_configured",
        }

    limit_kw_rounded = round(peak_limit_kw, 1)

    # No peak data yet — peak_kw is exactly 0.0 because no peak_intervals rows
    # exist for the current UTC month (COALESCE-MAX returns 0.0 on empty set).
    if peak_kw_rounded <= 0.0:
        return {
            "peak_kw_display": "0.0",
            "limit_display": f"{limit_kw_rounded:.1f}",
            "label": "Peak this month: 0.0 kW · No data yet",
            "status": "no_data",
        }

    ratio = current_month_peak_kw / peak_limit_kw

    if ratio >= 1.0:
        status = "exceeded"
        suffix = " (exceeded)"
    elif ratio >= _PEAK_APPROACH_RATIO:
        status = "approaching"
        suffix = " (approaching)"
    else:
        status = "under_limit"
        suffix = ""

    return {
        "peak_kw_display": f"{peak_kw_rounded:.1f}",
        "limit_display": f"{limit_kw_rounded:.1f}",
        "label": (
            f"Peak this month: {peak_kw_rounded:.1f} kW · Limit: {limit_kw_rounded:.1f} kW{suffix}"
        ),
        "status": status,
    }


def build_installer_event_log_preview_context(
    entries: list[EventLogEntry],
) -> dict[str, object]:
    """AC6: build the 5-row event-log preview context.

    Each row carries UTC ISO-8601 in ``data_utc`` and a server-side
    locally-formatted display string in ``display_timestamp``. The summary is
    truncated to ``_EVENT_LOG_PREVIEW_SUMMARY_MAX`` chars with ellipsis.
    """
    rows: list[dict[str, object]] = []
    for entry in entries:
        # UTC ISO for client-side re-rendering (Story 11.2 will replace the
        # server-side fallback with Intl.DateTimeFormat).
        utc_iso = entry.timestamp.astimezone(UTC).isoformat()

        # Server-side fallback display string in Europe/Brussels. Uses
        # zoneinfo (stdlib) — no new dependency.
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415

            local_ts = entry.timestamp.astimezone(ZoneInfo(_EVENT_LOG_DISPLAY_TIMEZONE))
            offset = local_ts.utcoffset()
            offset_hours = int(offset.total_seconds() // 3600) if offset is not None else 0
            tz_abbrev = local_ts.tzname() or "UTC"
            display_timestamp = (
                f"{local_ts.strftime('%Y-%m-%d %H:%M')} {tz_abbrev} / UTC{offset_hours:+d}"
            )
        except Exception:  # noqa: BLE001 — zoneinfo data missing on stripped builds
            display_timestamp = f"{entry.timestamp.astimezone(UTC).strftime('%Y-%m-%d %H:%M')} UTC"

        summary = entry.summary
        if len(summary) > _EVENT_LOG_PREVIEW_SUMMARY_MAX:
            summary = summary[: _EVENT_LOG_PREVIEW_SUMMARY_MAX - 1] + "…"

        rows.append(
            {
                "id": entry.id,
                "utc_iso": utc_iso,
                "display_timestamp": display_timestamp,
                "event_type": entry.event_type,
                "event_type_css_modifier": entry.event_type.lower(),
                "summary": summary,
                "device_id": entry.device_id,
            }
        )

    return {
        "rows": rows,
        "view_all_url": "/installer/event-log",
        "is_empty": len(rows) == 0,
    }


def build_installer_anomaly_notice_context(
    snapshot: SystemSnapshot,
    dismissed: AnomalySignature | None,
) -> dict[str, object]:
    """AC7 + AC8: build the anomaly-notice context.

    Pure-functional over ``SystemSnapshot`` + dismissed signature. Calls
    ``detect_anomaly_from_snapshot`` and ``evaluate_dismiss_state``. The
    function signature encodes the "no I/O" structural contract.

    Returns:
        - ``{"render": False, ...}`` when the notice should NOT show
          (no anomaly OR dismissed signature covers current)
        - ``{"render": True, "severity": ..., "summary": ..., "link": ...}``
          when the full notice should display
    """
    current = detect_anomaly_from_snapshot(snapshot)
    decision = evaluate_dismiss_state(current, dismissed)
    if decision in ("no_anomaly", "suppress") or current is None:
        return {"render": False, "signature": None}
    return {
        "render": True,
        "signature": current,
        "severity": current.severity,
        "severity_css_modifier": current.severity.lower(),
        "summary": current.summary,
        "event_log_link": f"/installer/event-log{current.link_query}",
        "anomaly_types_sorted": sorted(current.anomaly_types),
    }
