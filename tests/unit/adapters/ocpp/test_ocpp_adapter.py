"""Deterministic unit tests for the OCPP 1.6 Central System adapter.

Uses a dual-queue _FakeWebSocket — no real network, no running FastAPI server.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
import structlog.testing
from pydantic import ValidationError

from open_ems.adapters import ProtocolAdapter, ProtocolDegradedState, RawOCPPState
from open_ems.adapters.ocpp import OCPPAdapterConfig, OCPPCentralSystem, OCPPChargerAdapter
from open_ems.adapters.protocol import RawProtocolCommand

# ---------------------------------------------------------------------------
# Fake WebSocket
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Dual-queue fake WebSocket for deterministic OCPP unit tests."""

    def __init__(self) -> None:
        self._from_client: asyncio.Queue[str | None] = asyncio.Queue()
        self._to_client: asyncio.Queue[str] = asyncio.Queue()

    # Adapter (server) side — ocpp library calls these
    async def recv(self) -> str:
        msg = await self._from_client.get()
        if msg is None:
            raise ConnectionError("charger_disconnected")
        return msg

    async def send(self, message: str) -> None:
        await self._to_client.put(message)

    # Charger (test) side — simulate the charger
    async def client_send(self, message: str) -> None:
        await self._from_client.put(message)

    async def client_recv(self) -> str:
        return await self._to_client.get()

    def close(self) -> None:
        self._from_client.put_nowait(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    *,
    device_id: str = "ev-1",
    charge_point_id: str = "cp-001",
    heartbeat_timeout_s: float = 60.0,
    command_timeout_s: float = 1.0,
) -> OCPPAdapterConfig:
    return OCPPAdapterConfig(
        device_id=device_id,
        charge_point_id=charge_point_id,
        heartbeat_timeout_s=heartbeat_timeout_s,
        command_timeout_s=command_timeout_s,
    )


def make_boot_notification_msg(unique_id: str = "boot-1") -> str:
    payload = {"chargePointVendor": "Wallbox", "chargePointModel": "Pulsar"}
    return json.dumps([2, unique_id, "BootNotification", payload])


async def connect_charger(ws: _FakeWebSocket, adapter: OCPPChargerAdapter) -> asyncio.Task[None]:
    """Start the message loop and perform BootNotification handshake."""
    loop_task: asyncio.Task[None] = asyncio.create_task(adapter.handle_connection(ws))
    await ws.client_send(make_boot_notification_msg())
    response = json.loads(await asyncio.wait_for(ws.client_recv(), timeout=2.0))
    assert response[0] == 3  # CALLRESULT
    assert response[2]["status"] == "Accepted"
    return loop_task


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------


def test_config_rejects_bool_timeout_for_heartbeat() -> None:
    with pytest.raises(ValidationError) as exc_info:
        make_config(heartbeat_timeout_s=True)  # type: ignore[arg-type]
    assert any(e["loc"] == ("heartbeat_timeout_s",) for e in exc_info.value.errors())


def test_config_rejects_string_timeout_for_command() -> None:
    with pytest.raises(ValidationError) as exc_info:
        make_config(command_timeout_s="5")  # type: ignore[arg-type]
    assert any(e["loc"] == ("command_timeout_s",) for e in exc_info.value.errors())


def test_config_rejects_command_timeout_exceeding_max() -> None:
    with pytest.raises(ValidationError) as exc_info:
        make_config(command_timeout_s=10.1)
    assert any(e["loc"] == ("command_timeout_s",) for e in exc_info.value.errors())


def test_config_rejects_zero_heartbeat_timeout() -> None:
    with pytest.raises(ValidationError) as exc_info:
        make_config(heartbeat_timeout_s=0)
    assert any(e["loc"] == ("heartbeat_timeout_s",) for e in exc_info.value.errors())


def test_config_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        OCPPAdapterConfig(  # type: ignore[call-arg]
            device_id="ev-1",
            charge_point_id="cp-1",
            unknown_field="x",
        )


# ---------------------------------------------------------------------------
# BootNotification
# ---------------------------------------------------------------------------


async def test_boot_notification_returns_accepted_and_updates_state() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    state = await adapter.get_raw_state()
    assert isinstance(state, RawOCPPState)
    assert state.connection_status == "connected"
    assert state.last_status_notification is not None
    assert state.last_status_notification["type"] == "BootNotification"
    assert state.last_status_notification["charge_point_vendor"] == "Wallbox"
    assert state.last_status_notification["charge_point_model"] == "Pulsar"

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


