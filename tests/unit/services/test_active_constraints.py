"""Unit tests for ``ActiveConstraintsProvider`` — Story 9.0b AC4 + AC10 #10-#16."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import patch

import aiosqlite
import pytest
import pytest_asyncio

from open_ems.core.constraints import ActiveConstraintsInput
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.settings import Settings
from open_ems.storage.repositories.config_audit_repo import ConfigAuditRepo
from open_ems.storage.repositories.config_repo import ConfigRepo

_CREATE_ACTIVE_CONSTRAINTS = """
    CREATE TABLE active_constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        peak_limit_kw REAL NOT NULL CHECK (peak_limit_kw > 0),
        battery_reserve_floor_percent REAL NOT NULL
            CHECK (battery_reserve_floor_percent >= 0
                   AND battery_reserve_floor_percent <= 100),
        config_version INTEGER NOT NULL UNIQUE,
        activated_at TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('system', 'installer'))
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


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest_asyncio.fixture
async def conn_with_schema() -> AsyncGenerator[aiosqlite.Connection, None]:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_ACTIVE_CONSTRAINTS)
        await conn.execute(_CREATE_CONFIG_AUDIT_LOG)
        await conn.commit()
        yield conn


@pytest_asyncio.fixture
async def provider_repo(
    conn_with_schema: aiosqlite.Connection,
) -> AsyncGenerator[tuple[ActiveConstraintsProvider, ConfigRepo], None]:
    repo = ConfigRepo(conn_with_schema, audit_repo=ConfigAuditRepo(conn_with_schema))
    provider = ActiveConstraintsProvider(repo=repo, settings=_settings())
    yield provider, repo


# AC10 #10
async def test_get_before_hydrate_raises_runtime_error(
    provider_repo: tuple[ActiveConstraintsProvider, ConfigRepo],
) -> None:
    provider, _ = provider_repo
    with pytest.raises(RuntimeError, match="before hydrate"):
        provider.get()
    assert provider.is_hydrated is False


# AC10 #11
async def test_hydrate_empty_db_seeds_from_settings_with_version_zero(
    provider_repo: tuple[ActiveConstraintsProvider, ConfigRepo],
) -> None:
    provider, _ = provider_repo
    settings = _settings()

    with patch("open_ems.services.active_constraints.logger") as mock_logger:
        await provider.hydrate()

    assert provider.is_hydrated is True
    snapshot = provider.get()
    assert snapshot.config_version == 0
    assert snapshot.peak_limit_kw == settings.peak_limit_kw
    assert snapshot.battery_reserve_floor_percent == settings.battery_reserve_floor_percent
    # Structured event name asserted via direct logger mock — robust to whether
    # configure_logging() has been called elsewhere in the test session.
    seed_calls = [
        c
        for c in mock_logger.info.call_args_list
        if c.args and c.args[0] == "constraints_hydrate_seeded_from_settings"
    ]
    assert len(seed_calls) == 1


# AC10 #12
async def test_hydrate_with_db_row_uses_db_values(
    provider_repo: tuple[ActiveConstraintsProvider, ConfigRepo],
) -> None:
    provider, repo = provider_repo
    await repo.activate(
        ActiveConstraintsInput(peak_limit_kw=42.5, battery_reserve_floor_percent=15.0),
        actor="installer",
    )
    await provider.hydrate()
    snapshot = provider.get()
    assert snapshot.config_version == 1
    assert snapshot.peak_limit_kw == 42.5
    assert snapshot.battery_reserve_floor_percent == 15.0


# AC10 #13
async def test_hydrate_called_twice_is_noop_with_warning(
    provider_repo: tuple[ActiveConstraintsProvider, ConfigRepo],
) -> None:
    provider, _ = provider_repo
    await provider.hydrate()
    first = provider.get()

    with patch("open_ems.services.active_constraints.logger") as mock_logger:
        await provider.hydrate()

    second = provider.get()
    assert second is first  # snapshot unchanged
    warning_calls = [
        c
        for c in mock_logger.warning.call_args_list
        if c.args and c.args[0] == "constraints_provider_already_hydrated"
    ]
    assert len(warning_calls) == 1


# AC10 #14
async def test_reload_after_activate_replaces_snapshot_atomically(
    provider_repo: tuple[ActiveConstraintsProvider, ConfigRepo],
) -> None:
    provider, repo = provider_repo
    await repo.activate(
        ActiveConstraintsInput(peak_limit_kw=25.0, battery_reserve_floor_percent=20.0),
        actor="installer",
    )
    await provider.hydrate()
    old = provider.get()
    assert old.peak_limit_kw == 25.0

    await repo.activate(
        ActiveConstraintsInput(peak_limit_kw=40.0, battery_reserve_floor_percent=25.0),
        actor="installer",
    )
    await provider.reload()

    new = provider.get()
    assert new is not old  # snapshot replaced, not mutated
    assert new.peak_limit_kw == 40.0
    assert new.config_version == 2
    # Old reference is immutable proof — Pydantic frozen guarantees it
    assert old.peak_limit_kw == 25.0


