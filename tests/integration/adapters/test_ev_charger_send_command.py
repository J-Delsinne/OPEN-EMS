"""Integration tests for the real EVChargerAdapter end-to-end (Story 9.0 AC11).

Path: control loop trigger → RetryPolicy → PolicyGuard → EVChargerAdapter →
OCPPChargerAdapter → simulated charger over a fake WebSocket → SetChargingProfile
CALL → CommandResult.

Reuses the dual-queue fake WebSocket pattern from the OCPP unit tests.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from open_ems.adapters.ocpp import OCPPAdapterConfig, OCPPChargerAdapter
from open_ems.adapters.ocpp.charger_adapter import EVChargerAdapter
from open_ems.core import (
    DeviceRole,
    EVChargerState,
    StateStore,
    SystemOperatingMode,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandStatus,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.engine import PolicyGuard, RetryPolicy
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings
from open_ems.storage.repositories.event_log_repo import EventLogRepo

_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake WebSocket — mirrors the unit test pattern
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    def __init__(self) -> None:
        self._from_client: asyncio.Queue[str | None] = asyncio.Queue()
        self._to_client: asyncio.Queue[str] = asyncio.Queue()

    async def recv(self) -> str:
        msg = await self._from_client.get()
        if msg is None:
            raise ConnectionError("charger_disconnected")
        return msg

    async def send(self, message: str) -> None:
        await self._to_client.put(message)

    async def client_send(self, message: str) -> None:
        await self._from_client.put(message)

    async def client_recv(self) -> str:
        return await self._to_client.get()

    def close(self) -> None:
        self._from_client.put_nowait(None)


async def _connect_charger(ws: _FakeWebSocket, adapter: OCPPChargerAdapter) -> asyncio.Task[None]:
    loop_task: asyncio.Task[None] = asyncio.create_task(adapter.handle_connection(ws))
    boot_payload = {"chargePointVendor": "Wallbox", "chargePointModel": "Pulsar"}
    await ws.client_send(json.dumps([2, "boot-1", "BootNotification", boot_payload]))
    response = json.loads(await asyncio.wait_for(ws.client_recv(), timeout=2.0))
    assert response[0] == 3
    assert response[2]["status"] == "Accepted"
    return loop_task


# ---------------------------------------------------------------------------
# Pipeline fixture builders
# ---------------------------------------------------------------------------


async def _state_store_with_ev() -> StateStore:
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    ev = EVChargerState(
        device_id="ev-001",
        status="charging",
        session_active=True,
        current_power_kw=3.0,
        power_source="meter_values",
        power_measured_at=_NOW,
        read_at=_NOW,
    )
    await store.publish({DeviceRole.ev_charger: ev}, operating_mode=SystemOperatingMode.normal)
    return store


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        command_max_retries=2,
        command_retry_backoff_seconds=0.0,
    )


def _audit_spy_observability() -> tuple[ObservabilityService, AsyncMock]:
    repo = AsyncMock(spec=EventLogRepo)
    obs = ObservabilityService(repo=repo)
    spy = AsyncMock()
    obs.audit = spy  # type: ignore[method-assign]
    return obs, spy


def _ocpp_adapter() -> OCPPChargerAdapter:
    return OCPPChargerAdapter(
        OCPPAdapterConfig(
            device_id="ev-001",
            charge_point_id="cp-001",
            command_timeout_s=2.0,
        )
    )


def _ev_rate_command(rate_kw: float = 5.0) -> SetEVChargingRateCommand:
    return SetEVChargingRateCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
        correlation_id=uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# AC11 integration #2 — end-to-end EV charge rate command, success path
# ---------------------------------------------------------------------------


async def test_end_to_end_ev_charge_rate_success_via_set_charging_profile() -> None:
    ws = _FakeWebSocket()
    ocpp = _ocpp_adapter()
    loop_task = await _connect_charger(ws, ocpp)
    try:
        ev_adapter = EVChargerAdapter("ev-001", ocpp)
        store = await _state_store_with_ev()
        obs, audit_spy = _audit_spy_observability()
        settings = _settings()
        guard = PolicyGuard(
            state_store=store,
            adapters={DeviceRole.ev_charger: ev_adapter},
            observability=obs,
            settings=settings,
        )
        retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

        cmd = _ev_rate_command(rate_kw=5.0)

        async def _charger_side() -> dict[str, Any]:
            msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
            call_msg = json.loads(msg_str)
            assert call_msg[2] == "SetChargingProfile"
            payload: dict[str, Any] = call_msg[3]
            await ws.client_send(json.dumps([3, call_msg[1], {"status": "Accepted"}]))
            return payload

        charger_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(_charger_side())

        result = await retry.execute(cmd)
        observed_payload = await asyncio.wait_for(charger_task, timeout=2.0)

        assert result.status is CommandStatus.success
        assert result.applied is True
        assert result.reason == "ok"
        assert result.correlation_id == cmd.correlation_id

        # Verify the SetChargingProfile payload composed by the adapter.
        # OCPP library lowercases / camelcases field names depending on direction;
        # python-ocpp sends camelCase on the wire.
        profile = observed_payload["csChargingProfiles"]
        assert profile["chargingProfilePurpose"] == "TxProfile"
        assert profile["chargingProfileKind"] == "Absolute"
        schedule = profile["chargingSchedule"]
        assert schedule["chargingRateUnit"] == "W"
        assert schedule["chargingSchedulePeriod"] == [{"startPeriod": 0, "limit": 5000}]

        # DECISION audit emitted exactly once.
        assert audit_spy.await_count == 1
        assert audit_spy.await_args_list[0].kwargs["event_type"] == "DECISION"
    finally:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=2.0)


async def test_end_to_end_ev_rejected_response_returns_rejected_no_retry() -> None:
    """Charger returns status=Rejected: result.status=rejected, RetryPolicy stops retrying."""
    ws = _FakeWebSocket()
    ocpp = _ocpp_adapter()
    loop_task = await _connect_charger(ws, ocpp)
    try:
        ev_adapter = EVChargerAdapter("ev-001", ocpp)
        store = await _state_store_with_ev()
        obs, audit_spy = _audit_spy_observability()
        settings = _settings()
        guard = PolicyGuard(
            state_store=store,
            adapters={DeviceRole.ev_charger: ev_adapter},
            observability=obs,
            settings=settings,
        )
        retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

        cmd = _ev_rate_command(rate_kw=5.0)

        async def _charger_side() -> None:
            msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
            call_msg = json.loads(msg_str)
            await ws.client_send(json.dumps([3, call_msg[1], {"status": "Rejected"}]))

        charger_task: asyncio.Task[None] = asyncio.create_task(_charger_side())

        result = await retry.execute(cmd)
        await asyncio.wait_for(charger_task, timeout=2.0)

        assert result.status is CommandStatus.rejected
        assert result.applied is False
        assert result.reason == "ocpp_rejected:Rejected"
        # DEVICE audit on terminal failure (rejected).
        assert audit_spy.await_count == 1
        assert audit_spy.await_args_list[0].kwargs["event_type"] == "DEVICE"
    finally:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=2.0)


async def test_end_to_end_ev_stop_without_active_transaction_returns_failed() -> None:
    """No StartTransaction observed: StopEVChargingCommand → no_active_ocpp_transaction."""
    ws = _FakeWebSocket()
    ocpp = _ocpp_adapter()
    loop_task = await _connect_charger(ws, ocpp)
    try:
        ev_adapter = EVChargerAdapter("ev-001", ocpp)
        store = await _state_store_with_ev()
        obs, _ = _audit_spy_observability()
        settings = _settings()
        guard = PolicyGuard(
            state_store=store,
            adapters={DeviceRole.ev_charger: ev_adapter},
            observability=obs,
            settings=settings,
        )

        stop_cmd = StopEVChargingCommand(
            device_id="ev-001",
            device_role=DeviceRole.ev_charger,
            origin=CommandOrigin.decision_engine,
        )
        result = await guard.authorize_and_dispatch(stop_cmd)

        assert result.status is CommandStatus.failed
        assert result.reason == "no_active_ocpp_transaction"
    finally:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=2.0)


async def test_end_to_end_ev_stop_after_start_transaction_dispatches_remote_stop() -> None:
    """Charger StartTransaction → adapter remembers id → Stop dispatches RemoteStopTransaction."""
    ws = _FakeWebSocket()
    ocpp = _ocpp_adapter()
    loop_task = await _connect_charger(ws, ocpp)
    try:
        # Charger sends StartTransaction; central system replies with a transaction id.
        start_payload = {
            "connectorId": 1,
            "idTag": "user-1",
            "meterStart": 0,
            "timestamp": "2026-05-10T12:00:00Z",
        }
        await ws.client_send(json.dumps([2, "st-1", "StartTransaction", start_payload]))
        start_response = json.loads(await asyncio.wait_for(ws.client_recv(), timeout=2.0))
        assert start_response[0] == 3
        transaction_id = start_response[2]["transactionId"]
        assert isinstance(transaction_id, int) and transaction_id > 0

        ev_adapter = EVChargerAdapter("ev-001", ocpp)
        store = await _state_store_with_ev()
        obs, _ = _audit_spy_observability()
        settings = _settings()
        guard = PolicyGuard(
            state_store=store,
            adapters={DeviceRole.ev_charger: ev_adapter},
            observability=obs,
            settings=settings,
        )

        stop_cmd = StopEVChargingCommand(
            device_id="ev-001",
            device_role=DeviceRole.ev_charger,
            origin=CommandOrigin.decision_engine,
        )

        async def _charger_side() -> dict[str, Any]:
            msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
            call_msg = json.loads(msg_str)
            assert call_msg[2] == "RemoteStopTransaction"
            payload: dict[str, Any] = call_msg[3]
            await ws.client_send(json.dumps([3, call_msg[1], {"status": "Accepted"}]))
            return payload

        charger_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(_charger_side())

        result = await guard.authorize_and_dispatch(stop_cmd)
        observed_payload = await asyncio.wait_for(charger_task, timeout=2.0)

        assert result.status is CommandStatus.success
        assert observed_payload == {"transactionId": transaction_id}
    finally:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=2.0)


async def test_end_to_end_ev_charge_rate_timeout_returns_ocpp_command_timeout() -> None:
    """AC7: protocol-layer OCPP timeout fires before PolicyGuard's outer 10s timeout."""
    ws = _FakeWebSocket()
    ocpp = OCPPChargerAdapter(
        OCPPAdapterConfig(
            device_id="ev-001",
            charge_point_id="cp-001",
            command_timeout_s=0.1,  # very short to force protocol timeout first
        )
    )
    loop_task = await _connect_charger(ws, ocpp)
    try:
        ev_adapter = EVChargerAdapter("ev-001", ocpp)
        store = await _state_store_with_ev()
        obs, _ = _audit_spy_observability()
        settings = _settings()
        guard = PolicyGuard(
            state_store=store,
            adapters={DeviceRole.ev_charger: ev_adapter},
            observability=obs,
            settings=settings,
        )

        cmd = _ev_rate_command()

        # Charger never responds — protocol timeout fires.
        result = await guard.authorize_and_dispatch(cmd)

        assert result.status is CommandStatus.timeout
        assert result.reason == "ocpp_command_timeout"
        # Drain the leftover CALL so loop_task can exit cleanly.
        try:
            await asyncio.wait_for(ws.client_recv(), timeout=0.5)
        except TimeoutError:
            pass
    finally:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=2.0)


