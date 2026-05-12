"""Unit tests for :class:`EnergyRepo` Story 10.4 extensions (AC4 + AC15 #13–#18)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import aiosqlite
import pytest
import pytest_asyncio

from open_ems.core.energy import (
    CompletedEnergyFlowInterval,
    WeeklyEnergySummaryRow,
)
from open_ems.storage.repositories.energy_repo import EnergyRepo

_CREATE_ENERGY_FLOW_INTERVALS = """
    CREATE TABLE energy_flow_intervals (
        interval_start_utc TEXT NOT NULL PRIMARY KEY,
        pv_kwh REAL NOT NULL DEFAULT 0.0,
        battery_charged_kwh REAL NOT NULL DEFAULT 0.0,
        battery_discharged_kwh REAL NOT NULL DEFAULT 0.0,
        grid_imported_kwh REAL NOT NULL DEFAULT 0.0,
        grid_exported_kwh REAL NOT NULL DEFAULT 0.0,
        ev_charged_kwh REAL NOT NULL DEFAULT 0.0,
        sample_count INTEGER NOT NULL DEFAULT 0,
        data_quality TEXT NOT NULL DEFAULT 'complete'
    )
"""

_CREATE_WEEKLY_ENERGY_SUMMARY = """
    CREATE TABLE weekly_energy_summary (
        id INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
        window_start_utc TEXT NOT NULL,
        window_end_utc TEXT NOT NULL,
        peaks_avoided_count INTEGER,
        self_consumption_ratio REAL,
        estimated_cost_savings_eur REAL,
        data_complete_days_count INTEGER NOT NULL DEFAULT 0,
        insufficient_history INTEGER NOT NULL DEFAULT 1,
        computed_at TEXT NOT NULL
    )
