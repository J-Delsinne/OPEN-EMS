"""Base types shared across all Modbus register map modules.

Kept in a separate private module to avoid circular imports when
register_maps/__init__.py imports concrete map classes that in turn need
MissingRegisterError and _require_reg.
"""

from __future__ import annotations

from typing import Any, Protocol

from open_ems.adapters.protocol import RawModbusState
from open_ems.core.commands import (
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
)
from open_ems.core.devices import BatteryState, InverterState


class MissingRegisterError(Exception):
    """Raised when a required register is absent from RawModbusState."""

    def __init__(self, address: int) -> None:
        self.address = address
        super().__init__(f"missing_register:{address}")


class InvalidRegisterValueError(Exception):
    """Raised when a register value falls outside its defined domain."""

    def __init__(self, address: int, value: int, detail: str = "") -> None:
        self.address = address
        self.value = value
        msg = f"invalid_register_value:{address}={value}"
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


def _require_reg(registers: dict[int, int], address: int) -> int:
    """Return the register value at *address* or raise MissingRegisterError."""
    value = registers.get(address)
    if value is None:
        raise MissingRegisterError(address)
    return value


class InverterRegisterMap(Protocol):
    """Structural contract for all inverter register map implementations."""

    def map_state(self, raw: RawModbusState) -> InverterState: ...


# A typed alias for the dict payload that BatteryAdapter forwards to
# ``ModbusTcpAdapter.send_raw_command``. The shape matches ``_WriteRegisterPayload``
# / ``_WriteRegistersPayload`` in ``adapters.modbus.tcp``; we keep the type as
# ``dict[str, Any]`` here to avoid importing private TCP types.
ModbusCommandPayload = dict[str, Any]


class BatteryRegisterMap(Protocol):
    """Structural contract for all battery register map implementations."""

    def map_state(self, raw: RawModbusState) -> BatteryState: ...

    def command_payload(
        self,
        cmd: SetBatteryChargeRateCommand | SetBatteryDischargeRateCommand,
    ) -> ModbusCommandPayload:
        """Encode a battery setpoint command into a Modbus write payload.

        The returned dict is forwarded to ``ModbusTcpAdapter.send_raw_command``
        as ``RawProtocolCommand.payload``. Implementations MUST raise
        ``ValueError`` when ``cmd.rate_kw`` is outside the model's encodable
        range; the calling adapter wraps that into
        ``CommandResult(status=failed, reason=f"register_map_encoding_error:...")``
        per the cross-adapter command contract.
        """
        ...
