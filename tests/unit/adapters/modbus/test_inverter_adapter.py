"""Unit tests for InverterAdapter."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import structlog.testing

from open_ems.adapters.modbus.inverter_adapter import InverterAdapter
from open_ems.adapters.protocol import ProtocolDegradedState, RawModbusState
from open_ems.core.devices import DegradedDeviceState, DeviceAdapter, DeviceRole, InverterState

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


def _raw(registers: dict[int, int], device_id: str = "inv-001") -> RawModbusState:
    return RawModbusState(device_id=device_id, registers=registers, read_at=_NOW)


def _degraded_protocol(
    device_id: str = "inv-001", reason: str = "modbus_timeout"
) -> ProtocolDegradedState:
    return ProtocolDegradedState(device_id=device_id, reason=reason, occurred_at=_NOW)


def _make_adapter(
    model: str, device_id: str = "inv-001"
) -> tuple[InverterAdapter, FakeProtocolAdapter]:
    fake = FakeProtocolAdapter()
    adapter = InverterAdapter(device_id=device_id, protocol_adapter=fake, model=model)  # type: ignore[arg-type]
    return adapter, fake


# ---------------------------------------------------------------------------
# Task 2 / AC5: Unsupported model raises ValueError at init
# ---------------------------------------------------------------------------


def test_inverter_adapter_unsupported_model_raises() -> None:
    fake = FakeProtocolAdapter()
    with pytest.raises(ValueError, match="Unsupported inverter model"):
        InverterAdapter(device_id="inv-001", protocol_adapter=fake, model="unknown_xyz")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Task 2 / AC7: isinstance(adapter, DeviceAdapter) is True
# ---------------------------------------------------------------------------


def test_inverter_adapter_satisfies_device_adapter_protocol() -> None:
    adapter, _ = _make_adapter("fronius_gen24_v1")
    assert isinstance(adapter, DeviceAdapter)


# ---------------------------------------------------------------------------
# Task 2 / Connection Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inverter_connect_is_noop() -> None:
    adapter, fake = _make_adapter("fronius_gen24_v1")
    # connect() must complete without errors and without touching the fake adapter
    await adapter.connect()
    assert not fake.close_called


@pytest.mark.asyncio
async def test_inverter_disconnect_calls_close() -> None:
    adapter, fake = _make_adapter("fronius_gen24_v1")
    await adapter.disconnect()
    assert fake.close_called


# ---------------------------------------------------------------------------
# AC1 / Task 5: Fronius Gen24 — successful get_state()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fronius_gen24_get_state_success() -> None:
    adapter, fake = _make_adapter("fronius_gen24_v1")
    # pv=5000W, ac=4500W, mode=1 (mppt), fault=0
    fake.next_state = _raw({40083: 5000, 40084: 4500, 40085: 1, 40110: 0})
    state = await adapter.get_state()
    assert isinstance(state, InverterState)
    assert state.device_id == "inv-001"
    assert state.pv_power_kw == pytest.approx(5.0)
    assert state.ac_power_kw == pytest.approx(4.5)
    assert state.operating_mode == "mppt"
    assert state.fault_code is None
    assert state.read_at == _NOW


@pytest.mark.asyncio
async def test_fronius_gen24_fault_code_returned() -> None:
    adapter, fake = _make_adapter("fronius_gen24_v1")
    fake.next_state = _raw({40083: 1000, 40084: 900, 40085: 3, 40110: 42})
    state = await adapter.get_state()
    assert isinstance(state, InverterState)
    assert state.fault_code == "42"
    assert state.operating_mode == "fault"


@pytest.mark.asyncio
async def test_fronius_gen24_optional_fault_register_absent_treated_as_no_fault() -> None:
    adapter, fake = _make_adapter("fronius_gen24_v1")
    # _REG_FAULT_CODE (40110) absent — should default to 0 = no fault
    fake.next_state = _raw({40083: 2000, 40084: 1800, 40085: 1})
    state = await adapter.get_state()
    assert isinstance(state, InverterState)
    assert state.fault_code is None


@pytest.mark.asyncio
async def test_fronius_gen24_large_pv_decoded_as_uint16() -> None:
    """PV register is uint16: value 65000 → 65.0 kW (valid, not degraded)."""
    adapter, fake = _make_adapter("fronius_gen24_v1")
    # uint16(65000) = 65000  →  pv_power_kw = 65.0 kW (large but valid)
    fake.next_state = _raw({40083: 65000, 40084: 4500, 40085: 1})
    state = await adapter.get_state()
    assert isinstance(state, InverterState)
    assert state.pv_power_kw == pytest.approx(65.0)


# ---------------------------------------------------------------------------
# AC1 / Task 5: Huawei SUN2000 — successful get_state()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_huawei_sun2000_get_state_success() -> None:
    adapter, fake = _make_adapter("huawei_sun2000_v3")
    fake.next_state = _raw({32064: 6000, 32080: 5500, 32089: 1, 32090: 0})
    state = await adapter.get_state()
    assert isinstance(state, InverterState)
    assert state.pv_power_kw == pytest.approx(6.0)
    assert state.ac_power_kw == pytest.approx(5.5)
    assert state.operating_mode == "grid_tied"
    assert state.fault_code is None


@pytest.mark.asyncio
async def test_huawei_sun2000_fault_code_returned() -> None:
    adapter, fake = _make_adapter("huawei_sun2000_v3")
    fake.next_state = _raw({32064: 0, 32080: 0, 32089: 4, 32090: 100})
    state = await adapter.get_state()
    assert isinstance(state, InverterState)
    assert state.fault_code == "100"
    assert state.operating_mode == "fault"


# ---------------------------------------------------------------------------
# AC1 / Task 5: Growatt Hybrid — successful get_state()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_growatt_hybrid_get_state_success() -> None:
    adapter, fake = _make_adapter("growatt_hybrid_v1")
    fake.next_state = _raw({3001: 3000, 3002: 2800, 3003: 1, 3004: 0})
    state = await adapter.get_state()
    assert isinstance(state, InverterState)
    assert state.pv_power_kw == pytest.approx(3.0)
    assert state.ac_power_kw == pytest.approx(2.8)
    assert state.operating_mode == "normal"
    assert state.fault_code is None


@pytest.mark.asyncio
async def test_growatt_hybrid_unknown_mode() -> None:
    adapter, fake = _make_adapter("growatt_hybrid_v1")
    fake.next_state = _raw({3001: 1000, 3002: 900, 3003: 99, 3004: 0})
    state = await adapter.get_state()
    assert isinstance(state, InverterState)
    assert state.operating_mode == "unknown"


# ---------------------------------------------------------------------------
# AC3 / Task 5: ProtocolDegradedState → DegradedDeviceState with correct role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_protocol_degraded_state_translates_to_domain_degraded() -> None:
    adapter, fake = _make_adapter("fronius_gen24_v1")
    fake.next_state = _degraded_protocol(reason="modbus_timeout")
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.device_id == "inv-001"
    assert state.role == DeviceRole.inverter
    assert state.reason == "reconnecting"
    assert state.occurred_at == _NOW


# ---------------------------------------------------------------------------
# AC8 / Task 5: Missing required register → DegradedDeviceState with address in reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_required_register_returns_degraded_with_address() -> None:
    adapter, fake = _make_adapter("fronius_gen24_v1")
    # Missing _REG_PV_POWER_W (40083)
    fake.next_state = _raw({40084: 4500, 40085: 1})
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.role == DeviceRole.inverter
    assert "40083" in state.reason
    assert state.reason.startswith("missing_register:")


# ---------------------------------------------------------------------------
# AC3 / Task 5: structlog captures device_degraded event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structlog_device_degraded_event_emitted_on_protocol_degraded() -> None:
    adapter, fake = _make_adapter("fronius_gen24_v1")
    fake.next_state = _degraded_protocol(reason="modbus_timeout")
    with structlog.testing.capture_logs() as captured:
        await adapter.get_state()
    assert any(
        log.get("event") == "device_degraded"
        and log.get("component") == "adapters"
        and log.get("device_id") == adapter.device_id
        and log.get("role") == DeviceRole.inverter.value
        for log in captured
    ), f"Expected device_degraded log event, got: {captured}"


@pytest.mark.asyncio
async def test_structlog_device_degraded_event_emitted_on_missing_register() -> None:
    adapter, fake = _make_adapter("fronius_gen24_v1")
    fake.next_state = _raw({40084: 4500, 40085: 1})  # missing 40083
    with structlog.testing.capture_logs() as captured:
        await adapter.get_state()
    events = [log.get("event") for log in captured]
    assert "device_degraded" in events


# ---------------------------------------------------------------------------
# AC6 / Task 5: get_capabilities() returns stub DeviceCapabilityProfile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_capabilities_returns_stub_profile() -> None:
    adapter, _ = _make_adapter("fronius_gen24_v1", device_id="inv-42")
    caps = await adapter.get_capabilities()
    assert caps.device_id == "inv-42"
    assert caps.model == "fronius_gen24_v1"
    assert caps.capability_status == "full"
    assert caps.firmware_version is None


# ---------------------------------------------------------------------------
# AC1 / Task 5: Signed AC power via int16 conversion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fronius_gen24_negative_ac_power_accepted() -> None:
    """Negative AC power (export) is valid; int16 decoding must handle sign."""
    adapter, fake = _make_adapter("fronius_gen24_v1")
    # int16(65036) = 65036 - 65536 = -500 → -0.5 kW (exporting)
    fake.next_state = _raw({40083: 2000, 40084: 65036, 40085: 1})
    state = await adapter.get_state()
    assert isinstance(state, InverterState)
    assert state.ac_power_kw == pytest.approx(-0.5)
    assert state.pv_power_kw == pytest.approx(2.0)
