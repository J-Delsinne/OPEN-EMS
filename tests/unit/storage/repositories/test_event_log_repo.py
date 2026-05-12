from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import aiosqlite
import pytest
import pytest_asyncio

import open_ems.storage.repositories.event_log_repo as event_log_repo_module
from open_ems.storage.repositories.event_log_repo import (
    _CRITICAL_EVENT_TYPES,
    EventLogEntry,
    EventLogRepo,
)

_CREATE_EVENT_LOG = """
    CREATE TABLE event_log (
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

_NOW = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz: object = None) -> datetime:  # noqa: ANN401
        if tz is UTC:
            return _NOW
        return _NOW.replace(tzinfo=None)


@pytest_asyncio.fixture
async def repo_with_conn(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[EventLogRepo, aiosqlite.Connection], None]:
    monkeypatch.setattr(event_log_repo_module, "datetime", _FixedDateTime)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_EVENT_LOG)
        await conn.commit()
        yield EventLogRepo(conn), conn


async def _insert_entry(
    conn: aiosqlite.Connection,
    *,
    event_type: str,
    timestamp: datetime,
) -> None:
    await conn.execute(
        "INSERT INTO event_log (schema_version, timestamp, actor, event_type, summary)"
        " VALUES (1, ?, 'system', ?, 'test event')",
        (timestamp.isoformat(), event_type),
    )
    await conn.commit()


async def _count_entries(conn: aiosqlite.Connection) -> int:
    async with conn.execute("SELECT COUNT(*) FROM event_log") as cur:
        row = await cur.fetchone()
    return int(row[0])


async def _event_types(conn: aiosqlite.Connection) -> list[str]:
    async with conn.execute("SELECT event_type FROM event_log ORDER BY id") as cur:
        rows = await cur.fetchall()
    return [str(row["event_type"]) for row in rows]


def test_update_raises_append_only() -> None:
    repo = EventLogRepo(conn=MagicMock())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="append-only"):
        repo.update()


def test_delete_raises_append_only() -> None:
    repo = EventLogRepo(conn=MagicMock())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="append-only"):
        repo.delete()


@pytest.mark.asyncio
async def test_count_peak_limiting_applied_decisions_returns_zero_on_empty_log(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """Story 10.4 AC14: count returns 0 when no DECISION rows exist."""
    repo, _ = repo_with_conn
    result = await repo.count_peak_limiting_applied_decisions(
        since=_NOW - timedelta(days=7),
        until=_NOW,
    )
    assert result == 0


@pytest.mark.asyncio
async def test_count_peak_limiting_applied_decisions_only_counts_source_rule_peak_limiting_with_applied_true(  # noqa: E501  # fmt: skip
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """Story 10.4 AC14: only rows with source_rule=peak_limiting AND applied=true count."""
    repo, conn = repo_with_conn
    # Inside the window: one matching row.
    await repo.append(
        schema_version=1,
        timestamp=_NOW - timedelta(days=1),
        actor="system",
        event_type="DECISION",
        summary="ok",
        detail={
            "source_rule": "peak_limiting",
            "applied": True,
            "command_type": "SetEVChargingRateCommand",
        },
        device_id=None,
        config_version=None,
    )
    # Same source_rule but applied=false → excluded.
    await repo.append(
        schema_version=1,
        timestamp=_NOW - timedelta(days=2),
        actor="system",
        event_type="DECISION",
        summary="ok",
        detail={"source_rule": "peak_limiting", "applied": False},
        device_id=None,
        config_version=None,
    )
    # Different source_rule → excluded.
    await repo.append(
        schema_version=1,
        timestamp=_NOW - timedelta(days=3),
        actor="system",
        event_type="DECISION",
        summary="ok",
        detail={"source_rule": "strategy_minimize_cost", "applied": True},
        device_id=None,
        config_version=None,
    )
    # Wrong event_type → excluded.
    await repo.append(
        schema_version=1,
        timestamp=_NOW - timedelta(days=4),
        actor="system",
        event_type="CONSTRAINT",
        summary="ok",
        detail={"source_rule": "peak_limiting", "applied": True},
        device_id=None,
        config_version=None,
    )
    # Outside the window (too old) → excluded.
    await repo.append(
        schema_version=1,
        timestamp=_NOW - timedelta(days=8),
        actor="system",
        event_type="DECISION",
        summary="ok",
        detail={"source_rule": "peak_limiting", "applied": True},
        device_id=None,
        config_version=None,
    )

    result = await repo.count_peak_limiting_applied_decisions(
        since=_NOW - timedelta(days=7),
        until=_NOW,
    )
    assert result == 1


@pytest.mark.asyncio
async def test_prune_expired_deletes_non_critical_entry_older_than_retention(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    await _insert_entry(conn, event_type="DECISION", timestamp=_NOW - timedelta(days=91))

    deleted = await repo.prune_expired(retention_days=90)

    assert deleted == 1
    assert await _count_entries(conn) == 0


@pytest.mark.asyncio
async def test_prune_expired_keeps_non_critical_entry_newer_than_retention(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    await _insert_entry(conn, event_type="DECISION", timestamp=_NOW - timedelta(days=89))

    deleted = await repo.prune_expired(retention_days=90)

    assert deleted == 0
    assert await _count_entries(conn) == 1


@pytest.mark.asyncio
async def test_prune_expired_keeps_non_critical_entry_at_boundary(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    await _insert_entry(conn, event_type="DECISION", timestamp=_NOW - timedelta(days=90))

    deleted = await repo.prune_expired(retention_days=90)

    assert deleted == 0
    assert await _count_entries(conn) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", sorted(_CRITICAL_EVENT_TYPES))
async def test_prune_expired_keeps_critical_entry_older_than_retention(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
    event_type: str,
) -> None:
    repo, conn = repo_with_conn
    await _insert_entry(conn, event_type=event_type, timestamp=_NOW - timedelta(days=91))

    deleted = await repo.prune_expired(retention_days=90)

    assert deleted == 0
    assert await _count_entries(conn) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", sorted(_CRITICAL_EVENT_TYPES))
async def test_prune_expired_keeps_critical_entry_at_boundary(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
    event_type: str,
) -> None:
    repo, conn = repo_with_conn
    await _insert_entry(conn, event_type=event_type, timestamp=_NOW - timedelta(days=90))

    deleted = await repo.prune_expired(retention_days=90)

    assert deleted == 0
    assert await _count_entries(conn) == 1


@pytest.mark.asyncio
async def test_prune_expired_returns_deleted_count(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    for _ in range(3):
        await _insert_entry(conn, event_type="DECISION", timestamp=_NOW - timedelta(days=91))
    for _ in range(2):
        await _insert_entry(conn, event_type="CONSTRAINT", timestamp=_NOW - timedelta(days=91))

    deleted = await repo.prune_expired(retention_days=90)

    assert deleted == 3
    assert await _count_entries(conn) == 2


@pytest.mark.asyncio
async def test_prune_expired_mixed_entries_only_deletes_old_non_critical(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    await _insert_entry(conn, event_type="CONSTRAINT", timestamp=_NOW - timedelta(days=91))
    await _insert_entry(conn, event_type="DECISION", timestamp=_NOW - timedelta(days=91))
    await _insert_entry(conn, event_type="SYSTEM", timestamp=_NOW - timedelta(days=89))

    deleted = await repo.prune_expired(retention_days=90)

    assert deleted == 1
    assert await _event_types(conn) == ["CONSTRAINT", "SYSTEM"]


# ── Story 11.1 AC6 — list_recent + EventLogEntry ─────────────────────────────


@pytest.mark.asyncio
async def test_list_recent_returns_empty_list_on_empty_table(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC6: empty event_log returns []."""
    repo, _ = repo_with_conn
    result = await repo.list_recent(limit=5)
    assert result == []


