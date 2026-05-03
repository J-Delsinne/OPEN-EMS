"""Unit tests for EVChargerAdapter (Story 4.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import structlog.testing

from open_ems.adapters.ocpp.charger_adapter import EVChargerAdapter
from open_ems.adapters.protocol import ProtocolDegradedState, RawOCPPState
from open_ems.core.devices import (
    DegradedDeviceState,
    DeviceAdapter,
    DeviceRole,
    EVChargerState,
)

_NOW = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
_DEVICE_ID = "ev-001"
_CP_ID = "cp-001"


def _raw_state(**kwargs: object) -> RawOCPPState:
    defaults: dict[str, object] = {
        "device_id": _DEVICE_ID,
        "charge_point_id": _CP_ID,
        "connection_status": "connected",
        "last_status_notification": {"type": "StatusNotification", "status": "Available"},
    }
    defaults.update(kwargs)
    return RawOCPPState(**defaults)  # type: ignore[arg-type]


class FakeOCPPProtocolAdapter:
    """Minimal fake that controls get_raw_state() for EVChargerAdapter tests."""

    def __init__(self) -> None:
        self.next_state: RawOCPPState | ProtocolDegradedState | None = None

    async def get_raw_state(self) -> RawOCPPState | ProtocolDegradedState:
        assert self.next_state is not None
        return self.next_state


def _make_adapter(fake: FakeOCPPProtocolAdapter) -> EVChargerAdapter:
    return EVChargerAdapter(device_id=_DEVICE_ID, protocol_adapter=fake)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DeviceAdapter structural subtyping
# ---------------------------------------------------------------------------


def test_isinstance_device_adapter() -> None:
    fake = FakeOCPPProtocolAdapter()
    adapter = _make_adapter(fake)
    assert isinstance(adapter, DeviceAdapter)


# ---------------------------------------------------------------------------
# connect / disconnect are no-ops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_is_noop() -> None:
    fake = FakeOCPPProtocolAdapter()
    adapter = _make_adapter(fake)
    await adapter.connect()  # must not raise, must not call fake


@pytest.mark.asyncio
async def test_disconnect_is_noop() -> None:
    fake = FakeOCPPProtocolAdapter()
    adapter = _make_adapter(fake)
    await adapter.disconnect()  # must not raise, must not call fake


# ---------------------------------------------------------------------------
# OCPP status mapping — all 9 statuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ocpp_status, expected_domain, expected_session_active",
    [
        ("Available", "available", False),
        ("Preparing", "available", True),
        ("Charging", "charging", True),
        ("SuspendedEVSE", "charging", True),
        ("SuspendedEV", "charging", True),
        ("Finishing", "charging", True),
        ("Reserved", "unavailable", False),
        ("Unavailable", "unavailable", False),
        ("Faulted", "faulted", False),
    ],
)
async def test_ocpp_status_mapping(
    ocpp_status: str,
    expected_domain: str,
    expected_session_active: bool,
) -> None:
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = _raw_state(
        last_status_notification={"type": "StatusNotification", "status": ocpp_status}
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, EVChargerState)
    assert result.status == expected_domain
    assert result.session_active is expected_session_active


# ---------------------------------------------------------------------------
# Unknown OCPP status → DegradedDeviceState
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_ocpp_status_returns_degraded() -> None:
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = _raw_state(
        last_status_notification={"type": "StatusNotification", "status": "Alien"}
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, DegradedDeviceState)
    assert "ocpp_unknown_status:Alien" in result.reason


# ---------------------------------------------------------------------------
# No last_status_notification → DegradedDeviceState
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_status_notification_returns_degraded() -> None:
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = _raw_state(last_status_notification=None)
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, DegradedDeviceState)
    assert result.reason == "ocpp_no_status"


# ---------------------------------------------------------------------------
# ProtocolDegradedState → DegradedDeviceState
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_protocol_degraded_translates_to_domain_degraded() -> None:
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = ProtocolDegradedState(
        device_id=_DEVICE_ID,
        reason="ocpp_disconnected",
        occurred_at=_NOW,
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, DegradedDeviceState)
    assert result.role == DeviceRole.ev_charger
    assert result.reason == "ocpp_disconnected"


# ---------------------------------------------------------------------------
# MeterValues absent → current_power_kw is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_meter_values_power_is_none() -> None:
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = _raw_state(
        last_status_notification={"type": "StatusNotification", "status": "Charging"},
        last_meter_values_at=None,
        last_meter_values_power_kw=None,
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, EVChargerState)
    assert result.current_power_kw is None
    assert result.power_source is None
    assert result.power_measured_at is None


# ---------------------------------------------------------------------------
# MeterValues present → current_power_kw + metadata correct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meter_values_sets_power_and_metadata() -> None:
    mv_ts = datetime.now(UTC) - timedelta(seconds=30)
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = _raw_state(
        last_status_notification={"type": "StatusNotification", "status": "Charging"},
        last_meter_values_at=mv_ts,
        last_meter_values_power_kw=7.4,
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, EVChargerState)
    assert result.current_power_kw == 7.4
    assert result.power_source == "meter_values"
    assert result.power_measured_at == mv_ts


# ---------------------------------------------------------------------------
# Negative power → DegradedDeviceState
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_power_returns_degraded() -> None:
    mv_ts = datetime(2024, 1, 15, 11, 30, 0, tzinfo=UTC)
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = _raw_state(
        last_status_notification={"type": "StatusNotification", "status": "Charging"},
        last_meter_values_at=mv_ts,
        last_meter_values_power_kw=-1.0,
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, DegradedDeviceState)
    assert result.reason == "ocpp_invalid_power"


# ---------------------------------------------------------------------------
# structlog device_degraded event captured on degraded path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structlog_device_degraded_emitted() -> None:
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = ProtocolDegradedState(
        device_id=_DEVICE_ID,
        reason="ocpp_disconnected",
        occurred_at=_NOW,
    )
    adapter = _make_adapter(fake)
    with structlog.testing.capture_logs() as logs:
        await adapter.get_state()

    assert len(logs) == 1
    log = logs[0]
    assert log["event"] == "device_degraded"
    assert log["component"] == "adapters"
    assert log["device_id"] == _DEVICE_ID
    assert log["role"] == DeviceRole.ev_charger.value
    assert log["reason"] == "ocpp_disconnected"


# ---------------------------------------------------------------------------
# ocpp_disconnected branch: no status + disconnected connection_status (P3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnected_no_status_returns_ocpp_disconnected() -> None:
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = _raw_state(
        last_status_notification=None,
        connection_status="disconnected",
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, DegradedDeviceState)
    assert result.reason == "ocpp_disconnected"


# ---------------------------------------------------------------------------
# Zero power is valid — not treated as negative (P6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_power_is_valid() -> None:
    mv_ts = datetime.now(UTC) - timedelta(seconds=10)
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = _raw_state(
        last_status_notification={"type": "StatusNotification", "status": "Charging"},
        last_meter_values_at=mv_ts,
        last_meter_values_power_kw=0.0,
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, EVChargerState)
    assert result.current_power_kw == pytest.approx(0.0)
    assert result.power_source == "meter_values"
    assert result.power_measured_at == mv_ts


# ---------------------------------------------------------------------------
# MeterValues staleness guard (D1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_meter_values_exposes_power() -> None:
    mv_ts = datetime.now(UTC) - timedelta(seconds=30)
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = _raw_state(
        last_status_notification={"type": "StatusNotification", "status": "Charging"},
        last_meter_values_at=mv_ts,
        last_meter_values_power_kw=11.0,
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, EVChargerState)
    assert result.current_power_kw == pytest.approx(11.0)
    assert result.power_source == "meter_values"
    assert result.power_measured_at == mv_ts


@pytest.mark.asyncio
async def test_stale_meter_values_clears_power_fields() -> None:
    mv_ts = datetime.now(UTC) - timedelta(seconds=91)
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = _raw_state(
        last_status_notification={"type": "StatusNotification", "status": "Charging"},
        last_meter_values_at=mv_ts,
        last_meter_values_power_kw=11.0,
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, EVChargerState)
    assert result.current_power_kw is None
    assert result.power_source is None
    assert result.power_measured_at is None


@pytest.mark.asyncio
async def test_stale_meter_values_does_not_degrade_charger_state() -> None:
    mv_ts = datetime.now(UTC) - timedelta(seconds=91)
    fake = FakeOCPPProtocolAdapter()
    fake.next_state = _raw_state(
        last_status_notification={"type": "StatusNotification", "status": "Charging"},
        last_meter_values_at=mv_ts,
        last_meter_values_power_kw=11.0,
    )
    adapter = _make_adapter(fake)
    result = await adapter.get_state()
    assert isinstance(result, EVChargerState), (
        "stale MeterValues must not degrade the whole charger state"
    )
    assert result.status == "charging"
    assert result.session_active is True
