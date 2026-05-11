"""Unit tests for ``DeploymentValidationResultRepo`` (Story 9.4 AC3)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import aiosqlite
import pytest
import pytest_asyncio

from open_ems.core.deployment_validation import DeploymentCheckResult
from open_ems.storage.repositories.deployment_validation_repo import (
    DeploymentValidationResultRepo,
)

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 11, 12, 0, 5, tzinfo=UTC)


_CREATE_SESSIONS = """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY
    )
"""

_CREATE_RESULTS = """
    CREATE TABLE deployment_validation_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        config_version INTEGER NOT NULL CHECK (config_version >= 0),
        overall_status TEXT NOT NULL
            CHECK (overall_status IN ('running', 'complete-PASS',
                                      'complete-WARN', 'complete-FAIL')),
        checks_json TEXT NOT NULL,
        triggered_by_session_id TEXT,
        summary_text TEXT NOT NULL,
        FOREIGN KEY (triggered_by_session_id) REFERENCES sessions(id)
            ON DELETE SET NULL
    )
"""

_CREATE_ACKS = """
    CREATE TABLE deployment_validation_acks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        validation_result_id INTEGER NOT NULL,
        check_name TEXT NOT NULL
            CHECK (check_name IN ('connectivity', 'role_completeness',
                                  'capability_strategy', 'constraint_completeness',
                                  'constraint_safety_pre_check', 'control_readiness')),
        acknowledged_at TEXT NOT NULL,
        acknowledged_by_session_id TEXT,
        UNIQUE (validation_result_id, check_name),
        FOREIGN KEY (validation_result_id)
            REFERENCES deployment_validation_results(id) ON DELETE CASCADE,
        FOREIGN KEY (acknowledged_by_session_id) REFERENCES sessions(id)
            ON DELETE SET NULL
    )
