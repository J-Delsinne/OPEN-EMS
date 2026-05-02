import asyncio
from collections.abc import Callable
from typing import Any

import pytest
import structlog.testing
from pydantic import ValidationError

from open_ems.adapters import (
    ProtocolAdapter,
    ProtocolCommandResult,
    ProtocolDegradedState,
    RawModbusState,
    RawProtocolCommand,
)
from open_ems.adapters.modbus import (
    ModbusRegisterRange,
    ModbusTcpAdapter,
    ModbusTcpAdapterConfig,
)


class FakeModbusResponse:
    def __init__(self, registers: list[Any] | None = None, is_error: bool = False) -> None:
        self.registers = registers
        self._is_error = is_error

    def isError(self) -> bool:
        return self._is_error


class FakeAsyncModbusClient:
    def __init__(self) -> None:
        self.connected = False
        self.connect_calls = 0
        self.close_calls = 0
        self.connect_result = True
        self.connect_delay_s = 0.0
        self.connect_error: BaseException | None = None
        self.read_delay_s = 0.0
        self.write_delay_s = 0.0
        self.read_error: BaseException | None = None
        self.write_error: BaseException | None = None
        self.holding_response = FakeModbusResponse(registers=[100, 101])
        self.input_response = FakeModbusResponse(registers=[200])
        self.write_response = FakeModbusResponse()
        self.calls: list[tuple[str, int, int | list[int], int]] = []

    async def connect(self) -> bool:
        self.connect_calls += 1
        if self.connect_delay_s:
            await asyncio.sleep(self.connect_delay_s)
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = self.connect_result
        return self.connect_result

    def close(self) -> None:
        self.close_calls += 1
        self.connected = False

    async def read_holding_registers(
        self,
        address: int,
        *,
        count: int,
        device_id: int,
    ) -> FakeModbusResponse:
        self.calls.append(("read_holding_registers", address, count, device_id))
        if self.read_delay_s:
            await asyncio.sleep(self.read_delay_s)
        if self.read_error is not None:
            raise self.read_error
        return self.holding_response

    async def read_input_registers(
        self,
        address: int,
        *,
        count: int,
        device_id: int,
    ) -> FakeModbusResponse:
        self.calls.append(("read_input_registers", address, count, device_id))
        if self.read_delay_s:
            await asyncio.sleep(self.read_delay_s)
        if self.read_error is not None:
            raise self.read_error
        return self.input_response

    async def write_register(
        self,
        address: int,
        value: int,
        *,
        device_id: int,
    ) -> FakeModbusResponse:
        self.calls.append(("write_register", address, value, device_id))
        if self.write_delay_s:
            await asyncio.sleep(self.write_delay_s)
        if self.write_error is not None:
            raise self.write_error
        return self.write_response

    async def write_registers(
        self,
        address: int,
        values: list[int],
        *,
        device_id: int,
    ) -> FakeModbusResponse:
        self.calls.append(("write_registers", address, values, device_id))
        if self.write_delay_s:
            await asyncio.sleep(self.write_delay_s)
        if self.write_error is not None:
            raise self.write_error
        return self.write_response


ClientFactory = Callable[[], FakeAsyncModbusClient]


def make_config(
    *,
    timeout_s: float = 1.0,
    registers: tuple[ModbusRegisterRange, ...] | None = None,
) -> ModbusTcpAdapterConfig:
    return ModbusTcpAdapterConfig(
        device_id="modbus-1",
        host="192.0.2.10",
        port=502,
        modbus_device_id=3,
        registers=registers
        or (
            ModbusRegisterRange(table="holding", start_address=40001, count=2),
            ModbusRegisterRange(table="input", start_address=30001, count=1),
        ),
        timeout_s=timeout_s,
    )


def make_adapter(
    client: FakeAsyncModbusClient,
    *,
    config: ModbusTcpAdapterConfig | None = None,
) -> ModbusTcpAdapter:
    return ModbusTcpAdapter(config or make_config(), client_factory=lambda _: client)


