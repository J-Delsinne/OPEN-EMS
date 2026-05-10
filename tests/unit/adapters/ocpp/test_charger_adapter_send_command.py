"""Unit tests for ``EVChargerAdapter.send_command`` (Story 9.0).

Covers AC4, AC5, AC6, AC8, AC9, AC11.1–AC11.10 (EV charger scope) including the
unique OCPP-only cases: rejected (response.status="Rejected"), no-active-OCPP-
transaction stop, and SetChargingProfile payload composition.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import structlog.testing

from open_ems.adapters.ocpp.charger_adapter import EVChargerAdapter
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
# Fake OCPPChargerAdapter — programmable per test
# ---------------------------------------------------------------------------


class FakeOCPPProtocolAdapter:
    """Captures send_raw_command calls and exposes adapter-shaped accessors."""

    def __init__(self) -> None:
        self.active_transaction_id: int | None = 42
        self._next_profile_id: int = 7
        self._next_result: ProtocolCommandResult | None = None
        self._next_exc: BaseException | None = None
        self._cancel: bool = False
        self.last_command: RawProtocolCommand | None = None
        self.send_calls: int = 0
        self.profile_reservations: int = 0

    def reserve_charging_profile_id(self) -> int:
        self.profile_reservations += 1
        pid = self._next_profile_id
        self._next_profile_id += 1
        return pid

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter() -> tuple[EVChargerAdapter, FakeOCPPProtocolAdapter]:
    fake = FakeOCPPProtocolAdapter()
    adapter = EVChargerAdapter(
        device_id="ev-001",
        protocol_adapter=fake,  # type: ignore[arg-type]
    )
    return adapter, fake


def _rate_cmd(rate_kw: float = 7.0) -> SetEVChargingRateCommand:
    return SetEVChargingRateCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


def _stop_cmd() -> StopEVChargingCommand:
    return StopEVChargingCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
    )


def _accepted_result(
    correlation_id: uuid.UUID, *, correlation_id_override: str | None = None
) -> ProtocolCommandResult:
    return ProtocolCommandResult(
        correlation_id=correlation_id_override or str(correlation_id),
        device_id="ev-001",
        protocol_status="acked",
        raw_response={"status": "Accepted"},
    )


def _rejected_result(correlation_id: uuid.UUID) -> ProtocolCommandResult:
    return ProtocolCommandResult(
        correlation_id=str(correlation_id),
        device_id="ev-001",
        protocol_status="acked",
        raw_response={"status": "Rejected"},
    )


def _timeout_result(correlation_id: uuid.UUID) -> ProtocolCommandResult:
    return ProtocolCommandResult(
        correlation_id=str(correlation_id),
        device_id="ev-001",
        protocol_status="timeout",
        raw_response={"error": "ocpp_timeout"},
    )


def _error_result(
    correlation_id: uuid.UUID, error_code: str = "NotImplemented"
) -> ProtocolCommandResult:
    return ProtocolCommandResult(
        correlation_id=str(correlation_id),
        device_id="ev-001",
        protocol_status="error",
        raw_response={"error_code": error_code, "error_description": "no"},
    )


# ---------------------------------------------------------------------------
# AC11.1 — Success path: SetChargingProfile + Accepted
# ---------------------------------------------------------------------------


async def test_set_rate_success_returns_success_with_ok_reason() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd(rate_kw=7.0)
    fake.program_result(_accepted_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.success
    assert result.applied is True
    assert result.reason == "ok"
    assert result.correlation_id == cmd.correlation_id
    assert result.observed_state is None

    sent = fake.last_command
    assert sent is not None
    assert sent.command_name == "SetChargingProfile"
    assert isinstance(sent.payload, dict)
    payload: dict[str, Any] = sent.payload  # type: ignore[assignment]
    assert payload["connector_id"] == 1
    profile = payload["cs_charging_profiles"]
    assert profile["charging_profile_purpose"] == "TxProfile"
    assert profile["charging_profile_kind"] == "Absolute"
    assert profile["stack_level"] == 0
    assert profile["charging_profile_id"] == 7  # first reservation
    schedule = profile["charging_schedule"]
    assert schedule["charging_rate_unit"] == "W"
    assert schedule["charging_schedule_period"] == [{"start_period": 0, "limit": 7000}]
    assert fake.profile_reservations == 1


async def test_set_rate_zero_kw_emits_zero_limit() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd(rate_kw=0.0)
    fake.program_result(_accepted_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.success
    profile = fake.last_command.payload["cs_charging_profiles"]  # type: ignore[index]
    assert profile["charging_schedule"]["charging_schedule_period"][0]["limit"] == 0


# ---------------------------------------------------------------------------
# AC11.4 — OCPP-only: response status="Rejected" → CommandResult(rejected)
# ---------------------------------------------------------------------------


async def test_set_rate_rejected_response_returns_rejected() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_result(_rejected_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.rejected
    assert result.applied is False
    assert result.reason == "ocpp_rejected:Rejected"


async def test_set_rate_acked_with_not_supported_is_failed_unsupported() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_result(
        ProtocolCommandResult(
            correlation_id=str(cmd.correlation_id),
            device_id="ev-001",
            protocol_status="acked",
            raw_response={"status": "NotSupported"},
        )
    )

    result = await adapter.send_command(cmd)

    # NotSupported is a permanent capability error; map to failed/ocpp_unsupported
    # so RetryPolicy does not waste retries.
    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "ocpp_unsupported:NotSupported"


async def test_set_rate_acked_with_missing_status_is_failed_unsupported() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_result(
        ProtocolCommandResult(
            correlation_id=str(cmd.correlation_id),
            device_id="ev-001",
            protocol_status="acked",
            raw_response={},  # status missing entirely
        )
    )

    result = await adapter.send_command(cmd)

    # Missing/non-string status is treated as a permanent capability error
    # (same bucket as NotSupported) so RetryPolicy does not retry on a
    # malformed response.
    assert result.status is CommandStatus.failed
    assert result.reason == "ocpp_unsupported:Unknown"


# ---------------------------------------------------------------------------
# AC11.2 — Timeout
# ---------------------------------------------------------------------------


async def test_set_rate_timeout_returns_ocpp_command_timeout() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_result(_timeout_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.timeout
    assert result.applied is False
    assert result.reason == "ocpp_command_timeout"


# ---------------------------------------------------------------------------
# AC11.3 — Protocol error
# ---------------------------------------------------------------------------


async def test_set_rate_protocol_error_returns_ocpp_error_with_code() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_result(_error_result(cmd.correlation_id, error_code="GenericError"))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "ocpp_error:GenericError"


# ---------------------------------------------------------------------------
# AC11.5 — Unsupported command type → TypeError
# ---------------------------------------------------------------------------


async def test_send_command_with_battery_charge_command_raises_typeerror() -> None:
    adapter, _ = _make_adapter()
    cmd = SetBatteryChargeRateCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=3.0,
    )
    with pytest.raises(TypeError, match="EVChargerAdapter does not support"):
        await adapter.send_command(cmd)


async def test_send_command_with_battery_discharge_command_raises_typeerror() -> None:
    adapter, _ = _make_adapter()
    cmd = SetBatteryDischargeRateCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=3.0,
    )
    with pytest.raises(TypeError, match="EVChargerAdapter does not support"):
        await adapter.send_command(cmd)


# ---------------------------------------------------------------------------
# AC11.6 — Adapter internal error wrapped per AR16
# ---------------------------------------------------------------------------


async def test_send_command_protocol_layer_unexpected_exception_wrapped() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_exception(RuntimeError("library version mismatch"))

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.reason == "adapter_internal_error:RuntimeError"
    assert any(e["event"] == "adapter_internal_error" for e in logs)


# ---------------------------------------------------------------------------
# AC11.7 — CancelledError propagates
# ---------------------------------------------------------------------------


async def test_send_command_cancellation_propagates_without_wrapping() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_cancellation()

    with pytest.raises(asyncio.CancelledError):
        await adapter.send_command(cmd)


# ---------------------------------------------------------------------------
# AC11.8 — correlation_id mismatch
# ---------------------------------------------------------------------------


async def test_send_command_correlation_id_mismatch_returns_failed() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    other = uuid.uuid4()
    fake.program_result(_accepted_result(cmd.correlation_id, correlation_id_override=str(other)))

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.reason == "correlation_id_mismatch"
    assert result.correlation_id == cmd.correlation_id
    assert any(e["event"] == "adapter_correlation_mismatch" for e in logs)


# ---------------------------------------------------------------------------
# AC11.10 — StopEVChargingCommand without active transaction
# ---------------------------------------------------------------------------


async def test_stop_command_without_active_transaction_returns_failed() -> None:
    adapter, fake = _make_adapter()
    fake.active_transaction_id = None
    cmd = _stop_cmd()

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "no_active_ocpp_transaction"
    # Protocol layer must NOT have been called — avoids the stale-id stop hazard
    assert fake.send_calls == 0


async def test_stop_command_with_active_transaction_dispatches_remote_stop() -> None:
    adapter, fake = _make_adapter()
    fake.active_transaction_id = 42
    cmd = _stop_cmd()
    fake.program_result(_accepted_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.success
    sent = fake.last_command
    assert sent is not None
    assert sent.command_name == "RemoteStopTransaction"
    assert sent.payload == {"transaction_id": 42}


async def test_stop_command_acked_rejected_returns_rejected() -> None:
    adapter, fake = _make_adapter()
    fake.active_transaction_id = 42
    cmd = _stop_cmd()
    fake.program_result(_rejected_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.rejected
    assert result.reason == "ocpp_rejected:Rejected"


# ---------------------------------------------------------------------------
# correlation_id round-trip — exact UUID equality on success
# ---------------------------------------------------------------------------


async def test_send_command_correlation_id_round_trip_exact_uuid() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_result(_accepted_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.correlation_id == cmd.correlation_id
    assert isinstance(result.correlation_id, uuid.UUID)
