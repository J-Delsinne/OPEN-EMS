"""Async Modbus TCP raw protocol adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, Self, cast

import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException

from open_ems.adapters.protocol import (
    ProtocolCommandResult,
    ProtocolDegradedState,
    RawModbusState,
    RawProtocolCommand,
)

logger = structlog.get_logger(__name__)

RegisterTable = Literal["holding", "input"]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RegisterAddress = Annotated[StrictInt, Field(ge=0, le=65_535)]
RegisterCount = Annotated[StrictInt, Field(ge=1, le=125)]
RegisterValue = Annotated[StrictInt, Field(ge=0, le=65_535)]
ModbusDeviceId = Annotated[StrictInt, Field(ge=1, le=247)]
Port = Annotated[StrictInt, Field(ge=1, le=65_535)]

_RECOVERABLE_EXCEPTIONS = (OSError, ModbusException, ConnectionException)


class ModbusRegisterRange(BaseModel):
    """A raw Modbus register range to read from one table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: RegisterTable
    start_address: RegisterAddress
    count: RegisterCount

    @model_validator(mode="after")
    def _range_must_fit_in_16_bit_address_space(self) -> Self:
        if self.start_address + self.count - 1 > 65_535:
            raise ValueError("register range must fit in unsigned 16-bit address space")
        return self


