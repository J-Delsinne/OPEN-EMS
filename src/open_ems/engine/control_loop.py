"""Runtime control loop: polls adapters, publishes StateStore, runs decision engine each tick.

Story 8.3 update: the loop dispatches commands through ``RetryPolicy.execute()``
rather than calling ``PolicyGuard.authorize_and_dispatch()`` directly. RetryPolicy
owns retry behaviour, idempotency gating, and the final-failure DEVICE audit
event; this module no longer logs per-command outcomes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime

import structlog
from pydantic import ValidationError

from open_ems.core import (
    DegradedDeviceState,
    DeviceAdapter,
    DeviceRole,
    EnergyStrategy,
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
from open_ems.settings import Settings
from open_ems.storage.repositories.energy_repo import EnergyRepo

ADAPTER_GET_STATE_TIMEOUT_SECONDS: float = 10.0

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
    ) -> None:
        self._state_store = state_store
        self._adapters = adapters
        self._energy_repo = energy_repo
        self._settings = settings
        self._intent_executor = intent_executor
        self._retry_policy = retry_policy
        self._tracker = PartialIntervalTracker.from_current_time(at=datetime.now(UTC))
        self._monthly_peak_kw: float = 0.0
        self._last_mode: SystemOperatingMode | None = None

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
        peak_context = self._tracker.build_peak_context(
            configured_peak_limit_kw=self._settings.peak_limit_kw,
            current_monthly_recorded_peak_kw=self._monthly_peak_kw,
            at=now,
        )
        return EvaluationInput.from_snapshot(
            snapshot,
            peak_context=peak_context,
            strategy=EnergyStrategy.maximize_self_consumption,
            battery_control=BatteryControlContext(
                reserve_floor_percent=self._settings.battery_reserve_floor_percent,
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
        """Translate intents to commands and dispatch them through RetryPolicy."""
        self._last_mode = result.recommended_operating_mode
        logger.debug(
            "evaluation_cycle_complete",
            cycle_id=str(result.cycle_id),
            operating_mode=result.recommended_operating_mode.value,
            cycle_duration_ms=result.cycle_duration_ms,
            intent_count=len(result.intents),
            component="engine",
        )
        commands = self._intent_executor.translate(result, snapshot)
        for cmd in commands:
            await self._retry_policy.execute(cmd)


def _extract_grid_power(
    device_states: Mapping[DeviceRole, DeviceSlot],
) -> tuple[float, str]:
    slot = device_states.get(DeviceRole.grid_meter)
    if isinstance(slot, GridMeterState):
        return slot.grid_power_kw, "complete"
    return 0.0, "incomplete"
