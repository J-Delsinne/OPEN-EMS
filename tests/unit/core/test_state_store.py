"""Tests for StateStore publish/read behavior (Story 5.1)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from open_ems.core import (
    BatteryState,
    ComponentState,
    DegradedDeviceState,
    DeviceRole,
    GlobalState,
    GridMeterState,
    InverterState,
    StateStore,
    SystemOperatingMode,
)

_NOW_UTC = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


def _inverter(device_id: str = "inv-001", read_at: datetime = _NOW_UTC) -> InverterState:
    return InverterState(
        device_id=device_id,
        pv_power_kw=3.2,
        ac_power_kw=3.0,
        operating_mode="normal",
        read_at=read_at,
    )


def _battery(device_id: str = "bat-001", read_at: datetime = _NOW_UTC) -> BatteryState:
    return BatteryState(
        device_id=device_id,
        soc_percent=55.0,
        battery_power_kw=1.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=read_at,
    )


def _grid_meter(device_id: str = "grid-001", received_at: datetime = _NOW_UTC) -> GridMeterState:
    return GridMeterState(
        device_id=device_id,
        grid_power_kw=1.2,
        energy_delivered_kwh=100.0,
        energy_returned_kwh=20.0,
        received_at=received_at,
    )


def _degraded(role: DeviceRole, reason: str, device_id: str | None = None) -> DegradedDeviceState:
    return DegradedDeviceState(
        device_id=device_id or f"{role.value}-001",
        role=role,
        reason=reason,
        occurred_at=_NOW_UTC,
    )


def test_initial_snapshot_shape_and_clock_status() -> None:
    store = StateStore(system_clock_status="suspect")
    snapshot = store.get_snapshot()

    assert snapshot.sequence_id == 0
    assert snapshot.global_state == GlobalState.degraded
    assert snapshot.operating_mode == SystemOperatingMode.degraded
    assert snapshot.system_clock_status == "suspect"
    assert snapshot.inverter is None
    assert snapshot.battery is None
    assert snapshot.ev_charger is None
    assert snapshot.grid_meter is None
    assert snapshot.component_states == {
        DeviceRole.inverter: ComponentState.unavailable,
        DeviceRole.battery: ComponentState.unavailable,
        DeviceRole.ev_charger: ComponentState.unavailable,
        DeviceRole.grid_meter: ComponentState.unavailable,
    }


async def test_publish_returns_and_stores_new_snapshot_with_incremented_sequence() -> None:
    store = StateStore(system_clock_status="unknown")

    snapshot = await store.publish(
        {
            DeviceRole.inverter: _inverter(),
            DeviceRole.grid_meter: _grid_meter(),
        }
    )

    assert snapshot.sequence_id == 1
    assert snapshot.global_state == GlobalState.normal
    assert snapshot.system_clock_status == "unknown"
    assert store.get_snapshot() is snapshot


async def test_old_snapshot_remains_unchanged_after_publish() -> None:
    store = StateStore(system_clock_status="valid")
    old_snapshot = store.get_snapshot()

    new_snapshot = await store.publish(
        {
            DeviceRole.inverter: _inverter(),
            DeviceRole.grid_meter: _grid_meter(),
        }
    )

    assert old_snapshot.sequence_id == 0
    assert old_snapshot.inverter is None
    assert new_snapshot.sequence_id == 1
    assert new_snapshot.inverter is not None


async def test_runtime_known_device_preservation_across_publishes() -> None:
    store = StateStore(system_clock_status="valid")

    await store.publish(
        {
            DeviceRole.inverter: _inverter("inv-known"),
            DeviceRole.grid_meter: _grid_meter(),
            DeviceRole.battery: _battery("bat-known"),
        }
    )
    snapshot = await store.publish(
        {
            DeviceRole.inverter: _inverter("inv-known"),
            DeviceRole.grid_meter: _grid_meter(),
        }
    )

    assert isinstance(snapshot.battery, DegradedDeviceState)
    assert snapshot.battery.device_id == "bat-known"
    assert snapshot.battery.reason == "unavailable"
    assert snapshot.component_states[DeviceRole.battery] == ComponentState.unavailable
    assert snapshot.global_state == GlobalState.normal


async def test_publish_rejects_state_type_that_does_not_match_role_key() -> None:
    store = StateStore(system_clock_status="valid")

    try:
        await store.publish(
            {
                DeviceRole.inverter: _battery("bat-wrong-role"),
                DeviceRole.grid_meter: _grid_meter(),
            }
        )
    except ValueError as exc:
        assert "does not match publish key 'inverter'" in str(exc)
    else:
        raise AssertionError("Expected role/type mismatch to raise ValueError")


async def test_publish_rejects_degraded_state_role_that_does_not_match_role_key() -> None:
    store = StateStore(system_clock_status="valid")

    try:
        await store.publish(
            {
                DeviceRole.inverter: _inverter(),
                DeviceRole.grid_meter: _degraded(DeviceRole.battery, "dsmr_stale"),
            }
        )
    except ValueError as exc:
        assert "does not match publish key 'grid_meter'" in str(exc)
    else:
        raise AssertionError("Expected degraded role mismatch to raise ValueError")


async def test_rapid_successive_publishes_have_contiguous_sequence_ids() -> None:
    store = StateStore(system_clock_status="valid")

    sequence_ids = [
        (
            await store.publish(
                {
                    DeviceRole.inverter: _inverter(read_at=_NOW_UTC + timedelta(seconds=index)),
                    DeviceRole.grid_meter: _grid_meter(
                        received_at=_NOW_UTC + timedelta(seconds=index)
                    ),
                }
            )
        ).sequence_id
        for index in range(1, 26)
    ]

    assert sequence_ids == list(range(1, 26))


async def test_concurrent_readers_never_see_torn_snapshots_during_publish_activity() -> None:
    store = StateStore(system_clock_status="valid")
    stop = asyncio.Event()
    observed: list[tuple[int, str | None, str | None, GlobalState]] = []

    async def writer() -> None:
        for index in range(1, 50):
            await store.publish(
                {
                    DeviceRole.inverter: _inverter(f"inv-{index}"),
                    DeviceRole.grid_meter: _grid_meter(f"grid-{index}"),
                }
            )
            await asyncio.sleep(0)
        stop.set()

    async def reader() -> None:
        while not stop.is_set():
            snapshot = store.get_snapshot()
            observed.append(
                (
                    snapshot.sequence_id,
                    snapshot.inverter.device_id if snapshot.inverter else None,
                    snapshot.grid_meter.device_id if snapshot.grid_meter else None,
                    snapshot.global_state,
                )
            )
            await asyncio.sleep(0)

    await asyncio.gather(writer(), *(reader() for _ in range(5)))

    assert observed
    for sequence_id, inverter_id, grid_id, global_state in observed:
        if sequence_id == 0:
            assert inverter_id is None
            assert grid_id is None
            assert global_state == GlobalState.degraded
        else:
            assert inverter_id == f"inv-{sequence_id}"
            assert grid_id == f"grid-{sequence_id}"
            assert global_state == GlobalState.normal


async def test_get_snapshot_does_not_wait_for_writer_lock() -> None:
    store = StateStore(system_clock_status="valid")
    await store._writer_lock.acquire()
    try:
        snapshot = store.get_snapshot()
    finally:
        store._writer_lock.release()

    assert snapshot.sequence_id == 0
