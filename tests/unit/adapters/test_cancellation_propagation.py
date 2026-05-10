"""Cross-adapter cancellation propagation tests (Story 9.0 AC6, Task 8).

Asserts:
1. CancelledError raised mid-``send_command`` propagates without being wrapped
   into a CommandResult (cross-adapter contract clause 5).
2. The adapter does NOT emit a parallel cancellation audit / structured log —
   ``RetryPolicy._emit_cancellation_audit`` is the single emitter.
3. Modbus ``_lock`` is released on cancellation (no resource leak).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import structlog.testing

from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.modbus.tcp import (
    ModbusRegisterRange,
    ModbusTcpAdapter,
    ModbusTcpAdapterConfig,
)
from open_ems.adapters.ocpp.charger_adapter import EVChargerAdapter
from open_ems.adapters.protocol import (
    ProtocolCommandResult,
    RawProtocolCommand,
)
from open_ems.core.commands import (
    CommandOrigin,
    SetBatteryChargeRateCommand,
    SetEVChargingRateCommand,
)
from open_ems.core.devices import DeviceRole

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _BlockingModbus:
    """Suspends ``send_raw_command`` until cancelled — Modbus-shaped."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cleanup_observed = False

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        self.entered.set()
        try:
            await asyncio.sleep(60.0)
            raise AssertionError("not reached")
        finally:
            self.cleanup_observed = True

    async def close(self) -> None:
        pass


class _BlockingOCPP:
    """Suspends ``send_raw_command`` until cancelled — OCPP-shaped."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cleanup_observed = False
        self.active_transaction_id: int | None = 1
        self._next_profile_id: int = 1

    def reserve_charging_profile_id(self) -> int:
        pid = self._next_profile_id
        self._next_profile_id += 1
        return pid

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        self.entered.set()
        try:
            await asyncio.sleep(60.0)
            raise AssertionError("not reached")
        finally:
            self.cleanup_observed = True


# ---------------------------------------------------------------------------
# Battery cancellation propagation
# ---------------------------------------------------------------------------


async def test_battery_cancellation_propagates_without_wrapping_or_logging() -> None:
    fake = _BlockingModbus()
    adapter = BatteryAdapter("bat-1", fake, "byd_hvs_v1")  # type: ignore[arg-type]
    cmd = SetBatteryChargeRateCommand(
        device_id="bat-1",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )

    with structlog.testing.capture_logs() as logs:
        send_task: asyncio.Task[Any] = asyncio.create_task(adapter.send_command(cmd))
        await fake.entered.wait()
        send_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await send_task

    # No CommandResult is returned on cancellation — contract clause 5.
    assert send_task.cancelled() or send_task.done()
    # Adapter must not log a cancellation event of its own.
    forbidden_events = {
        "device_command_cancelled",
        "retry_cancelled",
        "command_cancelled",
        "adapter_cancelled",
    }
    assert not any(e.get("event") in forbidden_events for e in logs), (
        f"adapter emitted forbidden cancellation log: {logs}"
    )
    # Resource cleanup observed via the protocol layer's try/finally.
    assert fake.cleanup_observed is True


# ---------------------------------------------------------------------------
# EV charger cancellation propagation
# ---------------------------------------------------------------------------


async def test_ev_charger_cancellation_propagates_without_wrapping_or_logging() -> None:
    fake = _BlockingOCPP()
    adapter = EVChargerAdapter("ev-1", fake)  # type: ignore[arg-type]
    cmd = SetEVChargingRateCommand(
        device_id="ev-1",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=3.0,
    )

    with structlog.testing.capture_logs() as logs:
        send_task: asyncio.Task[Any] = asyncio.create_task(adapter.send_command(cmd))
        await fake.entered.wait()
        send_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await send_task

    assert send_task.cancelled() or send_task.done()
    forbidden_events = {
        "device_command_cancelled",
        "retry_cancelled",
        "command_cancelled",
        "adapter_cancelled",
    }
    assert not any(e.get("event") in forbidden_events for e in logs)
    assert fake.cleanup_observed is True


# ---------------------------------------------------------------------------
# Modbus protocol-layer lock is released on cancellation
# ---------------------------------------------------------------------------


class _CancellableClient:
    """Minimal async modbus client that suspends in write_register until cancelled."""

    def __init__(self) -> None:
        self.connected: bool = True
        self.entered = asyncio.Event()

    async def connect(self) -> bool:  # pragma: no cover — already connected
        return True

    def close(self) -> None:
        self.connected = False

    def read_holding_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> Any:  # pragma: no cover
        raise AssertionError("not used in cancellation test")

    def read_input_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> Any:  # pragma: no cover
        raise AssertionError("not used in cancellation test")

    async def write_register(self, address: int, value: int, *, device_id: int = 1) -> Any:
        self.entered.set()
        await asyncio.sleep(60.0)
        raise AssertionError("not reached")

    async def write_registers(
        self, address: int, values: list[int], *, device_id: int = 1
    ) -> Any:  # pragma: no cover
        raise AssertionError("not used in this test")


async def test_modbus_lock_released_on_cancellation() -> None:
    """Mid-write cancellation must release the adapter's _lock so the next call proceeds."""
    client = _CancellableClient()
    config = ModbusTcpAdapterConfig(
        device_id="bat-1",
        host="127.0.0.1",
        port=5020,
        registers=(ModbusRegisterRange(table="holding", start_address=0, count=1),),
        timeout_s=1.0,
    )
    adapter = ModbusTcpAdapter(config, client_factory=lambda _: client)  # type: ignore[arg-type, return-value]

    raw_command = RawProtocolCommand(
        correlation_id=str(uuid.uuid4()),
        device_id="bat-1",
        command_name="set_battery_charge_rate",
        payload={"operation": "write_register", "address": 110, "value": 1000},
    )

    send_task: asyncio.Task[Any] = asyncio.create_task(adapter.send_raw_command(raw_command))
    await client.entered.wait()
    send_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await send_task

    # The lock must have been released — a second send_raw_command should be able
    # to acquire it without blocking. We verify by checking the underlying lock.
    assert not adapter._lock.locked(), "Modbus _lock was not released after cancellation"
