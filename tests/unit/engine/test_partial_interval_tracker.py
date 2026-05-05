"""Unit tests for PartialIntervalTracker and CompletedInterval."""

from __future__ import annotations

import ast
import math
from datetime import UTC, datetime, timedelta, timezone

import pytest

from open_ems.engine import CompletedInterval, PartialIntervalTracker
from open_ems.engine.models import PeakContext

_BOUNDARY = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)  # valid 15-min boundary


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_initial_state_zero_samples() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    assert tracker.sample_count == 0
    assert tracker.latest_power_kw is None
    assert tracker.interval_start == _BOUNDARY


def test_from_current_time_floors_to_15_min_boundary() -> None:
    cases = [
        (datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC), datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)),
        (datetime(2026, 5, 5, 12, 7, 33, tzinfo=UTC), datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)),
        (datetime(2026, 5, 5, 12, 14, 59, tzinfo=UTC), datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)),
        (datetime(2026, 5, 5, 12, 15, 0, tzinfo=UTC), datetime(2026, 5, 5, 12, 15, 0, tzinfo=UTC)),
        (datetime(2026, 5, 5, 12, 29, 1, tzinfo=UTC), datetime(2026, 5, 5, 12, 15, 0, tzinfo=UTC)),
        (datetime(2026, 5, 5, 12, 30, 0, tzinfo=UTC), datetime(2026, 5, 5, 12, 30, 0, tzinfo=UTC)),
        (datetime(2026, 5, 5, 12, 44, 0, tzinfo=UTC), datetime(2026, 5, 5, 12, 30, 0, tzinfo=UTC)),
        (datetime(2026, 5, 5, 12, 45, 0, tzinfo=UTC), datetime(2026, 5, 5, 12, 45, 0, tzinfo=UTC)),
        (datetime(2026, 5, 5, 12, 59, 59, tzinfo=UTC), datetime(2026, 5, 5, 12, 45, 0, tzinfo=UTC)),
    ]
    for at, expected_boundary in cases:
        tracker = PartialIntervalTracker.from_current_time(at)
        assert tracker.interval_start == expected_boundary, f"at={at}"


def test_constructor_rejects_unaligned_interval_start() -> None:
    for bad_minute in (1, 7, 14, 16, 29, 31, 44, 46, 59):
        with pytest.raises(ValueError, match="aligned"):
            PartialIntervalTracker(datetime(2026, 5, 5, 12, bad_minute, 0, tzinfo=UTC))

    with pytest.raises(ValueError, match="aligned"):
        PartialIntervalTracker(datetime(2026, 5, 5, 12, 0, 1, tzinfo=UTC))  # non-zero second

    with pytest.raises(ValueError, match="aligned"):
        PartialIntervalTracker(datetime(2026, 5, 5, 12, 0, 0, 1, tzinfo=UTC))  # microsecond != 0


def test_constructor_rejects_non_utc_interval_start() -> None:
    naive = datetime(2026, 5, 5, 12, 0, 0)
    with pytest.raises(ValueError, match="UTC"):
        PartialIntervalTracker(naive)

    cet = timezone(timedelta(hours=1))
    with pytest.raises(ValueError, match="UTC"):
        PartialIntervalTracker(datetime(2026, 5, 5, 12, 0, 0, tzinfo=cet))


def test_from_current_time_rejects_non_utc() -> None:
    naive = datetime(2026, 5, 5, 12, 7, 0)
    with pytest.raises(ValueError, match="UTC"):
        PartialIntervalTracker.from_current_time(naive)


def test_update_rejects_non_utc_at() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    naive = datetime(2026, 5, 5, 12, 7, 0)
    with pytest.raises(ValueError, match="UTC"):
        tracker.update(3.0, naive)
    cet = timezone(timedelta(hours=1))
    with pytest.raises(ValueError, match="UTC"):
        tracker.update(3.0, datetime(2026, 5, 5, 12, 7, 0, tzinfo=cet))


# ---------------------------------------------------------------------------
# update() — accumulation
# ---------------------------------------------------------------------------


def test_update_within_same_interval_returns_none() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    result = tracker.update(3.0, _BOUNDARY + timedelta(seconds=30))
    assert result is None
    assert tracker.sample_count == 1
    assert tracker.latest_power_kw == 3.0