# ---------------------------------------------------------------------------
# Real-library cancellation: exercise python-ocpp ChargePoint.call under cancel
# (Story 9.0 code-review decision 2 — D2 patch)
# ---------------------------------------------------------------------------


async def test_real_python_ocpp_library_cancellation_propagates_and_recovers() -> None:
    """Cancel a real ``handler.call`` mid-flight and verify the adapter recovers.

    The unit cancellation test uses a fake ``_BlockingOCPP`` that short-circuits
    at ``send_raw_command`` and never exercises the real ``python-ocpp``
    library. This test does:

    1. Issues a SetEVChargingRateCommand against a real ``OCPPChargerAdapter`` +
       ``_InternalChargePoint`` (the python-ocpp ChargePoint subclass) over a
       fake WebSocket.
    2. The simulated charger NEVER answers — the ``handler.call`` future stays
       pending.
    3. The caller cancels the dispatch task.
    4. Asserts the CancelledError propagates to the caller (no CommandResult)
       AND that a SUBSEQUENT command on the same adapter succeeds — proving the
       library cleaned up its pending-response state (no stale message-id
       binding that would deliver the next response to the wrong waiter).
    """
    ws = _FakeWebSocket()
    ocpp = OCPPChargerAdapter(
        OCPPAdapterConfig(
            device_id="ev-001",
            charge_point_id="cp-001",
            command_timeout_s=5.0,  # long; we cancel before timeout fires
        )
    )
    loop_task = await _connect_charger(ws, ocpp)
    try:
        ev_adapter = EVChargerAdapter("ev-001", ocpp)

        first_cmd = _ev_rate_command()

        # Step 1+2: dispatch into the real python-ocpp call path; charger silent.
        first_task = asyncio.create_task(ev_adapter.send_command(first_cmd))
        # Wait until the CALL frame is on the wire — proves we are mid-call.
        sent_call_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
        sent_call = json.loads(sent_call_str)
        assert sent_call[2] == "SetChargingProfile"

        # Step 3: cancel the in-flight task.
        first_task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await first_task
        assert first_task.cancelled() or first_task.exception() is not None, (
            "Cancellation must surface — adapter must NOT swallow CancelledError "
            "into a CommandResult"
        )

        # Step 4: a fresh command on the same adapter must succeed, with the
        # charger's response routed to the new waiter (not the cancelled one).
        second_cmd = _ev_rate_command()
        second_task = asyncio.create_task(ev_adapter.send_command(second_cmd))

        # Wait for the second CALL, then answer it Accepted.
        second_call_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
        second_call = json.loads(second_call_str)
        assert second_call[2] == "SetChargingProfile"
        await ws.client_send(json.dumps([3, second_call[1], {"status": "Accepted"}]))

        second_result = await asyncio.wait_for(second_task, timeout=2.0)
        assert second_result.status is CommandStatus.success, (
            f"Library cleanup after cancellation regressed: post-cancel command "
            f"got {second_result.status.value!r} (reason={second_result.reason!r}) "
            f"— pending-response futures may be leaking across commands"
        )
        assert second_result.correlation_id == second_cmd.correlation_id
    finally:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=2.0)
