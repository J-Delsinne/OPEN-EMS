"""Tests for the DSMR P1 adapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog.testing
from pydantic import ValidationError

from open_ems.adapters import (
    ProtocolAdapter,
    ProtocolDegradedState,
    RawDSMRState,
    RawProtocolCommand,
)
from open_ems.adapters.dsmr import DSMRAdapter, DSMRAdapterConfig

# ---------------------------------------------------------------------------
# Test telegram: valid DSMR v5 with correct CRC (checksum=0x6039)
# ---------------------------------------------------------------------------
_VALID_TELEGRAM = (
    "/ISk5MT382-1000\r\n"
    "\r\n"
    "1-3:0.2.8(50)\r\n"
    "0-0:1.0.0(101209113020W)\r\n"
    "0-0:96.1.1(4B38454730303430343633)\r\n"
    "1-0:1.8.1(000123.456*kWh)\r\n"
    "1-0:2.8.1(000100.000*kWh)\r\n"
    "0-0:96.14.0(0001)\r\n"
    "1-0:1.7.0(00.234*kW)\r\n"
    "!6039\r\n"
)

# Telegram with wrong checksum for InvalidChecksumError testing
_BAD_CHECKSUM_TELEGRAM = (
    "/ISk5MT382-1000\r\n\r\n1-3:0.2.8(50)\r\n1-0:1.8.1(000123.456*kWh)\r\n!FFFF\r\n"
)


def _serial_config(**kwargs: Any) -> DSMRAdapterConfig:
    defaults: dict[str, Any] = {
        "device_id": "meter-001",
        "serial_port": "/dev/ttyUSB0",
        "reconnect_delay_s": 0.0,
    }
    defaults.update(kwargs)
    return DSMRAdapterConfig(**defaults)


def _tcp_config(**kwargs: Any) -> DSMRAdapterConfig:
    defaults: dict[str, Any] = {
        "device_id": "meter-001",
        "tcp_host": "192.168.1.10",
        "tcp_port": 23,
        "reconnect_delay_s": 0.0,
    }
    defaults.update(kwargs)
    return DSMRAdapterConfig(**defaults)


# ---------------------------------------------------------------------------
# Fake transport infrastructure
# ---------------------------------------------------------------------------


class FakeTransport:
    """Queue-backed fake transport; enqueue bytes or Exception to control flow."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | BaseException] = asyncio.Queue()
        self.close_called = False

    async def read_line(self) -> bytes:
        item = await self.queue.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.close_called = True

    def feed(self, data: str) -> None:
        """Push lines of a telegram string into the queue."""
        for line in data.splitlines(keepends=True):
            self.queue.put_nowait(line.encode("ascii"))

    def feed_error(self, exc: BaseException) -> None:
        self.queue.put_nowait(exc)


class FakeTransportFactory:
    """Returns pre-created FakeTransport instances in order; raises OSError when exhausted."""

    def __init__(self, transports: list[FakeTransport]) -> None:
        self._transports = transports
        self._index = 0

    async def open(self) -> FakeTransport:
        if self._index >= len(self._transports):
            raise OSError("no more transports")
        transport = self._transports[self._index]
        self._index += 1
        return transport


async def _drain_task(adapter: DSMRAdapter, steps: int = 5) -> None:
    """Yield control enough times for the background task to process queued items."""
    for _ in range(steps):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Config validation tests (Task 1)
# ---------------------------------------------------------------------------