# AC10 #15
async def test_reload_with_empty_db_post_hydrate_preserves_snapshot(
    provider_repo: tuple[ActiveConstraintsProvider, ConfigRepo],
    conn_with_schema: aiosqlite.Connection,
) -> None:
    provider, repo = provider_repo
    await repo.activate(
        ActiveConstraintsInput(peak_limit_kw=30.0, battery_reserve_floor_percent=10.0),
        actor="installer",
    )
    await provider.hydrate()
    before = provider.get()
    assert before.config_version == 1

    # Simulate a DB regression: delete the row out from under the provider.
    await conn_with_schema.execute("DELETE FROM active_constraints")
    await conn_with_schema.commit()

    with patch("open_ems.services.active_constraints.logger") as mock_logger:
        await provider.reload()

    after = provider.get()
    assert after is before  # snapshot unchanged
    warning_calls = [
        c
        for c in mock_logger.warning.call_args_list
        if c.args and c.args[0] == "constraints_reload_found_empty"
    ]
    assert len(warning_calls) == 1


# AC10 #16
async def test_concurrent_reload_is_serialized(
    provider_repo: tuple[ActiveConstraintsProvider, ConfigRepo],
) -> None:
    provider, repo = provider_repo
    await repo.activate(
        ActiveConstraintsInput(peak_limit_kw=25.0, battery_reserve_floor_percent=20.0),
        actor="installer",
    )
    await provider.hydrate()

    # Two concurrent reloads must complete without raising; the lock serializes them.
    await asyncio.gather(provider.reload(), provider.reload())

    snapshot = provider.get()
    assert snapshot.config_version == 1


async def test_is_hydrated_starts_false(
    provider_repo: tuple[ActiveConstraintsProvider, ConfigRepo],
) -> None:
    provider, _ = provider_repo
    assert provider.is_hydrated is False
    await provider.hydrate()
    assert provider.is_hydrated is True


async def test_get_returns_immutable_pydantic_instance(
    provider_repo: tuple[ActiveConstraintsProvider, ConfigRepo],
) -> None:
    provider, _ = provider_repo
    await provider.hydrate()
    snapshot = provider.get()
    # frozen=True — direct mutation must raise pydantic ValidationError.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        snapshot.peak_limit_kw = 999.0  # type: ignore[misc]


# P9 (D3 resolution): reload() rejects a DB row whose config_version is LOWER
# than the cached snapshot (post-backup-restore, manual DELETE of latest row).
# The cached snapshot is preserved and a warning is emitted; downstream
# consumers tagging events with config_version see a monotonic sequence.
async def test_reload_with_lower_db_version_preserves_cached_snapshot(
    provider_repo: tuple[ActiveConstraintsProvider, ConfigRepo],
    conn_with_schema: aiosqlite.Connection,
) -> None:
    provider, repo = provider_repo
    # Hydrate at version 1.
    await repo.activate(
        ActiveConstraintsInput(peak_limit_kw=25.0, battery_reserve_floor_percent=20.0),
        actor="installer",
    )
    # Activate version 2 so we have something to delete down to.
    await repo.activate(
        ActiveConstraintsInput(peak_limit_kw=30.0, battery_reserve_floor_percent=22.0),
        actor="installer",
    )
    await provider.hydrate()
    cached = provider.get()
    assert cached.config_version == 2

    # Simulate the regression: operator manually deletes the v=2 row, leaving v=1 highest.
    await conn_with_schema.execute("DELETE FROM active_constraints WHERE config_version = 2")
    await conn_with_schema.commit()

    with patch("open_ems.services.active_constraints.logger") as mock_logger:
        await provider.reload()

    # Snapshot preserved at v=2 — the lower-version DB row is rejected.
    after = provider.get()
    assert after is cached
    assert after.config_version == 2
    regression_warnings = [
        c
        for c in mock_logger.warning.call_args_list
        if c.args and c.args[0] == "constraints_reload_version_regressed"
    ]
    assert len(regression_warnings) == 1
    # Warning carries both versions for forensic correlation.
    kwargs = regression_warnings[0].kwargs
    assert kwargs["cached_config_version"] == 2
    assert kwargs["db_config_version"] == 1
    assert provider.is_hydrated is True
