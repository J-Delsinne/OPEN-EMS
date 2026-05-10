"""Cross-adapter consistency tests (Story 9.0 AC11).

Asserts that every controllable adapter (Battery, EV Charger) honors the same
shape of CommandResult for each protocol outcome. Inverter is read-only and is
exercised separately for its TypeError-only stance (AC3).

Reason-prefix invariant (AC5):
- Success: ``reason == "ok"`` exactly
- Timeout: ``reason == f"{protocol}_command_timeout"``
- Error:   ``reason`` starts with ``f"{protocol}_"`` (concrete code suffix)
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest

from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.ocpp.charger_adapter import EVChargerAdapter
from open_ems.adapters.protocol import (
    ProtocolCommandResult,
    RawProtocolCommand,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandStatus,
    DeviceCommand,
    SetBatteryChargeRateCommand,
    SetEVChargingRateCommand,
)
from open_ems.core.devices import DeviceRole

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeModbus:
    def __init__(self) -> None:
        self.next_result: ProtocolCommandResult | None = None

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        assert self.next_result is not None
        return self.next_result

    async def close(self) -> None:
        pass


class _FakeOCPP:
    def __init__(self) -> None:
        self.next_result: ProtocolCommandResult | None = None
        self.active_transaction_id: int | None = 99
        self._next_profile_id: int = 1

    def reserve_charging_profile_id(self) -> int:
        pid = self._next_profile_id
        self._next_profile_id += 1
        return pid

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        assert self.next_result is not None
        return self.next_result


# ---------------------------------------------------------------------------
# Adapter setup
# ---------------------------------------------------------------------------


def _battery_setup() -> tuple[BatteryAdapter, _FakeModbus, DeviceCommand, str]:
    fake = _FakeModbus()
    adapter = BatteryAdapter("bat-1", fake, "byd_hvs_v1")  # type: ignore[arg-type]
    cmd = SetBatteryChargeRateCommand(
        device_id="bat-1",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )
    return adapter, fake, cmd, "modbus"


def _ev_setup() -> tuple[EVChargerAdapter, _FakeOCPP, DeviceCommand, str]:
    fake = _FakeOCPP()
    adapter = EVChargerAdapter("ev-1", fake)  # type: ignore[arg-type]
    cmd = SetEVChargingRateCommand(
        device_id="ev-1",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=3.0,
    )
    return adapter, fake, cmd, "ocpp"


ADAPTER_SETUPS: list[tuple[str, Callable[[], tuple[Any, Any, DeviceCommand, str]]]] = [
    ("battery", _battery_setup),
    ("ev_charger", _ev_setup),
]


def _success_payload(protocol: str) -> dict[str, Any]:
    if protocol == "modbus":
        return {"operation": "write_register", "address": 110, "value": 1000}
    return {"status": "Accepted"}


# ---------------------------------------------------------------------------
# Cross-adapter parametrized tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name, setup", ADAPTER_SETUPS)
async def test_success_path_returns_success_applied_true_reason_ok(
    name: str, setup: Callable[[], tuple[Any, Any, DeviceCommand, str]]
) -> None:
    adapter, fake, cmd, protocol = setup()
    fake.next_result = ProtocolCommandResult(
        correlation_id=str(cmd.correlation_id),
        device_id=cmd.device_id,
        protocol_status="acked",
        raw_response=_success_payload(protocol),
    )

    result = await adapter.send_command(cmd)

    # AC5: applied=True ⇔ status=success (Pydantic invariant)
    assert result.status is CommandStatus.success
    assert result.applied is True
    assert result.reason == "ok"
    assert result.observed_state is None  # contract clause 7
    assert result.correlation_id == cmd.correlation_id


@pytest.mark.parametrize("name, setup", ADAPTER_SETUPS)
async def test_timeout_reason_has_protocol_command_timeout_form(
    name: str, setup: Callable[[], tuple[Any, Any, DeviceCommand, str]]
) -> None:
    adapter, fake, cmd, protocol = setup()
    fake.next_result = ProtocolCommandResult(
        correlation_id=str(cmd.correlation_id),
        device_id=cmd.device_id,
        protocol_status="timeout",
        raw_response={"error": f"{protocol}_timeout"},
    )

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.timeout
    assert result.applied is False
    assert result.reason == f"{protocol}_command_timeout"


@pytest.mark.parametrize("name, setup", ADAPTER_SETUPS)
async def test_error_reason_starts_with_protocol_prefix(
    name: str, setup: Callable[[], tuple[Any, Any, DeviceCommand, str]]
) -> None:
    adapter, fake, cmd, protocol = setup()
    fake.next_result = ProtocolCommandResult(
        correlation_id=str(cmd.correlation_id),
        device_id=cmd.device_id,
        protocol_status="error",
        raw_response={"error": "some_failure", "error_code": "some_failure"},
    )

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason.startswith(f"{protocol}_")


@pytest.mark.parametrize("name, setup", ADAPTER_SETUPS)
async def test_correlation_id_mismatch_uses_canonical_reason(
    name: str, setup: Callable[[], tuple[Any, Any, DeviceCommand, str]]
) -> None:
    adapter, fake, cmd, protocol = setup()
    other = uuid.uuid4()
    fake.next_result = ProtocolCommandResult(
        correlation_id=str(other),
        device_id=cmd.device_id,
        protocol_status="acked",
        raw_response=_success_payload(protocol),
    )

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "correlation_id_mismatch"
    assert result.correlation_id == cmd.correlation_id


@pytest.mark.parametrize("name, setup", ADAPTER_SETUPS)
async def test_pydantic_invariant_applied_iff_success_holds(
    name: str, setup: Callable[[], tuple[Any, Any, DeviceCommand, str]]
) -> None:
    """Each non-success path must report applied=False; success must report applied=True."""
    adapter, fake, cmd, protocol = setup()
    for protocol_status, raw_response in [
        ("acked", _success_payload(protocol)),
        ("timeout", {"error": "x"}),
        ("error", {"error": "x", "error_code": "x"}),
    ]:
        fake.next_result = ProtocolCommandResult(
            correlation_id=str(cmd.correlation_id),
            device_id=cmd.device_id,
            protocol_status=protocol_status,  # type: ignore[arg-type]
            raw_response=raw_response,
        )
        result = await adapter.send_command(cmd)
        if result.status is CommandStatus.success:
            assert result.applied is True
        else:
            assert result.applied is False


def test_pydantic_invariant_rejects_success_with_applied_false() -> None:
    """Direct construction: CommandResult(status=success, applied=False) MUST raise.

    The above behavioral test only proves adapter outputs are consistent; this
    test proves the Pydantic model itself enforces the biconditional, so any
    future code path that tries to construct an inconsistent CommandResult will
    fail at the model boundary, not at a downstream consumer.
    """
    import uuid as _uuid

    from open_ems.core.commands import CommandResult, CommandStatus

    base_kwargs: dict[str, Any] = {
        "correlation_id": _uuid.uuid4(),
        "device_id": "bat-001",
    }

    # success + applied=False is forbidden
    with pytest.raises(ValueError):
        CommandResult(
            **base_kwargs,
            status=CommandStatus.success,
            applied=False,
            reason="ok",
        )

    # failed + applied=True is forbidden
    with pytest.raises(ValueError):
        CommandResult(
            **base_kwargs,
            status=CommandStatus.failed,
            applied=True,
            reason="boom",
        )

    # timeout + applied=True is forbidden
    with pytest.raises(ValueError):
        CommandResult(
            **base_kwargs,
            status=CommandStatus.timeout,
            applied=True,
            reason="boom",
        )

    # rejected + applied=True is forbidden
    with pytest.raises(ValueError):
        CommandResult(
            **base_kwargs,
            status=CommandStatus.rejected,
            applied=True,
            reason="boom",
        )


# ---------------------------------------------------------------------------
# Inverter — TypeError-only stance is its own consistency requirement
# ---------------------------------------------------------------------------


async def test_inverter_send_command_consistently_typeerrors_for_all_command_types() -> None:
    from open_ems.adapters.modbus.inverter_adapter import InverterAdapter
    from open_ems.core.commands import (
        SetBatteryChargeRateCommand as _Charge,
    )
    from open_ems.core.commands import (
        SetBatteryDischargeRateCommand as _Discharge,
    )
    from open_ems.core.commands import (
        SetEVChargingRateCommand as _EV,
    )
    from open_ems.core.commands import (
        StopEVChargingCommand as _Stop,
    )

    class _NoOpModbus:
        async def get_raw_state(self) -> Any:  # pragma: no cover
            raise AssertionError

        async def send_raw_command(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

        async def close(self) -> None:
            pass

    adapter = InverterAdapter("inv-1", _NoOpModbus(), "fronius_gen24_v1")  # type: ignore[arg-type]
    common_kwargs: dict[str, Any] = {
        "device_id": "inv-1",
        "device_role": DeviceRole.inverter,
        "origin": CommandOrigin.decision_engine,
    }
    for cmd in (
        _Charge(rate_kw=1.0, **common_kwargs),
        _Discharge(rate_kw=1.0, **common_kwargs),
        _EV(rate_kw=1.0, **common_kwargs),
        _Stop(**common_kwargs),
    ):
        with pytest.raises(TypeError):
            await adapter.send_command(cmd)
