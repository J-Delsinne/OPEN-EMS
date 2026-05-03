"""Unit tests for BatteryAdapter."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import structlog.testing

from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.protocol import ProtocolDegradedState, RawModbusState
from open_ems.core.devices import BatteryState, DegradedDeviceState, DeviceAdapter, DeviceRole

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fake protocol adapter
# ---------------------------------------------------------------------------


class FakeProtocolAdapter:
    """Minimal fake that controls get_raw_state() return value.
    Exposes only get_raw_state() and close() — no .config property.
    """

    def __init__(self) -> None:
        self.next_state: RawModbusState | ProtocolDegradedState | None = None
        self.close_called: bool = False

    async def get_raw_state(self) -> RawModbusState | ProtocolDegradedState:
        assert self.next_state is not None, "next_state must be set before calling get_raw_state()"
        return self.next_state

    async def close(self) -> None:
        self.close_called = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw(registers: dict[int, int], device_id: str = "bat-001") -> RawModbusState:
    return RawModbusState(device_id=device_id, registers=registers, read_at=_NOW)


def _degraded_protocol(device_id: str = "bat-001", reason: str = "modbus_timeout") -> ProtocolDegradedState:
    return ProtocolDegradedState(device_id=device_id, reason=reason, occurred_at=_NOW)


def _make_adapter(model: str, device_id: str = "bat-001") -> tuple[BatteryAdapter, FakeProtocolAdapter]:
    fake = FakeProtocolAdapter()
    adapter = BatteryAdapter(device_id=device_id, protocol_adapter=fake, model=model)  # type: ignore[arg-type]
    return adapter, fake


# BYD HVS registers
_HVS_REGS_CHARGE = {100: 500, 101: 150, 102: 2000, 104: 0, 105: 0}  # soc=50%, cap=15kWh, 2kW charge
_HVS_REGS_DISCHARGE = {100: 500, 101: 150, 102: 3000, 104: 1, 105: 0}  # soc=50%, 3kW discharge

# BYD HVM registers
_HVM_REGS_CHARGE = {200: 800, 201: 200, 202: 1500, 204: 0, 205: 0}  # soc=80%, cap=20kWh, 1.5kW charge
_HVM_REGS_DISCHARGE = {200: 800, 201: 200, 202: 1500, 204: 1, 205: 0}  # soc=80%, 1.5kW discharge


# ---------------------------------------------------------------------------
# AC5: Unsupported model raises ValueError at init
# ---------------------------------------------------------------------------


def test_battery_adapter_unsupported_model_raises() -> None:
    fake = FakeProtocolAdapter()
    with pytest.raises(ValueError, match="Unsupported battery model"):
        BatteryAdapter(device_id="bat-001", protocol_adapter=fake, model="unknown_xyz")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC7: isinstance(adapter, DeviceAdapter) is True
# ---------------------------------------------------------------------------


def test_battery_adapter_satisfies_device_adapter_protocol() -> None:
    adapter, _ = _make_adapter("byd_hvs_v1")
    assert isinstance(adapter, DeviceAdapter)


# ---------------------------------------------------------------------------
# Connection Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_battery_connect_is_noop() -> None:
    adapter, fake = _make_adapter("byd_hvs_v1")
    await adapter.connect()
    assert not fake.close_called


@pytest.mark.asyncio
async def test_battery_disconnect_calls_close() -> None:
    adapter, fake = _make_adapter("byd_hvs_v1")
    await adapter.disconnect()
    assert fake.close_called


# ---------------------------------------------------------------------------
# AC2: BYD HVS — successful get_state() with charging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_byd_hvs_get_state_charging() -> None:
    adapter, fake = _make_adapter("byd_hvs_v1")
    fake.next_state = _raw(_HVS_REGS_CHARGE)
    state = await adapter.get_state()
    assert isinstance(state, BatteryState)
    assert state.device_id == "bat-001"
    assert state.soc_percent == pytest.approx(50.0)
    assert state.capacity_kwh == pytest.approx(15.0)
    assert state.operating_mode == "normal"
    assert state.read_at == _NOW
    # Sign convention: charging → battery_power_kw > 0
    assert state.battery_power_kw == pytest.approx(2.0)
    assert state.battery_power_kw > 0, "Charging must give positive battery_power_kw"


@pytest.mark.asyncio
async def test_byd_hvs_get_state_discharging() -> None:
    adapter, fake = _make_adapter("byd_hvs_v1")
    fake.next_state = _raw(_HVS_REGS_DISCHARGE)
    state = await adapter.get_state()
    assert isinstance(state, BatteryState)
    # Sign convention: discharging → battery_power_kw < 0
    assert state.battery_power_kw == pytest.approx(-3.0)
    assert state.battery_power_kw < 0, "Discharging must give negative battery_power_kw"


# ---------------------------------------------------------------------------
# AC2: BYD HVM — successful get_state()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_byd_hvm_get_state_charging() -> None:
    adapter, fake = _make_adapter("byd_hvm_v1")
    fake.next_state = _raw(_HVM_REGS_CHARGE)
    state = await adapter.get_state()
    assert isinstance(state, BatteryState)
    assert state.soc_percent == pytest.approx(80.0)
    assert state.capacity_kwh == pytest.approx(20.0)
    assert state.battery_power_kw == pytest.approx(1.5)
    assert state.battery_power_kw > 0


@pytest.mark.asyncio
async def test_byd_hvm_get_state_discharging() -> None:
    adapter, fake = _make_adapter("byd_hvm_v1")
    fake.next_state = _raw(_HVM_REGS_DISCHARGE)
    state = await adapter.get_state()
    assert isinstance(state, BatteryState)
    assert state.battery_power_kw == pytest.approx(-1.5)
    assert state.battery_power_kw < 0


# ---------------------------------------------------------------------------
# AC2 / Battery sign convention: boundary cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_battery_zero_power_charge_direction_is_nonnegative() -> None:
    """Zero power with charge direction must give battery_power_kw = 0.0 (not negative)."""
    adapter, fake = _make_adapter("byd_hvs_v1")
    regs = {100: 500, 101: 150, 102: 0, 104: 0, 105: 0}  # 0 W, direction=charge
    fake.next_state = _raw(regs)
    state = await adapter.get_state()
    assert isinstance(state, BatteryState)
    assert state.battery_power_kw == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# AC2 / soc_percent boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_battery_soc_at_zero_accepted() -> None:
    adapter, fake = _make_adapter("byd_hvs_v1")
    regs = {100: 0, 101: 150, 102: 1000, 104: 0, 105: 0}  # soc=0%
    fake.next_state = _raw(regs)
    state = await adapter.get_state()
    assert isinstance(state, BatteryState)
    assert state.soc_percent == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_battery_soc_at_100_accepted() -> None:
    adapter, fake = _make_adapter("byd_hvs_v1")
    regs = {100: 1000, 101: 150, 102: 500, 104: 0, 105: 0}  # soc=100%
    fake.next_state = _raw(regs)
    state = await adapter.get_state()
    assert isinstance(state, BatteryState)
    assert state.soc_percent == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_battery_soc_out_of_range_gives_degraded() -> None:
    """SOC > 100% is physically invalid → Pydantic ValidationError → DegradedDeviceState."""
    adapter, fake = _make_adapter("byd_hvs_v1")
    # 1010 * 0.1 = 101% → exceeds SocPercent le=100.0 → ValidationError
    regs = {100: 1010, 101: 150, 102: 500, 104: 0, 105: 0}
    fake.next_state = _raw(regs)
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.role == DeviceRole.battery
    assert "validation_error" in state.reason


# ---------------------------------------------------------------------------
# AC3: ProtocolDegradedState → DegradedDeviceState with correct role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_protocol_degraded_state_translates_to_domain_degraded() -> None:
    adapter, fake = _make_adapter("byd_hvs_v1")
    fake.next_state = _degraded_protocol(reason="modbus_timeout")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.device_id == "bat-001"
    assert state.role == DeviceRole.battery
    assert state.reason == "modbus_timeout"
    assert state.occurred_at == _NOW


# ---------------------------------------------------------------------------
# AC8: Missing required register → DegradedDeviceState with address in reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_required_register_returns_degraded_with_address() -> None:
    adapter, fake = _make_adapter("byd_hvs_v1")
    # Missing _REG_SOC (100)
    fake.next_state = _raw({101: 150, 102: 1000, 104: 0, 105: 0})
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.role == DeviceRole.battery
    assert "100" in state.reason
    assert state.reason.startswith("missing_register:")


# ---------------------------------------------------------------------------
# AC3: structlog captures device_degraded event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structlog_device_degraded_emitted_on_protocol_degraded() -> None:
    adapter, fake = _make_adapter("byd_hvs_v1")
    fake.next_state = _degraded_protocol(reason="modbus_error")
    with structlog.testing.capture_logs() as captured:
        await adapter.get_state()
    assert any(
        log.get("event") == "device_degraded"
        and log.get("component") == "adapters"
        and log.get("device_id") == adapter.device_id
        and log.get("role") == DeviceRole.battery.value
        for log in captured
    ), f"Expected device_degraded log, got: {captured}"


@pytest.mark.asyncio
async def test_structlog_device_degraded_emitted_on_missing_register() -> None:
    adapter, fake = _make_adapter("byd_hvs_v1")
    fake.next_state = _raw({101: 150, 102: 1000, 104: 0, 105: 0})  # missing soc reg 100
    with structlog.testing.capture_logs() as captured:
        await adapter.get_state()
    events = [log.get("event") for log in captured]
    assert "device_degraded" in events


# ---------------------------------------------------------------------------
# AC6: get_capabilities() returns stub DeviceCapabilityProfile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_capabilities_returns_stub_profile() -> None:
    adapter, _ = _make_adapter("byd_hvs_v1", device_id="bat-42")
    caps = await adapter.get_capabilities()
    assert caps.device_id == "bat-42"
    assert caps.model == "byd_hvs_v1"
    assert caps.capability_status == "full"
    assert caps.firmware_version is None
