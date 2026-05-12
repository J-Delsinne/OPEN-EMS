"""Async single-writer StateStore with lock-free snapshot reads.

Writer-lock invariant (Story 10.2 + 10.3): every single-field mutator
(``set_ev_override``, ``set_active_strategy``) MUST acquire
``self._writer_lock``, update the in-memory field, AND patch the published
snapshot via ``previous.model_copy(update={...})`` so ``get_snapshot()``
reflects the mutation immediately without waiting for the next ``publish()``.
The reader path inside ``publish()`` also runs under the same lock, so
single-field state cannot tear between read and snapshot-build.
``get_snapshot()`` itself is lock-free because the snapshot is immutable;
replacement is atomic.

Single-field mutators do NOT advance ``sequence_id`` or ``captured_at`` —
bumping the sequence on a non-publish mutation would emit a torn snapshot to
SSE/age consumers (the previous publish's ``captured_at`` would mismatch the
new sequence number).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import structlog

from open_ems.core.devices import (
    BatteryState,
    DegradedDeviceState,
    DeviceRole,
    DeviceState,
    EVChargerState,
    GridMeterState,
    InverterState,
)
from open_ems.core.state import (
    ALL_DEVICE_ROLES,
    ClockStatus,
    ComponentState,
    DeviceSlot,
    EnergyStrategy,
    EVOverrideState,
    GlobalState,
    SystemOperatingMode,
    SystemSnapshot,
    derive_component_state,
    derive_data_age_seconds,
    derive_global_state,
)

logger = structlog.get_logger(__name__)


class StateStore:
    """Holds the latest immutable SystemSnapshot.

    Writes are serialized with an asyncio lock. Reads are plain reference returns
    because snapshots are fully built before atomic replacement.
    """

    def __init__(
        self,
        *,
        system_clock_status: ClockStatus,
        stale_threshold_seconds: int = 30,
        operating_mode: SystemOperatingMode = SystemOperatingMode.degraded,
        active_strategy: EnergyStrategy = EnergyStrategy.maximize_self_consumption,
    ) -> None:
        self._writer_lock = asyncio.Lock()
        self._known_device_ids: dict[DeviceRole, str] = {}
        self._last_successful_states: dict[DeviceRole, DeviceState] = {}
        self._stale_threshold_seconds = stale_threshold_seconds
        self._active_strategy = active_strategy
        # Story 10.2 AC3: in-memory-only; cold-start default is None.
        # Writer-lock invariant: only ``set_ev_override`` and ``publish`` mutate this.
        self._active_ev_override: EVOverrideState | None = None
        captured_at = datetime.now(UTC)
        self._snapshot = SystemSnapshot(
            sequence_id=0,
            captured_at=captured_at,
            global_state=GlobalState.degraded,
            operating_mode=operating_mode,
            active_strategy=active_strategy,
            inverter=None,
            battery=None,
            ev_charger=None,
            grid_meter=None,
            component_states=dict.fromkeys(ALL_DEVICE_ROLES, ComponentState.unavailable),
            data_age_seconds=dict.fromkeys(ALL_DEVICE_ROLES),
            system_clock_status=system_clock_status,
            active_ev_override=None,
        )

    async def set_active_strategy(self, strategy: EnergyStrategy) -> bool:
        """Install a new active strategy under the writer lock (Story 10.3 AC1).

        Mirrors the ``set_ev_override`` canonical pattern: writer-lock + in-lock
        idempotency check + snapshot patch via ``previous.model_copy(update={...})``
        so ``get_snapshot()`` reflects immediately. ``sequence_id`` and
        ``captured_at`` are intentionally NOT advanced (single-field patch ≠
        publish cycle).

        Returns ``True`` if the strategy actually changed (caller can suppress a
        no-op audit emission); ``False`` if ``strategy`` already matched the
        in-lock current value and no mutation was applied.
        """
        async with self._writer_lock:
            if self._active_strategy == strategy:
                return False
            previous_strategy = self._active_strategy
            self._active_strategy = strategy
            previous_snapshot = self._snapshot
            self._snapshot = previous_snapshot.model_copy(update={"active_strategy": strategy})
            # Emit inside the lock so concurrent mutators cannot interleave
            # their log records relative to their mutation order. structlog is
            # non-blocking under standard configuration.
            logger.info(
                "active_strategy_changed",
                previous_strategy=previous_strategy.value,
                new_strategy=strategy.value,
                component="state_store",
            )
        return True

    async def set_ev_override(self, override: EVOverrideState | None) -> None:
        """Install or clear the active EV override under the writer lock (Story 10.2 AC3).

        Patches the published snapshot's ``active_ev_override`` so
        ``get_snapshot()`` reflects the change immediately. The snapshot's
        ``sequence_id`` and ``captured_at`` are intentionally NOT advanced —
        this is a single-field patch, not a publish cycle, and bumping the
        sequence while ``captured_at`` / ``data_age_seconds`` stayed frozen
        from the previous publish would emit a torn snapshot to consumers.
        The next ``publish()`` advances ``sequence_id`` normally.
        """
        async with self._writer_lock:
            previous_override = self._active_ev_override
            self._active_ev_override = override
            previous = self._snapshot
            self._snapshot = previous.model_copy(update={"active_ev_override": override})
            # Story 10.3 R7 patch — symmetric lifecycle observability with
            # set_active_strategy. Closes the 10.2 deferred items at
            # state_store.py:86-104 ("no INFO emission distinguishing install
            # vs clear") and :96-104 ("silently displaces existing non-None
            # override without log/audit"). Emitted inside the lock so the
            # ordering of records matches the ordering of mutations under
            # concurrent access.
            if override is None and previous_override is not None:
                logger.info(
                    "ev_override_cleared_via_mutator",
                    previous_correlation_id=str(previous_override.correlation_id),
                    previous_dispatch_status=previous_override.dispatch_status,
                    component="state_store",
                )
            elif override is not None and previous_override is None:
                logger.info(
                    "ev_override_installed",
                    correlation_id=str(override.correlation_id),
                    dispatch_status=override.dispatch_status,
                    component="state_store",
                )
            elif (
                override is not None
                and previous_override is not None
                and override.correlation_id != previous_override.correlation_id
            ):
                logger.info(
                    "ev_override_displaced",
                    previous_correlation_id=str(previous_override.correlation_id),
                    previous_dispatch_status=previous_override.dispatch_status,
                    new_correlation_id=str(override.correlation_id),
                    new_dispatch_status=override.dispatch_status,
                    component="state_store",
                )

    async def compare_and_set_ev_override(
        self,
        expected_correlation_id: uuid.UUID,
        updated: EVOverrideState | None,
    ) -> bool:
        """Atomic compare-and-swap on the override's ``correlation_id`` (Story 10.2 AC7).

        Returns True if the swap succeeded (current override matched the
        expected correlation_id under the writer lock), False otherwise. The
        background dispatch task uses this to settle ``dispatch_status`` only
        when its own override is still the active one — avoiding the TOCTOU
        between a lock-free ``get_snapshot()`` read and a subsequent
        ``set_ev_override`` that ``publish()`` could race past.
        """
        async with self._writer_lock:
            current = self._active_ev_override
            if current is None or current.correlation_id != expected_correlation_id:
                return False
            self._active_ev_override = updated
            previous = self._snapshot
            self._snapshot = previous.model_copy(update={"active_ev_override": updated})
            return True

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
            for role in ALL_DEVICE_ROLES:
                if self._is_stale_successful_state(slots[role], data_age_seconds[role]):
                    component_states[role] = ComponentState.stale
            self._evaluate_ev_override(slots[DeviceRole.ev_charger], captured_at)
            snapshot = SystemSnapshot(
                sequence_id=self._snapshot.sequence_id + 1,
                captured_at=captured_at,
                global_state=derive_global_state(slots),
                operating_mode=operating_mode or self._snapshot.operating_mode,
                active_strategy=self._active_strategy,
                inverter=slots[DeviceRole.inverter],
                battery=slots[DeviceRole.battery],
                ev_charger=slots[DeviceRole.ev_charger],
                grid_meter=slots[DeviceRole.grid_meter],
                component_states=component_states,
                data_age_seconds=data_age_seconds,
                system_clock_status=self._snapshot.system_clock_status,
                active_ev_override=self._active_ev_override,
            )
            self._snapshot = snapshot
            return snapshot

    def _evaluate_ev_override(self, ev_slot: DeviceSlot, captured_at: datetime) -> None:
        """AC4 three-clause evaluation — load-bearing order.

        1. Expiry-clear: ``expires_at <= captured_at`` → clear.
        2. Session-flip detection: pending override + ``session_active=True``
           → flip ``session_observed_active=True``.
        3. Natural-completion clear: ``session_observed_active=True`` AND
           current ``session_active=False`` (or no EVChargerState) → clear.

        Caller must hold ``_writer_lock``.
        """
        override = self._active_ev_override
        if override is None:
            return
        # Clause 1 — expiry clear (evaluated FIRST so an expired override does
        # not leak ``session_observed_active=True`` into the downstream snapshot).
        if override.expires_at <= captured_at:
            logger.info(
                "ev_override_expired",
                correlation_id=str(override.correlation_id),
                dispatch_status=override.dispatch_status,
                component="state_store",
            )
            self._active_ev_override = None
            return
        # Clauses 2 + 3 only consider real EVChargerState — degraded/None slots
        # cannot drive session-flip semantics.
        if not isinstance(ev_slot, EVChargerState):
            return
        # Clause 2 — session-flip detection.
        if (
            override.dispatch_status == "pending"
            and ev_slot.session_active
            and not override.session_observed_active
        ):
            self._active_ev_override = override.model_copy(update={"session_observed_active": True})
            logger.info(
                "ev_override_confirmed_observed",
                correlation_id=str(override.correlation_id),
                component="state_store",
            )
            return
        # Clause 3 — natural-completion clear.
        if override.session_observed_active and not ev_slot.session_active:
            logger.info(
                "ev_override_session_completed",
                correlation_id=str(override.correlation_id),
                component="state_store",
            )
            self._active_ev_override = None

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
                slots[role] = self._last_successful_states.get(
                    role
                ) or self._unavailable_known_device(role, captured_at)
                continue
            self._validate_state_role(role, incoming_state)
            slots[role] = incoming_state
            self._known_device_ids[role] = incoming_state.device_id
            if not isinstance(incoming_state, DegradedDeviceState):
                self._last_successful_states[role] = incoming_state
        return slots

    def _is_stale_successful_state(self, state: DeviceSlot, age_seconds: int | None) -> bool:
        if state is None or isinstance(state, DegradedDeviceState) or age_seconds is None:
            return False
        return age_seconds > self._stale_threshold_seconds

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