async def test_heartbeat_updates_last_heartbeat_at_as_utc() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    before = datetime.now(UTC)
    await ws.client_send(json.dumps([2, "hb-1", "Heartbeat", {}]))
    hb_response = json.loads(await asyncio.wait_for(ws.client_recv(), timeout=2.0))
    after = datetime.now(UTC)

    assert hb_response[0] == 3  # CALLRESULT

    state = await adapter.get_raw_state()
    assert isinstance(state, RawOCPPState)
    assert state.last_heartbeat_at is not None
    assert before <= state.last_heartbeat_at <= after
    assert state.last_heartbeat_at.utcoffset() is not None
    assert state.last_heartbeat_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


# ---------------------------------------------------------------------------
# StatusNotification
# ---------------------------------------------------------------------------


async def test_status_notification_stores_raw_payload() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    sn_payload = {"connectorId": 1, "errorCode": "NoError", "status": "Available"}
    await ws.client_send(json.dumps([2, "sn-1", "StatusNotification", sn_payload]))
    sn_response = json.loads(await asyncio.wait_for(ws.client_recv(), timeout=2.0))
    assert sn_response[0] == 3  # empty CALLRESULT

    state = await adapter.get_raw_state()
    assert isinstance(state, RawOCPPState)
    assert state.last_status_notification is not None
    assert state.last_status_notification["type"] == "StatusNotification"
    assert state.last_status_notification["connector_id"] == 1
    assert state.last_status_notification["error_code"] == "NoError"
    assert state.last_status_notification["status"] == "Available"

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


# ---------------------------------------------------------------------------
# get_raw_state — disconnected and stale
# ---------------------------------------------------------------------------


async def test_get_raw_state_before_connection_returns_degraded() -> None:
    adapter = OCPPChargerAdapter(make_config())
    state = await adapter.get_raw_state()
    assert isinstance(state, ProtocolDegradedState)
    assert state.device_id == "ev-1"
    assert state.reason == "ocpp_disconnected"


async def test_get_raw_state_after_disconnect_returns_degraded() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)

    state = await adapter.get_raw_state()
    assert isinstance(state, ProtocolDegradedState)
    assert state.reason == "ocpp_disconnected"


async def test_get_raw_state_degraded_occurred_at_is_utc_aware() -> None:
    adapter = OCPPChargerAdapter(make_config())
    state = await adapter.get_raw_state()
    assert isinstance(state, ProtocolDegradedState)
    assert state.occurred_at.utcoffset() is not None
    assert state.occurred_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


async def test_get_raw_state_when_heartbeat_stale_returns_degraded() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config(heartbeat_timeout_s=0.05))
    loop_task = await connect_charger(ws, adapter)

    # Trigger a heartbeat so last_heartbeat_at is set
    await ws.client_send(json.dumps([2, "hb-1", "Heartbeat", {}]))
    await asyncio.wait_for(ws.client_recv(), timeout=2.0)

    # Wait until heartbeat is stale
    await asyncio.sleep(0.1)

    state = await adapter.get_raw_state()
    assert isinstance(state, ProtocolDegradedState)
    assert state.reason == "ocpp_disconnected"

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


async def test_get_raw_state_with_no_heartbeat_yet_returns_connected() -> None:
    """Fresh connection without any heartbeat is still considered connected."""
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config(heartbeat_timeout_s=0.01))
    loop_task = await connect_charger(ws, adapter)

    # No heartbeat sent yet — last_heartbeat_at is None, staleness check skipped
    state = await adapter.get_raw_state()
    assert isinstance(state, RawOCPPState)
    assert state.connection_status == "connected"
    assert state.last_heartbeat_at is None

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


# ---------------------------------------------------------------------------
# send_raw_command — acked
# ---------------------------------------------------------------------------


