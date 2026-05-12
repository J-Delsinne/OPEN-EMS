"""Story 11.2 AC15 — NFR-P5 performance benchmark.

Event log queries covering the past 30 days must return within 5 seconds at
representative log volume (~3000 rows = ~100/day over 30 days).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from open_ems.storage.database import get_connection
from open_ems.storage.repositories.event_log_repo import EventLogRepo

_CREATE_EVENT_LOG = """
    CREATE TABLE IF NOT EXISTS event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schema_version INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        actor TEXT NOT NULL,
        event_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        detail TEXT,
        device_id TEXT,
        config_version TEXT
    )
"""

_CREATE_TIMESTAMP_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_event_log_timestamp ON event_log(timestamp)"
)
_CREATE_EVENT_TYPE_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_event_log_event_type ON event_log(event_type)"
)


@pytest.mark.asyncio
async def test_event_log_30_day_query_under_nfr_p5_budget_at_representative_volume(
    session_repo: object,  # session_repo fixture initializes the in-memory DB
) -> None:
    """AC15: NFR-P5 — past-30-day filtered queries must complete < 5 seconds.

    Strategy: insert 3000 rows over a 30-day window (~100/day, 5 event types
    rotating). Run 4 representative query shapes (no-filter, type-filter,
    keyword-filter, count) 10 times each. Assert mean times match the AC15
    targets AND every individual run is under the 5-second hard ceiling.

    NFR-P5 is the contract: ``max(timings) < 5000ms`` is the unconditional
    requirement. The per-shape mean ceilings are tighter to surface
    regressions early.
    """
    conn = get_connection()
    await conn.execute(_CREATE_EVENT_LOG)
    await conn.execute(_CREATE_TIMESTAMP_INDEX)
    await conn.execute(_CREATE_EVENT_TYPE_INDEX)
    await conn.commit()

    # 3000 rows over 30 days, ~100/day, rotating 5 event types + 4 device_ids.
    event_types = ("DECISION", "DEVICE", "SYSTEM", "CONSTRAINT", "INSTALLER")
    device_ids = ("batt-1", "inv-1", "grid-1", "ev-1")
    base = datetime.now(UTC)
    rows: list[tuple[object, ...]] = []
    for i in range(3000):
        ts = base - timedelta(minutes=i * 30 * 24 * 60 // 3000)  # spread over 30d
        rows.append(
            (
                1,
                ts.isoformat(),
                "system",
                event_types[i % 5],
                f"event summary {i} peak limit grid battery",
                device_ids[i % 4],
            )
        )
    await conn.executemany(
        "INSERT INTO event_log"
        " (schema_version, timestamp, actor, event_type, summary, device_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    await conn.commit()

    repo = EventLogRepo(conn)
    since = base - timedelta(days=30)
    until = base

    async def _time(coro_factory: object, iterations: int = 10) -> tuple[float, float]:
        """Run an async callable N times; return (mean_ms, max_ms)."""
        timings: list[float] = []
        for _ in range(iterations):
            start = time.perf_counter()
            await coro_factory()  # type: ignore[operator]
            timings.append((time.perf_counter() - start) * 1000.0)
        return sum(timings) / len(timings), max(timings)

    # Warm-up pass (one of each shape).
    await repo.list_filtered(
        event_types=frozenset(),
        device_id=None,
        since=since,
        until=until,
        keyword="",
        limit=50,
        offset=0,
    )

    # Shape 1: no-filter 30-day list (50 rows).
    mean1, max1 = await _time(
        lambda: repo.list_filtered(
            event_types=frozenset(),
            device_id=None,
            since=since,
            until=until,
            keyword="",
            limit=50,
            offset=0,
        )
    )
    # Shape 2: type-filter list.
    mean2, max2 = await _time(
        lambda: repo.list_filtered(
            event_types=frozenset({"DECISION"}),
            device_id=None,
            since=since,
            until=until,
            keyword="",
            limit=50,
            offset=0,
        )
    )
    # Shape 3: keyword-filter list.
    mean3, max3 = await _time(
        lambda: repo.list_filtered(
            event_types=frozenset(),
            device_id=None,
            since=since,
            until=until,
            keyword="peak",
            limit=50,
            offset=0,
        )
    )
    # Shape 4: count over the same 30-day window with a filter.
    mean4, max4 = await _time(
        lambda: repo.count_filtered(
            event_types=frozenset({"DECISION", "DEVICE"}),
            device_id=None,
            since=since,
            until=until,
            keyword="",
        )
    )

    # NFR-P5 hard ceiling: every individual query under 5 seconds.
    for label, max_ms in [
        ("no-filter list", max1),
        ("type-filter list", max2),
        ("keyword-filter list", max3),
        ("count_filtered", max4),
    ]:
        assert max_ms < 5000.0, f"NFR-P5 violation: {label} took {max_ms:.1f}ms (ceiling 5000ms)"

    # Looser mean checks per AC15 — these flag regressions before they hit NFR-P5.
    assert mean1 < 200.0, f"no-filter list mean {mean1:.1f}ms exceeded 200ms target"
    assert mean2 < 500.0, f"type-filter list mean {mean2:.1f}ms exceeded 500ms target"
    assert mean3 < 1500.0, f"keyword-filter list mean {mean3:.1f}ms exceeded 1500ms target"
    assert mean4 < 2000.0, f"count_filtered mean {mean4:.1f}ms exceeded 2000ms target"