class TestDSMRAdapterConfig:
    def test_valid_serial_config(self) -> None:
        cfg = DSMRAdapterConfig(device_id="m1", serial_port="/dev/ttyUSB0")
        assert cfg.serial_port == "/dev/ttyUSB0"
        assert cfg.tcp_host is None
        assert cfg.tcp_port is None

    def test_valid_tcp_config(self) -> None:
        cfg = DSMRAdapterConfig(device_id="m1", tcp_host="10.0.0.1", tcp_port=23)
        assert cfg.tcp_host == "10.0.0.1"
        assert cfg.tcp_port == 23

    def test_no_transport_raises(self) -> None:
        with pytest.raises(ValidationError, match="exactly one transport"):
            DSMRAdapterConfig(device_id="m1")

    def test_both_transports_raises(self) -> None:
        with pytest.raises(ValidationError, match="only one transport"):
            DSMRAdapterConfig(
                device_id="m1",
                serial_port="/dev/ttyUSB0",
                tcp_host="10.0.0.1",
                tcp_port=23,
            )

    def test_partial_tcp_host_only_raises(self) -> None:
        with pytest.raises(ValidationError, match="tcp_host and tcp_port must both be set"):
            DSMRAdapterConfig(device_id="m1", tcp_host="10.0.0.1")

    def test_partial_tcp_port_only_raises(self) -> None:
        with pytest.raises(ValidationError, match="tcp_host and tcp_port must both be set"):
            DSMRAdapterConfig(device_id="m1", tcp_port=23)

    def test_tcp_port_zero_raises(self) -> None:
        with pytest.raises(ValidationError):
            DSMRAdapterConfig(device_id="m1", tcp_host="10.0.0.1", tcp_port=0)

    def test_tcp_port_65536_raises(self) -> None:
        with pytest.raises(ValidationError):
            DSMRAdapterConfig(device_id="m1", tcp_host="10.0.0.1", tcp_port=65536)

    def test_bool_stale_after_s_raises(self) -> None:
        with pytest.raises(ValidationError):
            DSMRAdapterConfig(device_id="m1", serial_port="/dev/ttyUSB0", stale_after_s=True)

    def test_string_reconnect_delay_raises(self) -> None:
        with pytest.raises(ValidationError):
            DSMRAdapterConfig(device_id="m1", serial_port="/dev/ttyUSB0", reconnect_delay_s="fast")

    def test_default_dsmr_version_is_5(self) -> None:
        cfg = DSMRAdapterConfig(device_id="m1", serial_port="/dev/ttyUSB0")
        assert cfg.dsmr_version == "5"

    def test_all_dsmr_versions_accepted(self) -> None:
        for version in ("2.2", "4", "4+", "5", "5B"):
            cfg = DSMRAdapterConfig(
                device_id="m1", serial_port="/dev/ttyUSB0", dsmr_version=version
            )
            assert cfg.dsmr_version == version


# ---------------------------------------------------------------------------
# Adapter state tests (Tasks 3, 4, 6, 7)
# ---------------------------------------------------------------------------


