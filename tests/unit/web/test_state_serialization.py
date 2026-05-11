from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from open_ems.core import (
    BatteryState,
    ComponentState,
    DegradedDeviceState,
    DeviceRole,
    EnergyStrategy,
    EVChargerState,
    GlobalState,
    GridMeterState,
    InverterState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.web.state_serialization import (
    build_homeowner_card_context,
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
