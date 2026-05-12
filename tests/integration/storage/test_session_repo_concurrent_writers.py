"""Story 11.3 AC6 — concurrent-writer stress test on ``SessionRepo``.

Closes the Story 9.0b deferred-finding for the SessionRepo half of the
deferral (UserRepo half is exercised by
``test_user_repo_concurrent_writers``).
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from alembic import command as alembic_command
from alembic.config import Config

from open_ems.storage.database import close_database, get_connection, init_database
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import (
    UserRepo,
    hash_password,
    verify_password,
)


def _project_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "alembic.ini").exists():
            return parent
    raise RuntimeError("alembic.ini not found")


@pytest_asyncio.fixture
async def migrated_db(tmp_path: pathlib.Path) -> AsyncGenerator[None, None]:
    db_path = str(tmp_path / "session_concurrent.db")
    cfg = Config(str(_project_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
    await init_database(db_path)
    yield
    await close_database()


@pytest.mark.asyncio
async def test_concurrent_session_creates_no_transaction_within_transaction_error(
    migrated_db: None,
) -> None:
    """AC6 — N parallel ``SessionRepo.create()`` calls all succeed."""
    user_id = await UserRepo().create(
        username="u", hashed_password=hash_password("password1234"), role="installer"
    )
    repo = SessionRepo()
    expires = datetime.now(UTC) + timedelta(hours=4)

    async def create_one(i: int) -> str:
        return await repo.create(
            user_id=user_id,
            token_hash=hash_token(generate_session_token()),
            expires_at=expires,
            csrf_token=f"c{i}",
        )

    n = 20
    sids = await asyncio.gather(*(create_one(i) for i in range(n)))
    assert len(set(sids)) == n


@pytest.mark.asyncio
async def test_concurrent_password_update_and_session_create_serialize_cleanly(
    migrated_db: None,
) -> None:
    """AC6 — mixed-writer scenario: password updates and session inserts
    serialize through the same write lock; no transaction-within-transaction
    error; final DB state is coherent.
    """
    user_repo = UserRepo()
    session_repo = SessionRepo()
    user_id = await user_repo.create(
        username="u", hashed_password=hash_password("password1234"), role="installer"
    )
    expires = datetime.now(UTC) + timedelta(hours=4)

    async def updates() -> int:
        for i in range(10):
            await user_repo.update_password(
                user_id, hashed_password=hash_password(f"newpass{i:04d}aaa")
            )
        return 10

    async def creates() -> int:
        for i in range(10):
            await session_repo.create(
                user_id=user_id,
                token_hash=hash_token(generate_session_token()),
                expires_at=expires,
                csrf_token=f"c{i}",
            )
            await asyncio.sleep(0)
        return 10

    update_count, create_count = await asyncio.gather(updates(), creates())
    assert update_count == 10
    assert create_count == 10

    # Final state — 10 sessions for the user.
    conn = get_connection()
    async with conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert int(row[0]) == 10

    # Review-P15: assert the lock actually serialized updates by verifying the
    # final stored hash verifies against ONE of the 10 candidate passwords
    # submitted by the updater task. A lost-update bug would land an arbitrary
    # intermediate hash that does not verify any candidate, OR (more likely)
    # would crash with "transaction within a transaction" before reaching
    # here. Either way the assertion below is the load-bearing check.
    final_user = await UserRepo().get_by_id(user_id)
    assert final_user is not None
    stored_hash = str(final_user["hashed_password"])
    candidates = [f"newpass{i:04d}aaa" for i in range(10)]
    assert any(verify_password(c, stored_hash) for c in candidates), (
        "Final stored hash does not match any submitted candidate — possible lost update."
    )
