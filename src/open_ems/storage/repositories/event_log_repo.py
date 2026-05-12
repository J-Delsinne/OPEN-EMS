from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiosqlite

from open_ems.storage.database import get_connection, get_write_lock

_CRITICAL_EVENT_TYPES: frozenset[str] = frozenset({"CONSTRAINT"})


class EventLogRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    async def append(
        self,
        *,
        schema_version: int,
        timestamp: datetime,
        actor: str,
        event_type: str,
        summary: str,
        detail: dict[str, object] | None,
        device_id: str | None,
        config_version: str | None,
    ) -> int:
        """Append an event log entry. Returns the new row id."""
        detail_json = json.dumps(detail, allow_nan=False) if detail is not None else None
        async with get_write_lock():
            async with self._conn.execute(
                "INSERT INTO event_log"
                " (schema_version, timestamp, actor, event_type,"
                "  summary, detail, device_id, config_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    schema_version,
                    timestamp.astimezone(UTC).isoformat(),
                    actor,
                    event_type,
                    summary,
                    detail_json,
                    device_id,
                    config_version,
                ),
            ) as cursor:
                row_id = cursor.lastrowid
            await self._conn.commit()
        if row_id is None:
            raise RuntimeError("SQLite did not return a row id for event_log insert.")
        return row_id

    def update(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("event_log is append-only")

    def delete(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("event_log is append-only")

    async def count_peak_limiting_applied_decisions(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> int:
        """Story 10.4 AC14: count DECISION rows in [since, until] with
        ``source_rule="peak_limiting"`` AND ``applied=true`` in the JSON detail.

        Uses double LIKE on the JSON-serialized detail blob. Acceptable here
        because event_log volume is bounded by retention (90 days default) and
        this method is invoked at most once per ``weekly_summary_aggregation_interval_seconds``
        (default 1h), not on every page render. The single weekly_energy_summary
        row caches the result for the homeowner endpoint."""
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("since must be timezone-aware (UTC)")
        if until.tzinfo is None or until.utcoffset() is None:
            raise ValueError("until must be timezone-aware (UTC)")
        async with self._conn.execute(
            "SELECT COUNT(*) FROM event_log"
            " WHERE event_type = 'DECISION'"
            " AND timestamp >= ?"
            " AND timestamp < ?"
            ' AND detail LIKE \'%"source_rule": "peak_limiting"%\''
            " AND detail LIKE '%\"applied\": true%'",
            (
                since.astimezone(UTC).isoformat(),
                until.astimezone(UTC).isoformat(),
            ),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    async def prune_expired(self, *, retention_days: int = 90) -> int:
        """Delete non-critical entries older than retention_days. Returns count deleted."""
        if retention_days < 1:
            raise ValueError(f"retention_days must be >= 1, got {retention_days}")
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        placeholders = ",".join("?" * len(_CRITICAL_EVENT_TYPES))
        async with get_write_lock():
            async with self._conn.execute(
                f"DELETE FROM event_log WHERE timestamp < ? AND event_type NOT IN ({placeholders})",
                (cutoff, *_CRITICAL_EVENT_TYPES),
            ) as cursor:
                deleted = cursor.rowcount
            await self._conn.commit()
        return deleted
