"""``DraftConstraintsRepo`` — per-session staged constraint drafts (Story 9.3).

The ``draft_constraints`` table holds at most one row per installer session
(UNIQUE on ``session_id`` + FK CASCADE on ``sessions``). Each row carries the
raw form values, the most recent server-side ``ConstraintValidationReport``
(JSON-encoded), and a ``validation_status`` reflecting that report's overall
outcome. The repo is the only legal write surface for the table; route
handlers and the ``ConstraintsService`` go through it.

Mirrors ``WizardStateRepo`` shape: ``frozen=True, extra="forbid"`` Pydantic
view, async per-session methods, write-lock-protected mutations, ``HH:MM``
paired-NULL invariants enforced at both the repo and core/ model layers.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from open_ems.core.constraints import (
    ConstraintDraftValidationStatus,
    ConstraintValidationReport,
)
from open_ems.storage.database import get_connection, get_write_lock

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def _decode_report(raw: str | None) -> ConstraintValidationReport | None:
    if raw is None:
        return None
    payload = json.loads(raw)
    return ConstraintValidationReport.model_validate(payload)


def _encode_report(report: ConstraintValidationReport) -> str:
    # R2P1 — surface JSON encoding errors as typed ``ValueError`` rather than
    # letting raw ``TypeError`` propagate. Defensive: ``ConstraintValidationReport``
    # is a frozen Pydantic model with no exotic types so this should not fire,
    # but any future field addition (e.g. arbitrary metadata) could regress.
    try:
        return json.dumps(report.model_dump(mode="json"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ConstraintValidationReport JSON encoding failed: {exc}") from exc


class ConstraintDraft(BaseModel):
    """Immutable view of one ``draft_constraints`` row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: NonEmptyStr
    peak_limit_kw: Annotated[float, Field(gt=0.0)]
    battery_reserve_floor_percent: Annotated[float, Field(ge=0.0, le=100.0)]
    ev_charging_window_start: str | None = None
    ev_charging_window_end: str | None = None
    validation_status: ConstraintDraftValidationStatus
    validation_report: ConstraintValidationReport | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timestamps_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "timestamp")


class DraftConstraintsRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    async def get(self, session_id: str) -> ConstraintDraft | None:
        async with self._conn.execute(
            "SELECT session_id, peak_limit_kw, battery_reserve_floor_percent,"
            " ev_charging_window_start, ev_charging_window_end,"
            " validation_status, validation_report, created_at, updated_at"
            " FROM draft_constraints WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_draft(row)

    async def upsert(
        self,
        session_id: str,
        *,
        peak_limit_kw: float,
        battery_reserve_floor_percent: float,
        ev_charging_window_start: str | None,
        ev_charging_window_end: str | None,
        now: datetime,
    ) -> ConstraintDraft:
        """INSERT or REPLACE this session's draft.

        Always resets ``validation_status='pending'`` and clears
        ``validation_report`` (any field edit invalidates a prior validation).
        Preserves ``created_at`` if a prior row exists by reading it back into
        the new row; ``updated_at`` is set to ``now``.
        """
        _require_utc(now, "now")
        async with get_write_lock():
            await self._conn.commit()
            existing_created_at: str | None = None
            async with self._conn.execute(
                "SELECT created_at FROM draft_constraints WHERE session_id = ?",
                (session_id,),
            ) as cursor:
                existing_row = await cursor.fetchone()
            if existing_row is not None:
                existing_created_at = str(existing_row[0])
            created_at_iso = existing_created_at or now.isoformat()
            await self._conn.execute(
                "INSERT INTO draft_constraints"
                " (session_id, peak_limit_kw, battery_reserve_floor_percent,"
                "  ev_charging_window_start, ev_charging_window_end,"
                "  validation_status, validation_report, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                "  peak_limit_kw = excluded.peak_limit_kw,"
                "  battery_reserve_floor_percent = excluded.battery_reserve_floor_percent,"
                "  ev_charging_window_start = excluded.ev_charging_window_start,"
                "  ev_charging_window_end = excluded.ev_charging_window_end,"
                "  validation_status = 'pending',"
                "  validation_report = NULL,"
                "  updated_at = excluded.updated_at",
                (
                    session_id,
                    peak_limit_kw,
                    battery_reserve_floor_percent,
                    ev_charging_window_start,
                    ev_charging_window_end,
                    created_at_iso,
                    now.isoformat(),
                ),
            )
            await self._conn.commit()
        result = await self.get(session_id)
        if result is None:
            raise RuntimeError(
                f"DraftConstraintsRepo.upsert: row missing after upsert (session_id={session_id!r})"
            )
        return result

    async def record_validation_outcome(
        self,
        session_id: str,
        *,
        status: ConstraintDraftValidationStatus,
        report: ConstraintValidationReport,
        now: datetime,
    ) -> None:
        """Persist the most recent validation outcome onto the draft row.

        Raises ``ValueError`` if no row exists for ``session_id``.
        """
        if status not in ("valid", "failed"):
            raise ValueError(
                f"record_validation_outcome status must be 'valid' or 'failed' (got {status!r})"
            )
        _require_utc(now, "now")
        async with get_write_lock():
            await self.record_validation_outcome_locked(
                session_id, status=status, report=report, now=now
            )

    async def record_validation_outcome_locked(
        self,
        session_id: str,
        *,
        status: ConstraintDraftValidationStatus,
        report: ConstraintValidationReport,
        now: datetime,
        commit: bool = True,
    ) -> None:
        """Lock-free variant — caller MUST already hold ``get_write_lock()``.

        Same error contract as ``record_validation_outcome``. Used by the
        activate route to update the draft's persisted validation state inside
        the same write-lock window that owns the activate transaction.

        R2P1 — ``commit`` matches the convention of sibling ``_locked`` helpers
        (``delete_locked``, ``set_step_3_complete_locked``). With ``commit=False``
        the caller owns an outer ``BEGIN IMMEDIATE`` transaction; this method
        joins it (no leading flush, no trailing commit) so the persisted FAIL
        outcome rolls back atomically if a sibling write fails.
        """
        if status not in ("valid", "failed"):
            raise ValueError(
                f"record_validation_outcome status must be 'valid' or 'failed' (got {status!r})"
            )
        _require_utc(now, "now")
        encoded = _encode_report(report)
        if commit:
            await self._conn.commit()
        async with self._conn.execute(
            "UPDATE draft_constraints SET"
            " validation_status = ?,"
            " validation_report = ?,"
            " updated_at = ?"
            " WHERE session_id = ?",
            (status, encoded, now.isoformat(), session_id),
        ) as cursor:
            if cursor.rowcount == 0:
                # Symmetric to ``set_step_3_complete_locked`` — when commit=False
                # we are inside the caller's transaction so we propagate and let
                # the caller roll back; when commit=True we flush so the failed
                # UPDATE is not left in an implicit transaction.
                if commit:
                    await self._conn.commit()
                raise ValueError(f"No draft_constraints row for session_id={session_id!r}")
        if commit:
            await self._conn.commit()

    async def delete(self, session_id: str) -> None:
        """Remove the draft row. No-op if no row exists."""
        async with get_write_lock():
            await self.delete_locked(session_id)

    async def delete_locked(self, session_id: str, *, commit: bool = True) -> None:
        """Lock-free variant — caller MUST already hold ``get_write_lock()``.

        When ``commit=False`` (Story 9.3 P13), the caller is managing an outer
        ``BEGIN IMMEDIATE`` transaction (e.g. the staged-activation transaction
        owned by ``ConfigRepo._activate_locked``) — no leading flush, no
        trailing commit — so this DELETE joins that transaction and rolls back
        atomically if any sibling write fails.
        """
        if commit:
            await self._conn.commit()
        await self._conn.execute(
            "DELETE FROM draft_constraints WHERE session_id = ?",
            (session_id,),
        )
        if commit:
            await self._conn.commit()


def _row_to_draft(row: aiosqlite.Row | tuple[object, ...]) -> ConstraintDraft:
    return ConstraintDraft(
        session_id=str(row[0]),
        peak_limit_kw=float(str(row[1])),
        battery_reserve_floor_percent=float(str(row[2])),
        ev_charging_window_start=str(row[3]) if row[3] is not None else None,
        ev_charging_window_end=str(row[4]) if row[4] is not None else None,
        validation_status=str(row[5]),  # type: ignore[arg-type]
        validation_report=_decode_report(str(row[6]) if row[6] is not None else None),
        created_at=datetime.fromisoformat(str(row[7])),
        updated_at=datetime.fromisoformat(str(row[8])),
    )
