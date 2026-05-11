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
        step_2_complete INTEGER NOT NULL DEFAULT 0
            CHECK (step_2_complete IN (0, 1)),
        step_2_completed_at TEXT,
        step_2_acknowledged_gaps TEXT,
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


# ---------------------------------------------------------------------------
# Story 9.2 — step_2 columns + acknowledged-gaps JSON encoding
# ---------------------------------------------------------------------------


async def test_record_acknowledged_gaps_round_trip_through_json(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.get_or_create("session-x", now=_NOW)
    gaps = frozenset({"battery_missing", "inverter_missing"})
    await repo.record_acknowledged_gaps("session-x", gaps=gaps, now=_LATER)
    state = await repo.get("session-x")
    assert state is not None
    assert state.step_2_acknowledged_gaps == gaps


async def test_record_acknowledged_gaps_rejects_out_of_whitelist_label(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    """Repo-level defense-in-depth: an invalid label is rejected with the
    exact-match message even if the route handler is bypassed.
    """
    repo, _ = repo_with_conn
    await repo.get_or_create("session-x", now=_NOW)
    with pytest.raises(ValueError) as exc_info:
        await repo.record_acknowledged_gaps(
            "session-x",
            gaps=frozenset({"grid_meter_missing"}),
            now=_LATER,
        )
    assert str(exc_info.value) == "acknowledged_gap_label_invalid: grid_meter_missing"


async def test_record_acknowledged_gaps_rejects_unknown_label(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.get_or_create("session-x", now=_NOW)
    with pytest.raises(ValueError) as exc_info:
        await repo.record_acknowledged_gaps(
            "session-x",
            gaps=frozenset({"banana_missing"}),
            now=_LATER,
        )
    assert str(exc_info.value) == "acknowledged_gap_label_invalid: banana_missing"


async def test_set_step_2_complete_is_idempotent_on_completed_at(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    """A second passing advance MUST preserve the first completion timestamp."""
    repo, _ = repo_with_conn
    await repo.get_or_create("session-x", now=_NOW)
    await repo.set_step_2_complete("session-x", acknowledged_gaps=frozenset(), now=_LATER)
    first = await repo.get("session-x")
    assert first is not None
    assert first.step_2_complete is True
    assert first.step_2_completed_at == _LATER

    later2 = datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC)
    await repo.set_step_2_complete("session-x", acknowledged_gaps=frozenset(), now=later2)
    second = await repo.get("session-x")
    assert second is not None
    assert second.step_2_complete is True
    # ``step_2_completed_at`` is preserved from the first pass.
    assert second.step_2_completed_at == _LATER
    # But ``updated_at`` advances so audit can see the latest write.
    assert second.updated_at == later2


async def test_set_step_2_complete_persists_filtered_acknowledged_gaps(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.get_or_create("session-x", now=_NOW)
    gaps = frozenset({"battery_missing", "inverter_missing"})
    await repo.set_step_2_complete("session-x", acknowledged_gaps=gaps, now=_LATER)
    state = await repo.get("session-x")
    assert state is not None
    assert state.step_2_acknowledged_gaps == gaps


async def test_set_step_2_complete_rejects_invalid_label(
    repo_with_conn: tuple[WizardStateRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.get_or_create("session-x", now=_NOW)
    with pytest.raises(ValueError) as exc_info:
        await repo.set_step_2_complete(
            "session-x",
            acknowledged_gaps=frozenset({"grid_meter_missing"}),
            now=_LATER,
        )
    assert str(exc_info.value) == "acknowledged_gap_label_invalid: grid_meter_missing"


async def test_wizard_state_model_rejects_tampered_acknowledged_gaps() -> None:
    """Pydantic-level defense-in-depth: a row with an unknown label fails
    loud at every read, not only on write.
    """
    from pydantic import ValidationError

    from open_ems.storage.repositories.wizard_state_repo import WizardState

    with pytest.raises(ValidationError, match="unknown labels"):
        WizardState(
            session_id="session-x",
            step_1_complete=True,
            step_1_completed_at=_NOW,
            last_scan_id=None,
            step_2_complete=False,
            step_2_completed_at=None,
            step_2_acknowledged_gaps=frozenset({"banana_missing"}),
            created_at=_NOW,
            updated_at=_NOW,
        )
