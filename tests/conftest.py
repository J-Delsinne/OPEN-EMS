from __future__ import annotations

import pathlib
from collections.abc import AsyncGenerator, Generator

import aiosqlite
import pytest
import pytest_asyncio

from open_ems.services.readiness import reset as readiness_reset
from open_ems.storage.database import close_database, get_connection, init_database
from open_ems.storage.repositories.user_repo import UserRepo

# Single source of truth for the users table schema used in unit tests.
# Must stay in sync with migrations/versions/0002_add_users_table.py.
CREATE_USERS_TABLE_DDL = """
    CREATE TABLE users (
        id TEXT PRIMARY KEY NOT NULL,
        username TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL CHECK (role IN ('installer', 'homeowner')),
        hashed_password TEXT NOT NULL,
        must_change_password INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
"""


@pytest.fixture(autouse=True)
def _set_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-32-chars-xxxxxxxxxx")


@pytest.fixture
def tmp_db_path(tmp_path: pathlib.Path) -> str:
    """Temporary SQLite file path for tests that need an on-disk DB."""
    return str(tmp_path / "test.db")


@pytest_asyncio.fixture
async def initialized_db(tmp_db_path: str) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Initialized aiosqlite connection with WAL mode; cleaned up after each test."""
    await init_database(tmp_db_path)
    yield get_connection()
    await close_database()


@pytest_asyncio.fixture
async def user_repo(tmp_db_path: str) -> AsyncGenerator[UserRepo, None]:
    """Empty UserRepo backed by a fresh in-memory-like SQLite DB with the users table."""
    await init_database(tmp_db_path)
    conn = get_connection()
    await conn.execute(CREATE_USERS_TABLE_DDL)
    await conn.commit()
    yield UserRepo()
    await close_database()


@pytest.fixture(autouse=False)
def clean_readiness_state() -> Generator[None, None, None]:
    """Reset readiness flag before (and after) a test."""
    readiness_reset()
    yield
    readiness_reset()


@pytest_asyncio.fixture(autouse=False)
async def clean_db_state() -> AsyncGenerator[None, None]:
    """Ensure database connection is reset before (and after) a test.

    Use this in tests that require _connection to start as None.
    """
    await close_database()
    yield
    await close_database()