"""


def _connectivity_pass(at: datetime) -> DeploymentCheckResult:
    return DeploymentCheckResult(
        name="connectivity",
        status="pass",
        summary="All devices responded.",
        started_at=at,
        completed_at=at,
    )


def _connectivity_warn(at: datetime) -> DeploymentCheckResult:
    return DeploymentCheckResult(
        name="connectivity",
        status="warn",
        summary="One device returned reduced capability.",
        corrective_action="Review the device's firmware.",
        started_at=at,
        completed_at=at,
    )


@pytest_asyncio.fixture
async def repo_with_conn() -> AsyncGenerator[
    tuple[DeploymentValidationResultRepo, aiosqlite.Connection], None
]:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        for ddl in (_CREATE_SESSIONS, _CREATE_RESULTS, _CREATE_ACKS):
            await conn.execute(ddl)
        await conn.commit()
        # Seed one session row for FK satisfaction.
        await conn.execute("INSERT INTO sessions (id) VALUES (?)", ("sess-1",))
        await conn.commit()
        yield DeploymentValidationResultRepo(conn=conn), conn


@pytest.mark.asyncio
async def test_get_current_returns_none_when_empty(
    repo_with_conn: tuple[DeploymentValidationResultRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    assert await repo.get_current() is None


@pytest.mark.asyncio
async def test_start_run_locked_inserts_running_row(
    repo_with_conn: tuple[DeploymentValidationResultRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    result_id = await repo.start_run_locked(
        started_at=_NOW, config_version=7, triggered_by_session_id="sess-1"
    )
    assert result_id == 1
    current = await repo.get_current()
    assert current is not None
    assert current.overall_status == "running"
    assert current.completed_at is None
    assert current.config_version == 7
    assert current.checks == ()
    assert current.triggered_by_session_id == "sess-1"
    assert current.summary_text == "Validation running…"


@pytest.mark.asyncio
async def test_start_run_locked_replaces_prior_row_atomically(
    repo_with_conn: tuple[DeploymentValidationResultRepo, aiosqlite.Connection],
) -> None:
    """Single-row invariant: a subsequent start DELETEs the prior row."""
    repo, conn = repo_with_conn
    first_id = await repo.start_run_locked(
        started_at=_NOW, config_version=1, triggered_by_session_id="sess-1"
    )
    second_id = await repo.start_run_locked(
        started_at=_LATER, config_version=2, triggered_by_session_id="sess-1"
    )
    assert second_id != first_id
    async with conn.execute("SELECT COUNT(*) FROM deployment_validation_results") as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == 1
    current = await repo.get_current()
    assert current is not None and current.id == second_id


@pytest.mark.asyncio
async def test_start_run_locked_rejects_negative_config_version(
    repo_with_conn: tuple[DeploymentValidationResultRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    with pytest.raises(ValueError, match="config_version must be >= 0"):
        await repo.start_run_locked(
            started_at=_NOW, config_version=-1, triggered_by_session_id="sess-1"
        )


@pytest.mark.asyncio
async def test_update_check_progress_locked_appends_visible_to_readers(
    repo_with_conn: tuple[DeploymentValidationResultRepo, aiosqlite.Connection],
) -> None:
    """A concurrent get_current sees the progressive partial checks_json."""
    repo, _ = repo_with_conn
    result_id = await repo.start_run_locked(
        started_at=_NOW, config_version=1, triggered_by_session_id="sess-1"
    )
    check = _connectivity_pass(_NOW)
    await repo.update_check_progress_locked(result_id=result_id, checks=(check,))
    current = await repo.get_current()
    assert current is not None
    assert current.overall_status == "running"  # NOT bumped to terminal yet.
    assert len(current.checks) == 1
    assert current.checks[0].name == "connectivity"
    assert current.checks[0].status == "pass"


@pytest.mark.asyncio
async def test_finalize_run_locked_marks_terminal_state(
    repo_with_conn: tuple[DeploymentValidationResultRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    result_id = await repo.start_run_locked(
        started_at=_NOW, config_version=1, triggered_by_session_id="sess-1"
    )
    check = _connectivity_pass(_NOW)
    await repo.finalize_run_locked(
        result_id=result_id,
        overall_status="complete-PASS",
        summary_text="System is ready.",
        checks=(check,),
        completed_at=_LATER,
    )
    current = await repo.get_current()
    assert current is not None
    assert current.overall_status == "complete-PASS"
    assert current.completed_at == _LATER
    assert current.summary_text == "System is ready."


@pytest.mark.asyncio
async def test_finalize_rejects_running_status(
    repo_with_conn: tuple[DeploymentValidationResultRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    result_id = await repo.start_run_locked(
        started_at=_NOW, config_version=1, triggered_by_session_id="sess-1"
    )
    with pytest.raises(ValueError, match="non-running overall_status"):
        await repo.finalize_run_locked(
            result_id=result_id,
            overall_status="running",  # type: ignore[arg-type]
            summary_text="x",
            checks=(),
            completed_at=_LATER,
        )


@pytest.mark.asyncio
async def test_record_acknowledgment_is_idempotent(
    repo_with_conn: tuple[DeploymentValidationResultRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    result_id = await repo.start_run_locked(
        started_at=_NOW, config_version=1, triggered_by_session_id="sess-1"
    )
    await repo.finalize_run_locked(
        result_id=result_id,
        overall_status="complete-WARN",
        summary_text="Ready with limitations.",
        checks=(_connectivity_warn(_NOW),),
        completed_at=_LATER,
    )
    # First ack lands.
    await repo.record_acknowledgment_locked(
        result_id=result_id,
        check_name="connectivity",
        acknowledged_at=_LATER,
        acknowledged_by_session_id="sess-1",
    )
    current = await repo.get_current()
    assert current is not None
    assert current.acknowledged_warnings == frozenset({"connectivity"})
    # Re-ack is a no-op.
    await repo.record_acknowledgment_locked(
        result_id=result_id,
        check_name="connectivity",
        acknowledged_at=_LATER,
        acknowledged_by_session_id="sess-1",
    )
    current2 = await repo.get_current()
    assert current2 is not None
    assert current2.acknowledged_warnings == frozenset({"connectivity"})


@pytest.mark.asyncio
async def test_revoke_acknowledgment_clears_row(
    repo_with_conn: tuple[DeploymentValidationResultRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    result_id = await repo.start_run_locked(
        started_at=_NOW, config_version=1, triggered_by_session_id="sess-1"
    )
    await repo.finalize_run_locked(
        result_id=result_id,
        overall_status="complete-WARN",
        summary_text="Ready with limitations.",
        checks=(_connectivity_warn(_NOW),),
        completed_at=_LATER,
    )
    await repo.record_acknowledgment_locked(
        result_id=result_id,
        check_name="connectivity",
        acknowledged_at=_LATER,
        acknowledged_by_session_id="sess-1",
    )
    await repo.revoke_acknowledgment_locked(result_id=result_id, check_name="connectivity")
    current = await repo.get_current()
    assert current is not None
    assert current.acknowledged_warnings == frozenset()


@pytest.mark.asyncio
async def test_start_run_clears_prior_acks_via_cascade(
    repo_with_conn: tuple[DeploymentValidationResultRepo, aiosqlite.Connection],
) -> None:
    """CASCADE on the result row removes orphan acks (AC1 + AC7)."""
    repo, conn = repo_with_conn
    first_id = await repo.start_run_locked(
        started_at=_NOW, config_version=1, triggered_by_session_id="sess-1"
    )
    await repo.finalize_run_locked(
        result_id=first_id,
        overall_status="complete-WARN",
        summary_text="Ready with limitations.",
        checks=(_connectivity_warn(_NOW),),
        completed_at=_LATER,
    )
    await repo.record_acknowledgment_locked(
        result_id=first_id,
        check_name="connectivity",
        acknowledged_at=_LATER,
        acknowledged_by_session_id="sess-1",
    )
    # A new run replaces the row → CASCADE clears acks.
    await repo.start_run_locked(
        started_at=_LATER, config_version=2, triggered_by_session_id="sess-1"
    )
    async with conn.execute("SELECT COUNT(*) FROM deployment_validation_acks") as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == 0


@pytest.mark.asyncio
async def test_session_deletion_sets_triggered_by_to_null(
    repo_with_conn: tuple[DeploymentValidationResultRepo, aiosqlite.Connection],
) -> None:
    """FK SET NULL: the result row survives session deletion."""
    repo, conn = repo_with_conn
    result_id = await repo.start_run_locked(
        started_at=_NOW, config_version=1, triggered_by_session_id="sess-1"
    )
    await repo.finalize_run_locked(
        result_id=result_id,
        overall_status="complete-PASS",
        summary_text="System is ready.",
        checks=(_connectivity_pass(_NOW),),
        completed_at=_LATER,
    )
    await conn.execute("DELETE FROM sessions WHERE id = ?", ("sess-1",))
    await conn.commit()
    current = await repo.get_current()
    assert current is not None
    assert current.triggered_by_session_id is None
