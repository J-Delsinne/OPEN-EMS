from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from open_ems.core import (
    BatteryState,
    ComponentState,
    DegradedDeviceState,
    DeviceRole,
    EnergyStrategy,
    EVChargerState,
    EVOverrideState,
    GlobalState,
    GridMeterState,
    InverterState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.core.constraints import ActiveConstraints
from open_ems.web.state_serialization import (
    _OVERRIDE_FAILURE_FALLBACK,
    _OVERRIDE_FAILURE_REASONS,
    _resolve_failure_reason_plain,
    build_homeowner_card_context,
    build_homeowner_ev_card_context,
    build_homeowner_headline_context,
    serialize_homeowner_snapshot,
    serialize_installer_snapshot,
)

_NOW_UTC = datetime(2026, 5, 3, 21, 30, 0, tzinfo=UTC)


def _snapshot() -> SystemSnapshot:
    return SystemSnapshot(
        sequence_id=12,
        captured_at=_NOW_UTC,
        global_state=GlobalState.degraded,
        operating_mode=SystemOperatingMode.degraded,
        active_strategy=EnergyStrategy.maximize_self_consumption,
        inverter=InverterState(
            device_id="inv-001",
            pv_power_kw=3.2,
            ac_power_kw=3.0,
            operating_mode="normal",
            fault_code="installer-only-fault",
            read_at=_NOW_UTC,
        ),
        battery=BatteryState(
            device_id="bat-001",
            soc_percent=55.0,
            battery_power_kw=-1.5,
            capacity_kwh=10.0,
            operating_mode="self-use",
            read_at=_NOW_UTC,
        ),
        ev_charger=EVChargerState(
            device_id="ev-001",
            status="charging",
            session_active=True,
            current_power_kw=7.0,
            power_source="meter_values",
            power_measured_at=_NOW_UTC,
            read_at=_NOW_UTC,
        ),
        grid_meter=DegradedDeviceState(
            device_id="grid-001",
            role=DeviceRole.grid_meter,
            reason="validation_error:raw protocol stack trace",
            occurred_at=_NOW_UTC,
        ),
        component_states={
            DeviceRole.inverter: ComponentState.active,
            DeviceRole.battery: ComponentState.active,
            DeviceRole.ev_charger: ComponentState.active,
            DeviceRole.grid_meter: ComponentState.error,
        },
        data_age_seconds={
            DeviceRole.inverter: 0,
            DeviceRole.battery: 1,
            DeviceRole.ev_charger: 2,
            DeviceRole.grid_meter: 3,
        },
        system_clock_status="valid",
    )


def _keys_recursive(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(str(key))
            keys.update(_keys_recursive(nested))
    elif isinstance(value, list):
        for nested in value:
            keys.update(_keys_recursive(nested))
    return keys


def test_homeowner_snapshot_is_allowlisted_and_has_no_reason_key() -> None:
    payload = serialize_homeowner_snapshot(_snapshot())

    assert payload["sequence_id"] == 12
    assert payload["captured_at"] == "2026-05-03T21:30:00+00:00"
    assert payload["global_state"] == "DEGRADED"
    assert payload["operating_mode"] == "degraded"
    assert payload["system_clock_status"] == "valid"
    assert payload["battery"] == {
        "soc_percent": 55.0,
        "battery_power_kw": -1.5,
        "capacity_kwh": 10.0,
        "operating_mode": "self-use",
    }
    assert payload["grid_meter"] == {"state": "unavailable"}
    assert payload["components"]["grid_meter"] == "ERROR"
    assert "reason" not in _keys_recursive(payload)
    assert "device_id" not in _keys_recursive(payload)
    assert "fault_code" not in _keys_recursive(payload)
    assert "session_id" not in _keys_recursive(payload)


def test_installer_snapshot_includes_diagnostics_and_component_flags() -> None:
    payload = serialize_installer_snapshot(_snapshot())

    assert payload["component_states"]["grid_meter"] == "ERROR"
    assert payload["data_age_seconds"]["grid_meter"] == 3
    assert payload["grid_meter"] == {
        "state": "degraded",
        "device_id": "grid-001",
        "role": "grid_meter",
        "reason": "validation_error:raw protocol stack trace",
        "occurred_at": "2026-05-03T21:30:00+00:00",
    }
    assert payload["inverter"]["device_id"] == "inv-001"
    assert payload["inverter"]["fault_code"] == "installer-only-fault"


def test_serialized_payloads_are_plain_json_values() -> None:
    homeowner = serialize_homeowner_snapshot(_snapshot())
    installer = serialize_installer_snapshot(_snapshot())

    encoded_homeowner = json.dumps(homeowner)
    encoded_installer = json.dumps(installer)

    assert json.loads(encoded_homeowner)["components"]["inverter"] == "ACTIVE"
    assert json.loads(encoded_installer)["operating_mode"] == "degraded"


def test_homeowner_degraded_optional_device_remains_generic_without_reason() -> None:
    snapshot = _snapshot().model_copy(
        update={
            "battery": DegradedDeviceState(
                device_id="bat-001",
                role=DeviceRole.battery,
                reason="future-debug-detail",
                occurred_at=_NOW_UTC,
            )
        }
    )

    payload: dict[str, Any] = serialize_homeowner_snapshot(snapshot)

    assert payload["battery"] == {"state": "unavailable"}
    assert "reason" not in _keys_recursive(payload)


def test_homeowner_stale_payload_retains_public_values_and_age_metadata() -> None:
    snapshot = _snapshot().model_copy(
        update={
            "component_states": {
                DeviceRole.inverter: ComponentState.active,
                DeviceRole.battery: ComponentState.stale,
                DeviceRole.ev_charger: ComponentState.active,
                DeviceRole.grid_meter: ComponentState.error,
            },
            "data_age_seconds": {
                DeviceRole.inverter: 0,
                DeviceRole.battery: 185,
                DeviceRole.ev_charger: 2,
                DeviceRole.grid_meter: 3,
            },
        }
    )

    payload = serialize_homeowner_snapshot(snapshot)
    card = build_homeowner_card_context(snapshot, "battery")

    assert payload["battery"]["soc_percent"] == 55.0  # type: ignore[index]
    assert payload["components"]["battery"] == "STALE"  # type: ignore[index]
    assert payload["data_age_seconds"]["battery"] == 185  # type: ignore[index]
    assert card["stale_caption"] == "Battery data last updated 3 min ago"
    assert card["system_clock_status"] == "valid"


def test_homeowner_card_context_renders_unavailable_without_zero_fallbacks() -> None:
    snapshot = _snapshot().model_copy(
        update={
            "battery": None,
            "component_states": {
                DeviceRole.inverter: ComponentState.active,
                DeviceRole.battery: ComponentState.unavailable,
                DeviceRole.ev_charger: ComponentState.active,
                DeviceRole.grid_meter: ComponentState.error,
            },
            "data_age_seconds": {
                DeviceRole.inverter: 0,
                DeviceRole.battery: None,
                DeviceRole.ev_charger: 2,
                DeviceRole.grid_meter: 3,
            },
        }
    )

    card = build_homeowner_card_context(snapshot, "battery")

    assert card["unavailable"] is True
    assert card["rows"] == []
    assert card["stale_caption"] == ""


# ---------------------------------------------------------------------------
# Story 10.1 — active_strategy serializer + headline builder (AC4, AC5, AC9, AC14)
# ---------------------------------------------------------------------------


def test_homeowner_snapshot_includes_active_strategy_as_json_string() -> None:
    """AC4: active_strategy is emitted as the enum's string value (JSON-safe)."""
    snapshot = _snapshot().model_copy(update={"active_strategy": EnergyStrategy.minimize_cost})
    payload = serialize_homeowner_snapshot(snapshot)
    assert payload["active_strategy"] == "minimize_cost"
    # JSON round-trip — the enum must serialize to a string, not an enum object.
    json.loads(json.dumps(payload))


def test_installer_snapshot_includes_active_strategy_as_json_string() -> None:
    """AC4: installer serializer also emits active_strategy."""
    snapshot = _snapshot().model_copy(update={"active_strategy": EnergyStrategy.prioritize_ev})
    payload = serialize_installer_snapshot(snapshot)
    assert payload["active_strategy"] == "prioritize_ev"


@pytest.mark.parametrize(
    ("strategy", "label"),
    [
        (EnergyStrategy.minimize_cost, "Minimize Cost"),
        (EnergyStrategy.maximize_self_consumption, "Maximize Self-Consumption"),
        (EnergyStrategy.prioritize_ev, "Prioritize EV"),
    ],
)
def test_headline_context_normal_mode_renders_strategy_label(
    strategy: EnergyStrategy, label: str
) -> None:
    """AC5: normal headline reads 'Your home is running on solar · <strategy>'."""
    snapshot = _snapshot().model_copy(
        update={
            "operating_mode": SystemOperatingMode.normal,
            "active_strategy": strategy,
        }
    )
    context = build_homeowner_headline_context(snapshot)
    assert context["presentation_mode"] == "normal"
    assert context["headline_text"] == f"Your home is running on solar · {label}"
    assert context["explanation_text"] == ""
    assert context["active_strategy"] == strategy.value
    assert context["strategy_label"] == label


@pytest.mark.parametrize(
    "operating_mode",
    [
        SystemOperatingMode.degraded,
        SystemOperatingMode.conservative,
        SystemOperatingMode.fail_safe,
    ],
)
def test_headline_context_degraded_modes_render_calm_explanation(
    operating_mode: SystemOperatingMode,
) -> None:
    """AC5 / AC7: all three non-normal modes render the calm degraded headline."""
    snapshot = _snapshot().model_copy(update={"operating_mode": operating_mode})
    context = build_homeowner_headline_context(snapshot)
    assert context["presentation_mode"] == "degraded"
    assert context["headline_text"] == "Running with limited functionality"
    assert isinstance(context["explanation_text"], str)
    assert len(context["explanation_text"]) > 0
    # Calm language — no alarming phrasing.
    explanation = context["explanation_text"].lower()
    assert "error" not in explanation
    assert "fail" not in explanation
    assert "!" not in explanation


def test_headline_ignores_device_state_per_ac14() -> None:
    """AC14: headline derives ONLY from operating_mode + active_strategy.

    Constructs two snapshots that differ exclusively in device-state fields
    (slots, component_states, data_age_seconds) and asserts the headline
    context is identical. This is the structural enforcement of Epic 10's
    cross-story constraint: 'Status headline is derived exclusively from
    SystemOperatingMode + active strategy — no component independently
    infers degraded state from device values' (epics.md:2238).
    """
    base = _snapshot().model_copy(
        update={
            "operating_mode": SystemOperatingMode.normal,
            "active_strategy": EnergyStrategy.minimize_cost,
        }
    )
    variant = base.model_copy(
        update={
            # Drastically different device-state fields.
            "battery": None,
            "ev_charger": DegradedDeviceState(
                device_id="ev-001",
                role=DeviceRole.ev_charger,
                reason="ocpp_charger_not_connected",
                occurred_at=_NOW_UTC,
            ),
            "component_states": {
                DeviceRole.inverter: ComponentState.unavailable,
                DeviceRole.battery: ComponentState.unavailable,
                DeviceRole.ev_charger: ComponentState.error,
                DeviceRole.grid_meter: ComponentState.stale,
            },
            "data_age_seconds": {
                DeviceRole.inverter: None,
                DeviceRole.battery: None,
                DeviceRole.ev_charger: 999,
                DeviceRole.grid_meter: 12345,
            },
        }
    )
    assert build_homeowner_headline_context(base) == build_homeowner_headline_context(variant)


# ---------------------------------------------------------------------------
# Story 10.3 AC5 — strategy_options list in headline context
# ---------------------------------------------------------------------------


def test_build_homeowner_headline_context_includes_strategy_options_in_enum_declaration_order() -> (
    None
):
    """AC5: strategy_options follows EnergyStrategy declaration order (NOT alphabetized).

    UX spec line 805 and the journey-flow ordering rely on the declaration order:
    minimize_cost → maximize_self_consumption → prioritize_ev.
    """
    snapshot = _snapshot().model_copy(
        update={"active_strategy": EnergyStrategy.maximize_self_consumption}
    )
    context = build_homeowner_headline_context(snapshot)
    options = context["strategy_options"]
    assert isinstance(options, list)
    assert [opt["value"] for opt in options] == [
        "minimize_cost",
        "maximize_self_consumption",
        "prioritize_ev",
    ]


@pytest.mark.parametrize(
    "active",
    [
        EnergyStrategy.minimize_cost,
        EnergyStrategy.maximize_self_consumption,
        EnergyStrategy.prioritize_ev,
    ],
)
def test_build_homeowner_headline_context_marks_only_one_strategy_option_active(
    active: EnergyStrategy,
) -> None:
    """AC5: exactly one option has active=True, matching snapshot.active_strategy."""
    snapshot = _snapshot().model_copy(update={"active_strategy": active})
    context = build_homeowner_headline_context(snapshot)
    options = context["strategy_options"]
    active_flags = [opt["active"] for opt in options]
    assert active_flags.count(True) == 1
    assert active_flags.count(False) == 2
    active_option = next(opt for opt in options if opt["active"])
    assert active_option["value"] == active.value


def test_build_homeowner_headline_context_strategy_option_labels_match_strategy_labels_table() -> (
    None
):
    """AC5: option labels are sourced from _STRATEGY_LABELS (single source of truth).

    Closes the label-duplication anti-pattern structurally — if someone adds a
    hardcoded label string to the template OR the builder, this test fails.
    """
    from open_ems.web.state_serialization import _STRATEGY_LABELS

    snapshot = _snapshot()
    context = build_homeowner_headline_context(snapshot)
    for opt in context["strategy_options"]:
        enum_member = EnergyStrategy(opt["value"])
        assert opt["label"] == _STRATEGY_LABELS[enum_member]


def test_build_homeowner_headline_context_strategy_options_match_every_enum_member() -> None:
    """AC5: every EnergyStrategy member appears as an option (exhaustiveness).

    A future enum addition without updating the builder fails CI here.
    """
    snapshot = _snapshot()
    context = build_homeowner_headline_context(snapshot)
    option_values = {opt["value"] for opt in context["strategy_options"]}
    enum_values = {s.value for s in EnergyStrategy}
    assert option_values == enum_values


def test_grid_card_row_includes_direction_suffix_per_ac9() -> None:
    """AC9: grid card primary value is magnitude + plain-language direction."""
    base = _snapshot()

    # Importing — positive grid power.
    importing = base.model_copy(
        update={
            "grid_meter": GridMeterState(
                device_id="grid-001",
                grid_power_kw=1.2,
                energy_delivered_kwh=100.0,
                energy_returned_kwh=20.0,
                received_at=_NOW_UTC,
            ),
            "component_states": {
                DeviceRole.inverter: ComponentState.active,
                DeviceRole.battery: ComponentState.active,
                DeviceRole.ev_charger: ComponentState.active,
                DeviceRole.grid_meter: ComponentState.active,
            },
            "data_age_seconds": {
                DeviceRole.inverter: 0,
                DeviceRole.battery: 0,
                DeviceRole.ev_charger: 0,
                DeviceRole.grid_meter: 0,
            },
        }
    )
    context = build_homeowner_card_context(importing, "grid")
    assert context["rows"] == [{"label": "Grid power", "value": "1.2 kW importing"}]

    # Exporting — negative grid power.
    exporting = importing.model_copy(
        update={
            "grid_meter": GridMeterState(
                device_id="grid-001",
                grid_power_kw=-2.5,
                energy_delivered_kwh=100.0,
                energy_returned_kwh=20.0,
                received_at=_NOW_UTC,
            ),
        }
    )
    context = build_homeowner_card_context(exporting, "grid")
    assert context["rows"] == [{"label": "Grid power", "value": "2.5 kW exporting"}]

    # Idle — zero (rounded) grid power.
    idle = importing.model_copy(
        update={
            "grid_meter": GridMeterState(
                device_id="grid-001",
                grid_power_kw=0.04,  # rounds to 0.0
                energy_delivered_kwh=100.0,
                energy_returned_kwh=20.0,
                received_at=_NOW_UTC,
            ),
        }
    )
    context = build_homeowner_card_context(idle, "grid")
    assert context["rows"] == [{"label": "Grid power", "value": "0.0 kW idle"}]


def test_card_rows_are_single_primary_value_per_ac8() -> None:
    """AC8: each card returns a single-row primary value (not three rows)."""
    # Replace the base fixture's degraded grid_meter with a real reading; the
    # base snapshot uses DegradedDeviceState for grid_meter, which would route
    # the card through the "unavailable" branch.
    snapshot = _snapshot().model_copy(
        update={
            "grid_meter": GridMeterState(
                device_id="grid-001",
                grid_power_kw=1.2,
                energy_delivered_kwh=100.0,
                energy_returned_kwh=20.0,
                received_at=_NOW_UTC,
            ),
            "component_states": {
                DeviceRole.inverter: ComponentState.active,
                DeviceRole.battery: ComponentState.active,
                DeviceRole.ev_charger: ComponentState.active,
                DeviceRole.grid_meter: ComponentState.active,
            },
            "data_age_seconds": {
                DeviceRole.inverter: 0,
                DeviceRole.battery: 0,
                DeviceRole.ev_charger: 0,
                DeviceRole.grid_meter: 0,
            },
        }
    )
    for card_name in ("battery", "solar", "grid"):
        context = build_homeowner_card_context(snapshot, card_name)  # type: ignore[arg-type]
        assert isinstance(context["rows"], list)
        assert len(context["rows"]) == 1, f"card {card_name!r} must render a single primary row"


def test_strategy_label_table_covers_every_energy_strategy_member() -> None:
    """AC5: the label mapping is exhaustive — no EnergyStrategy member is unhandled."""
    from open_ems.web.state_serialization import _STRATEGY_LABELS

    assert set(_STRATEGY_LABELS.keys()) == set(EnergyStrategy)
    for label in _STRATEGY_LABELS.values():
        assert label and isinstance(label, str)


def test_degraded_explanations_cover_every_operating_mode() -> None:
    """AC5: the explanation mapping is exhaustive — every SystemOperatingMode is handled."""
    from open_ems.web.state_serialization import _DEGRADED_EXPLANATIONS

    assert set(_DEGRADED_EXPLANATIONS.keys()) == set(SystemOperatingMode)


# ---------------------------------------------------------------------------
# Story 10.2 — AC10/AC11/AC12 serializer + EV-card-context coverage
# ---------------------------------------------------------------------------


def _override(
    *,
    dispatch_status: str = "pending",
    failure_reason: str | None = None,
    session_observed_active: bool = False,
) -> EVOverrideState:
    return EVOverrideState(
        correlation_id=uuid.uuid4(),
        requested_at=_NOW_UTC,
        expires_at=_NOW_UTC + timedelta(hours=2),
        dispatch_status=dispatch_status,  # type: ignore[arg-type]
        failure_reason=failure_reason,
        session_observed_active=session_observed_active,
    )


def _idle_ev_charger() -> EVChargerState:
    return EVChargerState(
        device_id="ev-001",
        status="available",
        session_active=False,
        current_power_kw=0.0,
        power_source="meter_values",
        power_measured_at=_NOW_UTC,
        read_at=_NOW_UTC,
    )


_UNSET = object()


def _snapshot_with(
    *,
    override: EVOverrideState | None = None,
    ev_charger: Any = _UNSET,
    operating_mode: SystemOperatingMode = SystemOperatingMode.normal,
) -> SystemSnapshot:
    if ev_charger is _UNSET:
        ev_charger = _idle_ev_charger()
    return SystemSnapshot(
        sequence_id=42,
        captured_at=_NOW_UTC,
        global_state=GlobalState.normal,
        operating_mode=operating_mode,
        active_strategy=EnergyStrategy.minimize_cost,
        inverter=None,
        battery=None,
        ev_charger=ev_charger,
        grid_meter=None,
        component_states={
            DeviceRole.inverter: ComponentState.unavailable,
            DeviceRole.battery: ComponentState.unavailable,
            DeviceRole.ev_charger: ComponentState.active,
            DeviceRole.grid_meter: ComponentState.unavailable,
        },
        data_age_seconds=dict.fromkeys(DeviceRole),
        system_clock_status="valid",
        active_ev_override=override,
    )


def _active_constraints(
    *,
    ev_start: str | None = "22:30",
    ev_end: str | None = "06:00",
) -> ActiveConstraints:
    return ActiveConstraints(
        peak_limit_kw=5.0,
        battery_reserve_floor_percent=20.0,
        config_version=1,
        activated_at=_NOW_UTC,
        ev_charging_window_start=ev_start,
        ev_charging_window_end=ev_end,
    )


def test_homeowner_serializer_includes_active_ev_override_when_present() -> None:
    """Story 10.2 AC10: snapshot.active_ev_override flows into the homeowner SSE payload."""
    override = _override()
    snapshot = _snapshot_with(override=override)
    payload = serialize_homeowner_snapshot(snapshot)
    assert "active_ev_override" in payload
    assert payload["active_ev_override"] is not None
    assert isinstance(payload["active_ev_override"], dict)
    assert payload["active_ev_override"]["correlation_id"] == str(override.correlation_id)
    assert payload["active_ev_override"]["dispatch_status"] == "pending"


def test_homeowner_serializer_includes_active_ev_override_as_none_when_absent() -> None:
    """Story 10.2 AC10: absent override serializes as None (not omitted)."""
    payload = serialize_homeowner_snapshot(_snapshot_with(override=None))
    assert payload["active_ev_override"] is None


def test_installer_serializer_includes_active_ev_override() -> None:
    """Story 10.2 AC10: installer payload includes the same override shape."""
    override = _override()
    snapshot = _snapshot_with(override=override)
    payload = serialize_installer_snapshot(snapshot)
    assert payload["active_ev_override"] is not None
    assert isinstance(payload["active_ev_override"], dict)
    assert payload["active_ev_override"]["correlation_id"] == str(override.correlation_id)


def test_build_homeowner_ev_card_context_idle_when_no_override() -> None:
    """Story 10.2 AC11 — idle: no override + EV available."""
    ctx = build_homeowner_ev_card_context(_snapshot_with(override=None), _active_constraints())
    assert ctx["override_state"] == "idle"
    assert ctx["button_label"] == "Charge EV now"
    assert ctx["button_disabled"] is False
    assert ctx["retry_visible"] is False
    assert ctx["unavailable"] is False
    assert "22:30" in str(ctx["next_session_summary"])


def test_build_homeowner_ev_card_context_optimistic_when_pending_and_not_session_active() -> None:
    """Story 10.2 AC11 — optimistic: pending override + session_active=False."""
    snapshot = _snapshot_with(override=_override(), ev_charger=_idle_ev_charger())
    ctx = build_homeowner_ev_card_context(snapshot, _active_constraints())
    assert ctx["override_state"] == "optimistic"
    assert ctx["button_label"] == "Starting charging…"
    assert ctx["button_disabled"] is True
    assert ctx["retry_visible"] is False


def test_build_homeowner_ev_card_context_confirmed_when_pending_and_session_active() -> None:
    """Story 10.2 AC11 — confirmed: pending override + session_active=True. Critical AC."""
    ev_charging = EVChargerState(
        device_id="ev-001",
        status="charging",
        session_active=True,
        current_power_kw=7.0,
        power_source="meter_values",
        power_measured_at=_NOW_UTC,
        read_at=_NOW_UTC,
    )
    snapshot = _snapshot_with(override=_override(), ev_charger=ev_charging)
    ctx = build_homeowner_ev_card_context(snapshot, _active_constraints())
    assert ctx["override_state"] == "confirmed"
    assert ctx["button_label"] == "Charging now"
    assert ctx["button_disabled"] is True
    assert ctx["constraint_notice"]  # populated in confirmed state
    assert "EV charging started." in str(ctx["aria_live_message"])


def test_build_homeowner_ev_card_context_confirmed_omits_constraint_notice_in_fail_safe() -> None:
    """Story 10.2 AC11: peak-limit notice suppressed when operating_mode is fail_safe."""
    ev_charging = EVChargerState(
        device_id="ev-001",
        status="charging",
        session_active=True,
        current_power_kw=7.0,
        power_source="meter_values",
        power_measured_at=_NOW_UTC,
        read_at=_NOW_UTC,
    )
    snapshot = _snapshot_with(
        override=_override(),
        ev_charger=ev_charging,
        operating_mode=SystemOperatingMode.fail_safe,
    )
    ctx = build_homeowner_ev_card_context(snapshot, _active_constraints())
    assert ctx["override_state"] == "confirmed"
    assert ctx["constraint_notice"] == ""


@pytest.mark.parametrize(
    ("dispatch_status", "failure_reason", "expected_plain_substring"),
    [
        ("failed", "capability_check_failed", "did not respond"),
        ("timeout", "command_timeout", "did not respond in time"),
        ("rejected", "fail_safe_mode_active", "paused for safety"),
        ("rejected", "capability_missing: set_charge_rate", "does not support remote charging"),
    ],
)
def test_build_homeowner_ev_card_context_fallback_when_dispatch_status_terminal(
    dispatch_status: str, failure_reason: str, expected_plain_substring: str
) -> None:
    """Story 10.2 AC11/AC12: fallback state surfaces plain-language reason."""
    override = _override(dispatch_status=dispatch_status, failure_reason=failure_reason)
    snapshot = _snapshot_with(override=override)
    ctx = build_homeowner_ev_card_context(snapshot, _active_constraints())
    assert ctx["override_state"] == "fallback"
    assert ctx["button_label"] == "Could not start charging"
    assert ctx["retry_visible"] is True
    assert expected_plain_substring in str(ctx["failure_reason_plain"])


def test_build_homeowner_ev_card_context_unavailable_when_ev_charger_is_none() -> None:
    """Story 10.2 AC11 + UX spec line 1503."""
    ctx = build_homeowner_ev_card_context(_snapshot_with(ev_charger=None), _active_constraints())
    assert ctx["unavailable"] is True
    assert ctx["button_disabled"] is True


def test_build_homeowner_ev_card_context_unavailable_when_ev_charger_is_degraded() -> None:
    """Story 10.2 AC11 — DegradedDeviceState also routes to unavailable."""
    degraded = DegradedDeviceState(
        device_id="ev-001",
        role=DeviceRole.ev_charger,
        reason="ocpp_charger_not_connected",
        occurred_at=_NOW_UTC,
    )
    ctx = build_homeowner_ev_card_context(
        _snapshot_with(ev_charger=degraded), _active_constraints()
    )
    assert ctx["unavailable"] is True
    assert ctx["override_state"] == "idle"


def test_build_homeowner_ev_card_context_does_not_read_device_state_other_than_ev_charger() -> None:
    """Story 10.2 AC11 structural invariant: only ev_charger + active_ev_override drive state."""
    inverter = InverterState(
        device_id="inv-001",
        pv_power_kw=3.0,
        ac_power_kw=2.8,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )
    battery = BatteryState(
        device_id="bat-001",
        soc_percent=80.0,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="idle",
        read_at=_NOW_UTC,
    )
    snap_a = _snapshot_with(override=_override())
    snap_b = snap_a.model_copy(update={"inverter": inverter, "battery": battery})
    ctx_a = build_homeowner_ev_card_context(snap_a, _active_constraints())
    ctx_b = build_homeowner_ev_card_context(snap_b, _active_constraints())
    assert ctx_a["override_state"] == ctx_b["override_state"]
    assert ctx_a["button_label"] == ctx_b["button_label"]


def test_resolve_failure_reason_plain_handles_direct_hit() -> None:
    assert (
        _resolve_failure_reason_plain("fail_safe_mode_active")
        == _OVERRIDE_FAILURE_REASONS["fail_safe_mode_active"]
    )


def test_resolve_failure_reason_plain_handles_capability_missing_suffix() -> None:
    """Story 10.2 AC12 prefix-rule for P4 structured-suffix."""
    plain = _resolve_failure_reason_plain("capability_missing: set_charge_rate")
    assert plain == _OVERRIDE_FAILURE_REASONS["capability_missing"]


def test_resolve_failure_reason_plain_falls_back_for_unknown_reason() -> None:
    """Story 10.2 AC12: unknown reason returns calm catch-all."""
    plain = _resolve_failure_reason_plain("some_future_reason_string")
    assert plain == _OVERRIDE_FAILURE_FALLBACK


def test_resolve_failure_reason_plain_handles_none() -> None:
    assert _resolve_failure_reason_plain(None) == ""


def test_resolve_failure_reason_plain_covers_every_policy_guard_rejection_reason() -> None:
    """Story 10.2 AC18 #25 — exhaustiveness scan over engine/policy_guard.py.

    PolicyGuard's rejection-reason vocabulary is duplicated in
    _OVERRIDE_FAILURE_REASONS for plain-language UX. This test catches drift
    by scanning the source for every ``reason`` literal PolicyGuard can emit:
    (a) ``self._reject(command, "<reason>")`` calls,
    (b) inline ``CommandResult(..., reason="<reason>")`` constructions on
        the dispatch-timeout / dispatch-failed paths,
    (c) the ``capability_missing:<suffix>`` structured family (handled via
        the prefix rule in ``_resolve_failure_reason_plain``).
    """
    # Resolve the source path from the module's ``__file__`` rather than a
    # relative-to-cwd Path — pytest may be invoked from any working
    # directory. A relative Path that the runner cannot resolve would
    # silently extract an empty reason set and make this test vacuously pass.
    from open_ems.engine import policy_guard as _policy_guard_module

    policy_guard_path = Path(_policy_guard_module.__file__)
    text = policy_guard_path.read_text(encoding="utf-8")
    # (a) ``self._reject(command, "<reason>")`` literals.
    direct_reasons = set(re.findall(r'self\._reject\(\s*command\s*,\s*"([^"]+)"', text))
    # (b) ``reason="<reason>"`` literals on inline CommandResult constructions
    #     (covers the dispatch-timeout / dispatch-failed paths that do NOT go
    #     through ``self._reject``).
    inline_reasons = set(re.findall(r'reason="([^"]+)"', text))
    # (c) ``capability_missing:<suffix>`` structured family — prefix-matched
    #     by the UX resolver; strip the suffix and match the bare prefix.
    structured = {
        reason.split(":")[0] for reason in re.findall(r'f"(capability_missing:[^"]*)"', text)
    }
    covered = set(_OVERRIDE_FAILURE_REASONS.keys())
    # Defensive: assert the regex actually found something. A silent empty
    # extraction (e.g., if policy_guard.py is moved or restructured) would
    # otherwise make this test vacuously pass.
    assert direct_reasons or inline_reasons, (
        "Exhaustiveness scan found zero reason literals in policy_guard.py — "
        "the regex or the source layout has drifted."
    )
    missing = (direct_reasons | inline_reasons | structured) - covered
    assert not missing, f"PolicyGuard rejection reasons missing from UX map: {missing}"


# ── Story 10.4: build_homeowner_weekly_summary_context (AC8 + AC15 #34-#36) ──


def test_build_homeowner_weekly_summary_context_returns_insufficient_history_when_row_is_None() -> None:  # noqa: E501  # fmt: skip
    """AC15 #34: None row → insufficient-history branch with 0/7 days."""
    from open_ems.web.state_serialization import build_homeowner_weekly_summary_context

    ctx = build_homeowner_weekly_summary_context(None)
    assert ctx["insufficient_history"] is True
    assert ctx["data_complete_days_count"] == 0
    assert ctx["computed_at"] is None


def test_build_homeowner_weekly_summary_context_returns_insufficient_history_when_row_has_flag_set() -> None:  # noqa: E501  # fmt: skip
    """AC15 #34 (variant): row with insufficient_history=True maps to insufficient-history branch."""  # noqa: E501  # fmt: skip
    from datetime import UTC, datetime

    from open_ems.core.energy import WeeklyEnergySummaryRow
    from open_ems.web.state_serialization import build_homeowner_weekly_summary_context

    row = WeeklyEnergySummaryRow(
        window_start_utc=datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
        window_end_utc=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
        data_complete_days_count=4,
        insufficient_history=True,
        computed_at=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
    )
    ctx = build_homeowner_weekly_summary_context(row)
    assert ctx["insufficient_history"] is True
    assert ctx["data_complete_days_count"] == 4
    assert ctx["computed_at"] == "2026-05-12T00:00:00+00:00"


def test_build_homeowner_weekly_summary_context_converts_ratio_to_integer_percent_for_display() -> None:  # noqa: E501  # fmt: skip
    """AC15 #35: self_consumption_ratio (0.0-1.0) is converted to an integer percent."""
    from datetime import UTC, datetime

    from open_ems.core.energy import WeeklyEnergySummaryRow
    from open_ems.web.state_serialization import build_homeowner_weekly_summary_context

    row = WeeklyEnergySummaryRow(
        window_start_utc=datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
        window_end_utc=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
        peaks_avoided_count=3,
        self_consumption_ratio=0.725,
        estimated_cost_savings_eur=15.50,
        data_complete_days_count=7,
        insufficient_history=False,
        computed_at=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
    )
    ctx = build_homeowner_weekly_summary_context(row)
    assert ctx["insufficient_history"] is False
    # 0.725 × 100 = 72.5 → round() to 72 (banker's rounding in Python).
    assert ctx["self_consumption_percent"] == 72


def test_build_homeowner_weekly_summary_context_passes_eur_through_unrounded_for_template_format() -> None:  # noqa: E501  # fmt: skip
    """AC15 #36: estimated_cost_savings_eur is the raw float — the template formats it
    with %.2f|format."""
    from datetime import UTC, datetime

    from open_ems.core.energy import WeeklyEnergySummaryRow
    from open_ems.web.state_serialization import build_homeowner_weekly_summary_context

    row = WeeklyEnergySummaryRow(
        window_start_utc=datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
        window_end_utc=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
        peaks_avoided_count=3,
        self_consumption_ratio=0.5,
        estimated_cost_savings_eur=12.345,
        data_complete_days_count=7,
        insufficient_history=False,
        computed_at=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
    )
    ctx = build_homeowner_weekly_summary_context(row)
    assert ctx["estimated_cost_savings_eur"] == 12.345  # passed unrounded; template formats
    assert ctx["window_end_utc_date"] == "2026-05-12"