def test_update_accumulates_multiple_readings() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    readings = [2.0, 4.0, 6.0]
    for i, kw in enumerate(readings):
        tracker.update(kw, _BOUNDARY + timedelta(seconds=(i + 1) * 10))
    assert tracker.sample_count == 3
    assert tracker.latest_power_kw == 6.0


# ---------------------------------------------------------------------------
# update() — rollover
# ---------------------------------------------------------------------------


def test_update_crossing_boundary_returns_completed_interval() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    tracker.update(4.0, _BOUNDARY + timedelta(seconds=60))
    tracker.update(8.0, _BOUNDARY + timedelta(seconds=120))

    next_boundary = _BOUNDARY + timedelta(minutes=15)
    completed = tracker.update(10.0, next_boundary + timedelta(seconds=5))

    assert isinstance(completed, CompletedInterval)
    assert completed.interval_start_utc == _BOUNDARY
    assert completed.sample_count == 2
    assert completed.avg_power_kw == pytest.approx(6.0)


def test_update_rollover_credits_reading_to_new_interval() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    tracker.update(4.0, _BOUNDARY + timedelta(seconds=60))

    next_boundary = _BOUNDARY + timedelta(minutes=15)
    tracker.update(10.0, next_boundary + timedelta(seconds=5))

    assert tracker.sample_count == 1
    assert tracker.latest_power_kw == pytest.approx(10.0)
    assert tracker.interval_start == next_boundary


def test_update_multi_boundary_gap_returns_single_completed_interval() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    tracker.update(5.0, _BOUNDARY + timedelta(seconds=30))

    # Jump 30+ minutes — two boundaries crossed
    far_future = _BOUNDARY + timedelta(minutes=32)
    completed = tracker.update(7.0, far_future)

    assert isinstance(completed, CompletedInterval)
    assert completed.interval_start_utc == _BOUNDARY
    assert completed.sample_count == 1
    assert completed.avg_power_kw == pytest.approx(5.0)
    # Tracker reset to the boundary of far_future (12:30)
    assert tracker.interval_start == _BOUNDARY + timedelta(minutes=30)
    assert tracker.sample_count == 1


# ---------------------------------------------------------------------------
# build_peak_context() — projection
# ---------------------------------------------------------------------------


def test_build_peak_context_zero_samples_returns_zero_projection() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    ctx = tracker.build_peak_context(
        configured_peak_limit_kw=5.0,
        current_monthly_recorded_peak_kw=4.0,
        at=_BOUNDARY + timedelta(seconds=100),
    )
    assert ctx.current_partial_window_projection_kw == pytest.approx(0.0)


def test_build_peak_context_single_sample_full_projection() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    # Add one reading immediately (elapsed ≈ 0)
    tracker.update(8.0, _BOUNDARY + timedelta(seconds=1))
    ctx = tracker.build_peak_context(
        configured_peak_limit_kw=10.0,
        current_monthly_recorded_peak_kw=3.0,
        at=_BOUNDARY + timedelta(seconds=1),
    )
    # elapsed_fraction ≈ 1/900 ≈ 0; remaining ≈ 1; projection ≈ latest_power_kw = 8.0
    assert ctx.current_partial_window_projection_kw == pytest.approx(8.0, abs=0.1)


def test_build_peak_context_time_weighted_formula() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    # Two readings: 3.0 kW and 5.0 kW; avg = 4.0, latest = 5.0
    tracker.update(3.0, _BOUNDARY + timedelta(seconds=50))
    tracker.update(5.0, _BOUNDARY + timedelta(seconds=100))

    ctx = tracker.build_peak_context(
        configured_peak_limit_kw=10.0,
        current_monthly_recorded_peak_kw=3.0,
        at=_BOUNDARY + timedelta(seconds=450),
    )
    # elapsed_fraction = 450/900 = 0.5; avg_so_far = 4.0; latest = 5.0
    # projection = 4.0 * 0.5 + 5.0 * 0.5 = 4.5
    assert ctx.current_partial_window_projection_kw == pytest.approx(4.5)


def test_build_peak_context_elapsed_seconds_clamped_to_899() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    tracker.update(3.0, _BOUNDARY + timedelta(seconds=10))
    ctx = tracker.build_peak_context(
        configured_peak_limit_kw=5.0,
        current_monthly_recorded_peak_kw=2.0,
        at=_BOUNDARY + timedelta(seconds=899),
    )
    assert isinstance(ctx, PeakContext)
    assert ctx.current_interval_elapsed_seconds == 899