@pytest.mark.asyncio
async def test_list_recent_honors_limit(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC6: limit caps the result set length."""
    repo, conn = repo_with_conn
    for i in range(10):
        await _insert_entry(conn, event_type="SYSTEM", timestamp=_NOW - timedelta(minutes=i))
    result = await repo.list_recent(limit=3)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_list_recent_orders_by_timestamp_desc(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC6: newest first."""
    repo, conn = repo_with_conn
    await _insert_entry(conn, event_type="SYSTEM", timestamp=_NOW - timedelta(minutes=3))
    await _insert_entry(conn, event_type="DECISION", timestamp=_NOW - timedelta(minutes=1))
    await _insert_entry(conn, event_type="DEVICE", timestamp=_NOW - timedelta(minutes=2))
    result = await repo.list_recent(limit=5)
    assert [entry.event_type for entry in result] == ["DECISION", "DEVICE", "SYSTEM"]


@pytest.mark.asyncio
async def test_list_recent_ties_broken_by_id_desc(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC6: same-millisecond ties broken by id DESC (load-bearing tie-breaker)."""
    repo, conn = repo_with_conn
    same_ts = _NOW - timedelta(minutes=1)
    await _insert_entry(conn, event_type="FIRST", timestamp=same_ts)
    await _insert_entry(conn, event_type="SECOND", timestamp=same_ts)
    await _insert_entry(conn, event_type="THIRD", timestamp=same_ts)
    result = await repo.list_recent(limit=5)
    # Newest id (THIRD inserted last) appears first.
    assert [entry.event_type for entry in result] == ["THIRD", "SECOND", "FIRST"]


@pytest.mark.asyncio
async def test_list_recent_populates_all_event_log_entry_fields(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC6: every column round-trips into EventLogEntry."""
    repo, _ = repo_with_conn
    ts = _NOW - timedelta(minutes=2)
    await repo.append(
        schema_version=1,
        timestamp=ts,
        actor="installer",
        event_type="INSTALLER",
        summary="Test note from installer",
        detail={"kind": "manual"},
        device_id="dev-1",
        config_version="v3",
    )
    [entry] = await repo.list_recent(limit=1)
    assert entry.id >= 1
    assert entry.schema_version == 1
    assert entry.timestamp == ts
    assert entry.actor == "installer"
    assert entry.event_type == "INSTALLER"
    assert entry.summary == "Test note from installer"
    assert entry.detail_json is not None and '"kind": "manual"' in entry.detail_json
    assert entry.device_id == "dev-1"
    assert entry.config_version == "v3"


@pytest.mark.asyncio
async def test_list_recent_returns_none_for_null_optional_columns(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC6: NULL device_id / detail / config_version → None in the model."""
    repo, conn = repo_with_conn
    await _insert_entry(conn, event_type="SYSTEM", timestamp=_NOW)
    [entry] = await repo.list_recent(limit=1)
    assert entry.detail_json is None
    assert entry.device_id is None
    assert entry.config_version is None


@pytest.mark.asyncio
async def test_list_recent_rejects_zero_or_negative_limit() -> None:
    """AC6: limit must be >= 1; defensive guard against caller misuse."""
    repo = EventLogRepo(conn=MagicMock())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit must be"):
        await repo.list_recent(limit=0)


def test_event_log_entry_is_frozen() -> None:
    """AC6: EventLogEntry follows the codebase's frozen-Pydantic pattern."""
    entry = EventLogEntry(
        id=1,
        schema_version=1,
        timestamp=_NOW,
        actor="system",
        event_type="SYSTEM",
        summary="bootstrap",
    )
    with pytest.raises((TypeError, ValueError)):
        entry.summary = "mutated"  # type: ignore[misc]


# ── Story 11.2 AC14 — list_filtered + count_filtered ─────────────────────────


async def _insert(
    conn: aiosqlite.Connection,
    *,
    event_type: str = "SYSTEM",
    timestamp: datetime | None = None,
    summary: str = "test event",
    device_id: str | None = None,
    actor: str = "system",
) -> None:
    """Flexible insert helper for filter tests."""
    ts = timestamp if timestamp is not None else _NOW
    await conn.execute(
        "INSERT INTO event_log"
        " (schema_version, timestamp, actor, event_type, summary, device_id)"
        " VALUES (1, ?, ?, ?, ?, ?)",
        (ts.isoformat(), actor, event_type, summary, device_id),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_list_filtered_no_filter_returns_all_ordered_by_timestamp_desc_then_id_desc(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC14: empty filter returns all rows newest-first."""
    repo, conn = repo_with_conn
    await _insert(conn, event_type="A", timestamp=_NOW - timedelta(minutes=3))
    await _insert(conn, event_type="B", timestamp=_NOW - timedelta(minutes=1))
    await _insert(conn, event_type="C", timestamp=_NOW - timedelta(minutes=2))
    result = await repo.list_filtered(
        event_types=frozenset(),
        device_id=None,
        since=None,
        until=None,
        keyword="",
        limit=50,
        offset=0,
    )
    assert [e.event_type for e in result] == ["B", "C", "A"]


@pytest.mark.asyncio
async def test_list_filtered_respects_limit_and_offset(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC14: pagination via LIMIT + OFFSET."""
    repo, conn = repo_with_conn
    for i in range(10):
        await _insert(
            conn,
            event_type=f"E{i:02d}",
            timestamp=_NOW - timedelta(minutes=i),
        )
    page1 = await repo.list_filtered(
        event_types=frozenset(),
        device_id=None,
        since=None,
        until=None,
        keyword="",
        limit=3,
        offset=0,
    )
    page2 = await repo.list_filtered(
        event_types=frozenset(),
        device_id=None,
        since=None,
        until=None,
        keyword="",
        limit=3,
        offset=3,
    )
    assert [e.event_type for e in page1] == ["E00", "E01", "E02"]
    assert [e.event_type for e in page2] == ["E03", "E04", "E05"]


@pytest.mark.asyncio
async def test_list_filtered_by_single_event_type(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC14: filter on one event_type."""
    repo, conn = repo_with_conn
    await _insert(conn, event_type="DECISION")
    await _insert(conn, event_type="DEVICE")
    await _insert(conn, event_type="SYSTEM")
    result = await repo.list_filtered(
        event_types=frozenset({"DECISION"}),
        device_id=None,
        since=None,
        until=None,
        keyword="",
        limit=50,
        offset=0,
    )
    assert [e.event_type for e in result] == ["DECISION"]


@pytest.mark.asyncio
async def test_list_filtered_by_multiple_event_types_with_in_clause(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC14: IN clause over multi-type filter."""
    repo, conn = repo_with_conn
    await _insert(conn, event_type="DECISION", timestamp=_NOW - timedelta(minutes=4))
    await _insert(conn, event_type="DEVICE", timestamp=_NOW - timedelta(minutes=3))
    await _insert(conn, event_type="SYSTEM", timestamp=_NOW - timedelta(minutes=2))
    await _insert(conn, event_type="CONSTRAINT", timestamp=_NOW - timedelta(minutes=1))
    result = await repo.list_filtered(
        event_types=frozenset({"DECISION", "DEVICE"}),
        device_id=None,
        since=None,
        until=None,
        keyword="",
        limit=50,
        offset=0,
    )
    assert {e.event_type for e in result} == {"DECISION", "DEVICE"}
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_filtered_by_device_id_excludes_null_device_rows(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC18: device_id filter does NOT match NULL rows (SQLite NULL semantics)."""
    repo, conn = repo_with_conn
    await _insert(conn, event_type="DECISION", device_id="batt-1")
    await _insert(conn, event_type="DECISION", device_id=None)
    await _insert(conn, event_type="DECISION", device_id="inv-1")
    result = await repo.list_filtered(
        event_types=frozenset(),
        device_id="batt-1",
        since=None,
        until=None,
        keyword="",
        limit=50,
        offset=0,
    )
    assert len(result) == 1
    assert result[0].device_id == "batt-1"


@pytest.mark.asyncio
async def test_list_filtered_by_since_only(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC14: open-ended since boundary."""
    repo, conn = repo_with_conn
    await _insert(conn, timestamp=_NOW - timedelta(days=5))  # old
    await _insert(conn, timestamp=_NOW - timedelta(days=1))  # recent
    result = await repo.list_filtered(
        event_types=frozenset(),
        device_id=None,
        since=_NOW - timedelta(days=2),
        until=None,
        keyword="",
        limit=50,
        offset=0,
    )
    assert len(result) == 1


@pytest.mark.asyncio
async def test_list_filtered_by_until_only(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC14: open-ended until boundary."""
    repo, conn = repo_with_conn
    await _insert(conn, timestamp=_NOW - timedelta(days=5))
    await _insert(conn, timestamp=_NOW - timedelta(days=1))
    result = await repo.list_filtered(
        event_types=frozenset(),
        device_id=None,
        since=None,
        until=_NOW - timedelta(days=2),
        keyword="",
        limit=50,
        offset=0,
    )
    assert len(result) == 1


@pytest.mark.asyncio
async def test_list_filtered_by_since_and_until_inclusive_exclusive_semantics(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC14: since is inclusive (>=), until is exclusive (<)."""
    repo, conn = repo_with_conn
    boundary = _NOW - timedelta(days=2)
    await _insert(conn, timestamp=boundary - timedelta(seconds=1))  # before since
    await _insert(conn, timestamp=boundary)  # exactly at since (included)
    await _insert(conn, timestamp=_NOW - timedelta(days=1))  # within
    await _insert(conn, timestamp=_NOW)  # exactly at until (excluded)
    result = await repo.list_filtered(
        event_types=frozenset(),
        device_id=None,
        since=boundary,
        until=_NOW,
        keyword="",
        limit=50,
        offset=0,
    )
    assert len(result) == 2  # boundary itself + within


@pytest.mark.asyncio
async def test_list_filtered_keyword_substring_match_on_summary_only(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC6: keyword matches via LIKE on summary; not actor / event_type / device_id."""
    repo, conn = repo_with_conn
    await _insert(conn, summary="peak limit reached")
    await _insert(conn, summary="battery dispatched")
    await _insert(conn, summary="EV charging started", actor="peak-system")
    result = await repo.list_filtered(
        event_types=frozenset(),
        device_id=None,
        since=None,
        until=None,
        keyword="peak",
        limit=50,
        offset=0,
    )
    assert len(result) == 1
    assert result[0].summary == "peak limit reached"


@pytest.mark.asyncio
async def test_list_filtered_keyword_escapes_sql_wildcards_percent_and_underscore(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC6: %, _ in user keyword are escaped — a literal % search matches only literal %."""
    repo, conn = repo_with_conn
    await _insert(conn, summary="100% efficient")
    await _insert(conn, summary="100_efficient")
    await _insert(conn, summary="100 percent efficient")
    result_percent = await repo.list_filtered(
        event_types=frozenset(),
        device_id=None,
        since=None,
        until=None,
        keyword="%",
        limit=50,
        offset=0,
    )
    assert [e.summary for e in result_percent] == ["100% efficient"]

    result_underscore = await repo.list_filtered(
        event_types=frozenset(),
        device_id=None,
        since=None,
        until=None,
        keyword="_",
        limit=50,
        offset=0,
    )
    assert [e.summary for e in result_underscore] == ["100_efficient"]


@pytest.mark.asyncio
async def test_list_filtered_keyword_escapes_backslash(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC6: backslash in user keyword is escaped first (ESCAPE '\\' literal preserved)."""
    repo, conn = repo_with_conn
    await _insert(conn, summary="path\\to\\thing")
    await _insert(conn, summary="other thing")
    result = await repo.list_filtered(
        event_types=frozenset(),
        device_id=None,
        since=None,
        until=None,
        keyword="path\\",
        limit=50,
        offset=0,
    )
    assert [e.summary for e in result] == ["path\\to\\thing"]


@pytest.mark.asyncio
async def test_list_filtered_combined_filters_apply_AND_semantics(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC14: type + device + date + keyword all AND-combined."""
    repo, conn = repo_with_conn
    await _insert(
        conn,
        event_type="DECISION",
        device_id="batt-1",
        summary="peak limit applied",
        timestamp=_NOW - timedelta(minutes=1),
    )
    await _insert(
        conn,
        event_type="DEVICE",
        device_id="batt-1",
        summary="peak limit applied",
        timestamp=_NOW - timedelta(minutes=2),
    )
    await _insert(
        conn,
        event_type="DECISION",
        device_id="inv-1",
        summary="peak limit applied",
        timestamp=_NOW - timedelta(minutes=3),
    )
    await _insert(
        conn,
        event_type="DECISION",
        device_id="batt-1",
        summary="strategy changed",
        timestamp=_NOW - timedelta(minutes=4),
    )
    result = await repo.list_filtered(
        event_types=frozenset({"DECISION"}),
        device_id="batt-1",
        since=_NOW - timedelta(minutes=10),
        until=_NOW,
        keyword="peak",
        limit=50,
        offset=0,
    )
    assert len(result) == 1
    assert result[0].event_type == "DECISION"
    assert result[0].device_id == "batt-1"
    assert "peak" in result[0].summary


@pytest.mark.asyncio
async def test_count_filtered_matches_list_filtered_length_under_same_filter(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC14: count and list share the SAME WHERE clause."""
    repo, conn = repo_with_conn
    for i in range(5):
        await _insert(
            conn,
            event_type="DECISION",
            summary=f"event {i}",
            timestamp=_NOW - timedelta(minutes=i),
        )
    for i in range(3):
        await _insert(conn, event_type="SYSTEM", timestamp=_NOW - timedelta(minutes=i))

    filt = {
        "event_types": frozenset({"DECISION"}),
        "device_id": None,
        "since": None,
        "until": None,
        "keyword": "",
    }
    listed = await repo.list_filtered(**filt, limit=50, offset=0)  # type: ignore[arg-type]
    counted = await repo.count_filtered(**filt)  # type: ignore[arg-type]
    assert len(listed) == counted == 5


@pytest.mark.asyncio
async def test_count_filtered_no_filter_returns_full_table_count(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC14: empty filter → full COUNT(*)."""
    repo, conn = repo_with_conn
    for _ in range(7):
        await _insert(conn)
    counted = await repo.count_filtered(
        event_types=frozenset(),
        device_id=None,
        since=None,
        until=None,
        keyword="",
    )
    assert counted == 7


@pytest.mark.asyncio
async def test_count_filtered_with_type_filter(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC14: count with type filter."""
    repo, conn = repo_with_conn
    for _ in range(4):
        await _insert(conn, event_type="DECISION")
    for _ in range(2):
        await _insert(conn, event_type="SYSTEM")
    assert (
        await repo.count_filtered(
            event_types=frozenset({"DECISION"}),
            device_id=None,
            since=None,
            until=None,
            keyword="",
        )
        == 4
    )


@pytest.mark.asyncio
async def test_count_filtered_with_keyword(
    repo_with_conn: tuple[EventLogRepo, aiosqlite.Connection],
) -> None:
    """AC14: count with keyword filter."""
    repo, conn = repo_with_conn
    await _insert(conn, summary="peak limit hit")
    await _insert(conn, summary="peak limit hit again")
    await _insert(conn, summary="battery dispatched")
    assert (
        await repo.count_filtered(
            event_types=frozenset(),
            device_id=None,
            since=None,
            until=None,
            keyword="peak",
        )
        == 2
    )


@pytest.mark.asyncio
async def test_list_filtered_rejects_negative_offset() -> None:
    """AC14: defensive guard — offset must be >= 0."""
    repo = EventLogRepo(conn=MagicMock())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="offset must be"):
        await repo.list_filtered(
            event_types=frozenset(),
            device_id=None,
            since=None,
            until=None,
            keyword="",
            limit=50,
            offset=-1,
        )


@pytest.mark.asyncio
async def test_list_filtered_rejects_zero_or_negative_limit() -> None:
    """AC14: defensive guard — limit must be >= 1."""
    repo = EventLogRepo(conn=MagicMock())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit must be"):
        await repo.list_filtered(
            event_types=frozenset(),
            device_id=None,
            since=None,
            until=None,
            keyword="",
            limit=0,
            offset=0,
        )
