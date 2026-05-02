from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from open_ems.adapters import (
    ProtocolAdapter,
    ProtocolCommandResult,
    ProtocolDegradedState,
    RawDSMRState,
    RawModbusState,
    RawOCPPState,
    RawProtocolCommand,
    RawProtocolState,
)


class FakeProtocolAdapter:
    async def get_raw_state(self) -> RawProtocolState | ProtocolDegradedState:
        return RawModbusState(
            device_id="modbus-1",
            registers={40001: 1234},
            read_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        return ProtocolCommandResult(
            correlation_id=command.correlation_id,
            device_id=command.device_id,
            protocol_status="acked",
            raw_response=b"\x01\x06",
        )


def _accepts_protocol_adapter(adapter: ProtocolAdapter) -> ProtocolAdapter:
    return adapter


def test_fake_adapter_structurally_satisfies_protocol_adapter() -> None:
    adapter: ProtocolAdapter = FakeProtocolAdapter()

    assert isinstance(adapter, ProtocolAdapter)
    assert _accepts_protocol_adapter(adapter) is adapter


def test_command_result_accepts_bytes_and_dict_raw_responses() -> None:
    bytes_result = ProtocolCommandResult(
        correlation_id="corr-1",
        device_id="device-1",
        protocol_status="acked",
        raw_response=b"raw",
    )
    dict_result = ProtocolCommandResult(
        correlation_id="corr-2",
        device_id="device-2",
        protocol_status="sent",
        raw_response={"action": "BootNotification"},
    )

    assert bytes_result.raw_response == b"raw"
    assert dict_result.raw_response == {"action": "BootNotification"}


def test_command_result_rejects_unknown_protocol_status() -> None:
    with pytest.raises(ValidationError):
        ProtocolCommandResult(
            correlation_id="corr-1",
            device_id="device-1",
            protocol_status="failed",
        )


def test_protocol_payloads_reject_strings_instead_of_coercing_to_bytes() -> None:
    with pytest.raises(ValidationError):
        RawProtocolCommand(
            correlation_id="corr-1",
            device_id="device-1",
            command_name="write_register",
            payload="oops",
        )

    with pytest.raises(ValidationError):
        ProtocolCommandResult(
            correlation_id="corr-1",
            device_id="device-1",
            protocol_status="acked",
            raw_response="oops",
        )


def test_protocol_degraded_state_requires_timezone_aware_datetime() -> None:
    with pytest.raises(ValidationError):
        ProtocolDegradedState(
            device_id="device-1",
            reason="timeout",
            occurred_at=datetime(2026, 5, 2, 12, 0),
        )


def test_protocol_degraded_state_requires_utc_datetime() -> None:
    with pytest.raises(ValidationError):
        ProtocolDegradedState(
            device_id="device-1",
            reason="timeout",
            occurred_at=datetime(
                2026,
                5,
                2,
                12,
                0,
                tzinfo=timezone(timedelta(hours=1)),
            ),
        )


def test_protocol_identifiers_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        RawProtocolCommand(
            correlation_id="",
            device_id="device-1",
            command_name="write_register",
            payload=b"raw",
        )

    with pytest.raises(ValidationError):
        RawProtocolCommand(
            correlation_id="corr-1",
            device_id="   ",
            command_name="write_register",
            payload=b"raw",
        )

    with pytest.raises(ValidationError):
        RawProtocolCommand(
            correlation_id="corr-1",
            device_id="device-1",
            command_name="",
            payload=b"raw",
        )

    with pytest.raises(ValidationError):
        ProtocolDegradedState(
            device_id="device-1",
            reason=" ",
            occurred_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )


def test_raw_modbus_state_is_raw_register_data_only() -> None:
    state = RawModbusState(
        device_id="inverter-protocol-1",
        registers={40001: 0, 40002: 65535},
        read_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
    )

    assert state.registers == {40001: 0, 40002: 65535}
    assert "power_kw" not in RawModbusState.model_fields
    assert "soc_percent" not in RawModbusState.model_fields


def test_raw_modbus_state_rejects_non_16_bit_register_values() -> None:
    with pytest.raises(ValidationError):
        RawModbusState(
            device_id="inverter-protocol-1",
            registers={40001: 65536},
            read_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )


def test_raw_modbus_state_rejects_negative_register_addresses() -> None:
    with pytest.raises(ValidationError):
        RawModbusState(
            device_id="inverter-protocol-1",
            registers={-1: 1},
            read_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )


def test_raw_modbus_state_rejects_register_addresses_above_16_bit_range() -> None:
    with pytest.raises(ValidationError):
        RawModbusState(
            device_id="inverter-protocol-1",
            registers={65536: 1},
            read_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )


def test_raw_modbus_state_rejects_coerced_string_registers() -> None:
    with pytest.raises(ValidationError):
        RawModbusState(
            device_id="inverter-protocol-1",
            registers={"40001": "7"},
            read_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )


def test_raw_modbus_state_rejects_extra_domain_fields() -> None:
    with pytest.raises(ValidationError):
        RawModbusState(
            device_id="inverter-protocol-1",
            registers={40001: 1},
            read_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
            power_kw=1.5,
        )


def test_raw_ocpp_state_keeps_raw_message_payloads_without_energy_inference() -> None:
    state = RawOCPPState(
        device_id="charger-protocol-1",
        charge_point_id="cp-1",
        last_status_notification={"status": "Charging", "connectorId": 1},
        last_heartbeat_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        connection_status="connected",
        last_call_result={"status": "Accepted"},
        last_call_error=None,
    )

    assert state.last_status_notification == {"status": "Charging", "connectorId": 1}
    assert "session_active" not in RawOCPPState.model_fields
    assert "current_power_kw" not in RawOCPPState.model_fields


def test_raw_ocpp_state_allows_absent_optional_message_payloads() -> None:
    state = RawOCPPState(
        device_id="charger-protocol-1",
        charge_point_id="cp-1",
        connection_status="disconnected",
    )

    assert state.last_status_notification is None
    assert state.last_heartbeat_at is None
    assert state.last_call_result is None
    assert state.last_call_error is None


def test_raw_ocpp_state_rejects_non_utc_heartbeat_timestamp() -> None:
    with pytest.raises(ValidationError):
        RawOCPPState(
            device_id="charger-protocol-1",
            charge_point_id="cp-1",
            last_heartbeat_at=datetime(
                2026,
                5,
                2,
                12,
                0,
                tzinfo=timezone(timedelta(hours=-4)),
            ),
            connection_status="connected",
        )


def test_raw_dsmr_state_keeps_telegram_fields_without_sign_mapping() -> None:
    telegram_fields: dict[str, Any] = {
        "electricity_delivered_tariff1": "00123.456*kWh",
        "current_electricity_usage": "00.321*kW",
    }

    state = RawDSMRState(
        device_id="meter-protocol-1",
        telegram_fields=telegram_fields,
        received_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
    )

    assert state.telegram_fields == telegram_fields
    assert "grid_power_kw" not in RawDSMRState.model_fields
    assert "energy_kwh" not in RawDSMRState.model_fields


def test_raw_state_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        RawDSMRState(
            device_id="meter-protocol-1",
            telegram_fields={},
            received_at=datetime(2026, 5, 2, 12, 0),
        )