def test_config_rejects_invalid_port_timeout_unit_id_and_register_values() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ModbusTcpAdapterConfig(
            device_id="modbus-1",
            host="192.0.2.10",
            port="502",
            modbus_device_id=3,
            registers=(ModbusRegisterRange(table="holding", start_address=1, count=1),),
        )
    assert any(e["loc"] == ("port",) for e in exc_info.value.errors())

    with pytest.raises(ValidationError) as exc_info:
        make_config(timeout_s=10.1)
    assert any(e["loc"] == ("timeout_s",) for e in exc_info.value.errors())

    with pytest.raises(ValidationError) as exc_info:
        ModbusTcpAdapterConfig(
            device_id="modbus-1",
            host="192.0.2.10",
            port=502,
            modbus_device_id=0,
            registers=(ModbusRegisterRange(table="holding", start_address=1, count=1),),
        )
    assert any(e["loc"] == ("modbus_device_id",) for e in exc_info.value.errors())

    with pytest.raises(ValidationError) as exc_info:
        ModbusRegisterRange(table="holding", start_address=65_535, count=2)
    assert any("address space" in e["msg"] for e in exc_info.value.errors())


def test_config_rejects_register_collisions_between_tables() -> None:
    with pytest.raises(ValidationError):
        make_config(
            registers=(
                ModbusRegisterRange(table="holding", start_address=40001, count=2),
                ModbusRegisterRange(table="input", start_address=40002, count=1),
            )
        )


async def test_get_raw_state_reads_holding_and_input_registers() -> None:
    client = FakeAsyncModbusClient()
    adapter = make_adapter(client)

    state = await adapter.get_raw_state()

    assert isinstance(state, RawModbusState)
    assert state.device_id == "modbus-1"
    assert state.registers == {40001: 100, 40002: 101, 30001: 200}
    assert state.read_at.utcoffset().total_seconds() == 0
    assert client.calls == [
        ("read_holding_registers", 40001, 2, 3),
        ("read_input_registers", 30001, 1, 3),
    ]


async def test_get_raw_state_timeout_returns_degraded_and_logs() -> None:
    client = FakeAsyncModbusClient()
    client.read_delay_s = 1.0
    adapter = make_adapter(client, config=make_config(timeout_s=0.01))
    started_at = asyncio.get_running_loop().time()

    with structlog.testing.capture_logs() as logs:
        state = await adapter.get_raw_state()

    elapsed = asyncio.get_running_loop().time() - started_at
    assert elapsed < 0.5
    assert isinstance(state, ProtocolDegradedState)
    assert state.reason == "modbus_timeout"
    assert state.occurred_at.utcoffset().total_seconds() == 0
    assert any(entry["event"] == "adapter_timeout" for entry in logs)


async def test_get_raw_state_connect_timeout_returns_degraded_and_logs() -> None:
    client = FakeAsyncModbusClient()
    client.connect_delay_s = 1.0
    adapter = make_adapter(client, config=make_config(timeout_s=0.01))
    started_at = asyncio.get_running_loop().time()

    with structlog.testing.capture_logs() as logs:
        state = await adapter.get_raw_state()

    elapsed = asyncio.get_running_loop().time() - started_at
    assert elapsed < 0.5
    assert isinstance(state, ProtocolDegradedState)
    assert state.reason == "modbus_timeout"
    assert client.close_calls == 1
    assert any(entry["event"] == "adapter_timeout" for entry in logs)


async def test_get_raw_state_translates_connection_refused_without_affecting_other_adapter() -> (
    None
):
    failed_client = FakeAsyncModbusClient()
    failed_client.connect_error = ConnectionRefusedError("refused")
    healthy_client = FakeAsyncModbusClient()

    failed_adapter = make_adapter(failed_client)
    healthy_adapter = make_adapter(healthy_client, config=make_config())

    failed_state = await failed_adapter.get_raw_state()
    healthy_state = await healthy_adapter.get_raw_state()

    assert isinstance(failed_state, ProtocolDegradedState)
    assert failed_state.reason == "modbus_error"
    assert failed_state.occurred_at.utcoffset().total_seconds() == 0
    assert isinstance(healthy_state, RawModbusState)
    assert healthy_client.connect_calls == 1


async def test_get_raw_state_translates_modbus_exception_response() -> None:
    client = FakeAsyncModbusClient()
    client.holding_response = FakeModbusResponse(is_error=True)
    adapter = make_adapter(client)

    with structlog.testing.capture_logs() as logs:
        state = await adapter.get_raw_state()

    assert isinstance(state, ProtocolDegradedState)
    assert state.reason == "modbus_error"
    assert any(entry["event"] == "adapter_protocol_error" for entry in logs)


async def test_get_raw_state_translates_unexpected_register_payload() -> None:
    client = FakeAsyncModbusClient()
    client.holding_response = FakeModbusResponse(registers=["100", 101])
    adapter = make_adapter(client)

    state = await adapter.get_raw_state()

    assert isinstance(state, ProtocolDegradedState)
    assert state.reason == "modbus_error"


