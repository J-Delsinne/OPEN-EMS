"""Energy-flow tracker for per-15-min energy accumulation (Story 10.4 / FR30).

Sibling to :class:`PartialIntervalTracker`. Shares the same 15-min clock-aligned interval
boundaries — the two trackers MUST roll over at the same instant so ``peak_intervals``
and ``energy_flow_intervals`` rows are aligned for the same time window.

The tracker integrates instantaneous power readings (PV, battery, EV) via
``power × dt_seconds`` per tick, and computes grid kWh from MONOTONIC accumulator deltas
(``GridMeterState.energy_delivered_kwh`` / ``energy_returned_kwh``). Battery power is
sign-split into ``battery_charged_kwh`` (positive power = charging) /
``battery_discharged_kwh`` (negative power = discharging, stored as positive magnitude).

``dt_seconds`` is clamped to ``interval_seconds`` to defend against clock jumps (NTP
step, suspend/resume). Negative grid-accumulator deltas (meter rollover / re-zero) are
floored to zero and logged as ``grid_accumulator_rollback_detected``.

A ``None`` for any power input means that role's telemetry is absent for that tick: the
role contributes ZERO kWh for that tick AND the interval is marked
``data_quality='incomplete'``. Partial-interval state is preserved across mixed
None/non-None ticks — the tracker simply skips the contribution for that role on that
tick.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)

_INTERVAL_SECONDS: float = 900.0  # 15 minutes; shared with PartialIntervalTracker.


def _floor_to_15_min(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def _assert_clock_aligned_utc(dt: datetime, name: str) -> None:
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a UTC datetime")
    if dt.minute % 15 != 0 or dt.second != 0 or dt.microsecond != 0:
        raise ValueError(f"{name} must be aligned to a 15-minute clock boundary")


@dataclass(frozen=True)
class CompletedEnergyFlowInterval:
    """Tracker-output dataclass — mirrors :class:`open_ems.core.energy.CompletedEnergyFlowInterval`.

    The Pydantic version in ``core.energy`` is used at repo / persistence boundaries
    (run-time validation, JSON-safe). This dataclass is the in-process tracker output
    (zero-allocation hot path). The two are convertible by name/position.
    """

    interval_start_utc: datetime
    pv_kwh: float
    battery_charged_kwh: float
    battery_discharged_kwh: float
    grid_imported_kwh: float
    grid_exported_kwh: float
    ev_charged_kwh: float
    sample_count: int
    data_quality: Literal["complete", "incomplete"]


class EnergyFlowIntervalTracker:
    """Accumulates per-15-min energy flow for the weekly summary aggregator.

    Construction:
        tracker = EnergyFlowIntervalTracker.from_current_time(at=datetime.now(UTC))

    Per-tick:
        completed = tracker.update(
            at=now,
            pv_power_kw=...,
            battery_power_kw=...,
            ev_power_kw=...,
            grid_delivered_kwh=...,
            grid_returned_kwh=...,
            any_role_degraded=...,
        )
        if completed is not None:
            await energy_repo.write_energy_flow_interval(completed)
    """

    def __init__(self, interval_start: datetime) -> None:
        _assert_clock_aligned_utc(interval_start, "interval_start")
        self._interval_start = interval_start
        # Power-integration accumulators (kW-seconds).
        self._pv_kw_seconds: float = 0.0
        self._battery_charge_kw_seconds: float = 0.0
        self._battery_discharge_kw_seconds: float = 0.0
        self._ev_kw_seconds: float = 0.0
        # Grid monotonic accumulator baselines: latched on the FIRST tick of the
        # interval where the meter readings are non-None. None means "no baseline
        # yet"; the next non-None tick latches.
        self._grid_delivered_baseline_kwh: float | None = None
        self._grid_returned_baseline_kwh: float | None = None
        # The most-recent observed grid accumulator values (for computing the delta
        # on rollover). None when no observation has been made yet.
        self._latest_grid_delivered_kwh: float | None = None
        self._latest_grid_returned_kwh: float | None = None
        self._sample_count: int = 0
        self._last_at: datetime | None = None
        self._data_quality: Literal["complete", "incomplete"] = "complete"

    @classmethod
    def from_current_time(cls, at: datetime) -> EnergyFlowIntervalTracker:
        """Create a tracker floored to the nearest 15-minute boundary of ``at``."""
        if at.tzinfo is None or at.utcoffset() != timedelta(0):
            raise ValueError("at must be a UTC datetime")
        return cls(_floor_to_15_min(at))

    @property
    def interval_start(self) -> datetime:
        return self._interval_start

    @property
    def sample_count(self) -> int:
        return self._sample_count

    @property
    def data_quality(self) -> Literal["complete", "incomplete"]:
        return self._data_quality

    def update(
        self,
        *,
        at: datetime,
        pv_power_kw: float | None,
        battery_power_kw: float | None,
        ev_power_kw: float | None,
        grid_delivered_kwh: float | None,
        grid_returned_kwh: float | None,
        any_role_degraded: bool,
    ) -> CompletedEnergyFlowInterval | None:
        """Record a tick. Returns a :class:`CompletedEnergyFlowInterval` on rollover."""
        if at.tzinfo is None or at.utcoffset() != timedelta(0):
            raise ValueError("at must be a UTC datetime")

        new_boundary = _floor_to_15_min(at)
        if new_boundary > self._interval_start:
            # The segment between the last in-interval tick (``self._last_at``) and
            # the rollover instant ``at`` belongs to the OLD interval. Attribute it
            # at this tick's power level (left-endpoint Riemann; standard for sample-
            # at-tick-time integration). Without this, every interval rollover would
            # drop ~one-tick of time worth of energy — at 10s tick cadence that's
            # ~1% systemic under-reporting per interval.
            if self._last_at is not None:
                dt_seconds = (at - self._last_at).total_seconds()
                if dt_seconds < 0:
                    dt_seconds = 0.0
                elif dt_seconds > _INTERVAL_SECONDS:
                    dt_seconds = _INTERVAL_SECONDS

                if pv_power_kw is not None:
                    self._pv_kw_seconds += max(0.0, pv_power_kw) * dt_seconds

                if battery_power_kw is not None:
                    if battery_power_kw > 0.0:
                        self._battery_charge_kw_seconds += battery_power_kw * dt_seconds
                    elif battery_power_kw < 0.0:
                        self._battery_discharge_kw_seconds += -battery_power_kw * dt_seconds

                if ev_power_kw is not None:
                    self._ev_kw_seconds += max(0.0, ev_power_kw) * dt_seconds

                # Update grid latest readings before finalize (so the delta includes
                # this rollover-tick's accumulator reading).
                if grid_delivered_kwh is not None and grid_returned_kwh is not None:
                    if self._grid_delivered_baseline_kwh is None:
                        self._grid_delivered_baseline_kwh = grid_delivered_kwh
                    if self._grid_returned_baseline_kwh is None:
                        self._grid_returned_baseline_kwh = grid_returned_kwh
                    self._latest_grid_delivered_kwh = grid_delivered_kwh
                    self._latest_grid_returned_kwh = grid_returned_kwh

                # None on any signal at the rollover tick degrades the OLD interval.
                if (
                    pv_power_kw is None
                    or battery_power_kw is None
                    or ev_power_kw is None
                    or grid_delivered_kwh is None
                    or grid_returned_kwh is None
                ):
                    self._data_quality = "incomplete"
                if any_role_degraded:
                    self._data_quality = "incomplete"

            completed = self._finalize_interval()
            self._reset_for_new_interval(new_boundary)
            # First tick of the new interval: same as cold-start — set last_at but
            # contribute zero seconds.
            self._last_at = at
            if any_role_degraded:
                self._data_quality = "incomplete"
            # Latch grid baselines from this tick if both readings are present.
            if grid_delivered_kwh is not None and grid_returned_kwh is not None:
                self._grid_delivered_baseline_kwh = grid_delivered_kwh
                self._grid_returned_baseline_kwh = grid_returned_kwh
                self._latest_grid_delivered_kwh = grid_delivered_kwh
                self._latest_grid_returned_kwh = grid_returned_kwh
            else:
                # Grid telemetry absent on the first tick — interval is degraded;
                # baselines may latch on a later tick.
                self._data_quality = "incomplete"
            if pv_power_kw is None or battery_power_kw is None or ev_power_kw is None:
                self._data_quality = "incomplete"
            self._sample_count = 1
            return completed

        # Same interval. Integrate.
        if self._last_at is None:
            # First tick of the very first interval (cold-start). Set last_at and
            # contribute zero seconds.
            self._last_at = at
        else:
            dt_seconds = (at - self._last_at).total_seconds()
            # Clamp to interval seconds — defends against NTP step / suspend-resume.
            # Negative dt (clock went backwards) → contribute zero.
            if dt_seconds < 0:
                dt_seconds = 0.0
            elif dt_seconds > _INTERVAL_SECONDS:
                dt_seconds = _INTERVAL_SECONDS

            if pv_power_kw is not None:
                # PV is non-negative by domain invariant; defensively floor.
                self._pv_kw_seconds += max(0.0, pv_power_kw) * dt_seconds

            if battery_power_kw is not None:
                if battery_power_kw > 0.0:
                    self._battery_charge_kw_seconds += battery_power_kw * dt_seconds
                elif battery_power_kw < 0.0:
                    self._battery_discharge_kw_seconds += -battery_power_kw * dt_seconds

            if ev_power_kw is not None:
                self._ev_kw_seconds += max(0.0, ev_power_kw) * dt_seconds

            self._last_at = at

        # Latch grid baselines if they have not been latched yet AND both readings are
        # present this tick.
        if grid_delivered_kwh is not None and grid_returned_kwh is not None:
            if self._grid_delivered_baseline_kwh is None:
                self._grid_delivered_baseline_kwh = grid_delivered_kwh
            if self._grid_returned_baseline_kwh is None:
                self._grid_returned_baseline_kwh = grid_returned_kwh
            self._latest_grid_delivered_kwh = grid_delivered_kwh
            self._latest_grid_returned_kwh = grid_returned_kwh

        # None on any power input OR grid input degrades the interval.
        if (
            pv_power_kw is None
            or battery_power_kw is None
            or ev_power_kw is None
            or grid_delivered_kwh is None
            or grid_returned_kwh is None
        ):
            self._data_quality = "incomplete"

        if any_role_degraded:
            self._data_quality = "incomplete"

        self._sample_count += 1
        return None

    def _finalize_interval(self) -> CompletedEnergyFlowInterval:
        pv_kwh = self._pv_kw_seconds / 3600.0
        battery_charged_kwh = self._battery_charge_kw_seconds / 3600.0
        battery_discharged_kwh = self._battery_discharge_kw_seconds / 3600.0
        ev_charged_kwh = self._ev_kw_seconds / 3600.0

        # Grid kWh from monotonic accumulator deltas. If the meter never reported
        # during this interval (both baselines None), the interval kWh is zero AND
        # data_quality is already 'incomplete' (set in update()).
        grid_imported_kwh = 0.0
        grid_exported_kwh = 0.0
        if (
            self._grid_delivered_baseline_kwh is not None
            and self._latest_grid_delivered_kwh is not None
        ):
            delta = self._latest_grid_delivered_kwh - self._grid_delivered_baseline_kwh
            if delta < 0.0:
                logger.warning(
                    "grid_accumulator_rollback_detected",
                    accumulator="energy_delivered_kwh",
                    baseline=self._grid_delivered_baseline_kwh,
                    latest=self._latest_grid_delivered_kwh,
                    interval_start_utc=self._interval_start.isoformat(),
                    component="energy_flow_tracker",
                )
                grid_imported_kwh = 0.0
            else:
                grid_imported_kwh = delta
        if (
            self._grid_returned_baseline_kwh is not None
            and self._latest_grid_returned_kwh is not None
        ):
            delta = self._latest_grid_returned_kwh - self._grid_returned_baseline_kwh
            if delta < 0.0:
                logger.warning(
                    "grid_accumulator_rollback_detected",
                    accumulator="energy_returned_kwh",
                    baseline=self._grid_returned_baseline_kwh,
                    latest=self._latest_grid_returned_kwh,
                    interval_start_utc=self._interval_start.isoformat(),
                    component="energy_flow_tracker",
                )
                grid_exported_kwh = 0.0
            else:
                grid_exported_kwh = delta

        # Defensive: kWh values must be finite & non-negative.
        for name, value in (
            ("pv_kwh", pv_kwh),
            ("battery_charged_kwh", battery_charged_kwh),
            ("battery_discharged_kwh", battery_discharged_kwh),
            ("grid_imported_kwh", grid_imported_kwh),
            ("grid_exported_kwh", grid_exported_kwh),
            ("ev_charged_kwh", ev_charged_kwh),
        ):
            assert math.isfinite(value), f"{name} is not finite: {value}"
            assert value >= 0.0, f"{name} is negative: {value}"

        return CompletedEnergyFlowInterval(
            interval_start_utc=self._interval_start,
            pv_kwh=pv_kwh,
            battery_charged_kwh=battery_charged_kwh,
            battery_discharged_kwh=battery_discharged_kwh,
            grid_imported_kwh=grid_imported_kwh,
            grid_exported_kwh=grid_exported_kwh,
            ev_charged_kwh=ev_charged_kwh,
            sample_count=self._sample_count,
            data_quality=self._data_quality,
        )

    def _reset_for_new_interval(self, new_boundary: datetime) -> None:
        self._interval_start = new_boundary
        self._pv_kw_seconds = 0.0
        self._battery_charge_kw_seconds = 0.0
        self._battery_discharge_kw_seconds = 0.0
        self._ev_kw_seconds = 0.0
        self._grid_delivered_baseline_kwh = None
        self._grid_returned_baseline_kwh = None
        self._latest_grid_delivered_kwh = None
        self._latest_grid_returned_kwh = None
        self._sample_count = 0
        self._last_at = None
        self._data_quality = "complete"
