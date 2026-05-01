from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from pydantic import SecretStr

from open_ems.storage.database import close_database, get_connection, init_database
from open_ems.storage.repositories.user_repo import UserRepo, verify_password
from open_ems.web.app import _bootstrap_admin_if_needed

_CREATE_USERS_TABLE = """
    CREATE TABLE users (
        id TEXT PRIMARY KEY NOT NULL,
        username TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL CHECK (role IN ('installer', 'homeowner')),
        hashed_password TEXT NOT NULL,
        must_change_password INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
"""


@pytest_asyncio.fixture
async def empty_user_repo(tmp_db_path: str) -> AsyncGenerator[UserRepo, None]:
    await init_database(tmp_db_path)
    conn = get_connection()
    await conn.execute(_CREATE_USERS_TABLE)
    await conn.commit()
    yield UserRepo()
    await close_database()


async def test_bootstrap_exits_when_no_password(empty_user_repo: UserRepo) -> None:
    with pytest.raises(SystemExit) as exc_info:
        await _bootstrap_admin_if_needed(empty_user_repo, None)
    assert exc_info.value.code == 1


async def test_bootstrap_creates_admin_user(empty_user_repo: UserRepo) -> None:
    password = SecretStr("bootstrap-secret")
    await _bootstrap_admin_if_needed(empty_user_repo, password)
    row = await empty_user_repo.get_by_username("admin")
    assert row is not None
    assert row["username"] == "admin"
    assert row["role"] == "installer"
    assert row["must_change_password"] == 1


async def test_bootstrap_stores_valid_hash(empty_user_repo: UserRepo) -> None:
    raw = "my-admin-pass"
    await _bootstrap_admin_if_needed(empty_user_repo, SecretStr(raw))
    row = await empty_user_repo.get_by_username("admin")
    assert row is not None
    assert verify_password(raw, row["hashed_password"])


async def test_bootstrap_skips_when_users_exist(empty_user_repo: UserRepo) -> None:
    await _bootstrap_admin_if_needed(empty_user_repo, SecretStr("first"))
    assert await empty_user_repo.count() == 1
    await _bootstrap_admin_if_needed(empty_user_repo, None)
    assert await empty_user_repo.count() == 1