async def test_send_raw_command_acked_on_callresult() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    command = RawProtocolCommand(
        correlation_id="corr-1",
        device_id="ev-1",
        command_name="ChangeAvailability",
        payload={"connector_id": 0, "type": "Operative"},
    )

    async def charger_side() -> None:
        msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
        call_msg = json.loads(msg_str)
        unique_id = call_msg[1]
        await ws.client_send(json.dumps([3, unique_id, {"status": "Accepted"}]))

    charger_task: asyncio.Task[None] = asyncio.create_task(charger_side())
    result = await asyncio.wait_for(adapter.send_raw_command(command), timeout=2.0)
    await asyncio.wait_for(charger_task, timeout=2.0)

    assert result.protocol_status == "acked"
    assert result.raw_response == {"status": "Accepted"}
    assert result.correlation_id == "corr-1"

    # last_call_result is updated
    raw = await adapter.get_raw_state()
    assert isinstance(raw, RawOCPPState)
    assert raw.last_call_result == {"status": "Accepted"}

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


# ---------------------------------------------------------------------------
# send_raw_command — CALLERROR
# ---------------------------------------------------------------------------


async def test_send_raw_command_error_on_callerror() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    command = RawProtocolCommand(
        correlation_id="corr-2",
        device_id="ev-1",
        command_name="ChangeAvailability",
        payload={"connector_id": 0, "type": "Operative"},
    )

    async def charger_side() -> None:
        msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
        call_msg = json.loads(msg_str)
        unique_id = call_msg[1]
        await ws.client_send(json.dumps([4, unique_id, "NotImplemented", "Not supported", {}]))

    charger_task: asyncio.Task[None] = asyncio.create_task(charger_side())
    result = await asyncio.wait_for(adapter.send_raw_command(command), timeout=2.0)
    await asyncio.wait_for(charger_task, timeout=2.0)

    assert result.protocol_status == "error"
    assert result.raw_response is not None
    assert result.raw_response.get("error_code") is not None

    # last_call_error is updated
    raw = await adapter.get_raw_state()
    assert isinstance(raw, RawOCPPState)
    assert raw.last_call_error is not None

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


# ---------------------------------------------------------------------------
# send_raw_command — timeout
# ---------------------------------------------------------------------------


async def test_send_raw_command_timeout_returns_within_epsilon_and_logs() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config(command_timeout_s=0.1))
    loop_task = await connect_charger(ws, adapter)

    command = RawProtocolCommand(
        correlation_id="corr-3",
        device_id="ev-1",
        command_name="ChangeAvailability",
        payload={"connector_id": 0, "type": "Operative"},
    )

    started_at = asyncio.get_running_loop().time()

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_raw_command(command)

    elapsed = asyncio.get_running_loop().time() - started_at
    epsilon = 0.5
    assert elapsed < 0.1 + epsilon, f"Timed out too slowly: {elapsed:.3f}s"

    assert result.protocol_status == "timeout"
    assert result.raw_response == {"error": "ocpp_timeout"}
    assert any(
        entry["event"] == "adapter_timeout" and entry.get("component") == "ocpp" for entry in logs
    )
    log = next(e for e in logs if e["event"] == "adapter_timeout")
    assert log["command"] == "ChangeAvailability"
    assert log["timeout_s"] == 0.1

    # Drain the CALL from the charger queue to avoid blocking loop_task shutdown
    try:
        await asyncio.wait_for(ws.client_recv(), timeout=0.5)
    except TimeoutError:
        pass

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


# ---------------------------------------------------------------------------
# send_raw_command — error conditions
# ---------------------------------------------------------------------------


async def test_send_raw_command_bytes_payload_returns_error() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    command = RawProtocolCommand(
        correlation_id="corr-4",
        device_id="ev-1",
        command_name="ChangeAvailability",
        payload=b"not a dict",
    )

    result = await adapter.send_raw_command(command)
    assert result.protocol_status == "error"
    assert result.raw_response == {"error": "invalid_payload"}

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


async def test_send_raw_command_missing_required_fields_returns_error() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    command = RawProtocolCommand(
        correlation_id="corr-5",
        device_id="ev-1",
        command_name="ChangeAvailability",
        payload={},  # missing connector_id and type
    )

    result = await adapter.send_raw_command(command)
    assert result.protocol_status == "error"
    assert result.raw_response == {"error": "invalid_payload"}

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


async def test_send_raw_command_unknown_action_returns_error() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    command = RawProtocolCommand(
        correlation_id="corr-6",
        device_id="ev-1",
        command_name="NonExistentOCPPAction",
        payload={"some": "data"},
    )

    result = await adapter.send_raw_command(command)
    assert result.protocol_status == "error"
    assert result.raw_response == {"error": "unknown_action"}

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


