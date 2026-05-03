"""Base types shared across all Modbus register map modules.

Kept in a separate private module to avoid circular imports when
register_maps/__init__.py imports concrete map classes that in turn need
MissingRegisterError and _require_reg.
"""

from __future__ import annotations

from typing import Protocol

from open_ems.adapters.protocol import RawModbusState
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


class BatteryRegisterMap(Protocol):
    """Structural contract for all battery register map implementations."""

    def map_state(self, raw: RawModbusState) -> BatteryState: ...
