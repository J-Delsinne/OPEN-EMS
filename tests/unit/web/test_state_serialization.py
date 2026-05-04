from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from open_ems.core import (
    BatteryState,
    ComponentState,
    DegradedDeviceState,
    DeviceRole,
    EVChargerState,
    GlobalState,
    InverterState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.web.state_serialization import (
    build_homeowner_card_context,
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
