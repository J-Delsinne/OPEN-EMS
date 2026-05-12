"""Unit tests for :mod:`weekly_energy_summary` (Story 10.4 AC6 + AC15 #19-#28)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from open_ems.core.energy import EnergyFlowIntervalRow, WeeklyEnergySummaryRow
from open_ems.services.weekly_energy_summary import (
    _compute_and_upsert_weekly_summary,
    weekly_energy_summary_task,
)
from open_ems.settings import Settings

# Midnight so a 7-day window covers exactly 7 whole UTC dates (no straddle).
_FIXED_NOW = datetime(2026, 5, 12, 0, 0, 0, tzinfo=UTC)


def _settings(
    aggregation_interval_seconds: int = 3600,
    min_intervals_per_day: int = 80,
    import_tariff: float = 0.30,
    export_tariff: float = 0.05,
) -> Settings:
    return Settings(
        secret_key="test-secret",  # type: ignore[arg-type]
        weekly_summary_aggregation_interval_seconds=aggregation_interval_seconds,
        weekly_summary_min_intervals_per_complete_day=min_intervals_per_day,
        default_import_tariff_eur_per_kwh=import_tariff,
        default_export_tariff_eur_per_kwh=export_tariff,
        _env_file=None,  # type: ignore[call-arg]
    )


def _make_intervals_for_days(
    *,
    end_utc: datetime,
    days: int,
    intervals_per_day: int = 96,
    pv_kwh_per_interval: float = 0.5,
    grid_exported_kwh_per_interval: float = 0.1,
    battery_discharged_kwh_per_interval: float = 0.05,
    data_quality: str = "complete",
) -> list[EnergyFlowIntervalRow]:
    """Synthetic factory: for each of the most recent ``days`` UTC dates ending the
    day BEFORE ``end_utc``, generate ``intervals_per_day`` 15-min interval rows
    starting at that date's 00:00."""
    rows: list[EnergyFlowIntervalRow] = []
    for d in range(days):
        # d=0 → the date `days` days before end_utc; d=days-1 → date right before end_utc.
        day_start = (end_utc - timedelta(days=days - d)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        for slot in range(intervals_per_day):
            ts = day_start + timedelta(minutes=15 * slot)
            rows.append(
                EnergyFlowIntervalRow(
                    interval_start_utc=ts,
                    pv_kwh=pv_kwh_per_interval,
                    battery_charged_kwh=0.0,
                    battery_discharged_kwh=battery_discharged_kwh_per_interval,
                    grid_imported_kwh=0.0,
                    grid_exported_kwh=grid_exported_kwh_per_interval,
                    ev_charged_kwh=0.0,
                    sample_count=90,
                    data_quality=data_quality,  # type: ignore[arg-type]
                )
            )
    return rows


@pytest.mark.asyncio
async def test_weekly_summary_cold_start_writes_insufficient_history_row_immediately() -> None:
    """AC15 #19: empty energy_flow_intervals → insufficient_history=True row."""
    energy_repo = MagicMock()
    energy_repo.read_energy_flow_intervals_since = AsyncMock(return_value=[])
    energy_repo.upsert_weekly_energy_summary = AsyncMock()
    event_log_repo = MagicMock()
    event_log_repo.count_peak_limiting_applied_decisions = AsyncMock(return_value=0)

    await _compute_and_upsert_weekly_summary(
        energy_repo=energy_repo,
        event_log_repo=event_log_repo,
        settings=_settings(),
        now=_FIXED_NOW,
    )

    energy_repo.upsert_weekly_energy_summary.assert_awaited_once()
    written: WeeklyEnergySummaryRow = energy_repo.upsert_weekly_energy_summary.await_args.args[0]
    assert written.insufficient_history is True
    assert written.peaks_avoided_count is None
    assert written.self_consumption_ratio is None
    assert written.estimated_cost_savings_eur is None
    assert written.data_complete_days_count == 0
    # Event-log query MUST be skipped on insufficient-history path (AC6 step 4).
    event_log_repo.count_peak_limiting_applied_decisions.assert_not_awaited()


@pytest.mark.asyncio
async def test_weekly_summary_writes_insufficient_history_when_fewer_than_seven_complete_days_available() -> None:  # noqa: E501  # fmt: skip
    """AC15 #20: 5 complete days < 7 → insufficient_history=True."""
    intervals = _make_intervals_for_days(end_utc=_FIXED_NOW, days=5)
    energy_repo = MagicMock()
    energy_repo.read_energy_flow_intervals_since = AsyncMock(return_value=intervals)
    energy_repo.upsert_weekly_energy_summary = AsyncMock()
    event_log_repo = MagicMock()
    event_log_repo.count_peak_limiting_applied_decisions = AsyncMock(return_value=0)

    await _compute_and_upsert_weekly_summary(
        energy_repo=energy_repo,
        event_log_repo=event_log_repo,
        settings=_settings(),
        now=_FIXED_NOW,
    )
    written: WeeklyEnergySummaryRow = energy_repo.upsert_weekly_energy_summary.await_args.args[0]
    assert written.insufficient_history is True
    assert written.data_complete_days_count == 5


@pytest.mark.asyncio
async def test_weekly_summary_computes_self_consumption_ratio_from_pv_and_grid_export_totals() -> None:  # noqa: E501  # fmt: skip
    """AC15 #21: ratio = (total_pv - total_exported) / total_pv."""
    # 7 days × 96 intervals × 0.5 kWh PV = 336 kWh PV; × 0.1 kWh export = 67.2 kWh.
    # ratio = (336 - 67.2) / 336 = 0.8.
    intervals = _make_intervals_for_days(end_utc=_FIXED_NOW, days=7)
    energy_repo = MagicMock()
    energy_repo.read_energy_flow_intervals_since = AsyncMock(return_value=intervals)
    energy_repo.upsert_weekly_energy_summary = AsyncMock()
    event_log_repo = MagicMock()
    event_log_repo.count_peak_limiting_applied_decisions = AsyncMock(return_value=0)

    await _compute_and_upsert_weekly_summary(
        energy_repo=energy_repo,
        event_log_repo=event_log_repo,
        settings=_settings(),
        now=_FIXED_NOW,
    )
    written: WeeklyEnergySummaryRow = energy_repo.upsert_weekly_energy_summary.await_args.args[0]
    assert written.insufficient_history is False
    assert written.self_consumption_ratio == pytest.approx(0.8, abs=1e-6)


@pytest.mark.asyncio
async def test_weekly_summary_clamps_self_consumption_ratio_to_zero_when_total_pv_kwh_is_zero() -> None:  # noqa: E501  # fmt: skip
    """AC15 #22: winter scenario (no PV) → ratio = 0.0, NOT NaN."""
    intervals = _make_intervals_for_days(
        end_utc=_FIXED_NOW,
        days=7,
        pv_kwh_per_interval=0.0,
        grid_exported_kwh_per_interval=0.0,
        battery_discharged_kwh_per_interval=0.0,
    )
    energy_repo = MagicMock()
    energy_repo.read_energy_flow_intervals_since = AsyncMock(return_value=intervals)
    energy_repo.upsert_weekly_energy_summary = AsyncMock()
    event_log_repo = MagicMock()
    event_log_repo.count_peak_limiting_applied_decisions = AsyncMock(return_value=0)

    await _compute_and_upsert_weekly_summary(
        energy_repo=energy_repo,
        event_log_repo=event_log_repo,
        settings=_settings(),
        now=_FIXED_NOW,
    )
    written: WeeklyEnergySummaryRow = energy_repo.upsert_weekly_energy_summary.await_args.args[0]
    assert written.self_consumption_ratio == 0.0


@pytest.mark.asyncio
async def test_weekly_summary_estimated_cost_savings_includes_self_consumption_export_and_battery_components() -> None:  # noqa: E501  # fmt: skip
    """AC15 #23: cost savings = self_consumed × import_tariff + exported × export_tariff
    + battery_discharged × import_tariff × 0.5."""
    intervals = _make_intervals_for_days(end_utc=_FIXED_NOW, days=7)
    # 7d × 96 = 672 intervals × 0.5 PV = 336 kWh; × 0.1 export = 67.2 kWh; × 0.05 batt = 33.6 kWh.
    # self_consumed = 336 - 67.2 = 268.8 kWh.
    # savings = 268.8 × 0.30 + 67.2 × 0.05 + 33.6 × 0.30 × 0.5
    #         = 80.64 + 3.36 + 5.04 = 89.04
    energy_repo = MagicMock()
    energy_repo.read_energy_flow_intervals_since = AsyncMock(return_value=intervals)
    energy_repo.upsert_weekly_energy_summary = AsyncMock()
    event_log_repo = MagicMock()
    event_log_repo.count_peak_limiting_applied_decisions = AsyncMock(return_value=0)

    await _compute_and_upsert_weekly_summary(
        energy_repo=energy_repo,
        event_log_repo=event_log_repo,
        settings=_settings(),
        now=_FIXED_NOW,
    )
    written: WeeklyEnergySummaryRow = energy_repo.upsert_weekly_energy_summary.await_args.args[0]
    assert written.estimated_cost_savings_eur == pytest.approx(89.04, abs=1e-2)


@pytest.mark.asyncio
async def test_weekly_summary_peaks_avoided_count_reads_event_log_decisions_with_peak_limiting_source_rule_and_applied_true() -> None:  # noqa: E501  # fmt: skip
    """AC15 #24: peaks_avoided_count delegates to event_log_repo.count_peak_limiting_applied_decisions."""  # noqa: E501  # fmt: skip
    intervals = _make_intervals_for_days(end_utc=_FIXED_NOW, days=7)
    energy_repo = MagicMock()
    energy_repo.read_energy_flow_intervals_since = AsyncMock(return_value=intervals)
    energy_repo.upsert_weekly_energy_summary = AsyncMock()
    event_log_repo = MagicMock()
    event_log_repo.count_peak_limiting_applied_decisions = AsyncMock(return_value=4)

    await _compute_and_upsert_weekly_summary(
        energy_repo=energy_repo,
        event_log_repo=event_log_repo,
        settings=_settings(),
        now=_FIXED_NOW,
    )
    written: WeeklyEnergySummaryRow = energy_repo.upsert_weekly_energy_summary.await_args.args[0]
    assert written.peaks_avoided_count == 4
    # The query was bounded by [window_start, window_end].
    call_kwargs = event_log_repo.count_peak_limiting_applied_decisions.await_args.kwargs
    assert call_kwargs["until"] == _FIXED_NOW
    assert call_kwargs["since"] == _FIXED_NOW - timedelta(days=7)


@pytest.mark.asyncio
async def test_weekly_summary_aggregation_loop_survives_repo_error_and_continues_on_next_tick() -> None:  # noqa: E501  # fmt: skip
    """AC15 #25: a transient repo error must not crash the long-running task."""
    energy_repo = MagicMock()
    # First call raises; second succeeds with empty list.
    energy_repo.read_energy_flow_intervals_since = AsyncMock(
        side_effect=[RuntimeError("DB transient"), []]
    )
    energy_repo.upsert_weekly_energy_summary = AsyncMock()
    event_log_repo = MagicMock()
    event_log_repo.count_peak_limiting_applied_decisions = AsyncMock(return_value=0)
    settings = _settings(aggregation_interval_seconds=300)  # min allowed

    # Patch asyncio.sleep to a no-op so we don't actually wait the full cadence.
    sleep_calls: list[float] = []

    async def _instant_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError

    task = asyncio.create_task(
        weekly_energy_summary_task(
            energy_repo=energy_repo,
            event_log_repo=event_log_repo,
            settings=settings,
            clock=lambda: _FIXED_NOW,
        )
    )

    import unittest.mock

    with unittest.mock.patch("asyncio.sleep", side_effect=_instant_sleep):
        with pytest.raises(asyncio.CancelledError):
            await task

    # Two iterations: first raised, second succeeded with empty list → insufficient history.
    assert energy_repo.read_energy_flow_intervals_since.await_count == 2
    energy_repo.upsert_weekly_energy_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_weekly_summary_first_aggregation_runs_immediately_not_after_first_sleep() -> None:
    """AC15 #26: first aggregation runs BEFORE the first asyncio.sleep call."""
    energy_repo = MagicMock()
    energy_repo.read_energy_flow_intervals_since = AsyncMock(return_value=[])
    energy_repo.upsert_weekly_energy_summary = AsyncMock()
    event_log_repo = MagicMock()
    event_log_repo.count_peak_limiting_applied_decisions = AsyncMock(return_value=0)

    sleep_call_count = 0
    upsert_at_first_sleep: int | None = None

    async def _capture_sleep(seconds: float) -> None:
        nonlocal sleep_call_count, upsert_at_first_sleep
        sleep_call_count += 1
        if sleep_call_count == 1:
            upsert_at_first_sleep = energy_repo.upsert_weekly_energy_summary.await_count
        raise asyncio.CancelledError

    import unittest.mock

    with unittest.mock.patch("asyncio.sleep", side_effect=_capture_sleep):
        with pytest.raises(asyncio.CancelledError):
            await weekly_energy_summary_task(
                energy_repo=energy_repo,
                event_log_repo=event_log_repo,
                settings=_settings(),
                clock=lambda: _FIXED_NOW,
            )
    # By the time the FIRST sleep was called, the upsert had already happened.
    assert upsert_at_first_sleep == 1


@pytest.mark.asyncio
async def test_weekly_summary_data_complete_days_count_uses_settings_threshold_for_minimum_intervals_per_day() -> None:  # noqa: E501  # fmt: skip
    """AC15 #27: ``weekly_summary_min_intervals_per_complete_day`` gates the day count.

    Seed 7 days × 50 intervals (below default 80 threshold) → no day counts → insufficient.
    Seed 7 days × 50 intervals with threshold lowered to 50 → all days count → sufficient.
    """
    intervals = _make_intervals_for_days(end_utc=_FIXED_NOW, days=7, intervals_per_day=50)
    energy_repo = MagicMock()
    energy_repo.read_energy_flow_intervals_since = AsyncMock(return_value=intervals)
    energy_repo.upsert_weekly_energy_summary = AsyncMock()
    event_log_repo = MagicMock()
    event_log_repo.count_peak_limiting_applied_decisions = AsyncMock(return_value=0)

    # Default threshold = 80 → insufficient.
    await _compute_and_upsert_weekly_summary(
        energy_repo=energy_repo,
        event_log_repo=event_log_repo,
        settings=_settings(),
        now=_FIXED_NOW,
    )
    written: WeeklyEnergySummaryRow = energy_repo.upsert_weekly_energy_summary.await_args.args[0]
    assert written.insufficient_history is True

    # Lower threshold to 50 → sufficient.
    energy_repo.upsert_weekly_energy_summary.reset_mock()
    await _compute_and_upsert_weekly_summary(
        energy_repo=energy_repo,
        event_log_repo=event_log_repo,
        settings=_settings(min_intervals_per_day=50),
        now=_FIXED_NOW,
    )
    written2: WeeklyEnergySummaryRow = energy_repo.upsert_weekly_energy_summary.await_args.args[0]
    assert written2.insufficient_history is False


@pytest.mark.asyncio
async def test_weekly_summary_window_uses_seven_days_relative_to_aggregator_clock_function() -> None:  # noqa: E501  # fmt: skip
    """AC15 #28: window_end = clock(); window_start = window_end - 7d."""
    energy_repo = MagicMock()
    energy_repo.read_energy_flow_intervals_since = AsyncMock(return_value=[])
    energy_repo.upsert_weekly_energy_summary = AsyncMock()
    event_log_repo = MagicMock()

    custom_clock_value = datetime(2026, 6, 15, 18, 30, 0, tzinfo=UTC)
    await _compute_and_upsert_weekly_summary(
        energy_repo=energy_repo,
        event_log_repo=event_log_repo,
        settings=_settings(),
        now=custom_clock_value,
    )
    energy_repo.read_energy_flow_intervals_since.assert_awaited_once_with(
        custom_clock_value - timedelta(days=7)
    )
    written: WeeklyEnergySummaryRow = energy_repo.upsert_weekly_energy_summary.await_args.args[0]
    assert written.window_end_utc == custom_clock_value
    assert written.window_start_utc == custom_clock_value - timedelta(days=7)
