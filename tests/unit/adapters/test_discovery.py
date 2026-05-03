"""Unit tests for DiscoveryService and DeviceProbeError (Story 4-5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from open_ems.adapters.discovery import DeviceProbeError, DiscoveryService
from open_ems.adapters.protocol import ProtocolDegradedState, RawDSMRState
from open_ems.core.devices import CapabilityStatus, DeviceDiscoveryResult

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake protocol adapters
# ---------------------------------------------------------------------------


class _FakeModbusClient:
    """Fake _AsyncModbusClient that returns a configurable state."""

    def __init__(self, state: Any) -> None:
        self._state = state
        self.connected = False
        self.close_called = False

    async def connect(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False
        self.close_called = True

    async def read_holding_registers(
        self,
        address: int,
        *,
        count: int = 1,
        device_id: int = 1,
    ) -> Any:
        return self._state


class _FakeSuccessRegisterResponse:
    """Mimics a successful pymodbus holding register response."""

    def __init__(self, values: list[int]) -> None:
        self._values = values
        self.isError = lambda: False  # noqa: N802

    @property
    def registers(self) -> list[int]:
        return self._values


class _FakeErrorRegisterResponse:
    def __init__(self) -> None:
        self.isError = lambda: True  # noqa: N802


def _make_modbus_factory(state: Any) -> Any:
    """Return a client factory that always yields _FakeModbusClient(state)."""
    client = _FakeModbusClient(state)

    def factory(config: Any) -> _FakeModbusClient:
        return client

    return factory


# ---------------------------------------------------------------------------
# DeviceProbeError
# ---------------------------------------------------------------------------


def test_device_probe_error_is_exception() -> None:
    exc = DeviceProbeError("probe failed")
    assert isinstance(exc, Exception)
    assert str(exc) == "probe failed"


# ---------------------------------------------------------------------------
# probe_modbus_endpoint — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_modbus_success_returns_discovery_result() -> None:
    factory = _make_modbus_factory(_FakeSuccessRegisterResponse([0]))
    svc = DiscoveryService(modbus_client_factory=factory)
    result = await svc.probe_modbus_endpoint(
        device_id="inv-001",
        host="192.168.1.10",
        port=502,
    )
    assert isinstance(result, DeviceDiscoveryResult)
    assert result.device_id == "inv-001"
    assert result.protocol == "modbus_tcp"
    assert result.address == "192.168.1.10:502"


@pytest.mark.asyncio
async def test_probe_modbus_success_with_model_sets_full_capability() -> None:
    factory = _make_modbus_factory(_FakeSuccessRegisterResponse([0]))
    svc = DiscoveryService(modbus_client_factory=factory)
    result = await svc.probe_modbus_endpoint(
        device_id="inv-001",
        host="192.168.1.10",
        model="fronius_gen24_v1",
    )
    assert result.model == "fronius_gen24_v1"
    assert result.capability_status == CapabilityStatus.full


@pytest.mark.asyncio
async def test_probe_modbus_success_without_model_defaults_to_reduced() -> None:
    factory = _make_modbus_factory(_FakeSuccessRegisterResponse([0]))
    svc = DiscoveryService(modbus_client_factory=factory)
    result = await svc.probe_modbus_endpoint(
        device_id="inv-001",
        host="192.168.1.10",
    )
    assert result.model is None
    assert result.capability_status == CapabilityStatus.reduced


@pytest.mark.asyncio
async def test_probe_modbus_success_emits_device_discovered(caplog: Any) -> None:
    import structlog.testing

    factory = _make_modbus_factory(_FakeSuccessRegisterResponse([0]))
    svc = DiscoveryService(modbus_client_factory=factory)
    with structlog.testing.capture_logs() as logs:
        await svc.probe_modbus_endpoint(device_id="inv-001", host="192.168.1.10")
    events = [log["event"] for log in logs]
    assert "device_discovered" in events


# ---------------------------------------------------------------------------
# probe_modbus_endpoint — failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_modbus_degraded_raises_device_probe_error() -> None:
    factory = _make_modbus_factory(_FakeErrorRegisterResponse())
    svc = DiscoveryService(modbus_client_factory=factory)
    with pytest.raises(DeviceProbeError):
        await svc.probe_modbus_endpoint(device_id="inv-001", host="192.168.1.10")


@pytest.mark.asyncio
async def test_probe_modbus_degraded_emits_device_not_found() -> None:
    import structlog.testing

    factory = _make_modbus_factory(_FakeErrorRegisterResponse())
    svc = DiscoveryService(modbus_client_factory=factory)
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(DeviceProbeError):
            await svc.probe_modbus_endpoint(device_id="inv-001", host="192.168.1.10")
    events = [log["event"] for log in logs]
    assert "device_not_found" in events


# ---------------------------------------------------------------------------
# register_ocpp_discovery — synchronous
# ---------------------------------------------------------------------------


def test_register_ocpp_discovery_returns_result() -> None:
    svc = DiscoveryService()
    result = svc.register_ocpp_discovery(
        device_id="charger-001",
        address="192.168.1.50:9000",
    )
    assert isinstance(result, DeviceDiscoveryResult)
    assert result.device_id == "charger-001"
    assert result.protocol == "ocpp_1_6"
    assert result.address == "192.168.1.50:9000"


def test_register_ocpp_discovery_with_known_model_sets_capability() -> None:
    svc = DiscoveryService()
    result = svc.register_ocpp_discovery(
        device_id="charger-001",
        address="192.168.1.50:9000",
        model="ocpp_1_6",
    )
    assert result.model == "ocpp_1_6"
    assert result.capability_status == CapabilityStatus.full


def test_register_ocpp_discovery_without_model_defaults_to_reduced() -> None:
    svc = DiscoveryService()
    result = svc.register_ocpp_discovery(
        device_id="charger-001",
        address="192.168.1.50:9000",
    )
    assert result.model is None
    assert result.capability_status == CapabilityStatus.reduced


def test_register_ocpp_discovery_emits_device_discovered() -> None:
    import structlog.testing

    svc = DiscoveryService()
    with structlog.testing.capture_logs() as logs:
        svc.register_ocpp_discovery(device_id="charger-001", address="192.168.1.50:9000")
    events = [log["event"] for log in logs]
    assert "device_discovered" in events


# ---------------------------------------------------------------------------
# probe_dsmr_endpoint — via _wait_for_dsmr_telegram helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_dsmr_success_returns_discovery_result() -> None:
    """DiscoveryService.probe_dsmr_endpoint returns a full result on telegram arrival."""

    raw_dsmr = RawDSMRState(
        device_id="meter-001",
        telegram_fields={"1-0:1.7.0": {"value": 1.5, "unit": "kW"}},
        received_at=_NOW,
    )

    class _FakeDSMRAdapter:
        call_count = 0

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def get_raw_state(self) -> Any:
            self.call_count += 1
            if self.call_count < 3:
                return ProtocolDegradedState(
                    device_id="meter-001", reason="dsmr_unavailable", occurred_at=_NOW
                )
            return raw_dsmr

    fake_adapter = _FakeDSMRAdapter()
    svc = DiscoveryService()

    with patch("open_ems.adapters.discovery.DSMRAdapter", return_value=fake_adapter):
        result = await svc.probe_dsmr_endpoint(
            device_id="meter-001",
            tcp_host="192.168.1.20",
            tcp_port=2000,
        )

    assert isinstance(result, DeviceDiscoveryResult)
    assert result.device_id == "meter-001"
    assert result.protocol == "dsmr_p1"
    assert result.model == "dsmr_p1"
    assert result.capability_status == CapabilityStatus.full


@pytest.mark.asyncio
async def test_probe_dsmr_success_emits_device_discovered() -> None:
    import structlog.testing

    raw_dsmr = RawDSMRState(
        device_id="meter-001",
        telegram_fields={"1-0:1.7.0": {"value": 1.5, "unit": "kW"}},
        received_at=_NOW,
    )

    class _FakeAdapterImmediate:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def get_raw_state(self) -> Any:
            return raw_dsmr

    svc = DiscoveryService()
    with patch("open_ems.adapters.discovery.DSMRAdapter", return_value=_FakeAdapterImmediate()):
        with structlog.testing.capture_logs() as logs:
            await svc.probe_dsmr_endpoint(
                device_id="meter-001",
                tcp_host="192.168.1.20",
                tcp_port=2000,
            )
    events = [log["event"] for log in logs]
    assert "device_discovered" in events


@pytest.mark.asyncio
async def test_probe_dsmr_no_transport_raises_device_probe_error() -> None:
    """probe_dsmr_endpoint raises immediately when neither serial_port nor tcp_host is given."""
    svc = DiscoveryService()
    with pytest.raises(DeviceProbeError, match="requires serial_port or tcp_host"):
        await svc.probe_dsmr_endpoint(device_id="meter-001")


@pytest.mark.asyncio
async def test_probe_dsmr_timeout_raises_device_probe_error() -> None:
    """When no telegram arrives, probe_dsmr_endpoint raises DeviceProbeError."""

    class _FakeAdapterNeverReady:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def get_raw_state(self) -> Any:
            return ProtocolDegradedState(
                device_id="meter-001", reason="dsmr_unavailable", occurred_at=_NOW
            )

    svc = DiscoveryService()
    # Patch the module-level timeout to a tiny value so the test runs fast
    with patch("open_ems.adapters.discovery._DSMR_PROBE_TIMEOUT_S", 0.01):
        with patch(
            "open_ems.adapters.discovery.DSMRAdapter", return_value=_FakeAdapterNeverReady()
        ):
            with pytest.raises(DeviceProbeError):
                await svc.probe_dsmr_endpoint(
                    device_id="meter-001",
                    tcp_host="192.168.1.20",
                    tcp_port=2000,
                )


@pytest.mark.asyncio
async def test_probe_dsmr_timeout_emits_device_not_found() -> None:
    import structlog.testing

    class _FakeAdapterNeverReady:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def get_raw_state(self) -> Any:
            return ProtocolDegradedState(
                device_id="meter-001", reason="dsmr_unavailable", occurred_at=_NOW
            )

    svc = DiscoveryService()
    with patch("open_ems.adapters.discovery._DSMR_PROBE_TIMEOUT_S", 0.01):
        with patch(
            "open_ems.adapters.discovery.DSMRAdapter", return_value=_FakeAdapterNeverReady()
        ):
            with structlog.testing.capture_logs() as logs:
                with pytest.raises(DeviceProbeError):
                    await svc.probe_dsmr_endpoint(
                        device_id="meter-001",
                        tcp_host="192.168.1.20",
                        tcp_port=2000,
                    )
    events = [log["event"] for log in logs]
    assert "device_not_found" in events