async def test_send_raw_command_not_connected_returns_error() -> None:
    adapter = OCPPChargerAdapter(make_config())
    command = RawProtocolCommand(
        correlation_id="corr-7",
        device_id="ev-1",
        command_name="ChangeAvailability",
        payload={"connector_id": 0, "type": "Operative"},
    )

    result = await adapter.send_raw_command(command)
    assert result.protocol_status == "error"
    assert result.raw_response == {"error": "not_connected"}


# ---------------------------------------------------------------------------
# Malformed JSON — loop resilience
# ---------------------------------------------------------------------------


async def test_malformed_json_does_not_crash_loop() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    # Send malformed JSON — the ocpp library catches OCPPError and continues
    await ws.client_send("{ not valid json !!!")

    # Send a valid message after the malformed one — loop should still process it
    await ws.client_send(json.dumps([2, "hb-1", "Heartbeat", {}]))
    hb_response = json.loads(await asyncio.wait_for(ws.client_recv(), timeout=2.0))
    assert hb_response[0] == 3  # Loop survived and processed the Heartbeat

    # After disconnect, state becomes degraded
    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)

    state = await adapter.get_raw_state()
    assert isinstance(state, ProtocolDegradedState)
    assert state.reason == "ocpp_disconnected"


# ---------------------------------------------------------------------------
# Disconnect detection and logging
# ---------------------------------------------------------------------------


async def test_disconnect_logs_adapter_disconnected() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    with structlog.testing.capture_logs() as logs:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=2.0)

    assert any(
        entry["event"] == "adapter_disconnected"
        and entry.get("component") == "ocpp"
        and entry.get("device_id") == "ev-1"
        and entry.get("charge_point_id") == "cp-001"
        for entry in logs
    )


async def test_get_raw_state_disconnected_does_not_emit_log() -> None:
    """Degraded state from get_raw_state() must not emit adapter_disconnected."""
    adapter = OCPPChargerAdapter(make_config())

    with structlog.testing.capture_logs() as logs:
        state = await adapter.get_raw_state()

    assert isinstance(state, ProtocolDegradedState)
    assert not any(entry["event"] == "adapter_disconnected" for entry in logs)


# ---------------------------------------------------------------------------
# Reconnect after disconnect
# ---------------------------------------------------------------------------


async def test_reconnect_after_disconnect_restores_connected_state() -> None:
    ws1 = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())

    # First connection
    loop_task1 = await connect_charger(ws1, adapter)
    ws1.close()
    await asyncio.wait_for(loop_task1, timeout=2.0)

    first_state = await adapter.get_raw_state()
    assert isinstance(first_state, ProtocolDegradedState)

    # Reconnect
    ws2 = _FakeWebSocket()
    loop_task2 = await connect_charger(ws2, adapter)

    reconnected_state = await adapter.get_raw_state()
    assert isinstance(reconnected_state, RawOCPPState)
    assert reconnected_state.connection_status == "connected"

    ws2.close()
    await asyncio.wait_for(loop_task2, timeout=2.0)


# ---------------------------------------------------------------------------
# Per-instance isolation
# ---------------------------------------------------------------------------


async def test_per_instance_isolation() -> None:
    ws1 = _FakeWebSocket()
    ws2 = _FakeWebSocket()
    adapter1 = OCPPChargerAdapter(make_config(device_id="ev-1", charge_point_id="cp-1"))
    adapter2 = OCPPChargerAdapter(make_config(device_id="ev-2", charge_point_id="cp-2"))

    task1 = await connect_charger(ws1, adapter1)
    task2: asyncio.Task[None] = asyncio.create_task(adapter2.handle_connection(ws2))
    # adapter2 is connected but hasn't received any messages
    await asyncio.sleep(0)

    state1 = await adapter1.get_raw_state()
    state2 = await adapter2.get_raw_state()

    assert isinstance(state1, RawOCPPState)
    assert state1.charge_point_id == "cp-1"
    assert state1.last_status_notification is not None  # BootNotification received

    assert isinstance(state2, RawOCPPState)
    assert state2.charge_point_id == "cp-2"
    assert state2.last_status_notification is None  # No messages yet

    ws1.close()
    ws2.close()
    await asyncio.wait_for(task1, timeout=2.0)
    await asyncio.wait_for(task2, timeout=2.0)


