"""OCPP transaction-lifecycle and concurrency tests.

Covers Story 9.0 code-review findings:
- HIGH H1 — transaction id preserved across WebSocket reconnect (no boot)
- HIGH H2 — send_raw_command serializes concurrent calls
- HIGH H3 — overlapping StartTransaction logs a warning before overwriting
- HIGH H4 — mismatched StopTransaction logs a warning and does NOT clear local id
- MED  M2 — next_charging_profile_id preserved across reconnect, reset on boot
- MED  M3 — last_known_transaction_id reset on BootNotification

These tests exercise the OCPPChargerAdapter handlers and connection lifecycle
directly via the @on handler methods on _InternalChargePoint — no FastAPI, no
real WebSocket, no ocpp.ChargePoint.start() loop. The state-transition
contracts under test are independent of the WebSocket transport.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog.testing

from open_ems.adapters.ocpp.central_system import (
    OCPPAdapterConfig,
    OCPPChargerAdapter,
    _ChargerState,
    _InternalChargePoint,
)
from open_ems.adapters.protocol import RawProtocolCommand

pytestmark = pytest.mark.asyncio


def _config() -> OCPPAdapterConfig:
    return OCPPAdapterConfig(device_id="ev-1", charge_point_id="cp-1")


def _make_handler(state: _ChargerState) -> _InternalChargePoint:
    """Construct a handler tied to a state without opening a real connection."""

    class _NullConnection:
        async def send(self, _: str) -> None: ...
        async def recv(self) -> str:  # pragma: no cover — never called in these tests
            await asyncio.Future()
            return ""

    return _InternalChargePoint("cp-1", _NullConnection(), state, response_timeout=2.0)


def _start_transaction(handler: _InternalChargePoint, *, connector_id: int = 1) -> int:
    response = handler.on_start_transaction(
        connector_id=connector_id,
        id_tag="user-1",
        meter_start=0,
        timestamp="2026-05-10T12:00:00Z",
    )
    return int(response.transaction_id)


def _stop_transaction(handler: _InternalChargePoint, *, transaction_id: int) -> None:
    handler.on_stop_transaction(
        meter_stop=0,
        timestamp="2026-05-10T12:30:00Z",
        transaction_id=transaction_id,
    )


def _boot(handler: _InternalChargePoint) -> None:
    handler.on_boot_notification(
        charge_point_model="Pulsar",
        charge_point_vendor="Wallbox",
    )


# ---------------------------------------------------------------------------
# H1 / M3 — transaction id preserved on reconnect; cleared on boot
# ---------------------------------------------------------------------------


async def test_transaction_id_preserved_across_reconnect_without_boot() -> None:
    """Adapter's view of the active transaction survives a WebSocket reconnect.

    OCPP 1.6 transactions persist on the charger across a transport flap, so the
    EMS must not silently lose track of the active id (which would refuse a
    legitimate StopEVChargingCommand with no_active_ocpp_transaction).
    """
    adapter = OCPPChargerAdapter(_config())

    # First session
    state1 = _ChargerState()
    state1.connected = True
    adapter._state = state1
    handler1 = _make_handler(state1)
    _boot(handler1)
    tx_id = _start_transaction(handler1)
    assert adapter.active_transaction_id == tx_id

    # Simulate reconnect (no boot): handle_connection-equivalent — preserve fields
    previous_state = adapter._state
    state2 = _ChargerState()
    state2.connected = True
    state2.last_known_transaction_id = previous_state.last_known_transaction_id
    state2.active_transaction_connector_id = previous_state.active_transaction_connector_id
    state2.next_charging_profile_id = previous_state.next_charging_profile_id
    adapter._state = state2

    assert adapter.active_transaction_id == tx_id, (
        "Transaction id must survive a WebSocket reconnect without a boot"
    )


async def test_boot_notification_clears_transaction_state() -> None:
    """Charger reset wipes any prior transaction and profile-id state."""
    adapter = OCPPChargerAdapter(_config())
    state = _ChargerState()
    state.connected = True
    adapter._state = state
    handler = _make_handler(state)

    _boot(handler)
    tx_id = _start_transaction(handler)
    # Burn a few profile ids
    adapter.reserve_charging_profile_id()
    adapter.reserve_charging_profile_id()
    assert adapter.active_transaction_id == tx_id

    # Second BootNotification — charger has reset
    _boot(handler)
    assert adapter.active_transaction_id is None
    assert adapter._state.active_transaction_connector_id is None
    # Profile id counter resets to 1 (charger-side profiles wiped)
    assert adapter.reserve_charging_profile_id() == 1


# ---------------------------------------------------------------------------
# H3 — overlapping StartTransaction logs warning before overwriting
# ---------------------------------------------------------------------------


async def test_overlapping_start_transaction_logs_warning_and_overwrites() -> None:
    adapter = OCPPChargerAdapter(_config())
    state = _ChargerState()
    state.connected = True
    adapter._state = state
    handler = _make_handler(state)

    _boot(handler)
    first_id = _start_transaction(handler, connector_id=1)
    assert adapter.active_transaction_id == first_id

    with structlog.testing.capture_logs() as logs:
        second_id = _start_transaction(handler, connector_id=2)

    assert second_id > first_id, "New transaction id must be monotonic"
    assert adapter.active_transaction_id == second_id
    assert state.active_transaction_connector_id == 2

    overlap_logs = [le for le in logs if le.get("event") == "ocpp_overlapping_start_transaction"]
    assert overlap_logs, "Expected a warning when a second StartTransaction arrives without a Stop"
    assert overlap_logs[0]["previous_transaction_id"] == first_id
    assert overlap_logs[0]["previous_connector_id"] == 1
    assert overlap_logs[0]["new_connector_id"] == 2


# ---------------------------------------------------------------------------
# H4 — mismatched StopTransaction logs warning, does NOT clear local state
# ---------------------------------------------------------------------------


async def test_mismatched_stop_transaction_logs_warning_preserves_local_id() -> None:
    adapter = OCPPChargerAdapter(_config())
    state = _ChargerState()
    state.connected = True
    adapter._state = state
    handler = _make_handler(state)

    _boot(handler)
    tx_id = _start_transaction(handler)
    assert adapter.active_transaction_id == tx_id

    with structlog.testing.capture_logs() as logs:
        # Charger sends StopTransaction with a different id (network desync /
        # firmware bug). Adapter must log and KEEP the local id — clearing it
        # would lose track of what the EMS believes is still active.
        _stop_transaction(handler, transaction_id=tx_id + 99)

    mismatch_logs = [le for le in logs if le.get("event") == "ocpp_stop_transaction_id_mismatch"]
    assert mismatch_logs, "Expected a mismatch warning"
    assert mismatch_logs[0]["expected_transaction_id"] == tx_id
    assert mismatch_logs[0]["received_transaction_id"] == tx_id + 99

    assert adapter.active_transaction_id == tx_id, (
        "Local id must not be cleared by a mismatched StopTransaction"
    )


async def test_matching_stop_transaction_clears_local_id_without_warning() -> None:
    adapter = OCPPChargerAdapter(_config())
    state = _ChargerState()
    state.connected = True
    adapter._state = state
    handler = _make_handler(state)

    _boot(handler)
    tx_id = _start_transaction(handler)

    with structlog.testing.capture_logs() as logs:
        _stop_transaction(handler, transaction_id=tx_id)

    assert not [le for le in logs if le.get("event") == "ocpp_stop_transaction_id_mismatch"]
    assert adapter.active_transaction_id is None
    assert state.active_transaction_connector_id is None


# ---------------------------------------------------------------------------
# M2 — profile id preserved across reconnect (no boot); reset on boot
# ---------------------------------------------------------------------------


async def test_next_charging_profile_id_preserved_across_reconnect() -> None:
    adapter = OCPPChargerAdapter(_config())
    state = _ChargerState()
    state.connected = True
    adapter._state = state

    _boot(_make_handler(state))
    assert adapter.reserve_charging_profile_id() == 1
    assert adapter.reserve_charging_profile_id() == 2

    # Reconnect without boot
    previous_state = adapter._state
    state2 = _ChargerState()
    state2.connected = True
    state2.last_known_transaction_id = previous_state.last_known_transaction_id
    state2.active_transaction_connector_id = previous_state.active_transaction_connector_id
    state2.next_charging_profile_id = previous_state.next_charging_profile_id
    adapter._state = state2

    # Counter continues from 3, NOT reset to 1 — avoids overwriting profiles
    # that the charger still has persisted.
    assert adapter.reserve_charging_profile_id() == 3


# ---------------------------------------------------------------------------
# H2 — send_raw_command serializes concurrent dispatch
# ---------------------------------------------------------------------------


async def test_send_raw_command_serializes_concurrent_calls() -> None:
    """Concurrent send_raw_command must not overlap inside the lock.

    We verify by asserting the in-flight counter never exceeds 1 across two
    concurrent invocations.
    """
    adapter = OCPPChargerAdapter(_config())
    state = _ChargerState()
    state.connected = True
    adapter._state = state

    in_flight = 0
    max_observed = 0
    barrier = asyncio.Event()

    async def fake_locked(_command: RawProtocolCommand) -> Any:
        nonlocal in_flight, max_observed
        in_flight += 1
        max_observed = max(max_observed, in_flight)
        # Hold the lock long enough for both tasks to attempt entry
        await barrier.wait()
        in_flight -= 1
        return adapter._state  # arbitrary non-None sentinel; not inspected

    adapter._send_raw_command_locked = fake_locked  # type: ignore[assignment]

    def _raw(cid: str) -> RawProtocolCommand:
        return RawProtocolCommand(
            correlation_id=cid,
            device_id="ev-1",
            command_name="SetChargingProfile",
            payload={},
        )

    t1 = asyncio.create_task(adapter.send_raw_command(_raw("a")))
    t2 = asyncio.create_task(adapter.send_raw_command(_raw("b")))

    # Yield so the lock holder reaches the barrier.wait().
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert in_flight <= 1, "Lock failed to serialize concurrent dispatch"

    barrier.set()
    await asyncio.gather(t1, t2)

    assert max_observed == 1, (
        "Concurrent send_raw_command calls overlapped inside the lock-protected region"
    )
