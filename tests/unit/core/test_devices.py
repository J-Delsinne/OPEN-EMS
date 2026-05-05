"""Tests for domain device state models and DeviceAdapter protocol (Story 4.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from open_ems.core import (
    BatteryState,
    CommandResult,
    DegradedDeviceState,
    DeviceAdapter,
    DeviceCapabilityProfile,
    DeviceCommand,
    DeviceRole,
    DeviceState,
    EVChargerState,
    GridMeterState,
    InverterState,
)

_NOW_UTC = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# AC4: DeviceRole
# ---------------------------------------------------------------------------


def test_device_role_has_exactly_four_members() -> None:
    members = set(DeviceRole)
    assert members == {
        DeviceRole.inverter,
        DeviceRole.battery,
        DeviceRole.ev_charger,
        DeviceRole.grid_meter,
    }


def test_device_role_values() -> None:
    assert DeviceRole.inverter.value == "inverter"
    assert DeviceRole.battery.value == "battery"
    assert DeviceRole.ev_charger.value == "ev_charger"
    assert DeviceRole.grid_meter.value == "grid_meter"


# ---------------------------------------------------------------------------
# AC1: DeviceAdapter protocol structural satisfaction
# ---------------------------------------------------------------------------


class _ConcreteAdapter:
    device_id = "test-device"

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_state(self) -> DeviceState | DegradedDeviceState:
        raise NotImplementedError

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        raise NotImplementedError

    async def send_command(self, cmd: DeviceCommand) -> CommandResult:
        raise NotImplementedError


class _IncompleteAdapter:
    """Missing get_state and get_capabilities."""

    device_id = "x"

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...


def test_device_adapter_structural_satisfaction() -> None:
    assert isinstance(_ConcreteAdapter(), DeviceAdapter)


def test_class_missing_methods_does_not_satisfy_protocol() -> None:
    assert not isinstance(_IncompleteAdapter(), DeviceAdapter)


# ---------------------------------------------------------------------------
# AC2: DeviceState subtypes — construction with valid values
# ---------------------------------------------------------------------------


def test_inverter_state_construction() -> None:
    state = InverterState(
        device_id="inv-001",
        pv_power_kw=5.0,
        ac_power_kw=4.8,
        operating_mode="normal",
        fault_code=None,
        read_at=_NOW_UTC,
    )
    assert state.device_id == "inv-001"
    assert state.pv_power_kw == 5.0
    assert state.fault_code is None


def test_battery_state_construction() -> None:
    state = BatteryState(
        device_id="bat-001",
        soc_percent=80.0,
        battery_power_kw=2.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )
    assert state.device_id == "bat-001"
    assert state.soc_percent == 80.0


def test_ev_charger_state_construction() -> None:
    state = EVChargerState(
        device_id="ev-001",
        status="charging",
        session_active=True,
        current_power_kw=7.4,
        read_at=_NOW_UTC,
    )
    assert state.device_id == "ev-001"
    assert state.status == "charging"
    assert state.current_power_kw == 7.4


def test_grid_meter_state_construction() -> None:
    state = GridMeterState(
        device_id="grid-001",
        grid_power_kw=3.5,
        energy_delivered_kwh=1000.0,
        energy_returned_kwh=200.0,
        received_at=_NOW_UTC,
    )
    assert state.device_id == "grid-001"
    assert state.grid_power_kw == 3.5


# ---------------------------------------------------------------------------
# AC5: Energy sign convention validators — pv_power_kw
# ---------------------------------------------------------------------------


def test_pv_power_kw_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        InverterState(
            device_id="inv-001",
            pv_power_kw=-0.1,
            ac_power_kw=0.0,
            operating_mode="normal",
            read_at=_NOW_UTC,
        )


def test_pv_power_kw_accepts_zero() -> None:
    state = InverterState(
        device_id="inv-001",
        pv_power_kw=0.0,
        ac_power_kw=0.0,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )
    assert state.pv_power_kw == 0.0


def test_pv_power_kw_accepts_positive() -> None:
    state = InverterState(
        device_id="inv-001",
        pv_power_kw=5.0,
        ac_power_kw=4.5,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )
    assert state.pv_power_kw == 5.0


# ---------------------------------------------------------------------------
# AC5: Energy sign convention validators — soc_percent
# ---------------------------------------------------------------------------


def test_soc_percent_rejects_below_zero() -> None:
    with pytest.raises(ValidationError):
        BatteryState(
            device_id="bat-001",
            soc_percent=-1.0,
            battery_power_kw=0.0,
            capacity_kwh=10.0,
            operating_mode="normal",
            read_at=_NOW_UTC,
        )


def test_soc_percent_rejects_above_100() -> None:
    with pytest.raises(ValidationError):
        BatteryState(
            device_id="bat-001",
            soc_percent=101.0,
            battery_power_kw=0.0,
            capacity_kwh=10.0,
            operating_mode="normal",
            read_at=_NOW_UTC,
        )


def test_soc_percent_accepts_zero() -> None:
    state = BatteryState(
        device_id="bat-001",
        soc_percent=0.0,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )
    assert state.soc_percent == 0.0


def test_soc_percent_accepts_100() -> None:
    state = BatteryState(
        device_id="bat-001",
        soc_percent=100.0,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )
    assert state.soc_percent == 100.0


# ---------------------------------------------------------------------------
# AC5: Energy sign convention validators — energy_delivered/returned_kwh
# ---------------------------------------------------------------------------


def test_energy_delivered_kwh_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        GridMeterState(
            device_id="grid-001",
            grid_power_kw=0.0,
            energy_delivered_kwh=-1.0,
            energy_returned_kwh=0.0,
            received_at=_NOW_UTC,
        )


def test_energy_returned_kwh_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        GridMeterState(
            device_id="grid-001",
            grid_power_kw=0.0,
            energy_delivered_kwh=0.0,
            energy_returned_kwh=-1.0,
            received_at=_NOW_UTC,
        )


def test_energy_kwh_accepts_zero() -> None:
    state = GridMeterState(
        device_id="grid-001",
        grid_power_kw=0.0,
        energy_delivered_kwh=0.0,
        energy_returned_kwh=0.0,
        received_at=_NOW_UTC,
    )
    assert state.energy_delivered_kwh == 0.0
    assert state.energy_returned_kwh == 0.0


def test_energy_kwh_accepts_positive() -> None:
    state = GridMeterState(
        device_id="grid-001",
        grid_power_kw=0.0,
        energy_delivered_kwh=100.0,
        energy_returned_kwh=50.0,
        received_at=_NOW_UTC,
    )
    assert state.energy_delivered_kwh == 100.0
    assert state.energy_returned_kwh == 50.0


# ---------------------------------------------------------------------------
# AC3 + AC5: UTC datetime validators — DegradedDeviceState
# ---------------------------------------------------------------------------


def test_degraded_device_state_rejects_naive_occurred_at() -> None:
    with pytest.raises(ValidationError):
        DegradedDeviceState(
            device_id="dev-001",
            role=DeviceRole.inverter,
            reason="communication failure",
            occurred_at=datetime(2024, 1, 1, 12, 0, 0),  # naive
        )


def test_degraded_device_state_rejects_non_utc_occurred_at() -> None:
    tz_plus2 = timezone(timedelta(hours=2))
    with pytest.raises(ValidationError):
        DegradedDeviceState(
            device_id="dev-001",
            role=DeviceRole.battery,
            reason="timeout",
            occurred_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=tz_plus2),
        )


def test_degraded_device_state_accepts_utc() -> None:
    state = DegradedDeviceState(
        device_id="dev-001",
        role=DeviceRole.ev_charger,
        reason="connection lost",
        occurred_at=_NOW_UTC,
    )
    assert state.device_id == "dev-001"
    assert state.role == DeviceRole.ev_charger


# ---------------------------------------------------------------------------
# AC5: UTC validators on all DeviceState subtypes
# ---------------------------------------------------------------------------


def test_inverter_state_rejects_naive_read_at() -> None:
    with pytest.raises(ValidationError):
        InverterState(
            device_id="inv-001",
            pv_power_kw=1.0,
            ac_power_kw=1.0,
            operating_mode="normal",
            read_at=datetime(2024, 1, 1),
        )


def test_battery_state_rejects_naive_read_at() -> None:
    with pytest.raises(ValidationError):
        BatteryState(
            device_id="bat-001",
            soc_percent=50.0,
            battery_power_kw=0.0,
            capacity_kwh=10.0,
            operating_mode="normal",
            read_at=datetime(2024, 1, 1),
        )


def test_ev_charger_state_rejects_naive_read_at() -> None:
    with pytest.raises(ValidationError):
        EVChargerState(
            device_id="ev-001",
            status="available",
            session_active=False,
            read_at=datetime(2024, 1, 1),
        )


def test_grid_meter_state_rejects_naive_received_at() -> None:
    with pytest.raises(ValidationError):
        GridMeterState(
            device_id="grid-001",
            grid_power_kw=0.0,
            energy_delivered_kwh=0.0,
            energy_returned_kwh=0.0,
            received_at=datetime(2024, 1, 1),
        )


# ---------------------------------------------------------------------------
# AC7: No raw protocol types importable from open_ems.core
# ---------------------------------------------------------------------------


def test_raw_protocol_types_not_in_core() -> None:
    import open_ems.core as core_module

    for forbidden in (
        "RawModbusState",
        "RawOCPPState",
        "RawDSMRState",
        "ProtocolDegradedState",
    ):
        assert not hasattr(core_module, forbidden), f"{forbidden} must not be in open_ems.core"


# ---------------------------------------------------------------------------
# DeviceDiscoveryResult (Story 4-5)
# ---------------------------------------------------------------------------


def test_device_discovery_result_requires_device_id_protocol_address() -> None:
    from open_ems.core import DeviceDiscoveryResult

    result = DeviceDiscoveryResult(
        device_id="inv-001",
        protocol="modbus_tcp",
        address="192.168.1.10:502",
    )
    assert result.device_id == "inv-001"
    assert result.protocol == "modbus_tcp"
    assert result.address == "192.168.1.10:502"


def test_device_discovery_result_default_model_is_none() -> None:
    from open_ems.core import DeviceDiscoveryResult

    result = DeviceDiscoveryResult(
        device_id="bat-001",
        protocol="modbus_tcp",
        address="10.0.0.1:502",
    )
    assert result.model is None


def test_device_discovery_result_default_capability_status_is_reduced() -> None:
    from open_ems.core.devices import CapabilityStatus, DeviceDiscoveryResult

    result = DeviceDiscoveryResult(
        device_id="bat-001",
        protocol="modbus_tcp",
        address="10.0.0.1:502",
    )
    assert result.capability_status == CapabilityStatus.reduced


def test_device_discovery_result_accepts_all_protocols() -> None:
    from open_ems.core import DeviceDiscoveryResult

    for protocol in ("modbus_tcp", "ocpp_1_6", "dsmr_p1"):
        result = DeviceDiscoveryResult(
            device_id="dev-001",
            protocol=protocol,  # type: ignore[arg-type]
            address="localhost",
        )
        assert result.protocol == protocol


def test_device_discovery_result_rejects_unknown_protocol() -> None:
    from pydantic import ValidationError

    from open_ems.core import DeviceDiscoveryResult

    with pytest.raises(ValidationError):
        DeviceDiscoveryResult(
            device_id="dev-001",
            protocol="unknown_protocol",  # type: ignore[arg-type]
            address="localhost",
        )


def test_device_discovery_result_rejects_empty_device_id() -> None:
    from open_ems.core import DeviceDiscoveryResult

    with pytest.raises(ValidationError):
        DeviceDiscoveryResult(
            device_id="",
            protocol="modbus_tcp",
            address="localhost",
        )


def test_device_discovery_result_rejects_empty_address() -> None:
    from open_ems.core import DeviceDiscoveryResult

    with pytest.raises(ValidationError):
        DeviceDiscoveryResult(
            device_id="dev-001",
            protocol="modbus_tcp",
            address="",
        )


def test_device_discovery_result_is_immutable() -> None:
    from open_ems.core import DeviceDiscoveryResult

    result = DeviceDiscoveryResult(
        device_id="dev-001",
        protocol="modbus_tcp",
        address="localhost",
    )
    with pytest.raises(ValidationError):
        result.device_id = "other"  # type: ignore[misc]


def test_device_discovery_result_rejects_extra_fields() -> None:
    from open_ems.core import DeviceDiscoveryResult

    with pytest.raises(ValidationError):
        DeviceDiscoveryResult(
            device_id="dev-001",
            protocol="modbus_tcp",
            address="localhost",
            extra_field="not_allowed",  # type: ignore[call-arg]
        )


def test_device_discovery_result_model_stored() -> None:
    from open_ems.core import DeviceDiscoveryResult
    from open_ems.core.devices import CapabilityStatus

    result = DeviceDiscoveryResult(
        device_id="inv-001",
        protocol="modbus_tcp",
        address="192.168.1.10:502",
        model="fronius_gen24_v1",
        capability_status=CapabilityStatus.full,
    )
    assert result.model == "fronius_gen24_v1"
    assert result.capability_status == CapabilityStatus.full


def test_device_discovery_result_rejects_empty_model() -> None:
    from pydantic import ValidationError

    from open_ems.core import DeviceDiscoveryResult

    with pytest.raises(ValidationError):
        DeviceDiscoveryResult(
            device_id="dev-001",
            protocol="modbus_tcp",
            address="localhost",
            model="",
        )
