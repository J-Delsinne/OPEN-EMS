"""``WizardStateRepo`` — per-session installer wizard step tracking.

Story 9.1 introduces ``step_1_*`` columns; subsequent stories will extend with
``step_2_*`` / ``step_3_*`` / ``step_4_*`` columns in their own migrations.
Rows are lazily created on first GET of ``/installer/setup/discovery`` and
cleaned up via the FK CASCADE when the parent ``sessions`` row is deleted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import aiosqlite
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from open_ems.storage.database import get_connection, get_write_lock

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def _parse_optional_utc(raw: str | None, field_name: str) -> datetime | None:
    if raw is None:
        return None
    parsed = datetime.fromisoformat(raw)
    return _require_utc(parsed, field_name)


class WizardState(BaseModel):
    """Immutable view of one ``wizard_state`` row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: NonEmptyStr
    step_1_complete: bool
    step_1_completed_at: datetime | None = None
    last_scan_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timestamps_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "timestamp")

    @field_validator("step_1_completed_at")
    @classmethod
    def _step_completed_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value, "step_1_completed_at")


class WizardStateRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    async def get(self, session_id: str) -> WizardState | None:
        async with self._conn.execute(
            "SELECT session_id, step_1_complete, step_1_completed_at, last_scan_id,"
            " created_at, updated_at"
            " FROM wizard_state WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_state(row)

    async def get_or_create(
        self,
        session_id: str,
        *,
        now: datetime,
    ) -> WizardState:
        """Return the existing row or lazily create one with ``step_1_complete=0``."""
        _require_utc(now, "now")
        existing = await self.get(session_id)
        if existing is not None:
            return existing
        async with get_write_lock():
            await self._conn.commit()
            try:
                await self._conn.execute(
                    "INSERT INTO wizard_state"
                    " (session_id, step_1_complete, created_at, updated_at)"
                    " VALUES (?, 0, ?, ?)",
                    (session_id, now.isoformat(), now.isoformat()),
                )
                await self._conn.commit()
            except aiosqlite.IntegrityError:
                # Race: a concurrent request created the row between our get() and INSERT.
                # The UNIQUE constraint blocks the duplicate; the other writer wins.
                await self._conn.commit()
        result = await self.get(session_id)
        if result is None:
            raise RuntimeError(
                "WizardStateRepo.get_or_create: row missing after insert"
                f" (session_id={session_id!r})"
            )
        return result

    async def set_step_1_complete(
        self,
        session_id: str,
        *,
        now: datetime,
    ) -> None:
        """Mark step 1 complete; raises ``ValueError`` if no row exists."""
        _require_utc(now, "now")
        async with get_write_lock():
            await self._conn.commit()
            async with self._conn.execute(
                "UPDATE wizard_state SET"
                " step_1_complete = 1,"
                " step_1_completed_at = ?,"
                " updated_at = ?"
                " WHERE session_id = ?",
                (now.isoformat(), now.isoformat(), session_id),
            ) as cursor:
                if cursor.rowcount == 0:
                    await self._conn.commit()
                    raise ValueError(f"No wizard_state row for session_id={session_id!r}")
            await self._conn.commit()

    async def set_last_scan_id(
        self,
        session_id: str,
        *,
        scan_id: str,
        now: datetime,
    ) -> None:
        """Record the most recent scan UUID; raises ``ValueError`` if no row exists."""
        _require_utc(now, "now")
        async with get_write_lock():
            await self._conn.commit()
            async with self._conn.execute(
                "UPDATE wizard_state SET last_scan_id = ?, updated_at = ? WHERE session_id = ?",
                (scan_id, now.isoformat(), session_id),
            ) as cursor:
                if cursor.rowcount == 0:
                    await self._conn.commit()
                    raise ValueError(f"No wizard_state row for session_id={session_id!r}")
            await self._conn.commit()


def _row_to_state(row: aiosqlite.Row | tuple[object, ...]) -> WizardState:
    return WizardState(
        session_id=str(row[0]),
        step_1_complete=bool(row[1]),
        step_1_completed_at=_parse_optional_utc(
            str(row[2]) if row[2] is not None else None,
            "step_1_completed_at",
        ),
        last_scan_id=str(row[3]) if row[3] is not None else None,
        created_at=datetime.fromisoformat(str(row[4])),
        updated_at=datetime.fromisoformat(str(row[5])),
    )
