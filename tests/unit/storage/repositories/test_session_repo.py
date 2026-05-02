from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
import pytest_asyncio

from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password


@pytest_asyncio.fixture
async def user_id(session_repo: SessionRepo) -> str:
    repo = UserRepo(get_connection())
    return await repo.create(
        username="testuser",
        hashed_password=hash_password("testpass"),
        role="installer",
    )


def _expires() -> datetime:
    return datetime.now(UTC) + timedelta(hours=4)


# ── Helper token tests ────────────────────────────────────────────────────────


def test_generate_session_token_length() -> None:
    token = generate_session_token()
    assert len(token) == 64  # 32 bytes → 64 hex chars


def test_generate_session_token_unique() -> None:
    assert generate_session_token() != generate_session_token()


def test_hash_token_deterministic() -> None:
    token = generate_session_token()
    assert hash_token(token) == hash_token(token)


def test_hash_token_is_hex() -> None:
    digest = hash_token("some-token")
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not valid hex


def test_raw_token_not_equal_to_hash() -> None:
    token = generate_session_token()
    assert token != hash_token(token)


# ── SessionRepo tests ─────────────────────────────────────────────────────────


async def test_create_returns_id(session_repo: SessionRepo, user_id: str) -> None:
    sid = await session_repo.create(
        user_id=user_id,
        token_hash=hash_token(generate_session_token()),
        expires_at=_expires(),
        csrf_token="tok1",
    )
    assert sid and isinstance(sid, str)


async def test_get_by_token_hash_found(session_repo: SessionRepo, user_id: str) -> None:
    raw = generate_session_token()
    th = hash_token(raw)
    await session_repo.create(
        user_id=user_id, token_hash=th, expires_at=_expires(), csrf_token="tok2"
    )
    row = await session_repo.get_by_token_hash(th)
    assert row is not None
    assert row["user_id"] == user_id


async def test_get_by_token_hash_not_found(session_repo: SessionRepo) -> None:
    row = await session_repo.get_by_token_hash("nonexistent-hash")
    assert row is None


