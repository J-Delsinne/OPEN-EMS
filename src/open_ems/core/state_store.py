"""Async single-writer StateStore with lock-free snapshot reads."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime

from open_ems.core.devices import (
    BatteryState,
    DegradedDeviceState,
    DeviceRole,
    EVChargerState,
    GridMeterState,
    InverterState,
)
from open_ems.core.state import (
    ALL_DEVICE_ROLES,
    ClockStatus,
    ComponentState,
    DeviceSlot,
    GlobalState,
    SystemOperatingMode,
    SystemSnapshot,
    derive_component_state,
    derive_data_age_seconds,
    derive_global_state,
)


class StateStore:
    """Holds the latest immutable SystemSnapshot.

    Writes are serialized with an asyncio lock. Reads are plain reference returns
    because snapshots are fully built before atomic replacement.
    """

    def __init__(
        self,
        *,
        system_clock_status: ClockStatus,
        operating_mode: SystemOperatingMode = SystemOperatingMode.degraded,
    ) -> None:
        self._writer_lock = asyncio.Lock()
        self._known_device_ids: dict[DeviceRole, str] = {}
        captured_at = datetime.now(UTC)
        self._snapshot = SystemSnapshot(
            sequence_id=0,
            captured_at=captured_at,
            global_state=GlobalState.degraded,
            operating_mode=operating_mode,
            inverter=None,
            battery=None,
            ev_charger=None,
            grid_meter=None,
            component_states=dict.fromkeys(ALL_DEVICE_ROLES, ComponentState.unavailable),
            data_age_seconds=dict.fromkeys(ALL_DEVICE_ROLES),
            system_clock_status=system_clock_status,
        )

    async def publish(
        self,
        device_states: Mapping[DeviceRole, DeviceSlot],
        operating_mode: SystemOperatingMode | None = None,
    ) -> SystemSnapshot:
        """Publish a complete replacement snapshot from already-normalized state."""
        async with self._writer_lock:
            captured_at = datetime.now(UTC)
            slots = self._build_slots(device_states, captured_at)
            component_states = {
                role: derive_component_state(slots[role]) for role in ALL_DEVICE_ROLES
            }
            data_age_seconds = {
                role: derive_data_age_seconds(slots[role], captured_at) for role in ALL_DEVICE_ROLES
            }
            snapshot = SystemSnapshot(
                sequence_id=self._snapshot.sequence_id + 1,
                captured_at=captured_at,
                global_state=derive_global_state(slots),
                operating_mode=operating_mode or self._snapshot.operating_mode,
                inverter=slots[DeviceRole.inverter],
                battery=slots[DeviceRole.battery],
                ev_charger=slots[DeviceRole.ev_charger],
                grid_meter=slots[DeviceRole.grid_meter],
                component_states=component_states,
                data_age_seconds=data_age_seconds,
                system_clock_status=self._snapshot.system_clock_status,
            )
            self._snapshot = snapshot
            return snapshot

    def get_snapshot(self) -> SystemSnapshot:
        """Return the latest immutable snapshot without taking a lock."""
        return self._snapshot

    def _build_slots(
        self,
        device_states: Mapping[DeviceRole, DeviceSlot],
        captured_at: datetime,
    ) -> dict[DeviceRole, DeviceSlot]:
        slots: dict[DeviceRole, DeviceSlot] = {}
        for role in ALL_DEVICE_ROLES:
            incoming_state = device_states.get(role)
            if incoming_state is None:
                slots[role] = self._unavailable_known_device(role, captured_at)
                continue
            self._validate_state_role(role, incoming_state)
            slots[role] = incoming_state
            self._known_device_ids[role] = incoming_state.device_id
        return slots

    def _validate_state_role(self, role: DeviceRole, state: DeviceSlot) -> None:
        if state is None:
            return
        if isinstance(state, DegradedDeviceState):
            if state.role != role:
                raise ValueError(
                    f"DegradedDeviceState role {state.role.value!r} does not match "
                    f"publish key {role.value!r}"
                )
            return
        expected_types = {
            DeviceRole.inverter: InverterState,
            DeviceRole.battery: BatteryState,
            DeviceRole.ev_charger: EVChargerState,
            DeviceRole.grid_meter: GridMeterState,
        }
        if not isinstance(state, expected_types[role]):
            raise ValueError(
                f"State type {type(state).__name__} does not match publish key {role.value!r}"
            )

    def _unavailable_known_device(
        self,
        role: DeviceRole,
        captured_at: datetime,
    ) -> DegradedDeviceState | None:
        device_id = self._known_device_ids.get(role)
        if device_id is None:
            return None
        return DegradedDeviceState(
            device_id=device_id,
            role=role,
            reason="unavailable",
            occurred_at=captured_at,
        )