class TestDSMRAdapterState:
    async def test_get_raw_state_before_start_returns_unavailable(self) -> None:
        adapter = DSMRAdapter(_serial_config())
        state = await adapter.get_raw_state()
        assert isinstance(state, ProtocolDegradedState)
        assert state.reason == "dsmr_unavailable"

    async def test_get_raw_state_after_start_before_telegram_returns_unavailable(self) -> None:
        transport = FakeTransport()
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(), transport_factory=factory)
        await adapter.start()
        await _drain_task(adapter)
        state = await adapter.get_raw_state()
        assert isinstance(state, ProtocolDegradedState)
        assert state.reason == "dsmr_unavailable"
        await adapter.stop()

    async def test_successful_parse_returns_raw_dsmr_state(self) -> None:
        transport = FakeTransport()
        transport.feed(_VALID_TELEGRAM)
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(), transport_factory=factory)
        await adapter.start()
        await _drain_task(adapter, steps=10)
        state = await adapter.get_raw_state()
        assert isinstance(state, RawDSMRState)
        assert state.device_id == "meter-001"
        assert state.received_at.tzinfo is not None
        assert state.received_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
        assert "ELECTRICITY_USED_TARIFF_1" in state.telegram_fields
        entry = state.telegram_fields["ELECTRICITY_USED_TARIFF_1"]
        assert "value" in entry
        assert "unit" in entry
        assert entry["unit"] == "kWh"
        await adapter.stop()

    async def test_received_at_is_utc_aware(self) -> None:
        transport = FakeTransport()
        transport.feed(_VALID_TELEGRAM)
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(), transport_factory=factory)
        await adapter.start()
        await _drain_task(adapter, steps=10)
        state = await adapter.get_raw_state()
        assert isinstance(state, RawDSMRState)
        assert state.received_at.tzinfo is UTC
        await adapter.stop()

    async def test_staleness_returns_degraded_state(self) -> None:
        transport = FakeTransport()
        transport.feed(_VALID_TELEGRAM)
        transport.feed_error(OSError("done"))
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(stale_after_s=60.0), transport_factory=factory)
        await adapter.start()
        await _drain_task(adapter, steps=10)

        # Backdate the received_at timestamp to simulate staleness
        async with adapter._lock:
            adapter._received_at = datetime.now(UTC) - timedelta(seconds=61)

        state = await adapter.get_raw_state()
        assert isinstance(state, ProtocolDegradedState)
        assert state.reason == "dsmr_stale"
        await adapter.stop()

    async def test_staleness_log_is_one_shot(self) -> None:
        transport = FakeTransport()
        transport.feed(_VALID_TELEGRAM)
        transport.feed_error(OSError("done"))
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(stale_after_s=60.0), transport_factory=factory)
        await adapter.start()
        await _drain_task(adapter, steps=10)

        async with adapter._lock:
            adapter._received_at = datetime.now(UTC) - timedelta(seconds=61)

        with structlog.testing.capture_logs() as logs:
            await adapter.get_raw_state()
            await adapter.get_raw_state()

        stale_events = [e for e in logs if e.get("event") == "adapter_stale"]
        assert len(stale_events) == 1, "staleness warning must be emitted only once per transition"
        await adapter.stop()

    async def test_staleness_clears_after_new_telegram(self) -> None:
        transport = FakeTransport()
        transport.feed(_VALID_TELEGRAM)
        # No error — keep the transport open to inject a second telegram later
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(stale_after_s=60.0), transport_factory=factory)
        await adapter.start()
        await _drain_task(adapter, steps=10)

        # Make it stale
        async with adapter._lock:
            adapter._received_at = datetime.now(UTC) - timedelta(seconds=61)
        state = await adapter.get_raw_state()
        assert isinstance(state, ProtocolDegradedState)
        assert state.reason == "dsmr_stale"
        assert adapter._stale_logged is True

        # Feed another valid telegram — this should reset staleness
        transport.feed(_VALID_TELEGRAM)
        await _drain_task(adapter, steps=10)

        state = await adapter.get_raw_state()
        assert isinstance(state, RawDSMRState)
        assert adapter._stale_logged is False
        await adapter.stop()

    async def test_serial_disconnect_returns_unavailable(self) -> None:
        transport = FakeTransport()
        transport.feed_error(OSError("serial disconnected"))
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(), transport_factory=factory)
        await adapter.start()
        await _drain_task(adapter, steps=10)
        state = await adapter.get_raw_state()
        assert isinstance(state, ProtocolDegradedState)
        assert state.reason == "dsmr_unavailable"
        await adapter.stop()

    async def test_disconnect_after_valid_telegram_returns_unavailable(self) -> None:
        """AC5: transport loss after a successful parse must immediately yield dsmr_unavailable."""
        transport = FakeTransport()
        transport.feed(_VALID_TELEGRAM)
        # no error yet — task parks at queue.get() after parsing the telegram
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(), transport_factory=factory)
        await adapter.start()
        await _drain_task(adapter, steps=15)

        # State should be healthy at this point
        state_after_parse = await adapter.get_raw_state()
        assert isinstance(state_after_parse, RawDSMRState), (
            "expected RawDSMRState before disconnect"
        )

        # Inject a transport failure while the task is parked waiting for the next line
        transport.feed_error(OSError("serial disconnected"))
        await _drain_task(adapter, steps=10)

        state_after_disconnect = await adapter.get_raw_state()
        assert isinstance(state_after_disconnect, ProtocolDegradedState)
        assert state_after_disconnect.reason == "dsmr_unavailable"
        await adapter.stop()

    async def test_reconnect_after_disconnect_recovers(self) -> None:
        """Adapter recovers after first transport raises OSError, second succeeds."""
        failing_transport = FakeTransport()
        failing_transport.feed_error(OSError("disconnect"))

        recovering_transport = FakeTransport()
        recovering_transport.feed(_VALID_TELEGRAM)
        # no feed_error — task parks at queue.get() after telegram is processed

        factory = FakeTransportFactory([failing_transport, recovering_transport])
        adapter = DSMRAdapter(_serial_config(), transport_factory=factory)
        await adapter.start()
        await _drain_task(adapter, steps=20)
        state = await adapter.get_raw_state()
        assert isinstance(state, RawDSMRState)
        await adapter.stop()

    async def test_malformed_telegram_loop_continues_no_crash(self) -> None:
        """Bad checksum is caught; loop continues and processes the valid telegram."""
        transport = FakeTransport()
        transport.feed(_BAD_CHECKSUM_TELEGRAM)
        transport.feed(_VALID_TELEGRAM)
        # no feed_error — task parks at queue.get() after processing both telegrams
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(), transport_factory=factory)
        await adapter.start()
        await _drain_task(adapter, steps=15)
        # The valid telegram should still be processed
        state = await adapter.get_raw_state()
        assert isinstance(state, RawDSMRState)
        await adapter.stop()

    async def test_parse_error_is_logged(self) -> None:
        transport = FakeTransport()
        transport.feed(_BAD_CHECKSUM_TELEGRAM)
        transport.feed_error(OSError("done"))
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(), transport_factory=factory)

        with structlog.testing.capture_logs() as logs:
            await adapter.start()
            await _drain_task(adapter, steps=15)

        parse_errors = [e for e in logs if e.get("event") == "adapter_parse_error"]
        assert len(parse_errors) >= 1
        assert parse_errors[0]["component"] == "dsmr"
        await adapter.stop()

    async def test_connection_failed_is_logged(self) -> None:
        transport = FakeTransport()
        transport.feed_error(OSError("connection refused"))
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(), transport_factory=factory)

        with structlog.testing.capture_logs() as logs:
            await adapter.start()
            await _drain_task(adapter, steps=10)

        conn_events = [e for e in logs if e.get("event") == "adapter_connection_failed"]
        assert len(conn_events) >= 1
        assert conn_events[0]["component"] == "dsmr"
        await adapter.stop()

    async def test_send_raw_command_always_returns_error(self) -> None:
        adapter = DSMRAdapter(_serial_config())
        command = RawProtocolCommand(
            correlation_id="req-1",
            device_id="meter-001",
            command_name="read",
            payload=b"",
        )
        result = await adapter.send_raw_command(command)
        assert result.protocol_status == "error"
        assert result.raw_response == {"error": "dsmr_read_only"}
        assert result.correlation_id == "req-1"

    async def test_start_is_idempotent(self) -> None:
        transport = FakeTransport()
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(), transport_factory=factory)
        await adapter.start()
        task_before = adapter._task
        await adapter.start()  # second call — must be a no-op
        assert adapter._task is task_before
        await adapter.stop()

    async def test_stop_cleans_up_and_subsequent_get_state_returns_unavailable(self) -> None:
        transport = FakeTransport()
        transport.feed(_VALID_TELEGRAM)
        # no feed_error — task parks at queue.get() after telegram is processed
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_serial_config(), transport_factory=factory)
        await adapter.start()
        await _drain_task(adapter, steps=10)

        # Confirm we have a valid state before stop
        state_before = await adapter.get_raw_state()
        assert isinstance(state_before, RawDSMRState)

        await adapter.stop()
        state_after = await adapter.get_raw_state()
        assert isinstance(state_after, ProtocolDegradedState)
        assert state_after.reason == "dsmr_unavailable"

    async def test_protocol_adapter_structural_satisfaction(self) -> None:
        adapter = DSMRAdapter(_serial_config())
        assert isinstance(adapter, ProtocolAdapter)


# ---------------------------------------------------------------------------
# TCP transport path (Task 2, AC2)
# ---------------------------------------------------------------------------


class TestDSMRAdapterTcp:
    async def test_tcp_transport_uses_same_pipeline(self) -> None:
        transport = FakeTransport()
        transport.feed(_VALID_TELEGRAM)
        # no feed_error — task parks at queue.get()
        factory = FakeTransportFactory([transport])
        adapter = DSMRAdapter(_tcp_config(), transport_factory=factory)
        await adapter.start()
        await _drain_task(adapter, steps=10)
        state = await adapter.get_raw_state()
        assert isinstance(state, RawDSMRState)
        await adapter.stop()