# ---------------------------------------------------------------------------


async def test_successful_command_clears_last_call_error() -> None:
    """A successful command after an error must clear last_call_error (Story 3.3 C2)."""
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    command = RawProtocolCommand(
        correlation_id="lifecycle-1",
        device_id="ev-1",
        command_name="ChangeAvailability",
        payload={"connector_id": 0, "type": "Operative"},
    )

    # Step 1: trigger an error response so last_call_error is set.
    async def charger_error() -> None:
        msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
        unique_id = json.loads(msg_str)[1]
        await ws.client_send(json.dumps([4, unique_id, "NotImplemented", "Not supported", {}]))

    err_task: asyncio.Task[None] = asyncio.create_task(charger_error())
    await asyncio.wait_for(adapter.send_raw_command(command), timeout=2.0)
    await asyncio.wait_for(err_task, timeout=2.0)

    raw_after_error = await adapter.get_raw_state()
    assert isinstance(raw_after_error, RawOCPPState)
    assert raw_after_error.last_call_error is not None, (
        "last_call_error should be set after failure"
    )

    # Step 2: send a successful command and verify last_call_error is cleared.
    async def charger_ok() -> None:
        msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
        unique_id = json.loads(msg_str)[1]
        await ws.client_send(json.dumps([3, unique_id, {"status": "Accepted"}]))

    ok_task: asyncio.Task[None] = asyncio.create_task(charger_ok())
    result = await asyncio.wait_for(adapter.send_raw_command(command), timeout=2.0)
    await asyncio.wait_for(ok_task, timeout=2.0)

    assert result.protocol_status == "acked"
    raw_after_ok = await adapter.get_raw_state()
    assert isinstance(raw_after_ok, RawOCPPState)
    assert raw_after_ok.last_call_result == {"status": "Accepted"}
    assert raw_after_ok.last_call_error is None, (
        "last_call_error must be cleared after a successful command"
    )

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


# ---------------------------------------------------------------------------
# OCPPCentralSystem
# ---------------------------------------------------------------------------


def test_central_system_register_and_get_adapter() -> None:
    cs = OCPPCentralSystem()
    config = make_config()
    adapter = cs.register(config)
    assert isinstance(adapter, OCPPChargerAdapter)
    assert cs.get_adapter("cp-001") is adapter
    assert cs.get_adapter("unknown") is None


async def test_central_system_handle_charger_rejects_unknown_id() -> None:
    cs = OCPPCentralSystem()
    ws = _FakeWebSocket()
    with pytest.raises(KeyError, match="cp-unknown"):
        await cs.handle_charger("cp-unknown", ws)


async def test_central_system_handle_charger_uses_preregistered_adapter() -> None:
    cs = OCPPCentralSystem()
    config = OCPPAdapterConfig(device_id="dev-auto", charge_point_id="cp-auto")
    cs.register(config)
    ws = _FakeWebSocket()

    handle_task: asyncio.Task[None] = asyncio.create_task(cs.handle_charger("cp-auto", ws))
    await ws.client_send(make_boot_notification_msg())
    response = json.loads(await asyncio.wait_for(ws.client_recv(), timeout=2.0))
    assert response[2]["status"] == "Accepted"

    adapter = cs.get_adapter("cp-auto")
    assert adapter is not None
    state = await adapter.get_raw_state()
    assert isinstance(state, RawOCPPState)

    ws.close()
    await asyncio.wait_for(handle_task, timeout=2.0)


# ---------------------------------------------------------------------------
# ProtocolAdapter structural satisfaction
# ---------------------------------------------------------------------------


async def test_ocpp_charger_adapter_structurally_satisfies_protocol_adapter() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    assert isinstance(adapter, ProtocolAdapter)

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


# ---------------------------------------------------------------------------
# Completion notes / AC cross-checks
# ---------------------------------------------------------------------------


async def test_boot_notification_callresult_fields() -> None:
    """Verify BootNotification CALLRESULT contains required OCPP 1.6 fields."""
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task: asyncio.Task[None] = asyncio.create_task(adapter.handle_connection(ws))

    await ws.client_send(make_boot_notification_msg("boot-check"))
    response = json.loads(await asyncio.wait_for(ws.client_recv(), timeout=2.0))

    assert response[0] == 3
    assert response[2]["status"] == "Accepted"
    assert "currentTime" in response[2]
    assert response[2]["interval"] == 10

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


