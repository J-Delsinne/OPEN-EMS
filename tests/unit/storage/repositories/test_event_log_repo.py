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
