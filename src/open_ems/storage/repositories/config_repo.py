"""``ConfigRepo`` — persistent owner of the active site safety constraints.

Story 9.0b: this repo is the only legal write path into the
``active_constraints`` table. ``activate()`` runs a single atomic transaction
that inserts the new active row AND emits one ``config_audit_log`` row per
changed field (delegated to ``ConfigAuditRepo.append_activation``), so the
two tables can never disagree on ``config_version``.

Story 9.3: ``activate()`` is split into a public lock-acquiring entry point
and a private ``_activate_locked`` body. Routes that already hold
``get_write_lock()`` (the staged-activation route) call ``_activate_locked``
directly to avoid asyncio.Lock reentrancy deadlock. The EV charging window
(``ev_charging_window_start`` / ``ev_charging_window_end``) is folded into
the same row + audit emission so an EV-window change still produces exactly
one ``config_version`` increment and one combined audit row.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import aiosqlite
import structlog
from pydantic import ValidationError

from open_ems.core.constraints import ActiveConstraints, ActiveConstraintsInput
from open_ems.storage.database import get_connection, get_write_lock
from open_ems.storage.repositories.config_audit_repo import (
    ConfigAuditChange,
    ConfigAuditRepo,
)

logger = structlog.get_logger(__name__)

VALID_ACTIVATION_ACTORS: frozenset[str] = frozenset({"system", "installer"})


class NoChangedFieldsError(ValueError):
    """Raised by ``_activate_locked`` when the input matches the active row.

    Carried as a dedicated type so callers can map to the exact-match
    contract reason ``activate_called_with_no_changed_fields`` without
    inspecting the exception message text (Story 9.3 P7).
    """

    def __init__(self) -> None:
        super().__init__("activate called with no changed fields")


# Audit-vocabulary field names already established by Story 6.3
# (see SAFETY_RELEVANT_CONFIG_FIELDS in config_audit_repo).
_AUDIT_FIELD_PEAK_LIMIT: str = "peak_consumption_limit"
_AUDIT_FIELD_RESERVE_FLOOR: str = "battery_reserve_floor"
_AUDIT_FIELD_EV_WINDOW: str = "ev_charging_window"


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
            " activated_at, ev_charging_window_start, ev_charging_window_end"
            " FROM active_constraints"
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
                ev_charging_window_start=str(row[5]) if row[5] is not None else None,
                ev_charging_window_end=str(row[6]) if row[6] is not None else None,
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

        Acquires ``get_write_lock()`` and delegates to ``_activate_locked``.
        Callers that already hold the lock (Story 9.3's
        ``ConstraintsService.activate_draft``) MUST call ``_activate_locked``
        directly — asyncio.Lock is not reentrant.
        """
        async with get_write_lock():
            return await self._activate_locked(input, actor=actor)

    async def _activate_locked(
        self,
        input: ActiveConstraintsInput,
        *,
        actor: Literal["system", "installer"],
        inside_transaction: Callable[[int, datetime], Awaitable[None]] | None = None,
        now: datetime | None = None,
    ) -> int:
        """Lock-free body of ``activate``. Caller MUST hold ``get_write_lock()``.

        Audit emission is delegated to ``ConfigAuditRepo.append_activation``
        with ``commit=False`` so both inserts share the outer
        ``BEGIN IMMEDIATE`` transaction.

        ``inside_transaction`` is an optional async callback invoked AFTER the
        active_constraints + config_audit_log INSERTs but BEFORE COMMIT.
        Receives ``(new_version, now_utc)``. Story 9.3 P13 — the staged
        activation route uses this to fold ``wizard_state.step_3_*`` updates
        and ``draft_constraints`` deletion into the same transaction so the
        four-table activation is atomic. If the callback raises, the whole
        transaction rolls back (no partial commits across tables).

        R2P10 — when ``now`` is provided, it is used as the single timestamp
        threaded through every row touched by this activation (the
        ``active_constraints.activated_at`` column, the ``inside_transaction``
        callback parameter, and — transitively — any wizard / draft row written
        by the callback). Callers that want the implicit
        ``datetime.now(UTC)`` semantics (e.g. the public ``activate`` entry
        point) leave ``now=None``. Threading one ``now`` eliminates the
        sub-millisecond divergence between ``active_constraints.activated_at``
        and ``wizard_state.step_3_completed_at`` that the spec docstring
        promises to be a single moment.

        Raises ``NoChangedFieldsError`` (a ``ValueError`` subclass) when the
        input matches the active row — callers may catch the specific type to
        map to a stable contract reason.
        """
        if actor not in VALID_ACTIVATION_ACTORS:
            raise ValueError(f"Invalid actor {actor!r}")
        if now is not None and (now.tzinfo is None or now.utcoffset() != timedelta(0)):
            raise ValueError("now must be timezone-aware UTC")

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
                raise NoChangedFieldsError()

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
            now_utc = now if now is not None else datetime.now(UTC)
            now_iso = now_utc.isoformat()

            await self._conn.execute(
                "INSERT INTO active_constraints"
                " (peak_limit_kw, battery_reserve_floor_percent,"
                "  config_version, activated_at, actor,"
                "  ev_charging_window_start, ev_charging_window_end)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    input.peak_limit_kw,
                    input.battery_reserve_floor_percent,
                    next_version,
                    now_iso,
                    actor,
                    input.ev_charging_window_start,
                    input.ev_charging_window_end,
                ),
            )
            await self._audit_repo.append_activation(
                actor=actor,
                changes=changes,
                config_version=next_version,
                commit=False,
            )
            if inside_transaction is not None:
                await inside_transaction(next_version, now_utc)
            await self._conn.commit()
        except aiosqlite.OperationalError:
            # R2P13 — SQLite lock-contention / "database is locked" surfaces as
            # a typed OperationalError so callers (the service layer) can map
            # it to a retryable rejection (``ConstraintActivationError(
            # "activation_busy")``) rather than letting it bubble as a raw 500.
            await _safe_rollback(self._conn)
            raise
        except Exception:
            # R2P13 — shield the rollback so a connection-died failure during
            # rollback cannot mask the originating exception. The original
            # error is what tells the operator what actually went wrong.
            await _safe_rollback(self._conn)
            raise
        return next_version


async def _safe_rollback(conn: aiosqlite.Connection) -> None:
    """R2P13 — Roll back without masking the originating exception.

    A connection-died failure during rollback (rare but documented in
    aiosqlite) would otherwise replace the real cause in the traceback. The
    rollback failure is logged at WARN and swallowed; the caller still sees
    the original exception via its own ``raise``.
    """
    try:
        await conn.rollback()
    except Exception:  # noqa: BLE001
        logger.warning(
            "config_repo_rollback_failed",
            component="config_repo",
            exc_info=True,
        )


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
    # Story 9.3 — EV charging window emits ONE combined audit row when EITHER
    # endpoint changes. The window is a single semantic constraint (paired-NULL
    # or paired-set), so we never produce two rows for it. ``previous`` may be
    # NULL (cold-start: no prior active row), in which case both endpoints are
    # treated as ``None`` and the row only fires when the new input has them
    # populated.
    prev_start = None if previous is None else previous.ev_charging_window_start
    prev_end = None if previous is None else previous.ev_charging_window_end
    if prev_start != new.ev_charging_window_start or prev_end != new.ev_charging_window_end:
        changes.append(
            ConfigAuditChange(
                field=_AUDIT_FIELD_EV_WINDOW,
                previous_value={"start": prev_start, "end": prev_end},
                new_value={
                    "start": new.ev_charging_window_start,
                    "end": new.ev_charging_window_end,
                },
            )
        )
    return changes