async def test_get_raw_state_returns_all_required_fields() -> None:
    """AC2: RawOCPPState contains all required fields."""
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    state = await adapter.get_raw_state()
    assert isinstance(state, RawOCPPState)
    assert state.device_id == "ev-1"
    assert state.charge_point_id == "cp-001"
    assert state.connection_status == "connected"
    assert state.last_call_result is None
    assert state.last_call_error is None

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


async def test_send_raw_command_updates_last_call_result_in_state() -> None:
    """After a successful command, get_raw_state reflects last_call_result."""
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    command = RawProtocolCommand(
        correlation_id="c1",
        device_id="ev-1",
        command_name="ChangeAvailability",
        payload={"connector_id": 1, "type": "Inoperative"},
    )

    async def charger_side() -> None:
        msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
        call_msg = json.loads(msg_str)
        await ws.client_send(json.dumps([3, call_msg[1], {"status": "Accepted"}]))

    charger_task: asyncio.Task[None] = asyncio.create_task(charger_side())
    result = await asyncio.wait_for(adapter.send_raw_command(command), timeout=2.0)
    await asyncio.wait_for(charger_task, timeout=2.0)

    assert result.protocol_status == "acked"

    raw = await adapter.get_raw_state()
    assert isinstance(raw, RawOCPPState)
    assert raw.last_call_result == {"status": "Accepted"}

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


async def test_adapter_timeout_log_fields() -> None:
    """AC4: adapter_timeout log includes expected fields."""
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config(command_timeout_s=0.05))
    loop_task = await connect_charger(ws, adapter)

    command = RawProtocolCommand(
        correlation_id="c-timeout",
        device_id="ev-1",
        command_name="ChangeAvailability",
        payload={"connector_id": 0, "type": "Operative"},
    )

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_raw_command(command)

    assert result.protocol_status == "timeout"
    log = next(e for e in logs if e["event"] == "adapter_timeout")
    assert log["component"] == "ocpp"
    assert log["device_id"] == "ev-1"
    assert log["charge_point_id"] == "cp-001"
    assert log["command"] == "ChangeAvailability"
    assert log["timeout_s"] == pytest.approx(0.05)

    try:
        await asyncio.wait_for(ws.client_recv(), timeout=0.5)
    except TimeoutError:
        pass

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


async def test_callerror_logs_adapter_protocol_error() -> None:
    """AC4: CALLERROR response logs adapter_protocol_error."""
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    command = RawProtocolCommand(
        correlation_id="c-err",
        device_id="ev-1",
        command_name="ChangeAvailability",
        payload={"connector_id": 0, "type": "Operative"},
    )

    async def charger_side() -> None:
        msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
        call_msg = json.loads(msg_str)
        uid = call_msg[1]
        await ws.client_send(json.dumps([4, uid, "NotImplemented", "No.", {}]))

    charger_task: asyncio.Task[None] = asyncio.create_task(charger_side())

    with structlog.testing.capture_logs() as logs:
        result = await asyncio.wait_for(adapter.send_raw_command(command), timeout=2.0)

    await asyncio.wait_for(charger_task, timeout=2.0)

    assert result.protocol_status == "error"
    assert any(
        e["event"] == "adapter_protocol_error" and e.get("component") == "ocpp" for e in logs
    )

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)


async def test_last_status_notification_overwritten_by_status_notification() -> None:
    """StatusNotification overwrites last_status_notification set by BootNotification."""
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = await connect_charger(ws, adapter)

    sn_payload = {"connectorId": 1, "errorCode": "NoError", "status": "Charging"}
    await ws.client_send(json.dumps([2, "sn-1", "StatusNotification", sn_payload]))
    await asyncio.wait_for(ws.client_recv(), timeout=2.0)

    state = await adapter.get_raw_state()
    assert isinstance(state, RawOCPPState)
    assert state.last_status_notification is not None
    assert state.last_status_notification["type"] == "StatusNotification"
    assert state.last_status_notification["status"] == "Charging"

    ws.close()
    await asyncio.wait_for(loop_task, timeout=2.0)