def test_build_peak_context_raises_at_900_elapsed() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    tracker.update(3.0, _BOUNDARY + timedelta(seconds=10))
    with pytest.raises(ValueError, match="900"):
        tracker.build_peak_context(
            configured_peak_limit_kw=5.0,
            current_monthly_recorded_peak_kw=2.0,
            at=_BOUNDARY + timedelta(seconds=900),
        )


def test_build_peak_context_raises_on_time_backwards() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    with pytest.raises(ValueError):
        tracker.build_peak_context(
            configured_peak_limit_kw=5.0,
            current_monthly_recorded_peak_kw=2.0,
            at=_BOUNDARY - timedelta(seconds=1),
        )


def test_build_peak_context_populates_all_fields() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    tracker.update(4.0, _BOUNDARY + timedelta(seconds=30))
    ctx = tracker.build_peak_context(
        configured_peak_limit_kw=7.5,
        current_monthly_recorded_peak_kw=6.1,
        at=_BOUNDARY + timedelta(seconds=300),
    )
    assert ctx.configured_peak_limit_kw == pytest.approx(7.5)
    assert ctx.current_monthly_recorded_peak_kw == pytest.approx(6.1)
    assert ctx.current_interval_start == _BOUNDARY
    assert ctx.current_interval_elapsed_seconds == 300


# ---------------------------------------------------------------------------
# CompletedInterval
# ---------------------------------------------------------------------------


def test_completed_interval_avg_with_multiple_samples() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    tracker.update(2.0, _BOUNDARY + timedelta(seconds=30))
    tracker.update(4.0, _BOUNDARY + timedelta(seconds=60))
    tracker.update(6.0, _BOUNDARY + timedelta(seconds=90))

    completed = tracker.update(1.0, _BOUNDARY + timedelta(minutes=15, seconds=5))
    assert isinstance(completed, CompletedInterval)
    assert completed.avg_power_kw == pytest.approx(4.0)
    assert completed.sample_count == 3


def test_completed_interval_avg_zero_samples() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    # No readings before rollover
    completed = tracker.update(5.0, _BOUNDARY + timedelta(minutes=15, seconds=1))
    assert isinstance(completed, CompletedInterval)
    assert completed.avg_power_kw == pytest.approx(0.0)
    assert completed.sample_count == 0


def test_completed_interval_is_frozen_dataclass() -> None:
    ci = CompletedInterval(interval_start_utc=_BOUNDARY, avg_power_kw=3.5, sample_count=10)
    with pytest.raises(AttributeError):
        ci.avg_power_kw = 9.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Finiteness
# ---------------------------------------------------------------------------


def test_projection_is_finite() -> None:
    tracker = PartialIntervalTracker(_BOUNDARY)
    # Zero samples
    ctx = tracker.build_peak_context(
        configured_peak_limit_kw=5.0,
        current_monthly_recorded_peak_kw=0.0,
        at=_BOUNDARY + timedelta(seconds=1),
    )
    assert math.isfinite(ctx.current_partial_window_projection_kw)

    # Many samples with large values
    for i in range(1, 20):
        tracker.update(1e6, _BOUNDARY + timedelta(seconds=i * 10))
    ctx2 = tracker.build_peak_context(
        configured_peak_limit_kw=1e7,
        current_monthly_recorded_peak_kw=0.0,
        at=_BOUNDARY + timedelta(seconds=300),
    )
    assert math.isfinite(ctx2.current_partial_window_projection_kw)


# ---------------------------------------------------------------------------
# Public export and AST boundary
# ---------------------------------------------------------------------------


def test_engine_exports_partial_interval_tracker_and_completed_interval() -> None:
    import open_ems.engine as engine

    assert hasattr(engine, "PartialIntervalTracker")
    assert hasattr(engine, "CompletedInterval")
    assert "PartialIntervalTracker" in engine.__all__
    assert "CompletedInterval" in engine.__all__


def test_ast_import_boundary() -> None:
    import open_ems.engine.partial_interval_tracker as module

    imported_modules: set[str] = set()
    source = module.__loader__.get_source(module.__name__) or ""  # type: ignore[union-attr]
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    forbidden = ("open_ems.storage", "open_ems.web", "open_ems.adapters", "open_ems.services")
    assert not any(
        name == mod or name.startswith(f"{mod}.") for mod in forbidden for name in imported_modules
    )
