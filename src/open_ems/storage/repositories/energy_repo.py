from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from open_ems.core.energy import (
    CompletedEnergyFlowInterval,
    EnergyFlowIntervalRow,
    WeeklyEnergySummaryRow,
)
from open_ems.engine.partial_interval_tracker import CompletedInterval
from open_ems.storage.database import get_connection, get_write_lock


class EnergyRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    async def write_peak_interval(
        self,
        completed: CompletedInterval,
        *,
        data_quality: str = "complete",
    ) -> None:
        async with get_write_lock():
            async with self._conn.execute(
                "INSERT OR REPLACE INTO peak_intervals"
                " (interval_start_utc, avg_power_kw, sample_count, data_quality)"
                " VALUES (?, ?, ?, ?)",
                (
                    completed.interval_start_utc.isoformat(),
                    completed.avg_power_kw,
                    completed.sample_count,
                    data_quality,
                ),
            ):
                pass
            await self._conn.commit()

    async def get_current_monthly_peak_kw(self) -> float:
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        async with self._conn.execute(
            "SELECT COALESCE(MAX(avg_power_kw), 0.0) FROM peak_intervals"
            " WHERE interval_start_utc >= ?",
            (month_start,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None  # COALESCE guarantees one row from an aggregate query
        return float(row[0])

    async def write_energy_flow_interval(
        self,
        completed: CompletedEnergyFlowInterval,
    ) -> None:
        """Story 10.4 AC4: persist a completed 15-min energy-flow interval.

        Uses ``INSERT OR REPLACE`` for idempotency on duplicate
        ``interval_start_utc`` (e.g., test-driven re-execution or a control-loop
        replay scenario after restart with a clock that briefly went backwards).
        """
        async with get_write_lock():
            async with self._conn.execute(
                "INSERT OR REPLACE INTO energy_flow_intervals"
                " (interval_start_utc, pv_kwh, battery_charged_kwh, battery_discharged_kwh,"
                "  grid_imported_kwh, grid_exported_kwh, ev_charged_kwh,"
                "  sample_count, data_quality)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    completed.interval_start_utc.isoformat(),
                    completed.pv_kwh,
                    completed.battery_charged_kwh,
                    completed.battery_discharged_kwh,
                    completed.grid_imported_kwh,
                    completed.grid_exported_kwh,
                    completed.ev_charged_kwh,
                    completed.sample_count,
                    completed.data_quality,
                ),
            ):
                pass
            await self._conn.commit()

    async def read_energy_flow_intervals_since(
        self,
        since_utc: datetime,
    ) -> list[EnergyFlowIntervalRow]:
        """Story 10.4 AC4: read all energy-flow rows with ``interval_start_utc >= since_utc``,
        chronologically ordered."""
        if since_utc.tzinfo is None or since_utc.utcoffset() is None:
            raise ValueError("since_utc must be timezone-aware (UTC)")
        async with self._conn.execute(
            "SELECT interval_start_utc, pv_kwh, battery_charged_kwh, battery_discharged_kwh,"
            "       grid_imported_kwh, grid_exported_kwh, ev_charged_kwh,"
            "       sample_count, data_quality"
            " FROM energy_flow_intervals"
            " WHERE interval_start_utc >= ?"
            " ORDER BY interval_start_utc ASC",
            (since_utc.astimezone(UTC).isoformat(),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            EnergyFlowIntervalRow(
                interval_start_utc=datetime.fromisoformat(row[0]),
                pv_kwh=float(row[1]),
                battery_charged_kwh=float(row[2]),
                battery_discharged_kwh=float(row[3]),
                grid_imported_kwh=float(row[4]),
                grid_exported_kwh=float(row[5]),
                ev_charged_kwh=float(row[6]),
                sample_count=int(row[7]),
                data_quality=row[8],
            )
            for row in rows
        ]

    async def upsert_weekly_energy_summary(
        self,
        row: WeeklyEnergySummaryRow,
    ) -> None:
        """Story 10.4 AC4: upsert the single ``weekly_energy_summary`` row (``id = 1``)."""
        async with get_write_lock():
            async with self._conn.execute(
                "INSERT INTO weekly_energy_summary"
                " (id, window_start_utc, window_end_utc, peaks_avoided_count,"
                "  self_consumption_ratio, estimated_cost_savings_eur,"
                "  data_complete_days_count, insufficient_history, computed_at)"
                " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "  window_start_utc = excluded.window_start_utc,"
                "  window_end_utc = excluded.window_end_utc,"
                "  peaks_avoided_count = excluded.peaks_avoided_count,"
                "  self_consumption_ratio = excluded.self_consumption_ratio,"
                "  estimated_cost_savings_eur = excluded.estimated_cost_savings_eur,"
                "  data_complete_days_count = excluded.data_complete_days_count,"
                "  insufficient_history = excluded.insufficient_history,"
                "  computed_at = excluded.computed_at",
                (
                    row.window_start_utc.isoformat(),
                    row.window_end_utc.isoformat(),
                    row.peaks_avoided_count,
                    row.self_consumption_ratio,
                    row.estimated_cost_savings_eur,
                    row.data_complete_days_count,
                    1 if row.insufficient_history else 0,
                    row.computed_at.isoformat(),
                ),
            ):
                pass
            await self._conn.commit()

    async def read_weekly_energy_summary(self) -> WeeklyEnergySummaryRow | None:
        """Story 10.4 AC4: read the single weekly_energy_summary row (id=1).

        Returns ``None`` if the row does not exist yet (cold-start path; aggregator
        has not yet run its first pass)."""
        async with self._conn.execute(
            "SELECT window_start_utc, window_end_utc, peaks_avoided_count,"
            "       self_consumption_ratio, estimated_cost_savings_eur,"
            "       data_complete_days_count, insufficient_history, computed_at"
            " FROM weekly_energy_summary"
            " WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return WeeklyEnergySummaryRow(
            window_start_utc=datetime.fromisoformat(row[0]),
            window_end_utc=datetime.fromisoformat(row[1]),
            peaks_avoided_count=row[2] if row[2] is not None else None,
            self_consumption_ratio=float(row[3]) if row[3] is not None else None,
            estimated_cost_savings_eur=float(row[4]) if row[4] is not None else None,
            data_complete_days_count=int(row[5]),
            insufficient_history=bool(row[6]),
            computed_at=datetime.fromisoformat(row[7]),
        )
