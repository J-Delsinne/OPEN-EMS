"""Partial-interval power tracker for real-time peak projection.

**Finalize-before-evaluate invariant:** `PeakContext` must only be built from an active
(non-complete) interval. The control loop must call `tracker.update(power_kw, at=now)`
every tick. If `update()` returns a `CompletedInterval`, the loop persists it to
`peak_intervals` before calling `tracker.build_peak_context()`. Only after this
finalization step may the loop call `evaluate_cycle()`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from open_ems.engine.models import PeakContext


@dataclass(frozen=True)
class CompletedInterval:
    """Finalized record of a single 15-minute clock-aligned interval."""

    interval_start_utc: datetime
    avg_power_kw: float
    sample_count: int


def _floor_to_15_min(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def _assert_clock_aligned_utc(dt: datetime, name: str) -> None:
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a UTC datetime")
    if dt.minute % 15 != 0 or dt.second != 0 or dt.microsecond != 0:
        raise ValueError(f"{name} must be aligned to a 15-minute clock boundary")


class PartialIntervalTracker:
    """Accumulates grid meter readings within the active 15-minute clock-aligned interval.

    Produces a projected peak value each evaluation cycle so the control loop can build
    a `PeakContext` from live data without accessing the billing database.

    **Finalize-before-evaluate invariant:** `PeakContext` must only be built from an active
    (non-complete) interval. The control loop must call `tracker.update(power_kw, at=now)`
    every tick. If `update()` returns a `CompletedInterval`, the loop persists it to
    `peak_intervals` before calling `tracker.build_peak_context()`. Only after this
    finalization step may the loop call `evaluate_cycle()`.
    """

    def __init__(self, interval_start: datetime) -> None:
        _assert_clock_aligned_utc(interval_start, "interval_start")
        self._interval_start = interval_start
        self._power_sum_kw: float = 0.0
        self._sample_count: int = 0
        self._latest_power_kw: float | None = None

    @classmethod
    def from_current_time(cls, at: datetime) -> PartialIntervalTracker:
        """Create a tracker floored to the nearest 15-minute boundary of `at`."""
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
    def latest_power_kw(self) -> float | None:
        return self._latest_power_kw

    def update(self, power_kw: float, at: datetime) -> CompletedInterval | None:
        """Record a power reading. Returns a `CompletedInterval` if the interval rolled over."""
        if at.tzinfo is None or at.utcoffset() != timedelta(0):
            raise ValueError("at must be a UTC datetime")
        new_boundary = _floor_to_15_min(at)
        if new_boundary > self._interval_start:
            completed = CompletedInterval(
                interval_start_utc=self._interval_start,
                avg_power_kw=(
                    self._power_sum_kw / self._sample_count if self._sample_count > 0 else 0.0
                ),
                sample_count=self._sample_count,
            )
            self._interval_start = new_boundary
            self._power_sum_kw = power_kw
            self._sample_count = 1
            self._latest_power_kw = power_kw
            return completed

        self._power_sum_kw += power_kw
        self._sample_count += 1
        self._latest_power_kw = power_kw
        return None

    def build_peak_context(
        self,
        *,
        configured_peak_limit_kw: float,
        current_monthly_recorded_peak_kw: float,
        at: datetime,
    ) -> PeakContext:
        """Build a `PeakContext` from the current active-interval state.

        Raises `ValueError` if `elapsed_seconds >= 900` (interval is complete — call
        `update()` first) or if `at < interval_start` (time went backwards).
        """
        elapsed = (at - self._interval_start).total_seconds()
        if elapsed < 0:
            raise ValueError(f"at ({at!r}) is before interval_start ({self._interval_start!r})")
        if elapsed >= 900:
            raise ValueError(
                f"elapsed_seconds={elapsed:.1f} >= 900: interval is complete; "
                "call update() to finalize before calling build_peak_context()"
            )
        elapsed_int = int(elapsed)

        if self._sample_count == 0:
            projection_kw = 0.0
        else:
            elapsed_fraction = elapsed / 900.0
            remaining_fraction = 1.0 - elapsed_fraction
            avg_so_far_kw = self._power_sum_kw / self._sample_count
            # sample_count > 0 guarantees at least one update() call
            assert self._latest_power_kw is not None
            projection_kw = (
                avg_so_far_kw * elapsed_fraction + self._latest_power_kw * remaining_fraction
            )

        assert math.isfinite(projection_kw), f"projection_kw is not finite: {projection_kw}"

        return PeakContext(
            current_partial_window_projection_kw=projection_kw,
            configured_peak_limit_kw=configured_peak_limit_kw,
            current_monthly_recorded_peak_kw=current_monthly_recorded_peak_kw,
            current_interval_start=self._interval_start,
            current_interval_elapsed_seconds=elapsed_int,
        )
