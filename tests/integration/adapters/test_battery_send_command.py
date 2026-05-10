"""Integration tests for the real BatteryAdapter end-to-end (Story 9.0 AC11).

Path: control loop trigger → RetryPolicy → PolicyGuard → BatteryAdapter →
ModbusTcpAdapter → simulated pymodbus client → register write → CommandResult.

Uses an in-process fake pymodbus client (no real network) to drive each
ProtocolStatus outcome and verify that the contract holds across the whole
pipeline.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.modbus.tcp import (
    ModbusRegisterRange,
    ModbusTcpAdapter,
    ModbusTcpAdapterConfig,
)
from open_ems.core import (
    BatteryState,
    DeviceRole,
    StateStore,
    SystemOperatingMode,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandStatus,
    SetBatteryChargeRateCommand,
)
from open_ems.engine import PolicyGuard, RetryPolicy
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings
from open_ems.storage.repositories.event_log_repo import EventLogRepo

_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake pymodbus client
# ---------------------------------------------------------------------------


class _FakeModbusResponse:
    def __init__(self, *, registers: list[int] | None = None, error: bool = False) -> None:
        self._registers = registers or []
        self._error = error

    @property
    def registers(self) -> list[int]:
        return self._registers

    def isError(self) -> bool:  # noqa: N802 — pymodbus naming
        return self._error


class _FakeModbusClient:
    def __init__(
        self,
        *,
        write_should_error: bool = False,
        write_should_timeout: bool = False,
    ) -> None:
        self.connected: bool = True
        self.write_calls: list[tuple[int, int]] = []
        self._write_should_error = write_should_error
        self._write_should_timeout = write_should_timeout
        # Set in the try/finally when a timeout-cancellation cleanly unwinds —
        # let tests verify cleanup happened rather than hoping the runtime
        # was not pinned for 60s.
        self.timeout_sleep_exited: bool = False

    async def connect(self) -> bool:
        return True

    def close(self) -> None:
        self.connected = False

    def read_holding_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> Any:  # pragma: no cover — read path not exercised here
        async def _coro() -> _FakeModbusResponse:
            return _FakeModbusResponse(registers=[0] * count)

        return _coro()

    def read_input_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> Any:  # pragma: no cover
        async def _coro() -> _FakeModbusResponse:
            return _FakeModbusResponse(registers=[0] * count)

        return _coro()

    def write_register(self, address: int, value: int, *, device_id: int = 1) -> Any:
        async def _coro() -> _FakeModbusResponse:
            self.write_calls.append((address, value))
            if self._write_should_timeout:
                # Wrap the sleep in try/finally so cancellation from an outer
                # asyncio.wait_for cleanly exits this coroutine — without the
                # finally, a leaked task could keep the test runtime alive for
                # the full 60s if cancellation propagation regresses.
                try:
                    await asyncio.sleep(60.0)
                finally:
                    self.timeout_sleep_exited = True
            if self._write_should_error:
                return _FakeModbusResponse(error=True)
            return _FakeModbusResponse()

        return _coro()

    def write_registers(
        self, address: int, values: list[int], *, device_id: int = 1
    ) -> Any:  # pragma: no cover
        async def _coro() -> _FakeModbusResponse:
            return _FakeModbusResponse()

        return _coro()


# ---------------------------------------------------------------------------
# Pipeline fixture builder
# ---------------------------------------------------------------------------


async def _state_store_with_battery() -> StateStore:
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    battery = BatteryState(
        device_id="bat-001",
        soc_percent=80.0,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW,
    )
    await store.publish({DeviceRole.battery: battery}, operating_mode=SystemOperatingMode.normal)
    return store


def _settings(timeout_s: float = 5.0) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        command_max_retries=2,
        command_retry_backoff_seconds=0.0,
    )


def _modbus_adapter(client: _FakeModbusClient, *, timeout_s: float = 1.0) -> ModbusTcpAdapter:
    config = ModbusTcpAdapterConfig(
        device_id="bat-001",
        host="127.0.0.1",
        port=5020,
        registers=(ModbusRegisterRange(table="holding", start_address=0, count=1),),
        timeout_s=timeout_s,
    )
    return ModbusTcpAdapter(config, client_factory=lambda _: client)  # type: ignore[arg-type, return-value]


def _audit_spy_observability() -> tuple[ObservabilityService, AsyncMock]:
    repo = AsyncMock(spec=EventLogRepo)
    obs = ObservabilityService(repo=repo)
    spy = AsyncMock()
    obs.audit = spy  # type: ignore[method-assign]
    return obs, spy


def _charge_command() -> SetBatteryChargeRateCommand:
    return SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=2.5,
        correlation_id=uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# AC11 integration #1 — end-to-end battery charge command, success path
# ---------------------------------------------------------------------------


async def test_end_to_end_battery_charge_success_writes_register() -> None:
    store = await _state_store_with_battery()
    client = _FakeModbusClient()
    modbus = _modbus_adapter(client)
    battery = BatteryAdapter("bat-001", modbus, "byd_hvs_v1")

    obs, audit_spy = _audit_spy_observability()
    settings = _settings()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: battery},
        observability=obs,
        settings=settings,
    )
    retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

    cmd = _charge_command()
    result = await retry.execute(cmd)

    assert result.status is CommandStatus.success
    assert result.applied is True
    assert result.reason == "ok"
    assert result.correlation_id == cmd.correlation_id
    # Register write actually reached the simulated Modbus client.
    assert client.write_calls == [(110, 2500)]  # 2.5 kW → 2500 W at HVS charge register
    # DECISION audit emitted exactly once on success.
    assert audit_spy.await_count == 1
    decision = audit_spy.await_args_list[0]
    assert decision.kwargs["event_type"] == "DECISION"


# ---------------------------------------------------------------------------
# AC11 integration #3 — end-to-end timeout reaches PolicyGuard as protocol timeout
# ---------------------------------------------------------------------------


async def test_end_to_end_battery_timeout_reaches_policy_guard_with_modbus_reason() -> None:
    """AC7: protocol-layer timeout fires first; outer PolicyGuard timeout is defense in depth.

    Setting ``timeout_s=0.05`` on the Modbus adapter ensures the protocol-layer
    ``asyncio.wait_for`` fires well before PolicyGuard's 10s outer timeout.
    """
    store = await _state_store_with_battery()
    client = _FakeModbusClient(write_should_timeout=True)
    modbus = _modbus_adapter(client, timeout_s=0.05)
    battery = BatteryAdapter("bat-001", modbus, "byd_hvs_v1")

    obs, _ = _audit_spy_observability()
    settings = _settings()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: battery},
        observability=obs,
        settings=settings,
    )

    cmd = _charge_command()
    result = await guard.authorize_and_dispatch(cmd)

    assert result.status is CommandStatus.timeout
    assert result.applied is False
    assert result.reason == "modbus_command_timeout"
    # PolicyGuard's outer timeout reason ("command_timeout") MUST NOT be the
    # reason — the protocol timeout fired first.
    assert result.reason != "command_timeout"


# ---------------------------------------------------------------------------
# AC11 integration #4 — RetryPolicy retries against a flaky Modbus server
# ---------------------------------------------------------------------------


class _FlakeyModbusClient(_FakeModbusClient):
    """Fails the first ``failure_count`` writes (with isError), then succeeds."""

    def __init__(self, *, failure_count: int) -> None:
        super().__init__()
        self._remaining_failures = failure_count

    def write_register(self, address: int, value: int, *, device_id: int = 1) -> Any:
        async def _coro() -> _FakeModbusResponse:
            self.write_calls.append((address, value))
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
                return _FakeModbusResponse(error=True)
            return _FakeModbusResponse()

        return _coro()


async def test_end_to_end_battery_retry_recovers_after_transient_modbus_error() -> None:
    """AC11 integration #4: flaky Modbus server; second attempt succeeds via RetryPolicy."""
    store = await _state_store_with_battery()
    client = _FlakeyModbusClient(failure_count=1)
    modbus = _modbus_adapter(client)
    battery = BatteryAdapter("bat-001", modbus, "byd_hvs_v1")

    obs, audit_spy = _audit_spy_observability()
    settings = _settings()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: battery},
        observability=obs,
        settings=settings,
    )
    retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

    cmd = _charge_command()
    result = await retry.execute(cmd)

    assert result.status is CommandStatus.success
    assert result.applied is True
    # Two writes: first failed, second succeeded. This is the concrete proof
    # that RetryPolicy actually retried — a regression that suppressed the retry
    # would leave write_calls at length 1 with the result still failed.
    assert len(client.write_calls) == 2
    assert client.write_calls[0] == client.write_calls[1], (
        "Retried write must target the same register with the same value"
    )
    # Single terminal DECISION audit on the recovered success. Pin the
    # device_id and the attempts count from the audit detail so a future
    # RetryPolicy change that emits a stray audit for a different command would
    # fail here, not pass silently.
    assert audit_spy.await_count == 1, (
        f"Expected exactly one DECISION audit on success, got {audit_spy.await_count}"
    )
    audit_call = audit_spy.await_args_list[0].kwargs
    assert audit_call["event_type"] == "DECISION"
    assert audit_call["device_id"] == cmd.device_id
    # detail.attempts == 2 proves the retry actually happened end-to-end, not
    # just at the protocol layer — a regression that emitted DECISION on the
    # first (failed) attempt would record attempts=1 here.
    assert audit_call["detail"]["attempts"] == 2
    assert audit_call["detail"]["command_status"] == "success"


