"""Story 11.3 AC6 — concurrent-writer stress test on ``UserRepo``.

Closes the explicit Story 9.0b deferred-finding (deferred-work.md line ~107):
*"UserRepo and SessionRepo writes are not yet routed through the
database-module write lock... add the lock when Epic 11 wires real homeowner
credential flows."*

Mirrors ``test_config_repo_concurrent_writers`` structurally: spawn N
concurrent writers; assert all succeed without
``OperationalError: cannot start a transaction within a transaction``.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from alembic import command as alembic_command
from alembic.config import Config

from open_ems.storage.database import close_database, get_connection, init_database
from open_ems.storage.repositories.user_repo import UserRepo, hash_password


def _project_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "alembic.ini").exists():
            return parent
    raise RuntimeError("alembic.ini not found")


@pytest_asyncio.fixture
async def migrated_db(tmp_path: pathlib.Path) -> AsyncGenerator[None, None]:
    db_path = str(tmp_path / "user_concurrent.db")
    cfg = Config(str(_project_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
    await init_database(db_path)
    yield
    await close_database()


@pytest.mark.asyncio
async def test_concurrent_create_users_no_transaction_within_transaction_error(
    migrated_db: None,
) -> None:
    """AC6 — N parallel ``UserRepo.create()`` calls all succeed."""
    repo = UserRepo()
    n = 20

    async def create_one(i: int) -> str:
        return await repo.create(
            username=f"user{i:02d}",
            hashed_password=hash_password(f"password{i:04d}"),
            role="installer" if i % 2 == 0 else "homeowner",
        )

    ids = await asyncio.gather(*(create_one(i) for i in range(n)))
    assert len(ids) == n
    assert len(set(ids)) == n  # all UUIDs distinct

    # Round-trip — every row is queryable.
    conn = get_connection()
    async with conn.execute("SELECT COUNT(*) FROM users") as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert int(row[0]) == n
