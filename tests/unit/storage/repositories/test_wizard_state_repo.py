"""Unit tests for ``WizardStateRepo`` — Story 9.1 AC7, AC8."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import aiosqlite
import pytest
import pytest_asyncio

from open_ems.storage.repositories.wizard_state_repo import WizardStateRepo

_CREATE_WIZARD_STATE = """
    CREATE TABLE wizard_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL UNIQUE,
        step_1_complete INTEGER NOT NULL DEFAULT 0
            CHECK (step_1_complete IN (0, 1)),
        step_1_completed_at TEXT,
        last_scan_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
"""

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 11, 12, 30, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def repo_with_conn() -> AsyncGenerator[tuple[WizardStateRepo, aiosqlite.Connection], None]:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_WIZARD_STATE)
        await conn.commit()
        yield WizardStateRepo(conn), conn


async def test_get_on_empty_returns_none(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    assert await repo.get("session-x") is None


async def test_get_or_create_lazy_creates_with_step_1_incomplete(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    state = await repo.get_or_create("session-x", now=_NOW)
    assert state.session_id == "session-x"
    assert state.step_1_complete is False
    assert state.step_1_completed_at is None
    assert state.last_scan_id is None
    assert state.created_at == _NOW
    assert state.updated_at == _NOW


async def test_get_or_create_is_idempotent(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    first = await repo.get_or_create("session-x", now=_NOW)
    second = await repo.get_or_create("session-x", now=_LATER)
    assert first.created_at == second.created_at  # second call must NOT overwrite
    async with conn.execute("SELECT COUNT(*) FROM wizard_state") as cur:
        row = await cur.fetchone()
    assert row is not None and int(row[0]) == 1


async def test_set_step_1_complete_persists_completion_timestamp(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.get_or_create("session-x", now=_NOW)
    await repo.set_step_1_complete("session-x", now=_LATER)
    state = await repo.get("session-x")
    assert state is not None
    assert state.step_1_complete is True
    assert state.step_1_completed_at == _LATER
    assert state.updated_at == _LATER


async def test_set_step_1_complete_raises_when_missing(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    with pytest.raises(ValueError, match="No wizard_state row"):
        await repo.set_step_1_complete("missing", now=_LATER)


async def test_set_last_scan_id_persists_scan_uuid(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.get_or_create("session-x", now=_NOW)
    await repo.set_last_scan_id("session-x", scan_id="abc-123", now=_LATER)
    state = await repo.get("session-x")
    assert state is not None
    assert state.last_scan_id == "abc-123"
    assert state.updated_at == _LATER


async def test_get_or_create_rejects_naive_now(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    naive = datetime(2026, 5, 11, 12, 0, 0)
    with pytest.raises(ValueError, match="must be timezone-aware UTC"):
        await repo.get_or_create("session-x", now=naive)