# ---------------------------------------------------------------------------
# Negative space: PolicyGuard's outer except is not reached on adapter internal error
# ---------------------------------------------------------------------------


class _ExplodingModbusClient(_FakeModbusClient):
    """Raises a non-Modbus exception on write to verify AR16 wrapping."""

    def write_register(self, address: int, value: int, *, device_id: int = 1) -> Any:
        async def _coro() -> _FakeModbusResponse:
            raise RuntimeError("library version mismatch")

        return _coro()


async def test_end_to_end_battery_adapter_internal_error_wrapped_not_propagated() -> None:
    """AC9: adapter wraps unexpected exceptions; PolicyGuard's outer except is unreachable here.

    The Modbus protocol layer translates known exception families into
    ``ProtocolCommandResult(error)``; ``RuntimeError`` is unknown and propagates
    out of ``send_raw_command``. The domain adapter's ``except Exception``
    wraps it as ``adapter_internal_error:RuntimeError``. PolicyGuard's outer
    ``except Exception`` therefore never fires.
    """
    store = await _state_store_with_battery()
    client = _ExplodingModbusClient()
    modbus = _modbus_adapter(client)
    battery = BatteryAdapter("bat-001", modbus, "byd_hvs_v1")

    obs, _ = _audit_spy_observability()
    settings = _settings()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: battery},
        observability=obs,
        settings=settings,
    )

    cmd = _charge_command()
    result = await guard.authorize_and_dispatch(cmd)

    # Adapter wrapped the exception — reason has the canonical prefix.
    assert result.status is CommandStatus.failed
    assert result.reason == "adapter_internal_error:RuntimeError"
    # PolicyGuard's outer wrap would have used ``repr(exc)`` as reason; we
    # verify the adapter reached the result first.
    assert "RuntimeError(" not in result.reason