class ModbusTcpAdapterConfig(BaseModel):
    """Configuration for one Modbus TCP adapter instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    host: NonEmptyStr
    port: Port = 502
    modbus_device_id: ModbusDeviceId = 1
    registers: tuple[ModbusRegisterRange, ...] = Field(min_length=1)
    timeout_s: float = Field(default=10.0, gt=0, le=10.0)
    reconnect_delay_s: float = Field(default=0.1, ge=0)
    reconnect_delay_max_s: float = Field(default=30.0, ge=0)

    @field_validator("timeout_s", "reconnect_delay_s", "reconnect_delay_max_s", mode="before")
    @classmethod
    def _float_fields_must_be_numeric(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("value must be a numeric timeout or delay")
        return value

    @model_validator(mode="after")
    def _register_outputs_must_not_collide(self) -> Self:
        seen_addresses: set[int] = set()
        for register_range in self.registers:
            for address in range(
                register_range.start_address,
                register_range.start_address + register_range.count,
            ):
                if address in seen_addresses:
                    raise ValueError("register ranges must not produce duplicate raw addresses")
                seen_addresses.add(address)
        if self.reconnect_delay_max_s < self.reconnect_delay_s:
            raise ValueError("reconnect_delay_max_s must be >= reconnect_delay_s")
        return self


class _AsyncModbusClient(Protocol):
    @property
    def connected(self) -> bool: ...

    async def connect(self) -> bool: ...

    def close(self) -> None: ...

    def read_holding_registers(
        self,
        address: int,
        *,
        count: int = 1,
        device_id: int = 1,
    ) -> Awaitable[object]: ...

    def read_input_registers(
        self,
        address: int,
        *,
        count: int = 1,
        device_id: int = 1,
    ) -> Awaitable[object]: ...

    def write_register(
        self,
        address: int,
        value: int,
        *,
        device_id: int = 1,
    ) -> Awaitable[object]: ...

    def write_registers(
        self,
        address: int,
        values: list[int],
        *,
        device_id: int = 1,
    ) -> Awaitable[object]: ...


class ModbusClientFactory(Protocol):
    def __call__(self, config: ModbusTcpAdapterConfig) -> _AsyncModbusClient: ...


class _WriteRegisterPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["write_register"]
    address: RegisterAddress
    value: RegisterValue
    modbus_device_id: ModbusDeviceId | None = None


class _WriteRegistersPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["write_registers"]
    address: RegisterAddress
    values: list[RegisterValue] = Field(min_length=1, max_length=123)
    modbus_device_id: ModbusDeviceId | None = None

    @model_validator(mode="after")
    def _write_range_must_fit_in_16_bit_address_space(self) -> Self:
        if self.address + len(self.values) - 1 > 65_535:
            raise ValueError("write_registers range must fit in unsigned 16-bit address space")
        return self


class _ModbusTimeoutError(Exception):
    """Recoverable timeout used internally for boundary translation."""


class _ModbusCommunicationError(Exception):
    """Recoverable Modbus communication failure used internally."""


class ModbusTcpAdapter:
    """Raw Modbus TCP adapter implementing the ProtocolAdapter contract."""

    def __init__(
        self,
        config: ModbusTcpAdapterConfig,
        *,
        client_factory: ModbusClientFactory | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory or _create_client
        self._client: _AsyncModbusClient | None = None
        self._lock = asyncio.Lock()

    async def get_raw_state(self) -> RawModbusState | ProtocolDegradedState:
        """Read configured raw Modbus registers or return a typed degraded state."""

        async with self._lock:
            try:
                client = await self._ensure_connected("read")
                registers: dict[int, int] = {}
                for register_range in self.config.registers:
                    response = await self._read_range(client, register_range)
                    if _response_is_error(response):
                        self._log_protocol_error("read", "modbus_exception")
                        return self._degraded("modbus_error")

                    values = _extract_register_values(response, register_range.count)
                    if values is None:
                        self._log_protocol_error("read", "unexpected_register_response")
                        return self._degraded("modbus_error")

                    for offset, register_value in enumerate(values):
                        registers[register_range.start_address + offset] = register_value

                return RawModbusState(
                    device_id=self.config.device_id,
                    registers=registers,
                    read_at=datetime.now(UTC),
                )
            except _ModbusTimeoutError:
                return self._degraded("modbus_timeout")
            except _ModbusCommunicationError:
                return self._degraded("modbus_error")

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        """Send a supported raw Modbus write command."""

        payload = _parse_write_payload(command.payload)
        if payload is None:
            self._log_protocol_error("send_raw_command", "invalid_payload")
            return _command_error(command, "invalid_modbus_command")

        async with self._lock:
            try:
                client = await self._ensure_connected(payload.operation)
                response = await self._write_payload(client, payload)
                if _response_is_error(response):
                    self._log_protocol_error(payload.operation, "modbus_exception")
                    return _command_error(command, "modbus_exception")

                return ProtocolCommandResult(
                    correlation_id=command.correlation_id,
                    device_id=command.device_id,
                    protocol_status="acked",
                    raw_response=_payload_ack_response(payload, self.config.modbus_device_id),
                )
            except _ModbusTimeoutError:
                return ProtocolCommandResult(
                    correlation_id=command.correlation_id,
                    device_id=command.device_id,
                    protocol_status="timeout",
                    raw_response={"error": "modbus_timeout"},
                )
            except _ModbusCommunicationError:
                return _command_error(command, "modbus_error")

    async def close(self) -> None:
        """Close the underlying client for tests and future shutdown orchestration."""

        async with self._lock:
            self._discard_client()

    async def _ensure_connected(self, operation: str) -> _AsyncModbusClient:
        client = self._client
        if client is None:
            try:
                client = self._client_factory(self.config)
            except _RECOVERABLE_EXCEPTIONS as exc:
                self._log_connection_failure(operation, type(exc).__name__)
                raise _ModbusCommunicationError from exc
            self._client = client

        if client.connected:
            return client

        try:
            connected = await asyncio.wait_for(client.connect(), timeout=self.config.timeout_s)
        except TimeoutError as exc:
            self._log_timeout(operation)
            self._discard_client()
            raise _ModbusTimeoutError from exc
        except _RECOVERABLE_EXCEPTIONS as exc:
            self._log_connection_failure(operation, type(exc).__name__)
            self._discard_client()
            raise _ModbusCommunicationError from exc

        if not connected:
            self._log_connection_failure(operation, "connect_returned_false")
            self._discard_client()
            raise _ModbusCommunicationError
        return client

    async def _read_range(
        self,
        client: _AsyncModbusClient,
        register_range: ModbusRegisterRange,
    ) -> object:
        if register_range.table == "holding":
            operation = "read_holding_registers"
            read_call = client.read_holding_registers(
                register_range.start_address,
                count=register_range.count,
                device_id=self.config.modbus_device_id,
            )
        else:
            operation = "read_input_registers"
            read_call = client.read_input_registers(
                register_range.start_address,
                count=register_range.count,
                device_id=self.config.modbus_device_id,
            )
        return await self._await_modbus(read_call, operation)

    async def _write_payload(
        self,
        client: _AsyncModbusClient,
        payload: _WriteRegisterPayload | _WriteRegistersPayload,
    ) -> object:
        target_device_id = payload.modbus_device_id or self.config.modbus_device_id
        if payload.operation == "write_register":
            return await self._await_modbus(
                client.write_register(
                    payload.address,
                    payload.value,
                    device_id=target_device_id,
                ),
                payload.operation,
            )
        return await self._await_modbus(
            client.write_registers(
                payload.address,
                payload.values,
                device_id=target_device_id,
            ),
            payload.operation,
        )

    async def _await_modbus(self, awaitable: Awaitable[object], operation: str) -> object:
        try:
            return await asyncio.wait_for(awaitable, timeout=self.config.timeout_s)
        except TimeoutError as exc:
            self._log_timeout(operation)
            self._discard_client()
            raise _ModbusTimeoutError from exc
        except _RECOVERABLE_EXCEPTIONS as exc:
            self._log_connection_failure(operation, type(exc).__name__)
            self._discard_client()
            raise _ModbusCommunicationError from exc

    def _discard_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            client.close()

    def _degraded(self, reason: str) -> ProtocolDegradedState:
        return ProtocolDegradedState(
            device_id=self.config.device_id,
            reason=reason,
            occurred_at=datetime.now(UTC),
        )

    def _log_timeout(self, operation: str) -> None:
        logger.warning(
            "adapter_timeout",
            component="modbus",
            device_id=self.config.device_id,
            host=self.config.host,
            port=self.config.port,
            operation=operation,
            timeout_s=self.config.timeout_s,
        )

    def _log_connection_failure(self, operation: str, reason: str) -> None:
        logger.warning(
            "adapter_connection_failed",
            component="modbus",
            device_id=self.config.device_id,
            host=self.config.host,
            port=self.config.port,
            operation=operation,
            reason=reason,
        )

    def _log_protocol_error(self, operation: str, reason: str) -> None:
        logger.warning(
            "adapter_protocol_error",
            component="modbus",
            device_id=self.config.device_id,
            host=self.config.host,
            port=self.config.port,
            operation=operation,
            reason=reason,
        )


def _create_client(config: ModbusTcpAdapterConfig) -> _AsyncModbusClient:
    return cast(
        _AsyncModbusClient,
        AsyncModbusTcpClient(
            config.host,
            port=config.port,
            timeout=config.timeout_s,
            retries=0,
            reconnect_delay=config.reconnect_delay_s,
            reconnect_delay_max=config.reconnect_delay_max_s,
        ),
    )


def _response_is_error(response: object) -> bool:
    is_error = getattr(response, "isError", None)
    if callable(is_error):
        return bool(is_error())
    if isinstance(is_error, bool):
        return is_error
    return False


def _extract_register_values(response: object, expected_count: int) -> list[int] | None:
    raw_registers = getattr(response, "registers", None)
    if not isinstance(raw_registers, Sequence) or isinstance(raw_registers, str | bytes):
        return None
    if len(raw_registers) != expected_count:
        return None

    values: list[int] = []
    for raw_value in raw_registers:
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            return None
        if raw_value < 0 or raw_value > 65_535:
            return None
        values.append(raw_value)
    return values


def _parse_write_payload(
    payload: object,
) -> _WriteRegisterPayload | _WriteRegistersPayload | None:
    if not isinstance(payload, dict):
        return None
    operation = payload.get("operation")
    try:
        if operation == "write_register":
            return _WriteRegisterPayload.model_validate(payload)
        if operation == "write_registers":
            return _WriteRegistersPayload.model_validate(payload)
    except ValidationError:
        return None
    return None


def _payload_ack_response(
    payload: _WriteRegisterPayload | _WriteRegistersPayload,
    default_modbus_device_id: int,
) -> dict[str, Any]:
    target_device_id = payload.modbus_device_id or default_modbus_device_id
    if payload.operation == "write_register":
        return {
            "operation": payload.operation,
            "address": payload.address,
            "value": payload.value,
            "modbus_device_id": target_device_id,
        }
    return {
        "operation": payload.operation,
        "address": payload.address,
        "values": payload.values,
        "modbus_device_id": target_device_id,
    }


def _command_error(command: RawProtocolCommand, error: str) -> ProtocolCommandResult:
    return ProtocolCommandResult(
        correlation_id=command.correlation_id,
        device_id=command.device_id,
        protocol_status="error",
        raw_response={"error": error},
    )
