"""Unit tests for ``BatteryAdapter.send_command`` (Story 9.0).

Covers AC2, AC5, AC6, AC7 (timeout reason form), AC8, AC9, AC11.1–AC11.9 for the
battery scope. Uses a fake ``ModbusTcpAdapter``-shaped protocol adapter to drive
``ProtocolCommandResult`` outcomes deterministically.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import structlog.testing

from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.protocol import (
    ProtocolCommandResult,
    RawProtocolCommand,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandStatus,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.core.devices import DeviceRole

# ---------------------------------------------------------------------------
# Fake protocol adapter — programmable per test
# ---------------------------------------------------------------------------


class FakeModbusProtocolAdapter:
    """Captures send_raw_command calls and returns programmed results."""

    def __init__(self) -> None:
        self._next_result: ProtocolCommandResult | None = None
        self._next_exc: BaseException | None = None
        self._cancel: bool = False
        self.last_command: RawProtocolCommand | None = None
        self.send_calls: int = 0

    def program_result(self, result: ProtocolCommandResult) -> None:
        self._next_result = result
        self._next_exc = None

    def program_exception(self, exc: BaseException) -> None:
        self._next_exc = exc
        self._next_result = None

    def program_cancellation(self) -> None:
        self._cancel = True

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        self.send_calls += 1
        self.last_command = command
        if self._cancel:
            raise asyncio.CancelledError()
        if self._next_exc is not None:
            raise self._next_exc
        assert self._next_result is not None, "must program a result first"
        return self._next_result

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(model: str = "byd_hvs_v1") -> tuple[BatteryAdapter, FakeModbusProtocolAdapter]:
    fake = FakeModbusProtocolAdapter()
    adapter = BatteryAdapter(
        device_id="bat-001",
        protocol_adapter=fake,  # type: ignore[arg-type]
        model=model,
    )
    return adapter, fake


def _charge_cmd(rate_kw: float = 3.0) -> SetBatteryChargeRateCommand:
    return SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


def _discharge_cmd(rate_kw: float = 2.0) -> SetBatteryDischargeRateCommand:
    return SetBatteryDischargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


def _make_protocol_result(
    cmd_correlation_id: uuid.UUID,
    *,
    status: str = "acked",
    raw_response: dict[str, Any] | None = None,
    correlation_id_override: str | None = None,
) -> ProtocolCommandResult:
    if raw_response is None:
        raw_response = {"operation": "write_register", "address": 110, "value": 3000}
    return ProtocolCommandResult(
        correlation_id=correlation_id_override or str(cmd_correlation_id),
        device_id="bat-001",
        protocol_status=status,  # type: ignore[arg-type]
        raw_response=raw_response,
    )


# ---------------------------------------------------------------------------
# AC11.1 — Success path (charge, discharge, both BYD models)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, expected_charge_register",
    [
        ("byd_hvs_v1", 110),
        ("byd_hvm_v1", 210),
    ],
)
async def test_send_command_charge_success(model: str, expected_charge_register: int) -> None:
    adapter, fake = _make_adapter(model=model)
    cmd = _charge_cmd(rate_kw=3.0)
    fake.program_result(_make_protocol_result(cmd.correlation_id, status="acked"))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.success
    assert result.applied is True
    assert result.reason == "ok"
    assert result.correlation_id == cmd.correlation_id
    assert result.observed_state is None  # contract clause 7
    assert fake.send_calls == 1
    sent = fake.last_command
    assert sent is not None
    assert sent.command_name == "set_battery_charge_rate"
    assert sent.correlation_id == str(cmd.correlation_id)
    # payload encodes 3 kW = 3000 W and writes to the model-specific register
    # (HVS = 110, HVM = 210). The register address is the contract between the
    # adapter and the register map — pinning it catches map↔model drift.
    assert sent.payload["value"] == 3000  # type: ignore[index]
    assert sent.payload["address"] == expected_charge_register  # type: ignore[index]
    assert sent.payload["operation"] == "write_register"  # type: ignore[index]


async def test_send_command_discharge_success() -> None:
    adapter, fake = _make_adapter()
    cmd = _discharge_cmd(rate_kw=2.5)
    fake.program_result(_make_protocol_result(cmd.correlation_id, status="acked"))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.success
    assert result.applied is True
    assert fake.last_command is not None
    assert fake.last_command.command_name == "set_battery_discharge_rate"
    assert fake.last_command.payload["value"] == 2500  # type: ignore[index]


# ---------------------------------------------------------------------------
# AC11.2 — Timeout
# ---------------------------------------------------------------------------


async def test_send_command_timeout_returns_modbus_command_timeout() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_result(
        _make_protocol_result(
            cmd.correlation_id,
            status="timeout",
            raw_response={"error": "modbus_timeout"},
        )
    )

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.timeout
    assert result.applied is False
    assert result.reason == "modbus_command_timeout"
    assert result.correlation_id == cmd.correlation_id


# ---------------------------------------------------------------------------
# AC11.3 — Protocol error
# ---------------------------------------------------------------------------


async def test_send_command_protocol_error_returns_modbus_error_with_code() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_result(
        _make_protocol_result(
            cmd.correlation_id,
            status="error",
            raw_response={"error": "modbus_exception"},
        )
    )

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "modbus_error:modbus_exception"


# ---------------------------------------------------------------------------
# AC11.5 — Unsupported command type → TypeError
# ---------------------------------------------------------------------------


async def test_send_command_with_ev_command_raises_typeerror() -> None:
    adapter, _ = _make_adapter()
    cmd = SetEVChargingRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=3.0,
    )
    with pytest.raises(TypeError, match="BatteryAdapter does not support"):
        await adapter.send_command(cmd)


async def test_send_command_with_stop_command_raises_typeerror() -> None:
    adapter, _ = _make_adapter()
    cmd = StopEVChargingCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
    )
    with pytest.raises(TypeError, match="BatteryAdapter does not support"):
        await adapter.send_command(cmd)


# ---------------------------------------------------------------------------
# AC11.6 — Adapter internal error (RuntimeError mid-encoding) wrapped per AR16
# ---------------------------------------------------------------------------


async def test_send_command_protocol_layer_unexpected_exception_wrapped() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_exception(RuntimeError("library version mismatch"))

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "adapter_internal_error:RuntimeError"
    assert any(e["event"] == "adapter_internal_error" for e in logs)


# ---------------------------------------------------------------------------
# AC11.7 — CancelledError propagates without wrapping
# ---------------------------------------------------------------------------


async def test_send_command_cancellation_propagates_without_wrapping() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_cancellation()

    with pytest.raises(asyncio.CancelledError):
        await adapter.send_command(cmd)


# ---------------------------------------------------------------------------
# AC11.8 — correlation_id mismatch → failed/correlation_id_mismatch
# ---------------------------------------------------------------------------


async def test_send_command_correlation_id_mismatch_returns_failed() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    other = uuid.uuid4()
    fake.program_result(
        _make_protocol_result(
            cmd.correlation_id,
            status="acked",
            correlation_id_override=str(other),
        )
    )

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "correlation_id_mismatch"
    assert result.correlation_id == cmd.correlation_id  # NOT the mismatched one
    assert any(e["event"] == "adapter_correlation_mismatch" for e in logs)


async def test_send_command_unparseable_correlation_id_returns_failed() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_result(
        _make_protocol_result(
            cmd.correlation_id,
            status="acked",
            correlation_id_override="not-a-valid-uuid",
        )
    )

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.reason == "correlation_id_mismatch"
    # Same audit-log rigor as the mismatch case: a structured warning fires.
    assert any(e["event"] == "adapter_correlation_mismatch" for e in logs)


async def test_send_command_correlation_id_mismatch_log_sanitizes_control_chars() -> None:
    """Log injection defense: returned correlation id (untrusted input) is sanitized."""
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_result(
        _make_protocol_result(
            cmd.correlation_id,
            status="acked",
            # Newline + control char would otherwise allow downstream text-stream
            # log processors to be confused by a multi-line record.
            correlation_id_override="bad\nvalue\x07injected",
        )
    )

    with structlog.testing.capture_logs() as logs:
        await adapter.send_command(cmd)

    mismatch_logs = [e for e in logs if e["event"] == "adapter_correlation_mismatch"]
    assert mismatch_logs, "Expected mismatch log"
    received = mismatch_logs[0]["received"]
    assert "\n" not in received and "\r" not in received
    assert "\x07" not in received
    # The raw length is reported separately so an investigator can see truncation.
    assert mismatch_logs[0]["received_length"] == len("bad\nvalue\x07injected")


# ---------------------------------------------------------------------------
# AC11.9 — Register-map encoding error wrapped as register_map_encoding_error
# ---------------------------------------------------------------------------


async def test_send_command_register_map_encoding_error_wrapped() -> None:
    adapter, fake = _make_adapter()
    # 100 kW exceeds register-map's uint16 ceiling (65.535 kW)
    cmd = _charge_cmd(rate_kw=100.0)

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason.startswith("register_map_encoding_error:")
    # Protocol layer must NOT have been called
    assert fake.send_calls == 0


async def test_send_command_sub_watt_rate_wrapped_as_encoding_error() -> None:
    """0 < rate_kw < 0.0005 → register_map_encoding_error:sub_watt_precision.

    Documented in the contract status table: rounding to 0 W would silently
    turn a tiny non-zero setpoint into "stop". The encoder rejects so the
    caller's intent is unambiguous (send exactly 0.0 to stop).
    """
    adapter, fake = _make_adapter()
    cmd = _charge_cmd(rate_kw=0.0001)

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason.startswith("register_map_encoding_error:")
    assert "sub_watt_precision" in result.reason
    assert fake.send_calls == 0


async def test_send_command_inf_rate_wrapped_as_encoding_error_not_internal() -> None:
    """float('inf') raises OverflowError inside the encoder; wrapped as encoding error.

    Before the fix, OverflowError fell through to the generic AR16 catch and was
    misreported as adapter_internal_error:OverflowError. The encoder is the
    domain-correct error site, so the reason must be register_map_encoding_error.
    """
    adapter, fake = _make_adapter()
    cmd = _charge_cmd(rate_kw=float("inf"))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason.startswith("register_map_encoding_error:")
    assert "adapter_internal_error" not in result.reason
    assert fake.send_calls == 0


async def test_send_command_observed_state_is_none_on_non_success_paths() -> None:
    """Contract clause 7: observed_state is None on every path, not only success.

    Pinning this prevents a future helper from "helpfully" populating
    observed_state on timeout/error/encoding paths.
    """
    adapter, fake = _make_adapter()

    # Timeout
    cmd1 = _charge_cmd()
    fake.program_result(_make_protocol_result(cmd1.correlation_id, status="timeout"))
    assert (await adapter.send_command(cmd1)).observed_state is None

    # Error
    cmd2 = _charge_cmd()
    fake.program_result(_make_protocol_result(cmd2.correlation_id, status="error"))
    assert (await adapter.send_command(cmd2)).observed_state is None

    # Encoding error (no protocol call)
    cmd3 = _charge_cmd(rate_kw=100.0)
    assert (await adapter.send_command(cmd3)).observed_state is None

    # Internal error wrap
    cmd4 = _charge_cmd()
    fake.program_exception(RuntimeError("boom"))
    assert (await adapter.send_command(cmd4)).observed_state is None


async def test_send_command_raw_protocol_command_validation_error_wrapped_as_internal() -> None:
    """If the register map returns a malformed payload, RawProtocolCommand fails
    Pydantic validation. AR16 must catch it and produce an adapter_internal_error
    reason — never propagate the ValidationError to the control loop.
    """
    adapter, fake = _make_adapter()

    # Force the register map to return a non-dict, non-bytes payload. The
    # RawProtocolCommand constructor will raise ValidationError. Mutate via
    # the per-adapter attribute (a stand-in object shadows the shared register
    # map instance so other tests are not polluted).
    class _BadPayloadMap:
        def command_payload(self, _cmd: object) -> Any:
            return 12345  # int — not RawPayload

    adapter._register_map = _BadPayloadMap()  # type: ignore[assignment]

    cmd = _charge_cmd()

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason.startswith("adapter_internal_error:")
    assert any(e["event"] == "adapter_internal_error" for e in logs)
    # Protocol layer must NOT have been reached.
    assert fake.send_calls == 0


# ---------------------------------------------------------------------------
# AC8 — correlation_id round-trip is exact on success
# ---------------------------------------------------------------------------


async def test_send_command_correlation_id_round_trip_exact_uuid() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_result(_make_protocol_result(cmd.correlation_id, status="acked"))

    result = await adapter.send_command(cmd)

    assert result.correlation_id == cmd.correlation_id
    assert isinstance(result.correlation_id, uuid.UUID)