async def test_token_hash_unique_constraint(session_repo: SessionRepo, user_id: str) -> None:
    th = hash_token(generate_session_token())
    await session_repo.create(
        user_id=user_id, token_hash=th, expires_at=_expires(), csrf_token="tok3"
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await session_repo.create(
            user_id=user_id, token_hash=th, expires_at=_expires(), csrf_token="tok3"
        )


async def test_delete_by_id(session_repo: SessionRepo, user_id: str) -> None:
    th = hash_token(generate_session_token())
    sid = await session_repo.create(
        user_id=user_id, token_hash=th, expires_at=_expires(), csrf_token="tok4"
    )
    await session_repo.delete_by_id(sid)
    assert await session_repo.get_by_token_hash(th) is None


async def test_delete_all_for_user(session_repo: SessionRepo, user_id: str) -> None:
    # Create a second user to verify their sessions are NOT deleted
    other_id = await UserRepo(get_connection()).create(
        username="other",
        hashed_password=hash_password("pass"),
        role="homeowner",
    )
    th1 = hash_token(generate_session_token())
    th2 = hash_token(generate_session_token())
    th_other = hash_token(generate_session_token())
    await session_repo.create(
        user_id=user_id, token_hash=th1, expires_at=_expires(), csrf_token="tok5"
    )
    await session_repo.create(
        user_id=user_id, token_hash=th2, expires_at=_expires(), csrf_token="tok6"
    )
    await session_repo.create(
        user_id=other_id, token_hash=th_other, expires_at=_expires(), csrf_token="tok7"
    )

    await session_repo.delete_all_for_user(user_id)

    assert await session_repo.get_by_token_hash(th1) is None
    assert await session_repo.get_by_token_hash(th2) is None
    assert await session_repo.get_by_token_hash(th_other) is not None


async def test_update_last_active(session_repo: SessionRepo, user_id: str) -> None:
    th = hash_token(generate_session_token())
    sid = await session_repo.create(
        user_id=user_id, token_hash=th, expires_at=_expires(), csrf_token="tok8"
    )
    row_before = await session_repo.get_by_token_hash(th)
    assert row_before is not None
    old_ts = row_before["last_active_at"]

    await session_repo.update_last_active(sid)
    row_after = await session_repo.get_by_token_hash(th)
    assert row_after is not None
    # Timestamps are ISO strings — updated one is >= original
    assert row_after["last_active_at"] >= old_ts


async def test_update_last_active_raises_on_missing_id(
    session_repo: SessionRepo,
) -> None:
    with pytest.raises(ValueError, match="No session found"):
        await session_repo.update_last_active("nonexistent-id")


# ── SessionRepo.touch tests ───────────────────────────────────────────────────


async def test_touch_updates_last_active_and_expires_at(
    session_repo: SessionRepo, user_id: str
) -> None:
    th = hash_token(generate_session_token())
    sid = await session_repo.create(
        user_id=user_id, token_hash=th, expires_at=_expires(), csrf_token="tok-touch"
    )
    row_before = await session_repo.get_by_token_hash(th)
    assert row_before is not None
    old_last_active = row_before["last_active_at"]
    old_expires = row_before["expires_at"]

    new_exp = datetime.now(UTC) + timedelta(hours=8)
    await session_repo.touch(sid, new_exp)

    row_after = await session_repo.get_by_token_hash(th)
    assert row_after is not None
    assert row_after["last_active_at"] >= old_last_active
    assert row_after["expires_at"] != old_expires
    assert row_after["expires_at"] == new_exp.isoformat()


async def test_touch_raises_on_missing_session(session_repo: SessionRepo) -> None:
    with pytest.raises(ValueError, match="No session found"):
        await session_repo.touch("nonexistent-id", datetime.now(UTC) + timedelta(hours=1))


# ── SessionRepo.delete_expired_for_user tests ─────────────────────────────────


async def test_delete_expired_for_user_removes_only_expired(
    session_repo: SessionRepo, user_id: str
) -> None:
    past = datetime.now(UTC) - timedelta(hours=5)
    future = datetime.now(UTC) + timedelta(hours=5)

    th_expired = hash_token(generate_session_token())
    th_valid = hash_token(generate_session_token())

    await session_repo.create(
        user_id=user_id, token_hash=th_expired, expires_at=past, csrf_token="c1"
    )
    await session_repo.create(
        user_id=user_id, token_hash=th_valid, expires_at=future, csrf_token="c2"
    )

    await session_repo.delete_expired_for_user(user_id)

    assert await session_repo.get_by_token_hash(th_expired) is None
    assert await session_repo.get_by_token_hash(th_valid) is not None


async def test_delete_expired_for_user_returns_count(
    session_repo: SessionRepo, user_id: str
) -> None:
    past = datetime.now(UTC) - timedelta(hours=5)
    for i in range(3):
        th = hash_token(generate_session_token())
        await session_repo.create(
            user_id=user_id, token_hash=th, expires_at=past, csrf_token=f"c{i}"
        )

    count = await session_repo.delete_expired_for_user(user_id)
    assert count == 3


# ── SessionRepo.delete_all_expired tests ──────────────────────────────────────


async def test_delete_all_expired_removes_across_users(
    session_repo: SessionRepo, user_id: str
) -> None:
    other_id = await UserRepo(get_connection()).create(
        username="other2",
        hashed_password=hash_password("pass"),
        role="homeowner",
    )
    past = datetime.now(UTC) - timedelta(hours=1)
    th1 = hash_token(generate_session_token())
    th2 = hash_token(generate_session_token())
    await session_repo.create(user_id=user_id, token_hash=th1, expires_at=past, csrf_token="c1")
    await session_repo.create(user_id=other_id, token_hash=th2, expires_at=past, csrf_token="c2")

    await session_repo.delete_all_expired()

    assert await session_repo.get_by_token_hash(th1) is None
    assert await session_repo.get_by_token_hash(th2) is None


async def test_delete_all_expired_returns_count(session_repo: SessionRepo, user_id: str) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    for i in range(2):
        th = hash_token(generate_session_token())
        await session_repo.create(
            user_id=user_id, token_hash=th, expires_at=past, csrf_token=f"c{i}"
        )

    count = await session_repo.delete_all_expired()
    assert count == 2


async def test_delete_all_expired_leaves_valid_sessions_intact(
    session_repo: SessionRepo, user_id: str
) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    future = datetime.now(UTC) + timedelta(hours=4)

    th_expired = hash_token(generate_session_token())
    th_valid = hash_token(generate_session_token())
    await session_repo.create(
        user_id=user_id, token_hash=th_expired, expires_at=past, csrf_token="c1"
    )
    await session_repo.create(
        user_id=user_id, token_hash=th_valid, expires_at=future, csrf_token="c2"
    )

    count = await session_repo.delete_all_expired()
    assert count == 1
    assert await session_repo.get_by_token_hash(th_expired) is None
    assert await session_repo.get_by_token_hash(th_valid) is not None
