"""Unit tests for adapter get_capabilities() integration (Story 4.4)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import structlog.testing

from open_ems.adapters.dsmr.meter_adapter import GridMeterAdapter
from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.modbus.inverter_adapter import InverterAdapter
from open_ems.adapters.ocpp.charger_adapter import EVChargerAdapter
from open_ems.core.devices import (
    CapabilityStatus,
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)

# ---------------------------------------------------------------------------
# Fake protocol adapters (minimal — get_capabilities() never calls get_raw_state)
# ---------------------------------------------------------------------------


class FakeModbusProtocolAdapter:
    async def get_raw_state(self) -> None:  # pragma: no cover
        raise AssertionError("should not be called in capability tests")

    async def close(self) -> None:
        pass


class FakeOCPPProtocolAdapter:
    async def get_raw_state(self) -> None:  # pragma: no cover
        raise AssertionError("should not be called in capability tests")


class FakeDSMRProtocolAdapter:
    async def get_raw_state(self) -> None:  # pragma: no cover
        raise AssertionError("should not be called in capability tests")

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# InverterAdapter.get_capabilities()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inverter_adapter_capabilities_fronius() -> None:
    adapter = InverterAdapter(
        device_id="inv-001",
        protocol_adapter=FakeModbusProtocolAdapter(),  # type: ignore[arg-type]
        model="fronius_gen24_v1",
    )
    profile = await adapter.get_capabilities()
    assert profile.capability_status == CapabilityStatus.full
    assert profile.device_id == "inv-001"
    assert profile.model == "fronius_gen24_v1"
    # Story 9.0 AC3: inverter is read-only in v1 — write_capabilities is empty.
    assert profile.write_capabilities == frozenset()
    assert WriteCapability.set_charge_rate not in profile.write_capabilities
    assert WriteCapability.set_discharge_rate not in profile.write_capabilities
    assert WriteCapability.set_operating_mode not in profile.write_capabilities


@pytest.mark.asyncio
async def test_inverter_adapter_capabilities_satisfies_device_adapter_profile_type() -> None:
    adapter = InverterAdapter(
        device_id="inv-001",
        protocol_adapter=FakeModbusProtocolAdapter(),  # type: ignore[arg-type]
        model="huawei_sun2000_v3",
    )
    profile = await adapter.get_capabilities()
    assert isinstance(profile, DeviceCapabilityProfile)


# ---------------------------------------------------------------------------
# BatteryAdapter.get_capabilities()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_battery_adapter_capabilities_byd_hvs() -> None:
    adapter = BatteryAdapter(
        device_id="bat-001",
        protocol_adapter=FakeModbusProtocolAdapter(),  # type: ignore[arg-type]
        model="byd_hvs_v1",
    )
    profile = await adapter.get_capabilities()
    assert profile.capability_status == CapabilityStatus.full
    assert profile.device_id == "bat-001"
    assert ReadCapability.soc in profile.read_capabilities
    assert WriteCapability.set_charge_rate in profile.write_capabilities
    assert WriteCapability.set_discharge_rate in profile.write_capabilities


@pytest.mark.asyncio
async def test_battery_adapter_capabilities_byd_hvm() -> None:
    adapter = BatteryAdapter(
        device_id="bat-002",
        protocol_adapter=FakeModbusProtocolAdapter(),  # type: ignore[arg-type]
        model="byd_hvm_v1",
    )
    profile = await adapter.get_capabilities()
    assert profile.capability_status == CapabilityStatus.full
    assert WriteCapability.set_charge_rate in profile.write_capabilities


# ---------------------------------------------------------------------------
# EVChargerAdapter.get_capabilities()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ev_charger_adapter_capabilities_ocpp() -> None:
    adapter = EVChargerAdapter(
        device_id="ev-001",
        protocol_adapter=FakeOCPPProtocolAdapter(),  # type: ignore[arg-type]
    )
    profile = await adapter.get_capabilities()
    assert profile.capability_status == CapabilityStatus.full
    assert profile.device_id == "ev-001"
    assert WriteCapability.set_ev_charge_current in profile.write_capabilities
    assert ReadCapability.state in profile.read_capabilities
    assert ReadCapability.power in profile.read_capabilities


# ---------------------------------------------------------------------------
# GridMeterAdapter.get_capabilities() — FR6b guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grid_meter_adapter_capabilities_read_only() -> None:
    adapter = GridMeterAdapter(
        device_id="meter-001",
        protocol_adapter=FakeDSMRProtocolAdapter(),  # type: ignore[arg-type]
    )
    profile = await adapter.get_capabilities()
    assert profile.capability_status == CapabilityStatus.full
    assert profile.device_id == "meter-001"
    assert profile.write_capabilities == frozenset()


@pytest.mark.asyncio
async def test_grid_meter_adapter_fr6b_no_write_caps() -> None:
    """FR6b: grid meter must never expose any write capability."""
    adapter = GridMeterAdapter(
        device_id="meter-001",
        protocol_adapter=FakeDSMRProtocolAdapter(),  # type: ignore[arg-type]
    )
    profile = await adapter.get_capabilities()
    assert WriteCapability.set_charge_rate not in profile.write_capabilities
    assert WriteCapability.set_ev_charge_current not in profile.write_capabilities
    assert WriteCapability.set_discharge_rate not in profile.write_capabilities
    assert WriteCapability.set_operating_mode not in profile.write_capabilities


# ---------------------------------------------------------------------------
# capability_profile_unknown log event emitted on REDUCED profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inverter_adapter_logs_capability_profile_unknown_when_reduced() -> None:
    """When get_profile returns a REDUCED profile, adapter must log the event."""
    adapter = InverterAdapter(
        device_id="inv-unknown",
        protocol_adapter=FakeModbusProtocolAdapter(),  # type: ignore[arg-type]
        model="fronius_gen24_v1",
    )
    reduced_profile = DeviceCapabilityProfile(
        device_id="inv-unknown",
        model="fronius_gen24_v1",
        capability_status=CapabilityStatus.reduced,
        limitation_reason="capability_profile_unknown: model=fronius_gen24_v1 firmware=None",
    )
    with patch(
        "open_ems.adapters.modbus.inverter_adapter.get_profile",
        return_value=reduced_profile,
    ):
        with structlog.testing.capture_logs() as cap:
            await adapter.get_capabilities()

    events = [e for e in cap if e.get("event") == "capability_profile_unknown"]
    assert len(events) == 1
    assert events[0]["component"] == "adapters"
    assert events[0]["device_id"] == "inv-unknown"
    assert events[0]["model"] == "fronius_gen24_v1"
    assert events[0]["firmware_version"] is None


@pytest.mark.asyncio
async def test_battery_adapter_logs_capability_profile_unknown_when_reduced() -> None:
    adapter = BatteryAdapter(
        device_id="bat-unknown",
        protocol_adapter=FakeModbusProtocolAdapter(),  # type: ignore[arg-type]
        model="byd_hvs_v1",
    )
    reduced_profile = DeviceCapabilityProfile(
        device_id="bat-unknown",
        model="byd_hvs_v1",
        capability_status=CapabilityStatus.reduced,
        limitation_reason="capability_profile_unknown: model=byd_hvs_v1 firmware=None",
    )
    with patch(
        "open_ems.adapters.modbus.battery_adapter.get_profile",
        return_value=reduced_profile,
    ):
        with structlog.testing.capture_logs() as cap:
            await adapter.get_capabilities()

    events = [e for e in cap if e.get("event") == "capability_profile_unknown"]
    assert len(events) == 1
    assert events[0]["component"] == "adapters"
    assert events[0]["model"] == "byd_hvs_v1"


@pytest.mark.asyncio
async def test_ev_charger_adapter_logs_capability_profile_unknown_when_reduced() -> None:
    adapter = EVChargerAdapter(
        device_id="ev-unknown",
        protocol_adapter=FakeOCPPProtocolAdapter(),  # type: ignore[arg-type]
    )
    reduced_profile = DeviceCapabilityProfile(
        device_id="ev-unknown",
        model="ocpp_1_6",
        capability_status=CapabilityStatus.reduced,
        limitation_reason="capability_profile_unknown: model=ocpp_1_6 firmware=None",
    )
    with patch(
        "open_ems.adapters.ocpp.charger_adapter.get_profile",
        return_value=reduced_profile,
    ):
        with structlog.testing.capture_logs() as cap:
            await adapter.get_capabilities()

    events = [e for e in cap if e.get("event") == "capability_profile_unknown"]
    assert len(events) == 1
    assert events[0]["component"] == "adapters"
    assert events[0]["device_id"] == "ev-unknown"


@pytest.mark.asyncio
async def test_grid_meter_adapter_logs_capability_profile_unknown_when_reduced() -> None:
    adapter = GridMeterAdapter(
        device_id="meter-unknown",
        protocol_adapter=FakeDSMRProtocolAdapter(),  # type: ignore[arg-type]
    )
    reduced_profile = DeviceCapabilityProfile(
        device_id="meter-unknown",
        model="dsmr_p1",
        capability_status=CapabilityStatus.reduced,
        limitation_reason="capability_profile_unknown: model=dsmr_p1 firmware=None",
    )
    with patch(
        "open_ems.adapters.dsmr.meter_adapter.get_profile",
        return_value=reduced_profile,
    ):
        with structlog.testing.capture_logs() as cap:
            await adapter.get_capabilities()

    events = [e for e in cap if e.get("event") == "capability_profile_unknown"]
    assert len(events) == 1
    assert events[0]["component"] == "adapters"
    assert events[0]["device_id"] == "meter-unknown"
    assert events[0]["model"] == "dsmr_p1"


@pytest.mark.asyncio
async def test_no_log_emitted_when_profile_is_full() -> None:
    """Full profiles must NOT emit capability_profile_unknown."""
    adapter = InverterAdapter(
        device_id="inv-001",
        protocol_adapter=FakeModbusProtocolAdapter(),  # type: ignore[arg-type]
        model="fronius_gen24_v1",
    )
    with structlog.testing.capture_logs() as cap:
        await adapter.get_capabilities()

    events = [e for e in cap if e.get("event") == "capability_profile_unknown"]
    assert len(events) == 0
