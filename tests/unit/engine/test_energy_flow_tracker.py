"""Unit tests for :class:`EnergyFlowIntervalTracker` (Story 10.4 AC3 + AC15 #1–#12)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from open_ems.engine.energy_flow_tracker import (
    CompletedEnergyFlowInterval,
    EnergyFlowIntervalTracker,
)


def _utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:  # noqa: E501  # fmt: skip
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def test_energy_flow_tracker_initial_state_has_zero_accumulators_at_current_interval_boundary() -> None:  # noqa: E501  # fmt: skip
    at = _utc(2026, 5, 12, 10, 7, 30)  # 10:07:30 → floors to 10:00
    tracker = EnergyFlowIntervalTracker.from_current_time(at=at)
    assert tracker.interval_start == _utc(2026, 5, 12, 10, 0, 0)
    assert tracker.sample_count == 0
    assert tracker.data_quality == "complete"


def test_energy_flow_tracker_integrates_pv_power_into_pv_kwh_over_full_interval() -> None:
    """Constant 5 kW for 15 minutes → 1.25 kWh."""
    start = _utc(2026, 5, 12, 10, 0, 0)
    tracker = EnergyFlowIntervalTracker(interval_start=start)
    # Feed 15 ticks within the interval at 60s spacing (i=0..14 → 10:00..10:14).
    # The first tick (i=0) contributes 0 seconds; the next 14 ticks integrate
    # 60s × 5 kW each = 14 × 300 kW-s = 4200 kW-s. The rollover at 10:15:00 then
    # integrates the final 60s segment between tick i=14 (10:14:00) and 10:15:00:
    # 60 × 5 = 300 kW-s. Total = 4200 + 300 = 4500 kW-s → 1.25 kWh.
    for i in range(15):
        result = tracker.update(
            at=start + timedelta(seconds=60 * i),
            pv_power_kw=5.0,
            battery_power_kw=0.0,
            ev_power_kw=0.0,
            grid_delivered_kwh=100.0,
            grid_returned_kwh=50.0,
            any_role_degraded=False,
        )
        assert result is None
    # Rollover tick at 10:15:00
    completed = tracker.update(
        at=_utc(2026, 5, 12, 10, 15, 0),
        pv_power_kw=5.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=100.0,
        grid_returned_kwh=50.0,
        any_role_degraded=False,
    )
    assert completed is not None
    assert isinstance(completed, CompletedEnergyFlowInterval)
    assert completed.interval_start_utc == start
    # 15 × 60s × 5 kW = 4500 kW-s → 1.25 kWh.
    assert completed.pv_kwh == pytest.approx(1.25, abs=1e-9)
    assert completed.data_quality == "complete"


def test_energy_flow_tracker_splits_battery_power_into_charged_and_discharged_kwh_by_sign() -> None:
    start = _utc(2026, 5, 12, 10, 0, 0)
    tracker = EnergyFlowIntervalTracker(interval_start=start)
    # Tick 0: prime last_at.
    tracker.update(
        at=start,
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    # Tick at +60s: 3 kW charging.
    tracker.update(
        at=start + timedelta(seconds=60),
        pv_power_kw=0.0,
        battery_power_kw=3.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    # Tick at +120s: -2 kW discharging.
    tracker.update(
        at=start + timedelta(seconds=120),
        pv_power_kw=0.0,
        battery_power_kw=-2.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    # Rollover at 10:15:00 with 0 kW.
    completed = tracker.update(
        at=_utc(2026, 5, 12, 10, 15, 0),
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    assert completed is not None
    # 60s of +3 kW = 180 kW-s → 0.05 kWh charge; 60s of -2 kW = 120 kW-s → 1/30 ≈ 0.0333 kWh.
    assert completed.battery_charged_kwh == pytest.approx(180.0 / 3600.0, abs=1e-9)
    assert completed.battery_discharged_kwh == pytest.approx(120.0 / 3600.0, abs=1e-9)


def test_energy_flow_tracker_computes_grid_kwh_from_monotonic_accumulator_deltas() -> None:
    start = _utc(2026, 5, 12, 10, 0, 0)
    tracker = EnergyFlowIntervalTracker(interval_start=start)
    # Tick 0: latch baselines.
    tracker.update(
        at=start,
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=1000.0,
        grid_returned_kwh=500.0,
        any_role_degraded=False,
    )
    # Tick at +5 min: meter advanced by 0.75 kWh import + 0.20 kWh export.
    tracker.update(
        at=start + timedelta(minutes=5),
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=1000.75,
        grid_returned_kwh=500.20,
        any_role_degraded=False,
    )
    # Rollover.
    completed = tracker.update(
        at=_utc(2026, 5, 12, 10, 15, 0),
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=1001.5,
        grid_returned_kwh=500.5,
        any_role_degraded=False,
    )
    assert completed is not None
    # Delta from baseline 1000.0 → 1001.5 = 1.5 kWh imported.
    assert completed.grid_imported_kwh == pytest.approx(1.5, abs=1e-9)
    # Delta from baseline 500.0 → 500.5 = 0.5 kWh exported.
    assert completed.grid_exported_kwh == pytest.approx(0.5, abs=1e-9)


def test_energy_flow_tracker_floors_negative_grid_accumulator_delta_to_zero_and_logs_rollback() -> None:  # noqa: E501  # fmt: skip
    """Meter rollover / re-zero must not produce negative interval kWh."""
    start = _utc(2026, 5, 12, 10, 0, 0)
    tracker = EnergyFlowIntervalTracker(interval_start=start)
    # Tick 0: baseline at 999999.0 kWh (meter near rollover).
    tracker.update(
        at=start,
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=999999.0,
        grid_returned_kwh=500.0,
        any_role_degraded=False,
    )
    # Mid-interval: meter rolled over to 0.5 kWh. delta = 0.5 - 999999.0 < 0.
    tracker.update(
        at=start + timedelta(minutes=5),
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.5,
        grid_returned_kwh=500.0,
        any_role_degraded=False,
    )
    # Rollover.
    completed = tracker.update(
        at=_utc(2026, 5, 12, 10, 15, 0),
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=1.0,
        grid_returned_kwh=500.0,
        any_role_degraded=False,
    )
    assert completed is not None
    assert completed.grid_imported_kwh == 0.0  # floored from negative delta


def test_energy_flow_tracker_marks_interval_incomplete_when_any_role_degraded_at_any_tick() -> None:
    start = _utc(2026, 5, 12, 10, 0, 0)
    tracker = EnergyFlowIntervalTracker(interval_start=start)
    tracker.update(
        at=start,
        pv_power_kw=1.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    # One degraded tick mid-interval.
    tracker.update(
        at=start + timedelta(minutes=5),
        pv_power_kw=1.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=True,
    )
    # Rest of interval clean.
    completed = tracker.update(
        at=_utc(2026, 5, 12, 10, 15, 0),
        pv_power_kw=1.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    assert completed is not None
    assert completed.data_quality == "incomplete"


def test_energy_flow_tracker_clamps_dt_seconds_to_interval_seconds_on_clock_step() -> None:
    """A massive forward clock step (NTP correction, suspend/resume) must not produce
    absurd kWh values. dt_seconds is clamped to 900 (the interval length)."""
    start = _utc(2026, 5, 12, 10, 0, 0)
    tracker = EnergyFlowIntervalTracker(interval_start=start)
    # Tick 0 inside the interval: prime last_at.
    tracker.update(
        at=start + timedelta(seconds=1),
        pv_power_kw=2.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    # Clock jumps forward 1 hour mid-interval — clamped to 900s for the integration.
    # But the boundary check uses the actual `at`, so the rollover fires.
    completed = tracker.update(
        at=start + timedelta(hours=1, seconds=1),  # 11:00:01
        pv_power_kw=2.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    # The rollover tick at 11:00:01 attributes dt = (11:00:01 − 10:00:01) = 3600s,
    # CLAMPED to 900s (the interval length), at 2 kW → 1800 kW-s → 0.5 kWh.
    # Without the clamp, this would be 2.0 kWh (1 hour worth of 2 kW),
    # spuriously attributing time outside the interval to it.
    assert completed is not None
    assert tracker.interval_start == _utc(2026, 5, 12, 11, 0, 0)
    assert completed.pv_kwh == pytest.approx(1800.0 / 3600.0, abs=1e-9)


def test_energy_flow_tracker_returns_completed_interval_only_at_clock_aligned_rollover() -> None:
    start = _utc(2026, 5, 12, 10, 0, 0)
    tracker = EnergyFlowIntervalTracker(interval_start=start)
    # Five ticks within the interval (10:00 to 10:14:30) — no rollover.
    for i in range(5):
        completed = tracker.update(
            at=start + timedelta(minutes=i * 3),
            pv_power_kw=1.0,
            battery_power_kw=0.0,
            ev_power_kw=0.0,
            grid_delivered_kwh=0.0,
            grid_returned_kwh=0.0,
            any_role_degraded=False,
        )
        assert completed is None
    # Tick at exactly 10:15:00 — rollover.
    completed = tracker.update(
        at=_utc(2026, 5, 12, 10, 15, 0),
        pv_power_kw=1.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    assert completed is not None
    assert completed.interval_start_utc == start


def test_energy_flow_tracker_starts_fresh_accumulators_after_rollover() -> None:
    start = _utc(2026, 5, 12, 10, 0, 0)
    tracker = EnergyFlowIntervalTracker(interval_start=start)
    # Integrate non-zero values in the first interval.
    tracker.update(
        at=start,
        pv_power_kw=5.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=100.0,
        grid_returned_kwh=50.0,
        any_role_degraded=False,
    )
    tracker.update(
        at=start + timedelta(minutes=5),
        pv_power_kw=5.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=100.5,
        grid_returned_kwh=50.0,
        any_role_degraded=False,
    )
    first = tracker.update(
        at=_utc(2026, 5, 12, 10, 15, 0),
        pv_power_kw=5.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=101.0,
        grid_returned_kwh=50.0,
        any_role_degraded=False,
    )
    assert first is not None
    assert first.pv_kwh > 0.0
    assert first.grid_imported_kwh > 0.0
    # After rollover the tracker accumulators must be reset.
    assert tracker.interval_start == _utc(2026, 5, 12, 10, 15, 0)
    assert tracker.sample_count == 1  # the rollover tick is the first of the new interval
    # Add only zero ticks; the next rollover should yield zero kWh.
    tracker.update(
        at=_utc(2026, 5, 12, 10, 20, 0),
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=101.0,
        grid_returned_kwh=50.0,
        any_role_degraded=False,
    )
    second = tracker.update(
        at=_utc(2026, 5, 12, 10, 30, 0),
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=101.0,
        grid_returned_kwh=50.0,
        any_role_degraded=False,
    )
    assert second is not None
    assert second.pv_kwh == 0.0
    assert second.grid_imported_kwh == 0.0


def test_energy_flow_tracker_handles_none_power_inputs_as_zero_contribution_without_loss_of_partial_state() -> None:  # noqa: E501  # fmt: skip
    start = _utc(2026, 5, 12, 10, 0, 0)
    tracker = EnergyFlowIntervalTracker(interval_start=start)
    tracker.update(
        at=start,
        pv_power_kw=3.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    # Tick at +60s: PV is None (inverter temporarily degraded). PV contributes 0 for this tick.
    tracker.update(
        at=start + timedelta(seconds=60),
        pv_power_kw=None,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    # Tick at +120s: PV is back at 3 kW.
    tracker.update(
        at=start + timedelta(seconds=120),
        pv_power_kw=3.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    # Rollover.
    completed = tracker.update(
        at=_utc(2026, 5, 12, 10, 15, 0),
        pv_power_kw=3.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    assert completed is not None
    # The None tick at +60s contributed 0 for PV. The +120s tick integrated 60s @ 3 kW.
    # The rollover tick (+780s after the +120s) integrated 780s @ 3 kW.
    # Plus the +60s tick integrated 60s @ 0 (None) — so the contribution is from
    # last_at-aware deltas: at +60 (60s @ 0), at +120 (60s @ 3), at 10:15 (780s @ 3).
    # Total = 60 × 0 + 60 × 3 + 780 × 3 = 0 + 180 + 2340 = 2520 kW-s → 0.7 kWh.
    assert completed.pv_kwh == pytest.approx(2520.0 / 3600.0, abs=1e-9)
    # Data quality must be 'incomplete' because of the None at +60s.
    assert completed.data_quality == "incomplete"


def test_energy_flow_tracker_first_tick_of_interval_contributes_zero_seconds() -> None:
    """The very first tick after construction has no last_at — contributes 0 seconds."""
    start = _utc(2026, 5, 12, 10, 0, 0)
    tracker = EnergyFlowIntervalTracker(interval_start=start)
    tracker.update(
        at=start,
        pv_power_kw=100.0,  # huge value, but contributes zero seconds → zero kWh
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    completed = tracker.update(
        at=_utc(2026, 5, 12, 10, 15, 0),
        pv_power_kw=0.0,  # second tick (the rollover one) has dt = 900s, but power = 0
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=0.0,
        grid_returned_kwh=0.0,
        any_role_degraded=False,
    )
    assert completed is not None
    # 900s @ 0 kW = 0 kWh. The first tick contributed 0 seconds despite 100 kW.
    assert completed.pv_kwh == 0.0


def test_energy_flow_tracker_skipped_tick_when_grid_accumulators_None_leaves_baseline_unchanged_for_next_tick() -> None:  # noqa: E501  # fmt: skip
    """If the first tick lacks grid telemetry, the baseline latches on the next tick
    that has it (not later — we need the baseline established before we can compute
    a meaningful delta at rollover)."""
    start = _utc(2026, 5, 12, 10, 0, 0)
    tracker = EnergyFlowIntervalTracker(interval_start=start)
    # Tick 0: grid is None.
    tracker.update(
        at=start,
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=None,
        grid_returned_kwh=None,
        any_role_degraded=False,
    )
    # Tick at +5 min: grid telemetry arrives at 100.0/50.0 — latch baselines here.
    tracker.update(
        at=start + timedelta(minutes=5),
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=100.0,
        grid_returned_kwh=50.0,
        any_role_degraded=False,
    )
    # Rollover with grid at 100.5/50.1.
    completed = tracker.update(
        at=_utc(2026, 5, 12, 10, 15, 0),
        pv_power_kw=0.0,
        battery_power_kw=0.0,
        ev_power_kw=0.0,
        grid_delivered_kwh=100.5,
        grid_returned_kwh=50.1,
        any_role_degraded=False,
    )
    assert completed is not None
    # Delta = 100.5 - 100.0 = 0.5 kWh; export = 0.1 kWh.
    assert completed.grid_imported_kwh == pytest.approx(0.5, abs=1e-9)
    assert completed.grid_exported_kwh == pytest.approx(0.1, abs=1e-9)
    # data_quality must be 'incomplete' because of the None at tick 0.
    assert completed.data_quality == "incomplete"
