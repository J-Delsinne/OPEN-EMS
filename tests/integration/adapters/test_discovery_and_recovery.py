"""Integration tests: device degraded state and recovery across all domain adapters.

These tests exercise the full domain adapter stack (protocol adapter → domain adapter)
to verify that:
  1. Transient protocol failures produce DegradedDeviceState with reason="reconnecting"
  2. Domain adapters recover to normal state once the protocol layer is healthy again
  3. Non-transient reasons (e.g. "device_id_mismatch") are preserved unchanged
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from open_ems.adapters.discovery import DiscoveryService
from open_ems.adapters.dsmr.meter_adapter import GridMeterAdapter
from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.modbus.inverter_adapter import InverterAdapter
from open_ems.adapters.ocpp.central_system import (
    OCPPAdapterConfig,
    OCPPCentralSystem,
    _ChargerState,
)
from open_ems.adapters.ocpp.charger_adapter import EVChargerAdapter
from open_ems.adapters.protocol import (
    ProtocolDegradedState,
    RawDSMRState,
    RawModbusState,
    RawOCPPState,
)
from open_ems.core.devices import (
    BatteryState,
    CapabilityStatus,
    DegradedDeviceState,
    DeviceDiscoveryResult,
    DeviceRole,
    EVChargerState,
    GridMeterState,
    InverterState,
)

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake protocol adapters
# ---------------------------------------------------------------------------


class _FakeModbusProto:
    def __init__(self) -> None:
        self.next_state: RawModbusState | ProtocolDegradedState | None = None
        self.close_called = False

    async def get_raw_state(self) -> RawModbusState | ProtocolDegradedState:
        assert self.next_state is not None
        return self.next_state

    async def close(self) -> None:
        self.close_called = True


class _FakeDSMRProto:
    def __init__(self) -> None:
        self.next_state: RawDSMRState | ProtocolDegradedState | None = None

    async def get_raw_state(self) -> RawDSMRState | ProtocolDegradedState:
        assert self.next_state is not None
        return self.next_state

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _FakeOCPPProto:
    def __init__(self) -> None:
        self.next_state: RawOCPPState | ProtocolDegradedState | None = None

    async def get_raw_state(self) -> RawOCPPState | ProtocolDegradedState:
        assert self.next_state is not None
        return self.next_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INVERTER_REGISTERS: dict[int, int] = {40083: 5000, 40084: 4500, 40085: 1, 40110: 0}
_BATTERY_REGISTERS: dict[int, int] = {100: 500, 101: 150, 102: 2000, 104: 0, 105: 0}

_DSMR_FIELDS: dict[str, Any] = {
    "1-0:1.7.0": {"value": 0.5, "unit": "kW"},
    "1-0:2.7.0": {"value": 0.0, "unit": "kW"},
    "1-0:1.8.1": {"value": 100.0, "unit": "kWh"},
    "1-0:1.8.2": {"value": 50.0, "unit": "kWh"},
    "1-0:2.8.1": {"value": 20.0, "unit": "kWh"},
    "1-0:2.8.2": {"value": 10.0, "unit": "kWh"},
}


def _raw_modbus(device_id: str, registers: dict[int, int]) -> RawModbusState:
    return RawModbusState(device_id=device_id, registers=registers, read_at=_NOW)


def _protocol_degraded(device_id: str, reason: str) -> ProtocolDegradedState:
    return ProtocolDegradedState(device_id=device_id, reason=reason, occurred_at=_NOW)


def _raw_dsmr(device_id: str) -> RawDSMRState:
    return RawDSMRState(
        device_id=device_id,
        telegram_fields=_DSMR_FIELDS,
        received_at=datetime.now(UTC),
    )


def _raw_ocpp(device_id: str, cp_id: str = "cp-001") -> RawOCPPState:
    return RawOCPPState(
        device_id=device_id,
        charge_point_id=cp_id,
        connection_status="connected",
        last_status_notification={"type": "StatusNotification", "status": "Available"},
    )


# ---------------------------------------------------------------------------
# InverterAdapter: degraded → reconnecting, then recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inverter_modbus_timeout_produces_reconnecting() -> None:
    fake = _FakeModbusProto()
    adapter = InverterAdapter(
        device_id="inv-001",
        protocol_adapter=fake,  # type: ignore[arg-type]
        model="fronius_gen24_v1",
    )
    fake.next_state = _protocol_degraded("inv-001", "modbus_timeout")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.reason == "reconnecting"
    assert state.role == DeviceRole.inverter


@pytest.mark.asyncio
async def test_inverter_modbus_error_produces_reconnecting() -> None:
    fake = _FakeModbusProto()
    adapter = InverterAdapter(
        device_id="inv-001",
        protocol_adapter=fake,  # type: ignore[arg-type]
        model="fronius_gen24_v1",
    )
    fake.next_state = _protocol_degraded("inv-001", "modbus_error")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.reason == "reconnecting"


@pytest.mark.asyncio
async def test_inverter_recovery_after_reconnecting() -> None:
    fake = _FakeModbusProto()
    adapter = InverterAdapter(
        device_id="inv-001",
        protocol_adapter=fake,  # type: ignore[arg-type]
        model="fronius_gen24_v1",
    )
    # First call: degraded
    fake.next_state = _protocol_degraded("inv-001", "modbus_timeout")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.reason == "reconnecting"

    # Second call: recovered
    fake.next_state = _raw_modbus("inv-001", _INVERTER_REGISTERS)
    state = await adapter.get_state()
    assert isinstance(state, InverterState)


@pytest.mark.asyncio
async def test_inverter_device_id_mismatch_reason_preserved() -> None:
    fake = _FakeModbusProto()
    adapter = InverterAdapter(
        device_id="inv-001",
        protocol_adapter=fake,  # type: ignore[arg-type]
        model="fronius_gen24_v1",
    )
    fake.next_state = _protocol_degraded("inv-001", "device_id_mismatch")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.reason == "device_id_mismatch"


# ---------------------------------------------------------------------------
# BatteryAdapter: degraded → reconnecting, then recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_battery_modbus_timeout_produces_reconnecting() -> None:
    fake = _FakeModbusProto()
    adapter = BatteryAdapter(
        device_id="bat-001",
        protocol_adapter=fake,  # type: ignore[arg-type]
        model="byd_hvs_v1",
    )
    fake.next_state = _protocol_degraded("bat-001", "modbus_timeout")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.reason == "reconnecting"
    assert state.role == DeviceRole.battery


@pytest.mark.asyncio
async def test_battery_recovery_after_reconnecting() -> None:
    fake = _FakeModbusProto()
    adapter = BatteryAdapter(
        device_id="bat-001",
        protocol_adapter=fake,  # type: ignore[arg-type]
        model="byd_hvs_v1",
    )
    fake.next_state = _protocol_degraded("bat-001", "modbus_timeout")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)

    fake.next_state = _raw_modbus("bat-001", _BATTERY_REGISTERS)
    state = await adapter.get_state()
    assert isinstance(state, BatteryState)


# ---------------------------------------------------------------------------
# GridMeterAdapter: degraded → reconnecting, then recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meter_dsmr_unavailable_produces_reconnecting() -> None:
    fake = _FakeDSMRProto()
    adapter = GridMeterAdapter(device_id="meter-001", protocol_adapter=fake)  # type: ignore[arg-type]
    fake.next_state = _protocol_degraded("meter-001", "dsmr_unavailable")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.reason == "reconnecting"
    assert state.role == DeviceRole.grid_meter


@pytest.mark.asyncio
async def test_meter_dsmr_stale_produces_reconnecting() -> None:
    fake = _FakeDSMRProto()
    adapter = GridMeterAdapter(device_id="meter-001", protocol_adapter=fake)  # type: ignore[arg-type]
    fake.next_state = _protocol_degraded("meter-001", "dsmr_stale")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.reason == "reconnecting"


@pytest.mark.asyncio
async def test_meter_recovery_after_reconnecting() -> None:
    fake = _FakeDSMRProto()
    adapter = GridMeterAdapter(device_id="meter-001", protocol_adapter=fake)  # type: ignore[arg-type]
    fake.next_state = _protocol_degraded("meter-001", "dsmr_unavailable")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)

    fake.next_state = _raw_dsmr("meter-001")
    state = await adapter.get_state()
    assert isinstance(state, GridMeterState)


@pytest.mark.asyncio
async def test_meter_stale_data_from_freshness_check_produces_reconnecting() -> None:
    """GridMeterAdapter generates dsmr_stale internally when data is too old."""
    fake = _FakeDSMRProto()
    adapter = GridMeterAdapter(device_id="meter-001", protocol_adapter=fake)  # type: ignore[arg-type]
    stale_ts = datetime.now(UTC) - timedelta(seconds=61)
    fake.next_state = RawDSMRState(
        device_id="meter-001",
        telegram_fields=_DSMR_FIELDS,
        received_at=stale_ts,
    )
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.reason == "reconnecting"


# ---------------------------------------------------------------------------
# EVChargerAdapter: degraded → reconnecting, then recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_charger_ocpp_disconnected_produces_reconnecting() -> None:
    fake = _FakeOCPPProto()
    adapter = EVChargerAdapter(device_id="ev-001", protocol_adapter=fake)  # type: ignore[arg-type]
    fake.next_state = _protocol_degraded("ev-001", "ocpp_disconnected")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.reason == "reconnecting"
    assert state.role == DeviceRole.ev_charger


@pytest.mark.asyncio
async def test_charger_disconnected_status_produces_reconnecting() -> None:
    """EVChargerAdapter maps disconnected connection_status → reconnecting."""
    fake = _FakeOCPPProto()
    adapter = EVChargerAdapter(device_id="ev-001", protocol_adapter=fake)  # type: ignore[arg-type]
    fake.next_state = RawOCPPState(
        device_id="ev-001",
        charge_point_id="cp-001",
        connection_status="disconnected",
        last_status_notification=None,
    )
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.reason == "reconnecting"


@pytest.mark.asyncio
async def test_charger_recovery_after_reconnecting() -> None:
    fake = _FakeOCPPProto()
    adapter = EVChargerAdapter(device_id="ev-001", protocol_adapter=fake)  # type: ignore[arg-type]
    fake.next_state = _protocol_degraded("ev-001", "ocpp_disconnected")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)

    fake.next_state = _raw_ocpp("ev-001")
    state = await adapter.get_state()
    assert isinstance(state, EVChargerState)


@pytest.mark.asyncio
async def test_charger_ocpp_invalid_power_reason_preserved() -> None:
    """Non-reconnecting reasons must not be remapped."""
    fake = _FakeOCPPProto()
    adapter = EVChargerAdapter(device_id="ev-001", protocol_adapter=fake)  # type: ignore[arg-type]
    # Inject a raw state that will trigger ocpp_invalid_power from the adapter itself
    fake.next_state = RawOCPPState(
        device_id="ev-001",
        charge_point_id="cp-001",
        connection_status="connected",
        last_status_notification={"type": "StatusNotification", "status": "Charging"},
        last_meter_values_at=_NOW,
        last_meter_values_power_kw=-1.0,
    )
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.reason == "ocpp_invalid_power"


# ---------------------------------------------------------------------------
# OCPP BootNotification → DiscoveryService chain (AC1 test requirement)
# ---------------------------------------------------------------------------


def test_ocpp_boot_notification_discovery_chain() -> None:
    """BootNotification model captured via OCPPCentralSystem feeds register_ocpp_discovery.

    Chain: OCPPCentralSystem.register() → charger connects → BootNotification received
    (state updated) → caller extracts model → DiscoveryService.register_ocpp_discovery()
    → typed DeviceDiscoveryResult with correct capability classification.
    """
    cs = OCPPCentralSystem()
    config = OCPPAdapterConfig(device_id="ev-001", charge_point_id="cp-001")
    adapter = cs.register(config)

    # Simulate what OCPPCentralSystem.handle_charger() + on_boot_notification() do
    # when a charger connects and sends a BootNotification message.
    state = _ChargerState()
    state.connected = True
    state.last_status_notification = {
        "type": "BootNotification",
        "charge_point_model": "ocpp_1_6",
        "charge_point_vendor": "TestVendor",
    }
    adapter._state = state

    # Caller extracts model from BootNotification payload (as installer wizard would)
    sn = adapter._state.last_status_notification
    boot_model = (
        sn["charge_point_model"]
        if sn is not None and sn.get("type") == "BootNotification"
        else None
    )
    assert boot_model == "ocpp_1_6"

    # Register discovery result via DiscoveryService
    svc = DiscoveryService()
    result = svc.register_ocpp_discovery(
        device_id=config.device_id,
        address=config.charge_point_id,
        model=boot_model,
    )

    assert isinstance(result, DeviceDiscoveryResult)
    assert result.device_id == "ev-001"
    assert result.protocol == "ocpp_1_6"
    assert result.address == "cp-001"
    assert result.model == "ocpp_1_6"
    assert result.capability_status == CapabilityStatus.full
