"""Runtime control loop: polls adapters, publishes StateStore, runs decision engine each tick.

Story 8.3 update: the loop dispatches commands through ``RetryPolicy.execute()``
rather than calling ``PolicyGuard.authorize_and_dispatch()`` directly. RetryPolicy
owns retry behaviour, idempotency gating, and the final-failure DEVICE audit
event; this module no longer logs per-command outcomes.

Story 8.4 update: the loop participates in fail-safe lifecycle. It writes a
``LoopLiveness.mark_cycle_complete()`` after each successful cycle (so the
watchdog can gate ``sd_notify`` on real progress); it short-circuits dispatch
on ``recommended_operating_mode=fail_safe`` and emits a SYSTEM audit event on
fresh fail-safe entry; and it gates fail-safe exit on the entry-time degraded
roles having recovered AND the engine's recommendation no longer being
``fail_safe``.

Story 9.0b update: ``_build_evaluation_input`` reads ``peak_limit_kw`` and
``battery_reserve_floor_percent`` from the injected
``ActiveConstraintsProvider`` rather than from ``Settings``. The provider is
hydrated once at lifespan startup and atomically refreshed by the installer
activation endpoint; this loop and ``PolicyGuard`` share the same provider
instance, so within one evaluation cycle both layers see the same values.

Story 9.0d update: ``_monthly_peak_kw`` is now seeded via the
``initial_monthly_peak_kw`` constructor argument, populated by the lifespan
from ``EnergyRepo.get_current_monthly_peak_kw()`` BEFORE the loop is
constructed. The prior behaviour of initializing to ``0.0`` and refreshing
only on the next interval rollover meant the first ≤15 min of peak-limit
decisions after every restart silently used stale data; the installer
wizard restarts the process, so this window was user-visible. The
in-loop refresh inside ``_update_tracker`` (post-rollover re-query of
``MAX(avg_power_kw)``) is unchanged — the seed only affects the first
interval after restart.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from datetime import UTC, datetime

import structlog
from pydantic import ValidationError

from open_ems.core import (
    DegradedDeviceState,
    DeviceAdapter,
    DeviceRole,
    GridMeterState,
    StateStore,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.core.state import DeviceSlot
from open_ems.engine import (
    BatteryControlContext,
    EvaluationInput,
    EvaluationResult,
    EVSchedulingContext,
    PartialIntervalTracker,
    evaluate_cycle,
)
from open_ems.engine.intent_executor import IntentExecutor
from open_ems.engine.retry_policy import RetryPolicy
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.services.audit_log import ObservabilityService
from open_ems.services.loop_liveness import LoopLiveness
from open_ems.settings import Settings
from open_ems.storage.repositories.energy_repo import EnergyRepo

ADAPTER_GET_STATE_TIMEOUT_SECONDS: float = 10.0

# Static role-to-snapshot-attribute mapping (avoids reflection — see Dev Notes).
_SNAPSHOT_SLOT_ATTR: Mapping[DeviceRole, str] = {
    DeviceRole.inverter: "inverter",
    DeviceRole.battery: "battery",
    DeviceRole.ev_charger: "ev_charger",
    DeviceRole.grid_meter: "grid_meter",
}

# Guard: a future DeviceRole added without a corresponding entry would silently
# drop that role from fail-safe accounting. Fail loud at import time instead.
assert set(_SNAPSHOT_SLOT_ATTR) == set(DeviceRole), (
    f"_SNAPSHOT_SLOT_ATTR is missing entries for: "
    f"{sorted(r.value for r in set(DeviceRole) - set(_SNAPSHOT_SLOT_ATTR))}"
)

logger = structlog.get_logger(__name__)


class ControlLoop:
    """Periodic adapter polling and decision-engine evaluation."""

    def __init__(
        self,
        *,
        state_store: StateStore,
        adapters: Mapping[DeviceRole, DeviceAdapter],
        energy_repo: EnergyRepo,
        settings: Settings,
        intent_executor: IntentExecutor,
        retry_policy: RetryPolicy,
        loop_liveness: LoopLiveness,
        observability: ObservabilityService,
        active_constraints: ActiveConstraintsProvider,
        initial_monthly_peak_kw: float = 0.0,
    ) -> None:
        if initial_monthly_peak_kw < 0.0 or not math.isfinite(initial_monthly_peak_kw):
            raise ValueError("initial_monthly_peak_kw must be a finite non-negative float")
        self._state_store = state_store
        self._adapters = adapters
        self._energy_repo = energy_repo
        self._settings = settings
        self._intent_executor = intent_executor
        self._retry_policy = retry_policy
        self._loop_liveness = loop_liveness
        self._observability = observability
        self._active_constraints = active_constraints
        self._tracker = PartialIntervalTracker.from_current_time(at=datetime.now(UTC))
        self._monthly_peak_kw = initial_monthly_peak_kw
        self._last_mode: SystemOperatingMode | None = None
        self._fail_safe_entry_degraded_roles: frozenset[DeviceRole] = frozenset()

    async def run(self) -> None:
        """Run the control loop until cancelled."""
        while True:
            await self._tick()
            await asyncio.sleep(self._settings.control_loop_interval_seconds)

    async def _tick(self) -> None:
        now = datetime.now(UTC)
        device_states = await self._poll_adapters()
        snapshot = await self._state_store.publish(
            device_states,
            operating_mode=self._last_mode,
        )
        await self._update_tracker(device_states, now)
        try:
            evaluation_input = self._build_evaluation_input(snapshot, now)
        except ValidationError:
            logger.debug(
                "evaluation_skipped_missing_required_slots",
                inverter_present=snapshot.inverter is not None,
                grid_meter_present=snapshot.grid_meter is not None,
                component="engine",
            )
            return
        result = evaluate_cycle(evaluation_input)
        await self._handle_evaluation_result(result, snapshot)
        # Mark cycle completion ONLY after a full evaluation has run end-to-end
        # (fail-safe cycles count — the engine produced a decision; the
        # missing-slots early-return does NOT count — engine never ran).
        self._loop_liveness.mark_cycle_complete()

    async def _poll_adapters(self) -> dict[DeviceRole, DeviceSlot]:
        device_states: dict[DeviceRole, DeviceSlot] = {}
        for role, adapter in self._adapters.items():
            try:
                state = await asyncio.wait_for(
                    adapter.get_state(), timeout=ADAPTER_GET_STATE_TIMEOUT_SECONDS
                )
            except Exception as exc:  # noqa: BLE001 — AC1 mandates converting all adapter failures
                state = DegradedDeviceState(
                    device_id=adapter.device_id,
                    role=role,
                    reason=repr(exc) or type(exc).__name__,
                    occurred_at=datetime.now(UTC),
                )
            device_states[role] = state
        return device_states

    async def _update_tracker(
        self,
        device_states: Mapping[DeviceRole, DeviceSlot],
        now: datetime,
    ) -> None:
        grid_power_kw, tick_quality = _extract_grid_power(device_states)
        completed = self._tracker.update(grid_power_kw, at=now)
        if completed is not None:
            persist_quality = (
                "complete"
                if completed.sample_count > 0 and tick_quality == "complete"
                else "incomplete"
            )
            await self._energy_repo.write_peak_interval(completed, data_quality=persist_quality)
            self._monthly_peak_kw = await self._energy_repo.get_current_monthly_peak_kw()

    def _build_evaluation_input(
        self,
        snapshot: SystemSnapshot,
        now: datetime,
    ) -> EvaluationInput:
        constraints = self._active_constraints.get()
        peak_context = self._tracker.build_peak_context(
            configured_peak_limit_kw=constraints.peak_limit_kw,
            current_monthly_recorded_peak_kw=self._monthly_peak_kw,
            at=now,
        )
        return EvaluationInput.from_snapshot(
            snapshot,
            peak_context=peak_context,
            strategy=snapshot.active_strategy,
            battery_control=BatteryControlContext(
                reserve_floor_percent=constraints.battery_reserve_floor_percent,
                capability_profile=None,
            ),
            ev_scheduling=EVSchedulingContext(
                capability_profile=None,
                charging_window=None,
                homeowner_override_active=False,
                evaluated_at=now,
                target_charge_rate_kw=None,
            ),
        )

    async def _handle_evaluation_result(
        self,
        result: EvaluationResult,
        snapshot: SystemSnapshot,
    ) -> None:
        """Apply fail-safe lifecycle then dispatch commands through RetryPolicy."""
        new_mode = result.recommended_operating_mode
        logger.debug(
            "evaluation_cycle_complete",
            cycle_id=str(result.cycle_id),
            operating_mode=new_mode.value,
            cycle_duration_ms=result.cycle_duration_ms,
            intent_count=len(result.intents),
            component="engine",
        )

        # AC4 — fail-safe entry: short-circuit dispatch entirely.
        # mark_fail_safe_entered() runs BEFORE the audit await so a CancelledError
        # propagating out of the audit cannot leave the state machine in a half-
        # entered state (next cycle would otherwise re-enter and double-emit).
        if new_mode is SystemOperatingMode.fail_safe:
            self._last_mode = SystemOperatingMode.fail_safe
            if not self._loop_liveness.in_fail_safe:
                self._fail_safe_entry_degraded_roles = self._degraded_roles_in(snapshot)
                self._loop_liveness.mark_fail_safe_entered()
                await self._emit_fail_safe_entry_audit(result, snapshot)
            return

        # AC5 — fail-safe exit: gate on engine recommendation AND entry-degraded recovery.
        # _last_mode is deliberately NOT updated to `new_mode` until exit succeeds —
        # otherwise the NEXT _tick would publish a snapshot with operating_mode=normal
        # while the loop is still suppressing dispatch (in_fail_safe=True), making
        # downstream observers see contradictory state.
        if self._loop_liveness.in_fail_safe:
            if not self._can_exit_fail_safe(snapshot):
                # Engine no longer recommends fail-safe but degraded devices
                # haven't all recovered — stay in fail-safe; suppress dispatch.
                self._last_mode = SystemOperatingMode.fail_safe
                return
            self._loop_liveness.mark_fail_safe_exited()
            await self._emit_fail_safe_exit_audit(result, snapshot)
            self._fail_safe_entry_degraded_roles = frozenset()
            # Fall through: dispatch is allowed on the recovery cycle.

        self._last_mode = new_mode
        # Normal dispatch path (Story 8.3 unchanged)
        commands = self._intent_executor.translate(result, snapshot)
        for cmd in commands:
            await self._retry_policy.execute(cmd)

    # ── Fail-safe helpers (Story 8.4) ──────────────────────────────────────

    def _degraded_roles_in(self, snapshot: SystemSnapshot) -> frozenset[DeviceRole]:
        roles: set[DeviceRole] = set()
        for role, attr in _SNAPSHOT_SLOT_ATTR.items():
            slot = getattr(snapshot, attr)
            if isinstance(slot, DegradedDeviceState):
                roles.add(role)
        return frozenset(roles)

    def _can_exit_fail_safe(self, snapshot: SystemSnapshot) -> bool:
        for role in self._fail_safe_entry_degraded_roles:
            slot = getattr(snapshot, _SNAPSHOT_SLOT_ATTR[role])
            if slot is None or isinstance(slot, DegradedDeviceState):
                return False
        return True

    async def _emit_fail_safe_entry_audit(
        self,
        result: EvaluationResult,
        snapshot: SystemSnapshot,
    ) -> None:
        device_summary = _format_degraded_roles(self._fail_safe_entry_degraded_roles)
        summary = f"Fail-safe entered: {device_summary} (cycle {result.cycle_id})"
        try:
            await self._observability.audit(
                actor="system",
                event_type="SYSTEM",
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001 — audit failure must not block fail-safe
            logger.error(
                "audit_emit_failed",
                component="engine",
                cycle_id=str(result.cycle_id),
                error=repr(exc),
                exc_info=True,
            )

    async def _emit_fail_safe_exit_audit(
        self,
        result: EvaluationResult,
        snapshot: SystemSnapshot,
    ) -> None:
        recovered = _format_degraded_roles(self._fail_safe_entry_degraded_roles)
        summary = (
            f"Fail-safe exited: {recovered} restored; "
            f"cycle {result.cycle_id} completed without errors"
        )
        try:
            await self._observability.audit(
                actor="system",
                event_type="SYSTEM",
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001 — audit failure must not block fail-safe exit
            logger.error(
                "audit_emit_failed",
                component="engine",
                cycle_id=str(result.cycle_id),
                error=repr(exc),
                exc_info=True,
            )


def _extract_grid_power(
    device_states: Mapping[DeviceRole, DeviceSlot],
) -> tuple[float, str]:
    slot = device_states.get(DeviceRole.grid_meter)
    if isinstance(slot, GridMeterState):
        return slot.grid_power_kw, "complete"
    return 0.0, "incomplete"


def _format_degraded_roles(roles: frozenset[DeviceRole]) -> str:
    return ", ".join(sorted(role.value for role in roles)) or "(no specific role)"
