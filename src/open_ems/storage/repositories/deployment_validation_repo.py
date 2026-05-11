"""``DeploymentValidationResultRepo`` — single-row persistence for Story 9.4.

The ``deployment_validation_results`` table holds AT MOST one row at any
time. A new ``DeploymentValidationService.run()`` invocation DELETEs the
prior row (and CASCADE-clears its acks) BEFORE inserting the fresh
``running`` row, all inside one ``BEGIN IMMEDIATE`` transaction (9.0b
precedent for atomic single-row replacement).

Why single-row + a paired acks table (instead of a self-contained JSON column):

* The result is consumed by exactly one UX surface (Step 4) and one future
  consumer (Story 9.5 handoff). v1 has no validation-history view.
* Acks mutate independently of the result body — the result is immutable
  post-completion, the ack set grows + shrinks. Putting acks into the
  result JSON would force every ack write to rewrite the whole row.
* ``DeploymentValidationResult.acknowledged_warnings`` is computed by
  JOINing the ack rows at read time. CASCADE on result-row deletion
  guarantees no orphan acks.

Mirrors ``DraftConstraintsRepo`` shape: ``frozen=True, extra="forbid"``
Pydantic view, async per-call methods, write-lock-protected mutations, and
``_locked`` sibling helpers that join an outer ``BEGIN IMMEDIATE``
transaction without re-acquiring the lock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite
import structlog

from open_ems.core.deployment_validation import (
    DeploymentCheckName,
    DeploymentCheckResult,
    DeploymentOverallStatus,
    DeploymentValidationResult,
)
from open_ems.storage.database import get_connection, get_write_lock

logger = structlog.get_logger(__name__)


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def _encode_checks(checks: tuple[DeploymentCheckResult, ...]) -> str:
    """Serialise the check tuple to the ``checks_json`` column shape.

    Raises ``ValueError`` (typed) rather than letting raw ``TypeError`` from
    ``json.dumps`` bubble — same defensive pattern as Story 9.3's
    ``_encode_report``.
    """
    try:
        return json.dumps([c.model_dump(mode="json") for c in checks])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"DeploymentCheckResult JSON encoding failed: {exc}") from exc


def _decode_checks(raw: str) -> tuple[DeploymentCheckResult, ...]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError(
            "deployment_validation_results.checks_json must decode to a list"
            f" (got {type(payload).__name__})"
        )
    return tuple(DeploymentCheckResult.model_validate(item) for item in payload)


class DeploymentValidationResultRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_current(self) -> DeploymentValidationResult | None:
        """Return the single most-recent row (with its acks JOINed) or ``None``.

        Returns ``None`` only when the table is empty — i.e. the
        ``never_run`` derived state.
        """
        async with self._conn.execute(
            "SELECT id, started_at, completed_at, config_version,"
            " overall_status, checks_json, triggered_by_session_id, summary_text"
            " FROM deployment_validation_results"
            " ORDER BY id DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        result_id = int(row[0])
        acks = await self._fetch_acks(result_id)
        # P26 — defensive decode. If ``checks_json`` is corrupted (manual DB
        # edit, mid-write crash, forward-incompatible schema change without a
        # migration), surface a structured "corrupted" result that lets the
        # route render an operator-friendly "run again" banner rather than a
        # bare 500. Logged at ERROR so observability surfaces it.
        try:
            checks = _decode_checks(str(row[5]))
        except (ValueError, json.JSONDecodeError) as exc:
            logger.error(
                "deployment_validation_result_decode_failed",
                component="storage.deployment_validation_repo",
                result_id=result_id,
                error=repr(exc),
            )
            checks = ()
            return DeploymentValidationResult(
                id=result_id,
                started_at=datetime.fromisoformat(str(row[1])),
                completed_at=(
                    datetime.fromisoformat(str(row[2])) if row[2] is not None else datetime.now(UTC)
                ),
                config_version=int(row[3]),
                overall_status="complete-FAIL",
                checks=checks,
                triggered_by_session_id=str(row[6]) if row[6] is not None else None,
                summary_text=("Validation result data is corrupted; please run validation again."),
                acknowledged_warnings=acks,
            )
        return DeploymentValidationResult(
            id=result_id,
            started_at=datetime.fromisoformat(str(row[1])),
            completed_at=(datetime.fromisoformat(str(row[2])) if row[2] is not None else None),
            config_version=int(row[3]),
            overall_status=str(row[4]),  # type: ignore[arg-type]
            checks=checks,
            triggered_by_session_id=str(row[6]) if row[6] is not None else None,
            summary_text=str(row[7]),
            acknowledged_warnings=acks,
        )

    async def _fetch_acks(self, result_id: int) -> frozenset[DeploymentCheckName]:
        async with self._conn.execute(
            "SELECT check_name FROM deployment_validation_acks WHERE validation_result_id = ?",
            (result_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return frozenset(str(row[0]) for row in rows)  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Writes — public wrappers acquire the lock; ``_locked`` siblings do not.
    # ------------------------------------------------------------------

    async def start_run_locked(
        self,
        *,
        started_at: datetime,
        config_version: int,
        triggered_by_session_id: str | None,
        summary_text: str = "Validation running…",
    ) -> int:
        """Atomic single-row replacement.

        Caller MUST already hold ``get_write_lock()``. DELETEs any prior row
        (and CASCADE-clears its acks) BEFORE INSERTing the fresh ``running``
        row, all inside one ``BEGIN IMMEDIATE`` transaction so a crash
        between the two writes cannot leave the table in a partial state.
        Returns the new row's ``id``.
        """
        _require_utc(started_at, "started_at")
        if config_version < 0:
            raise ValueError(f"config_version must be >= 0 (got {config_version})")
        await self._conn.commit()
        try:
            await self._conn.execute("BEGIN IMMEDIATE")
            await self._conn.execute("DELETE FROM deployment_validation_results")
            await self._conn.execute(
                "INSERT INTO deployment_validation_results"
                " (started_at, completed_at, config_version, overall_status,"
                "  checks_json, triggered_by_session_id, summary_text)"
                " VALUES (?, NULL, ?, 'running', '[]', ?, ?)",
                (
                    started_at.isoformat(),
                    config_version,
                    triggered_by_session_id,
                    summary_text,
                ),
            )
            async with self._conn.execute("SELECT last_insert_rowid()") as cursor:
                row = await cursor.fetchone()
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        if row is None or row[0] is None:
            raise RuntimeError(
                "deployment_validation_results.start_run_locked: last_insert_rowid returned no row"
            )
        return int(row[0])

    async def update_check_progress_locked(
        self,
        *,
        result_id: int,
        checks: tuple[DeploymentCheckResult, ...],
    ) -> None:
        """Persist the latest progressive check tuple.

        Caller MUST already hold ``get_write_lock()``. Single-statement
        UPDATE — replaces ``checks_json`` with the full encoding of the
        in-progress tuple. Called once per check completion so a concurrent
        poller observes partial results.
        """
        encoded = _encode_checks(checks)
        await self._conn.commit()
        async with self._conn.execute(
            "UPDATE deployment_validation_results SET checks_json = ? WHERE id = ?",
            (encoded, result_id),
        ) as cursor:
            if cursor.rowcount == 0:
                await self._conn.commit()
                raise ValueError(f"No deployment_validation_results row with id={result_id}")
        await self._conn.commit()

    async def finalize_run_locked(
        self,
        *,
        result_id: int,
        overall_status: DeploymentOverallStatus,
        summary_text: str,
        checks: tuple[DeploymentCheckResult, ...],
        completed_at: datetime,
    ) -> None:
        """Mark the run terminal.

        Caller MUST already hold ``get_write_lock()``. Writes the final
        ``checks_json`` + ``overall_status`` + ``summary_text`` +
        ``completed_at`` atomically in one statement so the row never sits
        in a partial-terminal state (e.g. status terminal but completed_at
        still NULL).
        """
        if overall_status == "running":
            raise ValueError("finalize_run_locked must be called with a non-running overall_status")
        _require_utc(completed_at, "completed_at")
        if not summary_text:
            raise ValueError("summary_text must be non-empty")
        encoded = _encode_checks(checks)
        await self._conn.commit()
        async with self._conn.execute(
            "UPDATE deployment_validation_results SET"
            " checks_json = ?,"
            " overall_status = ?,"
            " summary_text = ?,"
            " completed_at = ?"
            " WHERE id = ?",
            (
                encoded,
                overall_status,
                summary_text,
                completed_at.isoformat(),
                result_id,
            ),
        ) as cursor:
            if cursor.rowcount == 0:
                await self._conn.commit()
                raise ValueError(f"No deployment_validation_results row with id={result_id}")
        await self._conn.commit()

    async def record_acknowledgment(
        self,
        *,
        result_id: int,
        check_name: DeploymentCheckName,
        acknowledged_at: datetime,
        acknowledged_by_session_id: str | None,
    ) -> None:
        """Public wrapper that acquires the write lock.

        Used by route handlers. The service-internal call paths use the
        ``_locked`` variant.
        """
        async with get_write_lock():
            await self.record_acknowledgment_locked(
                result_id=result_id,
                check_name=check_name,
                acknowledged_at=acknowledged_at,
                acknowledged_by_session_id=acknowledged_by_session_id,
            )

    async def record_acknowledgment_locked(
        self,
        *,
        result_id: int,
        check_name: DeploymentCheckName,
        acknowledged_at: datetime,
        acknowledged_by_session_id: str | None,
    ) -> None:
        """Idempotent ack INSERT. Caller MUST already hold ``get_write_lock()``.

        Uses ``INSERT OR IGNORE`` against the UNIQUE
        ``(validation_result_id, check_name)`` so a re-ack of the same WARN
        is a no-op (matches AC7's idempotency contract).
        """
        _require_utc(acknowledged_at, "acknowledged_at")
        await self._conn.commit()
        await self._conn.execute(
            "INSERT OR IGNORE INTO deployment_validation_acks"
            " (validation_result_id, check_name, acknowledged_at,"
            "  acknowledged_by_session_id)"
            " VALUES (?, ?, ?, ?)",
            (
                result_id,
                check_name,
                acknowledged_at.isoformat(),
                acknowledged_by_session_id,
            ),
        )
        await self._conn.commit()

    async def revoke_acknowledgment(
        self,
        *,
        result_id: int,
        check_name: DeploymentCheckName,
    ) -> None:
        async with get_write_lock():
            await self.revoke_acknowledgment_locked(
                result_id=result_id,
                check_name=check_name,
            )

    async def revoke_acknowledgment_locked(
        self,
        *,
        result_id: int,
        check_name: DeploymentCheckName,
    ) -> None:
        """Idempotent ack DELETE. Caller MUST already hold ``get_write_lock()``."""
        await self._conn.commit()
        await self._conn.execute(
            "DELETE FROM deployment_validation_acks"
            " WHERE validation_result_id = ? AND check_name = ?",
            (result_id, check_name),
        )
        await self._conn.commit()
