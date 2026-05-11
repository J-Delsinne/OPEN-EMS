"""Unit tests for ``ConfigRepo`` — Story 9.0b AC2 + AC10 #1-#9, #24.

Drives the real SQLite engine (in-memory) via the same conftest pattern
established for ``ConfigAuditRepo`` so CHECK constraints, BEGIN IMMEDIATE
semantics, and atomicity are exercised end-to-end rather than mocked.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from open_ems.core.constraints import ActiveConstraintsInput
from open_ems.storage.repositories.config_repo import ConfigRepo

# Schema mirrors migrations/versions/0008_add_active_constraints_table.py +
# 0011_add_constraint_configuration_tables.py + 0006_add_config_audit_log_table.py
# — kept in sync as a unit-test convenience.
_CREATE_ACTIVE_CONSTRAINTS = """
    CREATE TABLE active_constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        peak_limit_kw REAL NOT NULL CHECK (peak_limit_kw > 0),
        battery_reserve_floor_percent REAL NOT NULL
            CHECK (battery_reserve_floor_percent >= 0
                   AND battery_reserve_floor_percent <= 100),
        config_version INTEGER NOT NULL UNIQUE,
        activated_at TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('system', 'installer')),
        ev_charging_window_start TEXT NULL,
        ev_charging_window_end TEXT NULL,
        CHECK (
            (ev_charging_window_start IS NULL AND ev_charging_window_end IS NULL)
            OR (ev_charging_window_start IS NOT NULL
                AND ev_charging_window_end IS NOT NULL)
        )
    )
"""
_CREATE_CONFIG_AUDIT_LOG = """
    CREATE TABLE config_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        field TEXT NOT NULL,
        previous_value TEXT NOT NULL,
        new_value TEXT NOT NULL,
        config_version INTEGER NOT NULL
    )