"""


@pytest_asyncio.fixture
async def repo_with_conn() -> AsyncGenerator[tuple[EnergyRepo, aiosqlite.Connection], None]:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_ENERGY_FLOW_INTERVALS)
        await conn.execute(_CREATE_WEEKLY_ENERGY_SUMMARY)
        await conn.commit()
        yield EnergyRepo(conn), conn


def _completed(
    *,
    interval_start_utc: datetime,
    pv_kwh: float = 0.0,
    battery_charged_kwh: float = 0.0,
    battery_discharged_kwh: float = 0.0,
    grid_imported_kwh: float = 0.0,
    grid_exported_kwh: float = 0.0,
    ev_charged_kwh: float = 0.0,
    sample_count: int = 0,
    data_quality: str = "complete",
) -> CompletedEnergyFlowInterval:
    return CompletedEnergyFlowInterval(
        interval_start_utc=interval_start_utc,
        pv_kwh=pv_kwh,
        battery_charged_kwh=battery_charged_kwh,
        battery_discharged_kwh=battery_discharged_kwh,
        grid_imported_kwh=grid_imported_kwh,
        grid_exported_kwh=grid_exported_kwh,
        ev_charged_kwh=ev_charged_kwh,
        sample_count=sample_count,
        data_quality=data_quality,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_energy_repo_write_energy_flow_interval_inserts_row_with_all_columns(
    repo_with_conn: tuple[EnergyRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    completed = _completed(
        interval_start_utc=datetime(2026, 5, 12, 10, 0, tzinfo=UTC),
        pv_kwh=1.25,
        battery_charged_kwh=0.05,
        battery_discharged_kwh=0.03,
        grid_imported_kwh=0.5,
        grid_exported_kwh=0.1,
        ev_charged_kwh=0.2,
        sample_count=90,
        data_quality="complete",
    )
    await repo.write_energy_flow_interval(completed)

    async with conn.execute(
        "SELECT interval_start_utc, pv_kwh, battery_charged_kwh, battery_discharged_kwh,"
        " grid_imported_kwh, grid_exported_kwh, ev_charged_kwh, sample_count, data_quality"
        " FROM energy_flow_intervals"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "2026-05-12T10:00:00+00:00"
    assert row[1] == pytest.approx(1.25)
    assert row[2] == pytest.approx(0.05)
    assert row[3] == pytest.approx(0.03)
    assert row[4] == pytest.approx(0.5)
    assert row[5] == pytest.approx(0.1)
    assert row[6] == pytest.approx(0.2)
    assert row[7] == 90
    assert row[8] == "complete"


@pytest.mark.asyncio
async def test_energy_repo_write_energy_flow_interval_is_idempotent_on_duplicate_interval_start(
    repo_with_conn: tuple[EnergyRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    start = datetime(2026, 5, 12, 10, 0, tzinfo=UTC)
    await repo.write_energy_flow_interval(_completed(interval_start_utc=start, pv_kwh=1.0))
    # Re-write with different values for the same interval — INSERT OR REPLACE overwrites.
    await repo.write_energy_flow_interval(_completed(interval_start_utc=start, pv_kwh=2.5))
    async with conn.execute("SELECT pv_kwh, COUNT(*) OVER () FROM energy_flow_intervals") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_energy_repo_read_energy_flow_intervals_since_returns_rows_in_chronological_order(
    repo_with_conn: tuple[EnergyRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    # Write in reverse chronological order.
    await repo.write_energy_flow_interval(
        _completed(interval_start_utc=datetime(2026, 5, 12, 10, 30, tzinfo=UTC), pv_kwh=3.0)
    )
    await repo.write_energy_flow_interval(
        _completed(interval_start_utc=datetime(2026, 5, 12, 10, 0, tzinfo=UTC), pv_kwh=1.0)
    )
    await repo.write_energy_flow_interval(
        _completed(interval_start_utc=datetime(2026, 5, 12, 10, 15, tzinfo=UTC), pv_kwh=2.0)
    )
    rows = await repo.read_energy_flow_intervals_since(datetime(2026, 5, 12, 0, 0, tzinfo=UTC))
    assert [r.pv_kwh for r in rows] == [1.0, 2.0, 3.0]
    assert [r.interval_start_utc for r in rows] == [
        datetime(2026, 5, 12, 10, 0, tzinfo=UTC),
        datetime(2026, 5, 12, 10, 15, tzinfo=UTC),
        datetime(2026, 5, 12, 10, 30, tzinfo=UTC),
    ]


@pytest.mark.asyncio
async def test_energy_repo_read_energy_flow_intervals_since_respects_filter(
    repo_with_conn: tuple[EnergyRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.write_energy_flow_interval(
        _completed(interval_start_utc=datetime(2026, 5, 12, 9, 0, tzinfo=UTC))
    )
    await repo.write_energy_flow_interval(
        _completed(interval_start_utc=datetime(2026, 5, 12, 10, 0, tzinfo=UTC), pv_kwh=5.0)
    )
    rows = await repo.read_energy_flow_intervals_since(datetime(2026, 5, 12, 9, 30, tzinfo=UTC))
    assert len(rows) == 1
    assert rows[0].pv_kwh == 5.0


@pytest.mark.asyncio
async def test_energy_repo_upsert_weekly_energy_summary_inserts_when_no_row_exists(
    repo_with_conn: tuple[EnergyRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    summary = WeeklyEnergySummaryRow(
        window_start_utc=datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
        window_end_utc=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
        peaks_avoided_count=None,
        self_consumption_ratio=None,
        estimated_cost_savings_eur=None,
        data_complete_days_count=0,
        insufficient_history=True,
        computed_at=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
    )
    await repo.upsert_weekly_energy_summary(summary)
    async with conn.execute("SELECT id, insufficient_history FROM weekly_energy_summary") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] == 1  # SQLite stores bool True as int 1


@pytest.mark.asyncio
async def test_energy_repo_upsert_weekly_energy_summary_overwrites_existing_single_row(
    repo_with_conn: tuple[EnergyRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    first = WeeklyEnergySummaryRow(
        window_start_utc=datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
        window_end_utc=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
        data_complete_days_count=0,
        insufficient_history=True,
        computed_at=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
    )
    await repo.upsert_weekly_energy_summary(first)
    second = WeeklyEnergySummaryRow(
        window_start_utc=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
        window_end_utc=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
        peaks_avoided_count=4,
        self_consumption_ratio=0.65,
        estimated_cost_savings_eur=12.34,
        data_complete_days_count=7,
        insufficient_history=False,
        computed_at=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )
    await repo.upsert_weekly_energy_summary(second)
    async with conn.execute(
        "SELECT peaks_avoided_count, self_consumption_ratio, estimated_cost_savings_eur,"
        "       insufficient_history, COUNT(*) OVER ()"
        " FROM weekly_energy_summary"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 4
    assert rows[0][1] == pytest.approx(0.65)
    assert rows[0][2] == pytest.approx(12.34)
    assert rows[0][3] == 0  # insufficient_history=False → 0


@pytest.mark.asyncio
async def test_energy_repo_read_weekly_energy_summary_returns_None_on_cold_start_table(
    repo_with_conn: tuple[EnergyRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    result = await repo.read_weekly_energy_summary()
    assert result is None


@pytest.mark.asyncio
async def test_energy_repo_read_weekly_energy_summary_round_trips_through_upsert(
    repo_with_conn: tuple[EnergyRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    written = WeeklyEnergySummaryRow(
        window_start_utc=datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
        window_end_utc=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
        peaks_avoided_count=3,
        self_consumption_ratio=0.72,
        estimated_cost_savings_eur=18.50,
        data_complete_days_count=7,
        insufficient_history=False,
        computed_at=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
    )
    await repo.upsert_weekly_energy_summary(written)
    read = await repo.read_weekly_energy_summary()
    assert read == written
