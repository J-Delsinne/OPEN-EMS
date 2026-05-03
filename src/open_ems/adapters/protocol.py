"""Raw protocol adapter contracts.

This module intentionally stays below the domain device layer. It carries protocol
payloads and protocol failure states only; Epic 4 owns normalized device models.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBytes,
    StrictInt,
    StringConstraints,
    field_validator,
)

ProtocolStatus = Literal["sent", "acked", "timeout", "error"]
ConnectionStatus = Literal["connected", "disconnected"]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RawPayload = StrictBytes | dict[str, Any]
RawMessagePayload = dict[str, Any] | None


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be in UTC")
    return value


class RawProtocolCommand(BaseModel):
    """A protocol-level command payload, before any domain safety semantics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: NonEmptyStr
    device_id: NonEmptyStr
    command_name: NonEmptyStr
    payload: RawPayload


class ProtocolCommandResult(BaseModel):
    """Protocol-level command acknowledgement or failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: NonEmptyStr
    device_id: NonEmptyStr
    protocol_status: ProtocolStatus
    raw_response: RawPayload | None = None


class ProtocolDegradedState(BaseModel):
    """Recoverable protocol communication failure represented as data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    reason: NonEmptyStr
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_utc(value, "occurred_at")


class RawProtocolState(BaseModel):
    """Base class for raw protocol state containers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr


class RawModbusState(RawProtocolState):
    """Raw Modbus register values with no device-role or energy interpretation."""

    registers: dict[StrictInt, StrictInt]
    read_at: datetime

    @field_validator("registers")
    @classmethod
    def _registers_must_be_16_bit(cls, value: dict[int, int]) -> dict[int, int]:
        for register_address, register_value in value.items():
            if register_address < 0 or register_address > 65_535:
                raise ValueError("register addresses must be unsigned 16-bit integers")
            if register_value < 0 or register_value > 65_535:
                raise ValueError("register values must be unsigned 16-bit integers")
        return value

    @field_validator("read_at")
    @classmethod
    def _read_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_utc(value, "read_at")


class RawOCPPState(RawProtocolState):
    """Raw OCPP 1.6 connection and message payload state."""

    charge_point_id: NonEmptyStr
    last_status_notification: RawMessagePayload = None
    last_heartbeat_at: datetime | None = None
    connection_status: ConnectionStatus
    last_call_result: RawMessagePayload = None
    last_call_error: RawMessagePayload = None
    last_meter_values_at: datetime | None = None
    last_meter_values_power_kw: float | None = None

    @field_validator("last_heartbeat_at")
    @classmethod
    def _last_heartbeat_at_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value, "last_heartbeat_at")

    @field_validator("last_meter_values_at")
    @classmethod
    def _last_meter_values_at_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value, "last_meter_values_at")


class RawDSMRState(RawProtocolState):
    """Raw parsed DSMR telegram fields with receipt timestamp."""

    telegram_fields: dict[str, Any]
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def _received_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_utc(value, "received_at")


@runtime_checkable
class ProtocolAdapter(Protocol):
    """Structural contract implemented by all raw protocol adapters."""

    async def get_raw_state(self) -> RawProtocolState | ProtocolDegradedState:
        """Read the current raw protocol state or protocol degraded state."""

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        """Send a raw protocol command and return protocol-level acknowledgement."""
