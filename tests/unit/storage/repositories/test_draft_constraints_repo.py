"""Unit tests for ``DraftConstraintsRepo`` — Story 9.3 AC10."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import aiosqlite
import pytest
import pytest_asyncio

from open_ems.core.constraints import (
    ConstraintCheckResult,
    ConstraintValidationReport,
)
from open_ems.storage.repositories.draft_constraints_repo import (
    ConstraintDraft,
    DraftConstraintsRepo,
)

# Schema mirrors migrations 0011 — kept inline so this test does not depend on
# the full Alembic stack. The R7 deferred-finding (multiple inline DDL copies)
# applies; this is the new copy added by 9.3 — same shape as production.
_CREATE_SESSIONS = """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY
    )
"""
_CREATE_DRAFT_CONSTRAINTS = """
    CREATE TABLE draft_constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL UNIQUE,
        peak_limit_kw REAL NOT NULL CHECK (peak_limit_kw > 0),
        battery_reserve_floor_percent REAL NOT NULL
            CHECK (battery_reserve_floor_percent >= 0
                   AND battery_reserve_floor_percent <= 100),
        ev_charging_window_start TEXT,
        ev_charging_window_end TEXT,
        validation_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (validation_status IN ('pending', 'valid', 'failed')),
        validation_report TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (
            (ev_charging_window_start IS NULL AND ev_charging_window_end IS NULL)
            OR (ev_charging_window_start IS NOT NULL
                AND ev_charging_window_end IS NOT NULL)
        ),
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    )
"""

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 11, 12, 30, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def repo_with_conn() -> AsyncGenerator[
    tuple[DraftConstraintsRepo, aiosqlite.Connection], None
]:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute(_CREATE_SESSIONS)
        await conn.execute(_CREATE_DRAFT_CONSTRAINTS)
        await conn.execute("INSERT INTO sessions (id) VALUES ('session-x')")
        await conn.commit()
        yield DraftConstraintsRepo(conn), conn


def _report(status: str = "valid") -> ConstraintValidationReport:
    overall = "valid" if status == "valid" else "failed"
    return ConstraintValidationReport(
        overall_status=overall,  # type: ignore[arg-type]
        checks=(
            ConstraintCheckResult(name="schema", status="pass"),
            ConstraintCheckResult(
                name="capability_strategy",
                status="pass",
            ),
            ConstraintCheckResult(name="safety_pre_check", status="pass"),
        ),
    )


async def test_get_on_empty_returns_none(
    repo_with_conn: tuple[DraftConstraintsRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    assert await repo.get("session-x") is None


async def test_upsert_inserts_with_pending_status_and_no_report(
    repo_with_conn: tuple[DraftConstraintsRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    draft = await repo.upsert(
        "session-x",
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        ev_charging_window_start=None,
        ev_charging_window_end=None,
        now=_NOW,
    )
    assert draft.peak_limit_kw == 25.0
    assert draft.validation_status == "pending"
    assert draft.validation_report is None
    assert draft.created_at == _NOW


async def test_upsert_replaces_and_resets_validation_status(
    repo_with_conn: tuple[DraftConstraintsRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.upsert(
        "session-x",
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        ev_charging_window_start=None,
        ev_charging_window_end=None,
        now=_NOW,
    )
    await repo.record_validation_outcome("session-x", status="valid", report=_report(), now=_NOW)
    # Re-upsert with different values — must reset validation_status='pending'
    # and clear validation_report. created_at must be preserved.
    updated = await repo.upsert(
        "session-x",
        peak_limit_kw=30.0,
        battery_reserve_floor_percent=15.0,
        ev_charging_window_start="09:00",
        ev_charging_window_end="17:00",
        now=_LATER,
    )
    assert updated.peak_limit_kw == 30.0
    assert updated.battery_reserve_floor_percent == 15.0
    assert updated.ev_charging_window_start == "09:00"
    assert updated.ev_charging_window_end == "17:00"
    assert updated.validation_status == "pending"
    assert updated.validation_report is None
    assert updated.created_at == _NOW
    assert updated.updated_at == _LATER


async def test_record_validation_outcome_round_trips_through_json(
    repo_with_conn: tuple[DraftConstraintsRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.upsert(
        "session-x",
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        ev_charging_window_start=None,
        ev_charging_window_end=None,
        now=_NOW,
    )
    report = _report("failed")
    await repo.record_validation_outcome("session-x", status="failed", report=report, now=_LATER)
    fetched = await repo.get("session-x")
    assert fetched is not None
    assert fetched.validation_status == "failed"
    assert fetched.validation_report == report


async def test_record_validation_outcome_raises_when_missing(
    repo_with_conn: tuple[DraftConstraintsRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    with pytest.raises(ValueError, match="No draft_constraints row"):
        await repo.record_validation_outcome("missing", status="valid", report=_report(), now=_NOW)


async def test_record_validation_outcome_rejects_pending(
    repo_with_conn: tuple[DraftConstraintsRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.upsert(
        "session-x",
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        ev_charging_window_start=None,
        ev_charging_window_end=None,
        now=_NOW,
    )
    with pytest.raises(ValueError, match="status must be 'valid' or 'failed'"):
        await repo.record_validation_outcome(
            "session-x",
            status="pending",
            report=_report(),
            now=_LATER,  # type: ignore[arg-type]
        )


async def test_delete_removes_row(
    repo_with_conn: tuple[DraftConstraintsRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    await repo.upsert(
        "session-x",
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        ev_charging_window_start=None,
        ev_charging_window_end=None,
        now=_NOW,
    )
    await repo.delete("session-x")
    assert await repo.get("session-x") is None
    async with conn.execute("SELECT COUNT(*) FROM draft_constraints") as cur:
        row = await cur.fetchone()
    assert row is not None and int(row[0]) == 0


async def test_delete_is_noop_for_missing_session(
    repo_with_conn: tuple[DraftConstraintsRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.delete("missing")  # must not raise


async def test_check_constraint_rejects_negative_peak_limit(
    repo_with_conn: tuple[DraftConstraintsRepo, aiosqlite.Connection],
) -> None:
    _, conn = repo_with_conn
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(
            "INSERT INTO draft_constraints"
            " (session_id, peak_limit_kw, battery_reserve_floor_percent,"
            "  validation_status, created_at, updated_at)"
            " VALUES ('session-x', -5.0, 20.0, 'pending', ?, ?)",
            (_NOW.isoformat(), _NOW.isoformat()),
        )


async def test_check_constraint_rejects_unpaired_ev_window(
    repo_with_conn: tuple[DraftConstraintsRepo, aiosqlite.Connection],
) -> None:
    _, conn = repo_with_conn
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(
            "INSERT INTO draft_constraints"
            " (session_id, peak_limit_kw, battery_reserve_floor_percent,"
            "  ev_charging_window_start, ev_charging_window_end,"
            "  validation_status, created_at, updated_at)"
            " VALUES ('session-x', 25.0, 20.0, '09:00', NULL, 'pending', ?, ?)",
            (_NOW.isoformat(), _NOW.isoformat()),
        )


async def test_fk_cascade_removes_draft_when_session_deleted(
    repo_with_conn: tuple[DraftConstraintsRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    await repo.upsert(
        "session-x",
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        ev_charging_window_start=None,
        ev_charging_window_end=None,
        now=_NOW,
    )
    await conn.execute("DELETE FROM sessions WHERE id = 'session-x'")
    await conn.commit()
    assert await repo.get("session-x") is None


async def test_constraint_draft_pydantic_model_rejects_naive_timestamps() -> None:
    from pydantic import ValidationError

    naive = datetime(2026, 5, 11, 12, 0, 0)
    with pytest.raises(ValidationError, match="must be timezone-aware UTC"):
        ConstraintDraft(
            session_id="session-x",
            peak_limit_kw=25.0,
            battery_reserve_floor_percent=20.0,
            validation_status="pending",
            created_at=naive,
            updated_at=_NOW,
        )
