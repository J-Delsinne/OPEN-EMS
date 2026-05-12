"""Periodic weekly-energy-summary aggregator service (Story 10.4 / FR30).

Module-level async function ``weekly_energy_summary_task(...)`` is the long-running
task spawned from ``lifespan(...)`` — mirrors the ``_event_log_pruning_task`` /
``_session_cleanup_task`` pattern at ``web/app.py:127-146``. It runs the FIRST
aggregation immediately on startup so the ``weekly_energy_summary`` row exists
within seconds of process boot (insufficient-history sentinel until 7 full days of
data exist).

Aggregation is in-process pure-function (``_compute_and_upsert_weekly_summary``) over
three repo reads and one upsert. No live computation runs at HTTP request time — the
homeowner endpoint reads the pre-aggregated row directly.

Failure isolation: any exception inside the body is caught + logged + the loop
continues on the next sleep interval. A crashed aggregator leaves the existing row
in place; the homeowner endpoint serves stale data with the most-recent ``computed_at``
timestamp.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog

from open_ems.core.energy import WeeklyEnergySummaryRow
from open_ems.settings import Settings
from open_ems.storage.repositories.energy_repo import EnergyRepo
from open_ems.storage.repositories.event_log_repo import EventLogRepo

logger = structlog.get_logger(__name__)


async def weekly_energy_summary_task(
    *,
    energy_repo: EnergyRepo,
    event_log_repo: EventLogRepo,
    settings: Settings,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Long-running aggregator loop. Runs the FIRST pass immediately, then sleeps
    for ``settings.weekly_summary_aggregation_interval_seconds`` between passes.

    Survives transient repo errors (logs + continues). Cancelled only by lifespan
    shutdown.
    """
    while True:
        try:
            await _compute_and_upsert_weekly_summary(
                energy_repo=energy_repo,
                event_log_repo=event_log_repo,
                settings=settings,
                now=clock(),
            )
        except Exception:  # noqa: BLE001 — task must survive transient errors
            logger.error(
                "weekly_summary_aggregation_failed",
                exc_info=True,
                component="weekly_summary",
            )
        await asyncio.sleep(settings.weekly_summary_aggregation_interval_seconds)


async def _compute_and_upsert_weekly_summary(
    *,
    energy_repo: EnergyRepo,
    event_log_repo: EventLogRepo,
    settings: Settings,
    now: datetime,
) -> None:
    """Single aggregation pass. Reads intervals + events, computes the three FR30
    metrics, and upserts the single ``weekly_energy_summary`` row.

    Insufficient-history branch (``data_complete_days_count < 7``): writes a row
    with the three metrics as ``None`` and ``insufficient_history=True``.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (UTC)")
    window_end = now.astimezone(UTC)
    window_start = window_end - timedelta(days=7)

    intervals = await energy_repo.read_energy_flow_intervals_since(window_start)

    # Single source of truth for the [window_start, window_end) window. The read
    # query only filters >= window_start; the upper bound is enforced here so
    # both the day-count semantics AND the kWh totals share the same window.
    # Without this, a future-dated interval (clock skew / test seed / defensive
    # scenario) would be summed into totals while NOT counting toward
    # data_complete_days_count, producing asymmetric window semantics between
    # AC6 step 3 and AC6 step 5.
    windowed_intervals = [r for r in intervals if window_start <= r.interval_start_utc < window_end]

    # Count distinct UTC dates within the window where the day has at least
    # `weekly_summary_min_intervals_per_complete_day` rows of data_quality='complete'.
    days_with_threshold: dict[str, int] = {}
    for row in windowed_intervals:
        if row.data_quality != "complete":
            continue
        date_key = row.interval_start_utc.astimezone(UTC).date().isoformat()
        days_with_threshold[date_key] = days_with_threshold.get(date_key, 0) + 1

    threshold = settings.weekly_summary_min_intervals_per_complete_day
    data_complete_days_count = sum(
        1 for count in days_with_threshold.values() if count >= threshold
    )

    if data_complete_days_count < 7:
        insufficient_row = WeeklyEnergySummaryRow(
            window_start_utc=window_start,
            window_end_utc=window_end,
            peaks_avoided_count=None,
            self_consumption_ratio=None,
            estimated_cost_savings_eur=None,
            data_complete_days_count=data_complete_days_count,
            insufficient_history=True,
            computed_at=window_end,
        )
        await energy_repo.upsert_weekly_energy_summary(insufficient_row)
        logger.info(
            "weekly_summary_aggregated",
            insufficient_history=True,
            data_complete_days_count=data_complete_days_count,
            component="weekly_summary",
        )
        return

    # Sufficient history: compute the three metrics over complete-quality intervals.
    complete_intervals = [r for r in windowed_intervals if r.data_quality == "complete"]
    total_pv_kwh = sum(r.pv_kwh for r in complete_intervals)
    total_grid_exported_kwh = sum(r.grid_exported_kwh for r in complete_intervals)
    total_battery_discharged_kwh = sum(r.battery_discharged_kwh for r in complete_intervals)

    # Self-consumption ratio. Zero-PV winter scenario → 0.0 (NOT NaN).
    if total_pv_kwh <= 0.0:
        self_consumption_ratio = 0.0
        logger.info(
            "self_consumption_zero_pv_window",
            window_start_utc=window_start.isoformat(),
            window_end_utc=window_end.isoformat(),
            component="weekly_summary",
        )
    else:
        ratio = (total_pv_kwh - total_grid_exported_kwh) / total_pv_kwh
        self_consumption_ratio = max(0.0, min(1.0, ratio))

    # Cost savings (EUR), three components:
    #   1. Self-consumption: kWh produced and consumed on-site (would have been imported)
    #      × import tariff.
    #   2. Export revenue: kWh exported × export tariff.
    #   3. Battery cycling savings: half-weight of discharged kWh × import tariff
    #      (conservative — actual savings depend on time-of-use tariff structure).
    self_consumed_kwh = max(0.0, total_pv_kwh - total_grid_exported_kwh)
    estimated_cost_savings_eur = round(
        self_consumed_kwh * settings.default_import_tariff_eur_per_kwh
        + total_grid_exported_kwh * settings.default_export_tariff_eur_per_kwh
        + total_battery_discharged_kwh * settings.default_import_tariff_eur_per_kwh * 0.5,
        2,
    )

    peaks_avoided_count = await event_log_repo.count_peak_limiting_applied_decisions(
        since=window_start,
        until=window_end,
    )

    sufficient_row = WeeklyEnergySummaryRow(
        window_start_utc=window_start,
        window_end_utc=window_end,
        peaks_avoided_count=peaks_avoided_count,
        self_consumption_ratio=self_consumption_ratio,
        estimated_cost_savings_eur=estimated_cost_savings_eur,
        data_complete_days_count=data_complete_days_count,
        insufficient_history=False,
        computed_at=window_end,
    )
    await energy_repo.upsert_weekly_energy_summary(sufficient_row)
    logger.info(
        "weekly_summary_aggregated",
        insufficient_history=False,
        data_complete_days_count=data_complete_days_count,
        peaks_avoided_count=peaks_avoided_count,
        self_consumption_ratio=self_consumption_ratio,
        estimated_cost_savings_eur=estimated_cost_savings_eur,
        component="weekly_summary",
    )
