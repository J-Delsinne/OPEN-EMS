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
