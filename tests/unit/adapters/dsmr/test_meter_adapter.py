"""Unit tests for GridMeterAdapter (Story 4.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog.testing

from open_ems.adapters.dsmr.meter_adapter import GridMeterAdapter, MissingDSMRFieldError
from open_ems.adapters.protocol import ProtocolDegradedState, RawDSMRState
from open_ems.core.devices import (
    DegradedDeviceState,
    DeviceAdapter,
    DeviceRole,
    GridMeterState,
)

_DEVICE_ID = "meter-001"


def _now() -> datetime:
    return datetime.now(UTC)


def _telegram(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "1-0:1.7.0": {"value": 0.500, "unit": "kW"},
        "1-0:2.7.0": {"value": 0.000, "unit": "kW"},
        "1-0:1.8.1": {"value": 100.0, "unit": "kWh"},
        "1-0:1.8.2": {"value": 50.0, "unit": "kWh"},
        "1-0:2.8.1": {"value": 20.0, "unit": "kWh"},
        "1-0:2.8.2": {"value": 10.0, "unit": "kWh"},
    }
    base.update(overrides)
    return base


def _raw_dsmr(
    fields: dict[str, Any] | None = None,
    received_at: datetime | None = None,
) -> RawDSMRState:
    return RawDSMRState(
        device_id=_DEVICE_ID,
        telegram_fields=fields if fields is not None else _telegram(),
        received_at=received_at if received_at is not None else _now(),
    )


class FakeDSMRProtocolAdapter:
    """Minimal fake that controls get_raw_state() and tracks start/stop calls."""

    def __init__(self) -> None:
        self.next_state: RawDSMRState | ProtocolDegradedState | None = None
        self.start_called: int = 0
        self.stop_called: int = 0

    async def get_raw_state(self) -> RawDSMRState | ProtocolDegradedState:
        assert self.next_state is not None
        return self.next_state

    async def start(self) -> None:
        self.start_called += 1

    async def stop(self) -> None:
        self.stop_called += 1


def _make_adapter(fake: FakeDSMRProtocolAdapter) -> GridMeterAdapter:
    return GridMeterAdapter(device_id=_DEVICE_ID, protocol_adapter=fake)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DeviceAdapter structural subtyping
# ---------------------------------------------------------------------------


def test_isinstance_device_adapter() -> None:
    fake = FakeDSMRProtocolAdapter()
    adapter = _make_adapter(fake)
    assert isinstance(adapter, DeviceAdapter)


# ---------------------------------------------------------------------------
# connect / disconnect delegate to protocol adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_calls_start() -> None:
    fake = FakeDSMRProtocolAdapter()
    adapter = _make_adapter(fake)
    await adapter.connect()
    assert fake.start_called == 1


@pytest.mark.asyncio
async def test_disconnect_calls_stop() -> None:
    fake = FakeDSMRProtocolAdapter()
    adapter = _make_adapter(fake)
    await adapter.disconnect()
    assert fake.stop_called == 1


# ---------------------------------------------------------------------------
# DSMR sign convention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_maps_to_positive_grid_power() -> None:
    fake = FakeDSMRProtocolAdapter()
    fake.next_state = _raw_dsmr(
        fields=_telegram(
            **{"1-0:1.7.0": {"value": 1.5, "unit": "kW"}, "1-0:2.7.0": {"value": 0.0, "unit": "kW"}}
        )
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, GridMeterState)
    assert result.grid_power_kw == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_delivery_maps_to_negative_grid_power() -> None:
    fake = FakeDSMRProtocolAdapter()
    fake.next_state = _raw_dsmr(
        fields=_telegram(
            **{"1-0:1.7.0": {"value": 0.0, "unit": "kW"}, "1-0:2.7.0": {"value": 2.3, "unit": "kW"}}
        )
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, GridMeterState)
    assert result.grid_power_kw == pytest.approx(-2.3)


@pytest.mark.asyncio
async def test_both_zero_grid_power_is_zero() -> None:
    fake = FakeDSMRProtocolAdapter()
    fake.next_state = _raw_dsmr(
        fields=_telegram(
            **{"1-0:1.7.0": {"value": 0.0, "unit": "kW"}, "1-0:2.7.0": {"value": 0.0, "unit": "kW"}}
        )
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, GridMeterState)
    assert result.grid_power_kw == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Energy cumulative fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_energy_delivered_is_tariff1_plus_tariff2() -> None:
    fake = FakeDSMRProtocolAdapter()
    fake.next_state = _raw_dsmr(
        fields=_telegram(
            **{
                "1-0:1.8.1": {"value": 100.0, "unit": "kWh"},
                "1-0:1.8.2": {"value": 50.0, "unit": "kWh"},
            }
        )
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, GridMeterState)
    assert result.energy_delivered_kwh == pytest.approx(150.0)


@pytest.mark.asyncio
async def test_energy_returned_is_tariff1_plus_tariff2() -> None:
    fake = FakeDSMRProtocolAdapter()
    fake.next_state = _raw_dsmr(
        fields=_telegram(
            **{
                "1-0:2.8.1": {"value": 20.0, "unit": "kWh"},
                "1-0:2.8.2": {"value": 10.0, "unit": "kWh"},
            }
        )
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, GridMeterState)
    assert result.energy_returned_kwh == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# DSMR 60-second staleness gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_data_returns_degraded() -> None:
    stale_ts = datetime.now(UTC) - timedelta(seconds=61)
    fake = FakeDSMRProtocolAdapter()
    fake.next_state = _raw_dsmr(received_at=stale_ts)
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, DegradedDeviceState)
    assert result.reason == "reconnecting"


@pytest.mark.asyncio
async def test_data_just_past_60s_is_stale() -> None:
    stale_ts = datetime.now(UTC) - timedelta(seconds=60, microseconds=1)
    fake = FakeDSMRProtocolAdapter()
    fake.next_state = _raw_dsmr(received_at=stale_ts)
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, DegradedDeviceState)
    assert result.reason == "reconnecting"


@pytest.mark.asyncio
async def test_data_at_59s_is_not_stale() -> None:
    fresh_ts = datetime.now(UTC) - timedelta(seconds=59)
    fake = FakeDSMRProtocolAdapter()
    fake.next_state = _raw_dsmr(received_at=fresh_ts)
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, GridMeterState)


# ---------------------------------------------------------------------------
# ProtocolDegradedState passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("proto_reason", ["dsmr_stale", "dsmr_unavailable"])
async def test_protocol_degraded_translates_to_domain_degraded(proto_reason: str) -> None:
    fake = FakeDSMRProtocolAdapter()
    fake.next_state = ProtocolDegradedState(
        device_id=_DEVICE_ID,
        reason=proto_reason,
        occurred_at=_now(),
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, DegradedDeviceState)
    assert result.role == DeviceRole.grid_meter
    assert result.reason == "reconnecting"


# ---------------------------------------------------------------------------
# Missing OBIS key → DegradedDeviceState
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_obis_key_returns_degraded() -> None:
    fields = _telegram()
    del fields["1-0:1.7.0"]
    fake = FakeDSMRProtocolAdapter()
    fake.next_state = _raw_dsmr(fields=fields)
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, DegradedDeviceState)
    assert "missing_dsmr_field:1-0:1.7.0" in result.reason


@pytest.mark.asyncio
async def test_null_obis_value_returns_degraded() -> None:
    fields = _telegram(**{"1-0:1.7.0": {"value": None, "unit": "kW"}})
    fake = FakeDSMRProtocolAdapter()
    fake.next_state = _raw_dsmr(fields=fields)
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, DegradedDeviceState)
    assert "missing_dsmr_field:1-0:1.7.0" in result.reason


# ---------------------------------------------------------------------------
# structlog device_degraded event captured on degraded path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structlog_device_degraded_emitted() -> None:
    fake = FakeDSMRProtocolAdapter()
    fake.next_state = ProtocolDegradedState(
        device_id=_DEVICE_ID,
        reason="dsmr_unavailable",
        occurred_at=_now(),
    )
    adapter = _make_adapter(fake)
    with structlog.testing.capture_logs() as logs:
        await adapter.get_state()

    assert len(logs) == 1
    log = logs[0]
    assert log["event"] == "device_degraded"
    assert log["component"] == "adapters"
    assert log["device_id"] == _DEVICE_ID
    assert log["role"] == DeviceRole.grid_meter.value
    assert log["reason"] == "reconnecting"


# ---------------------------------------------------------------------------
# MissingDSMRFieldError: unit test of the exception itself
# ---------------------------------------------------------------------------


def test_missing_dsmr_field_error_str() -> None:
    exc = MissingDSMRFieldError("1-0:1.7.0")
    assert exc.key == "1-0:1.7.0"
    assert str(exc) == "missing_dsmr_field:1-0:1.7.0"
