from __future__ import annotations

import pathlib
from collections.abc import AsyncGenerator, Generator

import aiosqlite
import pytest
import pytest_asyncio

from open_ems.services.readiness import reset as readiness_reset
from open_ems.storage.database import close_database, get_connection, init_database


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
