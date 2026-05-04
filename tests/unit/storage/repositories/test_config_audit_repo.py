from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import aiosqlite
import pytest
import pytest_asyncio

import open_ems.storage.repositories.config_audit_repo as config_audit_repo_module
from open_ems.storage.repositories.config_audit_repo import (
    SAFETY_RELEVANT_CONFIG_FIELDS,
    ConfigAuditChange,
    ConfigAuditRepo,
)

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

_NOW = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz: object = None) -> datetime:  # noqa: ANN401
        if tz is UTC:
            return _NOW
        return _NOW.replace(tzinfo=None)


@pytest_asyncio.fixture
async def repo_with_conn(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[ConfigAuditRepo, aiosqlite.Connection], None]:
    monkeypatch.setattr(config_audit_repo_module, "datetime", _FixedDateTime)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_CONFIG_AUDIT_LOG)
        await conn.commit()
        yield ConfigAuditRepo(conn), conn


async def _rows(conn: aiosqlite.Connection) -> list[aiosqlite.Row]:
    async with conn.execute("SELECT * FROM config_audit_log ORDER BY id") as cur:
        return list(await cur.fetchall())


async def _count_rows(conn: aiosqlite.Connection) -> int:
    async with conn.execute("SELECT COUNT(*) FROM config_audit_log") as cur:
        row = await cur.fetchone()
    return int(row[0])


def _change(
    field: str = "peak_consumption_limit",
    previous_value: dict[str, object] | None = None,
    new_value: dict[str, object] | None = None,
) -> ConfigAuditChange:
    return ConfigAuditChange(
        field=field,
        previous_value=previous_value or {"value": 3.0, "unit": "kW"},
        new_value=new_value or {"value": 3.5, "unit": "kW"},
    )


@pytest.mark.asyncio
async def test_append_activation_writes_structured_values_and_actor(
    repo_with_conn: tuple[ConfigAuditRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn

    version = await repo.append_activation(actor="installer", changes=[_change()])

    assert version == 1
    rows = await _rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "installer"
    assert row["timestamp"] == _NOW.isoformat()
    assert row["field"] == "peak_consumption_limit"
    assert json.loads(row["previous_value"]) == {"value": 3.0, "unit": "kW"}
    assert json.loads(row["new_value"]) == {"value": 3.5, "unit": "kW"}
    assert row["config_version"] == 1


@pytest.mark.asyncio
async def test_append_activation_uses_one_version_per_activation(
    repo_with_conn: tuple[ConfigAuditRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn

    version = await repo.append_activation(
        actor="installer",
        changes=[
            _change("peak_consumption_limit"),
            _change(
                "battery_reserve_floor",
                {"value": 20, "unit": "%"},
                {"value": 25, "unit": "%"},
            ),
        ],
    )

    assert version == 1
    rows = await _rows(conn)
    assert [row["config_version"] for row in rows] == [1, 1]


@pytest.mark.asyncio
async def test_append_activation_increments_version_per_activation(
    repo_with_conn: tuple[ConfigAuditRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn

    first = await repo.append_activation(actor="installer", changes=[_change()])
    second = await repo.append_activation(
        actor="installer", changes=[_change("energy_strategy_default")]
    )

    assert (first, second) == (1, 2)
    rows = await _rows(conn)
    assert [row["config_version"] for row in rows] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(SAFETY_RELEVANT_CONFIG_FIELDS))
async def test_all_safety_relevant_fields_are_accepted(
    repo_with_conn: tuple[ConfigAuditRepo, aiosqlite.Connection],
    field: str,
) -> None:
    repo, conn = repo_with_conn

    await repo.append_activation(actor="system", changes=[_change(field)])

    rows = await _rows(conn)
    assert rows[0]["field"] == field


@pytest.mark.asyncio
async def test_append_activation_rejects_empty_changes(
    repo_with_conn: tuple[ConfigAuditRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn

    with pytest.raises(ValueError, match="changes must be non-empty"):
        await repo.append_activation(actor="installer", changes=[])

    assert await _count_rows(conn) == 0


@pytest.mark.asyncio
async def test_append_activation_rejects_unknown_field_before_write(
    repo_with_conn: tuple[ConfigAuditRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn

    with pytest.raises(ValueError, match="Unknown config audit field"):
        await repo.append_activation(actor="installer", changes=[_change("not_safety_relevant")])

    assert await _count_rows(conn) == 0


@pytest.mark.asyncio
async def test_append_activation_rejects_unknown_field_in_mixed_batch_before_write(
    repo_with_conn: tuple[ConfigAuditRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn

    with pytest.raises(ValueError, match="Unknown config audit field"):
        await repo.append_activation(
            actor="installer",
            changes=[_change("peak_consumption_limit"), _change("not_safety_relevant")],
        )

    assert await _count_rows(conn) == 0


@pytest.mark.asyncio
async def test_append_activation_rejects_invalid_actor_before_write(
    repo_with_conn: tuple[ConfigAuditRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn

    with pytest.raises(ValueError, match="Invalid actor"):
        await repo.append_activation(actor="robot", changes=[_change()])

    assert await _count_rows(conn) == 0


@pytest.mark.asyncio
async def test_append_activation_rejects_invalid_json_before_write(
    repo_with_conn: tuple[ConfigAuditRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn

    with pytest.raises(ValueError, match="config audit values must be JSON-serializable"):
        await repo.append_activation(
            actor="installer",
            changes=[_change(new_value={"value": float("nan"), "unit": "kW"})],
        )

    assert await _count_rows(conn) == 0


@pytest.mark.asyncio
async def test_append_activation_rejects_invalid_json_in_mixed_batch_before_write(
    repo_with_conn: tuple[ConfigAuditRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn

    with pytest.raises(ValueError, match="config audit values must be JSON-serializable"):
        await repo.append_activation(
            actor="installer",
            changes=[
                _change("peak_consumption_limit"),
                _change("battery_reserve_floor", new_value={"value": float("nan"), "unit": "%"}),
            ],
        )

    assert await _count_rows(conn) == 0