async def test_adapter_reconnects_after_mid_session_disconnect() -> None:
    first_client = FakeAsyncModbusClient()
    first_client.read_error = ConnectionResetError("lost")
    second_client = FakeAsyncModbusClient()
    clients = [first_client, second_client]

    adapter = ModbusTcpAdapter(make_config(), client_factory=lambda _: clients.pop(0))

    first_state = await adapter.get_raw_state()
    second_state = await adapter.get_raw_state()

    assert isinstance(first_state, ProtocolDegradedState)
    assert first_client.close_calls == 1
    assert isinstance(second_state, RawModbusState)
    assert second_client.connect_calls == 1


async def test_send_raw_command_returns_ack_for_write_register() -> None:
    client = FakeAsyncModbusClient()
    adapter = make_adapter(client)
    command = RawProtocolCommand(
        correlation_id="corr-1",
        device_id="modbus-1",
        command_name="modbus_write",
        payload={"operation": "write_register", "address": 40010, "value": 7},
    )

    result = await adapter.send_raw_command(command)

    assert result == ProtocolCommandResult(
        correlation_id="corr-1",
        device_id="modbus-1",
        protocol_status="acked",
        raw_response={
            "operation": "write_register",
            "address": 40010,
            "value": 7,
            "modbus_device_id": 3,
        },
    )
    assert client.calls == [("write_register", 40010, 7, 3)]


async def test_send_raw_command_returns_ack_for_write_registers() -> None:
    client = FakeAsyncModbusClient()
    adapter = make_adapter(client)
    command = RawProtocolCommand(
        correlation_id="corr-1",
        device_id="modbus-1",
        command_name="modbus_write",
        payload={
            "operation": "write_registers",
            "address": 40010,
            "values": [7, 8],
            "modbus_device_id": 4,
        },
    )

    result = await adapter.send_raw_command(command)

    assert result.protocol_status == "acked"
    assert result.raw_response == {
        "operation": "write_registers",
        "address": 40010,
        "values": [7, 8],
        "modbus_device_id": 4,
    }
    assert client.calls == [("write_registers", 40010, [7, 8], 4)]


async def test_send_raw_command_timeout_returns_timeout_result() -> None:
    client = FakeAsyncModbusClient()
    client.write_delay_s = 1.0
    adapter = make_adapter(client, config=make_config(timeout_s=0.01))
    command = RawProtocolCommand(
        correlation_id="corr-1",
        device_id="modbus-1",
        command_name="modbus_write",
        payload={"operation": "write_register", "address": 40010, "value": 7},
    )
    started_at = asyncio.get_running_loop().time()

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_raw_command(command)

    elapsed = asyncio.get_running_loop().time() - started_at
    assert elapsed < 0.5
    assert result.protocol_status == "timeout"
    assert result.raw_response == {"error": "modbus_timeout"}
    assert any(entry["event"] == "adapter_timeout" for entry in logs)


async def test_send_raw_command_malformed_payload_returns_error_result() -> None:
    client = FakeAsyncModbusClient()
    adapter = make_adapter(client)
    command = RawProtocolCommand(
        correlation_id="corr-1",
        device_id="modbus-1",
        command_name="modbus_write",
        payload={"operation": "write_register", "address": "40010", "value": 7},
    )

    result = await adapter.send_raw_command(command)

    assert result.protocol_status == "error"
    assert result.raw_response == {"error": "invalid_modbus_command"}
    assert client.connect_calls == 0


async def test_send_raw_command_modbus_exception_response_returns_error() -> None:
    client = FakeAsyncModbusClient()
    client.write_response = FakeModbusResponse(is_error=True)
    adapter = make_adapter(client)
    command = RawProtocolCommand(
        correlation_id="corr-1",
        device_id="modbus-1",
        command_name="modbus_write",
        payload={"operation": "write_register", "address": 40010, "value": 7},
    )

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_raw_command(command)

    assert result.protocol_status == "error"
    assert result.raw_response == {"error": "modbus_exception"}
    assert any(entry["event"] == "adapter_protocol_error" for entry in logs)


async def test_modbus_tcp_adapter_structurally_satisfies_protocol_adapter() -> None:
    adapter: ProtocolAdapter = make_adapter(FakeAsyncModbusClient())

    assert isinstance(adapter, ProtocolAdapter)


async def test_close_closes_reused_client() -> None:
    client = FakeAsyncModbusClient()
    adapter = make_adapter(client)

    await adapter.get_raw_state()
    await adapter.close()

    assert client.close_calls == 1
