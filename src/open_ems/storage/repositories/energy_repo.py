from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

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