"""


@pytest_asyncio.fixture
async def repo_with_conn() -> AsyncGenerator[tuple[ConfigRepo, aiosqlite.Connection], None]:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_ACTIVE_CONSTRAINTS)
        await conn.execute(_CREATE_CONFIG_AUDIT_LOG)
        await conn.commit()
        yield ConfigRepo(conn), conn


def _input(peak: float = 25.0, floor: float = 20.0) -> ActiveConstraintsInput:
    return ActiveConstraintsInput(peak_limit_kw=peak, battery_reserve_floor_percent=floor)


async def _audit_count(conn: aiosqlite.Connection) -> int:
    async with conn.execute("SELECT COUNT(*) FROM config_audit_log") as cur:
        row = await cur.fetchone()
    return int(cast(aiosqlite.Row, row)[0])


async def _active_count(conn: aiosqlite.Connection) -> int:
    async with conn.execute("SELECT COUNT(*) FROM active_constraints") as cur:
        row = await cur.fetchone()
    return int(cast(aiosqlite.Row, row)[0])


# AC10 #1
async def test_get_active_on_empty_table_returns_none(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    assert await repo.get_active() is None


# AC10 #2
async def test_get_active_after_one_activate_returns_inserted_row(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    version = await repo.activate(_input(25.0, 20.0), actor="system")
    assert version == 1
    active = await repo.get_active()
    assert active is not None
    assert active.peak_limit_kw == 25.0
    assert active.battery_reserve_floor_percent == 20.0
    assert active.config_version == 1


# AC10 #3
async def test_get_active_after_two_activations_returns_highest_version(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.activate(_input(25.0, 20.0), actor="installer")
    await repo.activate(_input(40.0, 25.0), actor="installer")
    active = await repo.get_active()
    assert active is not None
    assert active.config_version == 2
    assert active.peak_limit_kw == 40.0
    assert active.battery_reserve_floor_percent == 25.0


# AC10 #4
async def test_activate_writes_one_audit_row_per_changed_field(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    # First activation: previous is None, both fields are "changed" → 2 rows
    await repo.activate(_input(25.0, 20.0), actor="installer")
    assert await _audit_count(conn) == 2

    # Second activation: only peak_limit changes → 1 audit row, with the right field
    await repo.activate(_input(30.0, 20.0), actor="installer")
    assert await _audit_count(conn) == 3
    async with conn.execute(
        "SELECT field, previous_value, new_value, config_version FROM config_audit_log"
        " WHERE config_version = 2"
    ) as cur:
        rows = list(await cur.fetchall())
    assert len(rows) == 1
    assert rows[0]["field"] == "peak_consumption_limit"
    assert json.loads(rows[0]["previous_value"]) == {"kw": 25.0}
    assert json.loads(rows[0]["new_value"]) == {"kw": 30.0}

    # Third activation: only reserve floor changes → 1 audit row labelled battery_reserve_floor
    await repo.activate(_input(30.0, 25.0), actor="installer")
    async with conn.execute("SELECT field FROM config_audit_log WHERE config_version = 3") as cur:
        fields = [cast(aiosqlite.Row, r)[0] for r in await cur.fetchall()]
    assert fields == ["battery_reserve_floor"]


# AC10 #5
async def test_activate_with_no_changes_raises(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    await repo.activate(_input(25.0, 20.0), actor="installer")
    before_active = await _active_count(conn)
    before_audit = await _audit_count(conn)
    with pytest.raises(ValueError, match="no changed fields"):
        await repo.activate(_input(25.0, 20.0), actor="installer")
    # No DB write
    assert await _active_count(conn) == before_active
    assert await _audit_count(conn) == before_audit


# AC10 #6
async def test_activate_with_invalid_actor_raises_no_db_write(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    with pytest.raises(ValueError, match="Invalid actor"):
        await repo.activate(_input(25.0, 20.0), actor="bogus")  # type: ignore[arg-type]
    assert await _active_count(conn) == 0
    assert await _audit_count(conn) == 0


# AC10 #7 + #24 — atomicity under audit-insert failure
async def test_activate_atomic_under_audit_failure(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    # Hand the repo a stub audit_repo that raises mid-transaction
    audit_stub = AsyncMock()
    audit_stub.append_activation = AsyncMock(side_effect=RuntimeError("audit explode"))
    repo_with_failing_audit = ConfigRepo(conn, audit_repo=audit_stub)

    with pytest.raises(RuntimeError, match="audit explode"):
        await repo_with_failing_audit.activate(_input(25.0, 20.0), actor="installer")

    # Both tables must be unchanged — atomicity verified
    assert await _active_count(conn) == 0
    assert await _audit_count(conn) == 0


# AC10 #8 — DB-side CHECK guard for peak_limit_kw
async def test_db_check_constraint_rejects_zero_peak_limit_at_db_layer(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    # Pydantic catches it earlier — bypass with a raw INSERT to verify the DB CHECK
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(
            "INSERT INTO active_constraints"
            " (peak_limit_kw, battery_reserve_floor_percent, config_version,"
            "  activated_at, actor) VALUES (?, ?, ?, ?, ?)",
            (0.0, 50.0, 1, "2026-05-10T00:00:00+00:00", "system"),
        )


# AC10 #9
@pytest.mark.parametrize("n", [5])
async def test_config_version_monotonic_across_n_activations(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
    n: int,
) -> None:
    repo, _ = repo_with_conn
    versions: list[int] = []
    for i in range(n):
        v = await repo.activate(_input(25.0 + i, 20.0), actor="installer")
        versions.append(v)
    assert versions == list(range(1, n + 1))


async def test_get_active_returns_active_constraints_typed_model(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.activate(_input(25.0, 20.0), actor="system")
    active = await repo.get_active()
    assert active is not None
    # Pydantic frozen invariants preserved on retrieval
    assert active.activated_at.tzinfo is not None
    assert active.activated_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


async def test_actor_value_is_persisted_on_active_row(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    await repo.activate(_input(25.0, 20.0), actor="installer")
    async with conn.execute("SELECT actor FROM active_constraints") as cur:
        rows = list(await cur.fetchall())
    assert rows[0]["actor"] == "installer"


# P11 (D5 resolution): a row with a naive `activated_at` string (operator-edited
# DB) causes `get_active` to fail loud — but the error message must include the
# row's id and config_version so the operator can locate the bad row.
async def test_get_active_raises_with_row_diagnostics_on_naive_timestamp(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    # Bypass the typed write path: directly INSERT a row with a naive ISO
    # timestamp (no '+00:00' suffix). The DB CHECK constraints don't enforce
    # timezone-awareness on the TEXT column, so this is the exact shape of
    # operator-edit-then-restart bad data.
    await conn.execute(
        "INSERT INTO active_constraints"
        " (peak_limit_kw, battery_reserve_floor_percent,"
        "  config_version, activated_at, actor)"
        " VALUES (?, ?, ?, ?, ?)",
        (25.0, 20.0, 42, "2026-05-10T12:00:00", "system"),  # NB: no timezone
    )
    await conn.commit()

    with pytest.raises(ValueError) as exc_info:
        await repo.get_active()

    message = str(exc_info.value)
    # The operator needs to be able to grep for the bad row's id AND version.
    assert "config_version=42" in message
    assert "2026-05-10T12:00:00" in message
    # id should be present; SQLite AUTOINCREMENT starts at 1.
    assert "id=1" in message


# P8 (D2 resolution): config_version assignment is monotonic across BOTH
# active_constraints AND config_audit_log. If active_constraints is truncated
# (operator-edited DB / restore-from-empty-backup), the next activate() must
# resume from the audit log's high-water mark, not regress to v=1.
async def test_activate_uses_cross_table_max_for_next_config_version(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    # Activate three times: versions 1, 2, 3.
    await repo.activate(_input(25.0, 20.0), actor="installer")
    await repo.activate(_input(26.0, 21.0), actor="installer")
    await repo.activate(_input(27.0, 22.0), actor="installer")

    # Operator manually deletes ALL active_constraints rows. The audit log
    # retains config_versions 1..3 because it is append-only by contract.
    await conn.execute("DELETE FROM active_constraints")
    await conn.commit()

    # Next activate() must NOT regress to version 1 — that would break the
    # cross-table monotonicity contract and silently mask the regression.
    v = await repo.activate(_input(28.0, 23.0), actor="installer")
    assert v == 4, (
        f"Expected next config_version=4 (audit-log max + 1); got {v}. "
        "Cross-table monotonic version assignment is broken."
    )


# ---------------------------------------------------------------------------
# Story 9.3 — EV charging window audit emission
# ---------------------------------------------------------------------------


def _input_with_window(
    peak: float = 25.0,
    floor: float = 20.0,
    *,
    start: str | None = None,
    end: str | None = None,
) -> ActiveConstraintsInput:
    return ActiveConstraintsInput(
        peak_limit_kw=peak,
        battery_reserve_floor_percent=floor,
        ev_charging_window_start=start,
        ev_charging_window_end=end,
    )


async def test_ev_window_only_change_emits_one_combined_audit_row(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    """Story 9.3 AC2: an EV-window-only change emits exactly ONE audit row
    with ``field='ev_charging_window'`` containing both endpoints.
    """
    repo, conn = repo_with_conn
    # First activation: peak + floor change (window is NULL on both sides).
    await repo.activate(_input(25.0, 20.0), actor="installer")
    # Second activation: only EV window changes (start=NULL → 09:00, end=NULL → 17:00).
    await repo.activate(
        _input_with_window(25.0, 20.0, start="09:00", end="17:00"), actor="installer"
    )
    async with conn.execute(
        "SELECT field, previous_value, new_value FROM config_audit_log WHERE config_version = 2"
    ) as cur:
        rows = list(await cur.fetchall())
    assert len(rows) == 1
    assert rows[0]["field"] == "ev_charging_window"
    assert json.loads(rows[0]["previous_value"]) == {"start": None, "end": None}
    assert json.loads(rows[0]["new_value"]) == {"start": "09:00", "end": "17:00"}


async def test_all_three_field_change_shares_single_config_version(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    """Story 9.3 AC2: peak + floor + EV window change in one activate() emits
    exactly THREE audit rows with the same config_version.
    """
    repo, conn = repo_with_conn
    await repo.activate(_input(25.0, 20.0), actor="installer")
    await repo.activate(
        _input_with_window(30.0, 25.0, start="09:00", end="17:00"), actor="installer"
    )
    async with conn.execute(
        "SELECT field, config_version FROM config_audit_log WHERE config_version = 2 ORDER BY field"
    ) as cur:
        rows = list(await cur.fetchall())
    assert len(rows) == 3
    assert sorted(r["field"] for r in rows) == [
        "battery_reserve_floor",
        "ev_charging_window",
        "peak_consumption_limit",
    ]
    assert {r["config_version"] for r in rows} == {2}


async def test_unchanged_ev_window_emits_no_audit_row(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    """The combined window row only fires when start OR end actually changes."""
    repo, conn = repo_with_conn
    await repo.activate(
        _input_with_window(25.0, 20.0, start="09:00", end="17:00"), actor="installer"
    )
    # Now change only peak_limit; window stays the same.
    await repo.activate(
        _input_with_window(30.0, 20.0, start="09:00", end="17:00"), actor="installer"
    )
    async with conn.execute("SELECT field FROM config_audit_log WHERE config_version = 2") as cur:
        fields = [cast(aiosqlite.Row, r)[0] for r in await cur.fetchall()]
    assert fields == ["peak_consumption_limit"]


async def test_ev_window_round_trips_through_active_constraints(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.activate(
        _input_with_window(25.0, 20.0, start="09:00", end="17:00"), actor="installer"
    )
    active = await repo.get_active()
    assert active is not None
    assert active.ev_charging_window_start == "09:00"
    assert active.ev_charging_window_end == "17:00"


async def test_activate_locked_does_not_acquire_lock(
    repo_with_conn: tuple[ConfigRepo, aiosqlite.Connection],
) -> None:
    """Story 9.3: ``_activate_locked`` MUST NOT acquire ``get_write_lock``;
    callers (the staged-activation route) own that lock. Verify by asserting
    the lock can be held by the caller while ``_activate_locked`` runs.
    """
    from open_ems.storage.database import get_write_lock

    repo, _ = repo_with_conn
    async with get_write_lock():
        version = await repo._activate_locked(_input(25.0, 20.0), actor="installer")
    assert version == 1
