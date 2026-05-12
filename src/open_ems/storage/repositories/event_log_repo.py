from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from open_ems.storage.database import get_connection, get_write_lock

_CRITICAL_EVENT_TYPES: frozenset[str] = frozenset({"CONSTRAINT"})


def _escape_like_keyword(keyword: str) -> str:
    """Story 11.2 AC6: escape SQL LIKE wildcards in the user-supplied keyword.

    Order matters: escape the escape character (``\\``) FIRST so subsequent
    ``%``/``_`` escapes don't double-process the literal backslashes we just
    inserted. Pair with ``ESCAPE '\\'`` in the LIKE clause.
    """
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_event_log_filter_where(
    *,
    event_types: frozenset[str],
    device_id: str | None,
    since: datetime | None,
    until: datetime | None,
    keyword: str,
) -> tuple[str, list[object]]:
    """Story 11.2 AC14: single source of truth for filter WHERE-clause assembly.

    Returns ``("", [])`` when no filter is active. Otherwise returns
    ``(" WHERE <conditions>", [<param>, ...])`` joined with ``AND``. Both
    ``list_filtered`` and ``count_filtered`` call this — any change to one
    surface is automatically reflected in the other.
    """
    conditions: list[str] = []
    params: list[object] = []

    if event_types:
        # Deterministic ordering (sorted) keeps the SQL string stable in tests/logs.
        types_sorted = sorted(event_types)
        placeholders = ",".join("?" for _ in types_sorted)
        conditions.append(f"event_type IN ({placeholders})")
        params.extend(types_sorted)

    if device_id is not None:
        conditions.append("device_id = ?")
        params.append(device_id)

    if since is not None:
        conditions.append("timestamp >= ?")
        params.append(since.astimezone(UTC).isoformat())

    if until is not None:
        conditions.append("timestamp < ?")
        params.append(until.astimezone(UTC).isoformat())

    if keyword:
        conditions.append("summary LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like_keyword(keyword)}%")

    if not conditions:
        return "", []
    return " WHERE " + " AND ".join(conditions), params


class EventLogEntry(BaseModel):
    """Story 11.1 AC6: immutable view of one ``event_log`` row, used by the
    read path (installer dashboard preview + Story 11.2 paginated list).

    Fields mirror the ``event_log`` table columns. ``detail_json`` is the raw
    JSON string from the DB column (None if the column is NULL); callers parse
    on demand to keep the constructor cheap for high-cardinality reads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    timestamp: datetime
    actor: str
    event_type: str
    summary: str
    detail_json: str | None = None
    device_id: str | None = None
    config_version: str | None = None


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

    async def list_recent(self, *, limit: int = 5) -> list[EventLogEntry]:
        """Story 11.1 AC6: return the N most recent event_log rows.

        Ordered by ``timestamp DESC, id DESC`` — the secondary ``id`` sort is
        load-bearing for ties (two events emitted in the same millisecond under
        fast adapter polling). Without it row order would be undefined and the
        ordering test would be flaky.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        async with self._conn.execute(
            "SELECT id, schema_version, timestamp, actor, event_type,"
            "       summary, detail, device_id, config_version"
            " FROM event_log"
            " ORDER BY timestamp DESC, id DESC"
            " LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            EventLogEntry(
                id=int(row[0]),
                schema_version=int(row[1]),
                timestamp=datetime.fromisoformat(str(row[2])),
                actor=str(row[3]),
                event_type=str(row[4]),
                summary=str(row[5]),
                detail_json=str(row[6]) if row[6] is not None else None,
                device_id=str(row[7]) if row[7] is not None else None,
                config_version=str(row[8]) if row[8] is not None else None,
            )
            for row in rows
        ]

    async def list_filtered(
        self,
        *,
        event_types: frozenset[str],
        device_id: str | None,
        since: datetime | None,
        until: datetime | None,
        keyword: str,
        limit: int,
        offset: int,
    ) -> list[EventLogEntry]:
        """Story 11.2 AC14: paginated + filtered event_log read.

        Ordered by ``timestamp DESC, id DESC`` (id is the load-bearing tie-breaker
        for same-millisecond ties under fast adapter polling). The WHERE clause
        is assembled via ``_build_event_log_filter_where`` — the SAME helper used
        by ``count_filtered`` so the two methods cannot drift.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got {offset}")
        where_clause, where_params = _build_event_log_filter_where(
            event_types=event_types,
            device_id=device_id,
            since=since,
            until=until,
            keyword=keyword,
        )
        sql = (
            "SELECT id, schema_version, timestamp, actor, event_type,"
            "       summary, detail, device_id, config_version"
            " FROM event_log"
            f"{where_clause}"
            " ORDER BY timestamp DESC, id DESC"
            " LIMIT ? OFFSET ?"
        )
        params: list[object] = [*where_params, limit, offset]
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [
            EventLogEntry(
                id=int(row[0]),
                schema_version=int(row[1]),
                timestamp=datetime.fromisoformat(str(row[2])),
                actor=str(row[3]),
                event_type=str(row[4]),
                summary=str(row[5]),
                detail_json=str(row[6]) if row[6] is not None else None,
                device_id=str(row[7]) if row[7] is not None else None,
                config_version=str(row[8]) if row[8] is not None else None,
            )
            for row in rows
        ]

    async def count_filtered(
        self,
        *,
        event_types: frozenset[str],
        device_id: str | None,
        since: datetime | None,
        until: datetime | None,
        keyword: str,
    ) -> int:
        """Story 11.2 AC14: COUNT(*) over the same WHERE clause as ``list_filtered``."""
        where_clause, where_params = _build_event_log_filter_where(
            event_types=event_types,
            device_id=device_id,
            since=since,
            until=until,
            keyword=keyword,
        )
        sql = f"SELECT COUNT(*) FROM event_log{where_clause}"
        async with self._conn.execute(sql, where_params) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

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
