"""``ConfigRepo`` — persistent owner of the active site safety constraints.

Story 9.0b: this repo is the only legal write path into the
``active_constraints`` table. ``activate()`` runs a single atomic transaction
that inserts the new active row AND emits one ``config_audit_log`` row per
changed field (delegated to ``ConfigAuditRepo.append_activation``), so the
two tables can never disagree on ``config_version``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import aiosqlite
from pydantic import ValidationError

from open_ems.core.constraints import ActiveConstraints, ActiveConstraintsInput
from open_ems.storage.database import get_connection, get_write_lock
from open_ems.storage.repositories.config_audit_repo import (
    ConfigAuditChange,
    ConfigAuditRepo,
)

VALID_ACTIVATION_ACTORS: frozenset[str] = frozenset({"system", "installer"})

# Audit-vocabulary field names already established by Story 6.3
# (see SAFETY_RELEVANT_CONFIG_FIELDS in config_audit_repo).
_AUDIT_FIELD_PEAK_LIMIT: str = "peak_consumption_limit"
_AUDIT_FIELD_RESERVE_FLOOR: str = "battery_reserve_floor"


class ConfigRepo:
    def __init__(
        self,
        conn: aiosqlite.Connection | None = None,
        *,
        audit_repo: ConfigAuditRepo | None = None,
    ) -> None:
        self._conn = conn if conn is not None else get_connection()
        self._audit_repo = audit_repo if audit_repo is not None else ConfigAuditRepo(self._conn)

    async def get_active(self) -> ActiveConstraints | None:
        """Return the currently-active row (highest ``config_version``) or ``None``.

        Raises ``ValueError`` with the offending ``id`` / ``config_version`` if
        the highest row cannot be parsed (e.g. a manually-edited row with a
        naive ``activated_at`` timestamp). Fail-loud is intentional — a process
        that cannot determine its safety constraints must not start — but the
        operator needs the row identifier to repair the bad data.
        """
        async with self._conn.execute(
            "SELECT id, peak_limit_kw, battery_reserve_floor_percent, config_version,"
            " activated_at FROM active_constraints"
            " ORDER BY config_version DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return ActiveConstraints(
                peak_limit_kw=float(row[1]),
                battery_reserve_floor_percent=float(row[2]),
                config_version=int(row[3]),
                activated_at=datetime.fromisoformat(row[4]),
            )
        except (ValidationError, ValueError) as exc:
            raise ValueError(
                "active_constraints row failed validation:"
                f" id={row[0]} config_version={row[3]} activated_at={row[4]!r}: {exc}"
            ) from exc

    async def activate(
        self,
        input: ActiveConstraintsInput,
        *,
        actor: Literal["system", "installer"],
    ) -> int:
        """Atomically activate a new constraint set; return the assigned ``config_version``.

        Audit emission is delegated to ``ConfigAuditRepo.append_activation``
        with ``commit=False`` so both inserts share the outer
        ``BEGIN IMMEDIATE`` transaction.
        """
        if actor not in VALID_ACTIVATION_ACTORS:
            raise ValueError(f"Invalid actor {actor!r}")

        # Serialize all writers on the shared aiosqlite connection. Without this
        # lock another coroutine's DML can open an implicit transaction between
        # our commit() flush and BEGIN IMMEDIATE, causing
        # "cannot start a transaction within a transaction". The audit repo
        # call below runs with ``commit=False`` so it inherits this lock — do
        # NOT re-acquire it from inside ConfigAuditRepo.
        async with get_write_lock():
            # Flush any implicit transaction the connection may have opened so
            # BEGIN IMMEDIATE below acquires the write lock cleanly.
            await self._conn.commit()
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Read `previous` INSIDE the transaction so the audit-log payload
                # (previous_value / new_value) reflects the row that is genuinely
                # the predecessor of the new one. A concurrent activation cannot
                # interleave between this read and the INSERT below.
                previous = await self.get_active()
                changes = _build_audit_changes(previous, input)
                if not changes:
                    raise ValueError("activate called with no changed fields")

                # Cross-table monotonic next_version: take the max across BOTH
                # active_constraints AND config_audit_log, so a manual DELETE
                # from active_constraints cannot make the next activation regress
                # to a version below an already-audited one.
                async with self._conn.execute(
                    "SELECT COALESCE(MAX(v), 0) + 1 FROM ("
                    " SELECT MAX(config_version) AS v FROM active_constraints"
                    " UNION ALL"
                    " SELECT MAX(config_version) AS v FROM config_audit_log"
                    ")"
                ) as cursor:
                    version_row = await cursor.fetchone()
                if version_row is None:
                    raise RuntimeError("SQLite did not return a config version")
                next_version = int(version_row[0])
                now_iso = datetime.now(UTC).isoformat()

                await self._conn.execute(
                    "INSERT INTO active_constraints"
                    " (peak_limit_kw, battery_reserve_floor_percent,"
                    "  config_version, activated_at, actor)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        input.peak_limit_kw,
                        input.battery_reserve_floor_percent,
                        next_version,
                        now_iso,
                        actor,
                    ),
                )
                await self._audit_repo.append_activation(
                    actor=actor,
                    changes=changes,
                    config_version=next_version,
                    commit=False,
                )
                await self._conn.commit()
            except Exception:
                await self._conn.rollback()
                raise
        return next_version


def _build_audit_changes(
    previous: ActiveConstraints | None,
    new: ActiveConstraintsInput,
) -> list[ConfigAuditChange]:
    changes: list[ConfigAuditChange] = []
    prev_peak = None if previous is None else previous.peak_limit_kw
    if prev_peak != new.peak_limit_kw:
        changes.append(
            ConfigAuditChange(
                field=_AUDIT_FIELD_PEAK_LIMIT,
                previous_value={"kw": prev_peak},
                new_value={"kw": new.peak_limit_kw},
            )
        )
    prev_floor = None if previous is None else previous.battery_reserve_floor_percent
    if prev_floor != new.battery_reserve_floor_percent:
        changes.append(
            ConfigAuditChange(
                field=_AUDIT_FIELD_RESERVE_FLOOR,
                previous_value={"percent": prev_floor},
                new_value={"percent": new.battery_reserve_floor_percent},
            )
        )
    return changes
