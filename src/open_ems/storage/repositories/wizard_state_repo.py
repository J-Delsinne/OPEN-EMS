"""``WizardStateRepo`` — per-session installer wizard step tracking.

Story 9.1 introduces ``step_1_*`` columns; Story 9.2 extends with
``step_2_*`` (including a JSON-encoded ``step_2_acknowledged_gaps``).
Subsequent stories will extend with ``step_3_*`` / ``step_4_*`` columns in
their own migrations. Rows are lazily created on first GET of
``/installer/setup/discovery`` and cleaned up via the FK CASCADE when the
parent ``sessions`` row is deleted.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated

import aiosqlite
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator, model_validator

from open_ems.core.devices import _VALID_GAP_LABELS
from open_ems.storage.database import get_connection, get_write_lock

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Story 9.2 AC3: ``_VALID_GAP_LABELS`` is imported from ``core.devices`` so the
# repo layer, the service layer, the Pydantic model validator, and the route
# handler all read from a single authoritative definition (the only source of
# truth lives next to ``DeviceRole``).


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def _parse_optional_utc(raw: str | None, field_name: str) -> datetime | None:
    if raw is None:
        return None
    parsed = datetime.fromisoformat(raw)
    return _require_utc(parsed, field_name)


def _decode_gap_labels(raw: str | None) -> frozenset[str]:
    if raw is None:
        return frozenset()
    decoded = json.loads(raw)
    if not isinstance(decoded, list):
        raise ValueError("step_2_acknowledged_gaps must decode to a list")
    if not all(isinstance(item, str) for item in decoded):
        raise ValueError("step_2_acknowledged_gaps must contain only strings")
    return frozenset(decoded)


def _encode_gap_labels(labels: frozenset[str]) -> str:
    return json.dumps(sorted(labels))


class WizardState(BaseModel):
    """Immutable view of one ``wizard_state`` row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: NonEmptyStr
    step_1_complete: bool
    step_1_completed_at: datetime | None = None
    last_scan_id: str | None = None
    step_2_complete: bool = False
    step_2_completed_at: datetime | None = None
    step_2_acknowledged_gaps: frozenset[str] = frozenset()
    step_3_complete: bool = False
    step_3_completed_at: datetime | None = None
    step_3_activated_config_version: int | None = None
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

    @field_validator("step_2_completed_at")
    @classmethod
    def _step_2_completed_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value, "step_2_completed_at")

    @field_validator("step_3_completed_at")
    @classmethod
    def _step_3_completed_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value, "step_3_completed_at")

    @model_validator(mode="after")
    def _acknowledged_gaps_whitelisted(self) -> WizardState:
        # Defense-in-depth: a tampered DB row carrying an unknown label fails
        # loud at every read, not only on write.
        unknown = self.step_2_acknowledged_gaps - _VALID_GAP_LABELS
        if unknown:
            raise ValueError(
                f"step_2_acknowledged_gaps contains unknown labels: {sorted(unknown)!r}"
            )
        return self

    @model_validator(mode="after")
    def _step_3_paired(self) -> WizardState:
        # Story 9.3 R3 #5: ``step_3_complete=1`` IFF ``step_3_completed_at`` IS
        # NOT NULL IFF ``step_3_activated_config_version`` IS NOT NULL. The DB
        # CHECK constraint enforces this on writes; this read-side guard
        # rejects a tampered row.
        complete = self.step_3_complete
        ts_set = self.step_3_completed_at is not None
        version_set = self.step_3_activated_config_version is not None
        if complete != ts_set or complete != version_set:
            raise ValueError(
                "step_3_complete, step_3_completed_at and step_3_activated_config_version"
                " must all be set together"
            )
        return self


class WizardStateRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    async def get(self, session_id: str) -> WizardState | None:
        async with self._conn.execute(
            "SELECT session_id, step_1_complete, step_1_completed_at, last_scan_id,"
            " step_2_complete, step_2_completed_at, step_2_acknowledged_gaps,"
            " step_3_complete, step_3_completed_at, step_3_activated_config_version,"
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
                    " (session_id, step_1_complete, step_2_complete, created_at, updated_at)"
                    " VALUES (?, 0, 0, ?, ?)",
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

    async def record_acknowledged_gaps(
        self,
        session_id: str,
        *,
        gaps: frozenset[str],
        now: datetime,
    ) -> None:
        """Replace ``step_2_acknowledged_gaps`` with ``gaps`` atomically.

        Raises ``ValueError`` with the exact-match message
        ``"acknowledged_gap_label_invalid: <label>"`` for any out-of-whitelist
        label (defense-in-depth alongside the route handler and Pydantic model).
        Raises ``ValueError`` if no row exists for ``session_id``.
        """
        _require_utc(now, "now")
        for label in sorted(gaps):
            if label not in _VALID_GAP_LABELS:
                raise ValueError(f"acknowledged_gap_label_invalid: {label}")
        encoded = _encode_gap_labels(gaps)
        async with get_write_lock():
            await self._conn.commit()
            async with self._conn.execute(
                "UPDATE wizard_state SET"
                " step_2_acknowledged_gaps = ?,"
                " updated_at = ?"
                " WHERE session_id = ?",
                (encoded, now.isoformat(), session_id),
            ) as cursor:
                if cursor.rowcount == 0:
                    await self._conn.commit()
                    raise ValueError(f"No wizard_state row for session_id={session_id!r}")
            await self._conn.commit()

    async def set_step_2_complete(
        self,
        session_id: str,
        *,
        acknowledged_gaps: frozenset[str],
        now: datetime,
    ) -> None:
        """Mark step 2 complete idempotently.

        Writes both ``step_2_complete=1`` and the filtered acknowledged-gap set
        atomically in the same UPDATE. Idempotent: if ``step_2_complete`` is
        already ``1``, ``step_2_completed_at`` is preserved (no shift on retry)
        but ``step_2_acknowledged_gaps`` is still overwritten with the supplied
        set (the stale-acknowledgment filter must keep persisted state honest
        even on a re-click). Raises ``ValueError`` with the exact-match
        per-label message for any out-of-whitelist label. Raises
        ``ValueError`` if no row exists for ``session_id``.
        """
        async with get_write_lock():
            await self.set_step_2_complete_locked(
                session_id, acknowledged_gaps=acknowledged_gaps, now=now
            )

    async def set_step_3_complete(
        self,
        session_id: str,
        *,
        activated_config_version: int,
        now: datetime,
    ) -> None:
        """Mark step 3 complete idempotently (Story 9.3).

        Writes ``step_3_complete=1`` + ``step_3_completed_at`` +
        ``step_3_activated_config_version`` atomically. Idempotent: if
        ``step_3_complete`` is already ``1``, ``step_3_completed_at`` AND
        ``step_3_activated_config_version`` are preserved (no shift on retry).
        Mirrors ``set_step_2_complete``'s 9.2 idempotent pattern. Raises
        ``ValueError`` if no row exists for ``session_id``.
        """
        async with get_write_lock():
            await self.set_step_3_complete_locked(
                session_id,
                activated_config_version=activated_config_version,
                now=now,
            )

    async def set_step_3_complete_locked(
        self,
        session_id: str,
        *,
        activated_config_version: int,
        now: datetime,
        commit: bool = True,
    ) -> None:
        """Lock-free variant — caller MUST already hold ``get_write_lock()``.

        Used by the staged-activation route (Story 9.3) to flip the wizard
        state inside the same lock window that owns ``ConfigRepo._activate_locked``.

        When ``commit=False`` (Story 9.3 P13), the caller is managing an outer
        ``BEGIN IMMEDIATE`` transaction — no leading flush, no trailing commit —
        so this UPDATE joins that transaction and rolls back atomically with
        ``active_constraints`` + ``config_audit_log`` + ``draft_constraints``
        if any subsequent step fails.
        """
        if activated_config_version <= 0:
            raise ValueError(
                f"activated_config_version must be > 0 (got {activated_config_version})"
            )
        _require_utc(now, "now")
        if commit:
            await self._conn.commit()
        async with self._conn.execute(
            "UPDATE wizard_state SET"
            " step_3_complete = 1,"
            " step_3_completed_at = CASE"
            "   WHEN step_3_complete = 1 THEN step_3_completed_at"
            "   ELSE ?"
            " END,"
            " step_3_activated_config_version = CASE"
            "   WHEN step_3_complete = 1 THEN step_3_activated_config_version"
            "   ELSE ?"
            " END,"
            " updated_at = ?"
            " WHERE session_id = ?",
            (
                now.isoformat(),
                activated_config_version,
                now.isoformat(),
                session_id,
            ),
        ) as cursor:
            if cursor.rowcount == 0:
                # When commit=False we are inside the caller's transaction;
                # raising propagates and the caller rolls back. When
                # commit=True we flush so the failed UPDATE is not left in an
                # implicit transaction the caller does not own.
                if commit:
                    await self._conn.commit()
                raise ValueError(f"No wizard_state row for session_id={session_id!r}")
        if commit:
            await self._conn.commit()

    async def set_step_2_complete_locked(
        self,
        session_id: str,
        *,
        acknowledged_gaps: frozenset[str],
        now: datetime,
    ) -> None:
        """Lock-free variant of ``set_step_2_complete``.

        Caller MUST already hold ``get_write_lock()``. Used by the advance
        route to close the TOCTOU window between gate evaluation and
        persistence: hold the lock, re-evaluate the gate (no other writer can
        intervene because ``DeviceRepo.assign_role`` and the acknowledgment
        write take the same lock), then call this method to perform the
        UPDATE without re-acquiring the lock (asyncio.Lock is not reentrant).
        Same error contract as ``set_step_2_complete``.
        """
        _require_utc(now, "now")
        for label in sorted(acknowledged_gaps):
            if label not in _VALID_GAP_LABELS:
                raise ValueError(f"acknowledged_gap_label_invalid: {label}")
        encoded = _encode_gap_labels(acknowledged_gaps)
        await self._conn.commit()
        async with self._conn.execute(
            "UPDATE wizard_state SET"
            " step_2_complete = 1,"
            " step_2_completed_at = CASE"
            "   WHEN step_2_complete = 1 THEN step_2_completed_at"
            "   ELSE ?"
            " END,"
            " step_2_acknowledged_gaps = ?,"
            " updated_at = ?"
            " WHERE session_id = ?",
            (now.isoformat(), encoded, now.isoformat(), session_id),
        ) as cursor:
            if cursor.rowcount == 0:
                await self._conn.commit()
                raise ValueError(f"No wizard_state row for session_id={session_id!r}")
        await self._conn.commit()


def _row_to_state(row: aiosqlite.Row | tuple[object, ...]) -> WizardState:
    step_2_acknowledged_gaps_raw = str(row[6]) if row[6] is not None else None
    step_3_activated_config_version = (
        int(row[9]) if row[9] is not None else None  # type: ignore[arg-type]
    )
    return WizardState(
        session_id=str(row[0]),
        step_1_complete=bool(row[1]),
        step_1_completed_at=_parse_optional_utc(
            str(row[2]) if row[2] is not None else None,
            "step_1_completed_at",
        ),
        last_scan_id=str(row[3]) if row[3] is not None else None,
        step_2_complete=bool(row[4]),
        step_2_completed_at=_parse_optional_utc(
            str(row[5]) if row[5] is not None else None,
            "step_2_completed_at",
        ),
        step_2_acknowledged_gaps=_decode_gap_labels(step_2_acknowledged_gaps_raw),
        step_3_complete=bool(row[7]),
        step_3_completed_at=_parse_optional_utc(
            str(row[8]) if row[8] is not None else None,
            "step_3_completed_at",
        ),
        step_3_activated_config_version=step_3_activated_config_version,
        created_at=datetime.fromisoformat(str(row[10])),
        updated_at=datetime.fromisoformat(str(row[11])),
    )
