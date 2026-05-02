from __future__ import annotations

import aiosqlite
import pytest

from open_ems.storage.repositories.user_repo import UserRepo, hash_password, verify_password


async def test_count_empty(user_repo: UserRepo) -> None:
    assert await user_repo.count() == 0


async def test_create_returns_uuid(user_repo: UserRepo) -> None:
    uid = await user_repo.create("admin", hash_password("secret"), "installer")
    assert isinstance(uid, str)
    assert len(uid) > 0


async def test_create_increments_count(user_repo: UserRepo) -> None:
    await user_repo.create("admin", hash_password("secret"), "installer")
    assert await user_repo.count() == 1


async def test_username_unique_constraint(user_repo: UserRepo) -> None:
    await user_repo.create("admin", hash_password("secret"), "installer")
    with pytest.raises(aiosqlite.IntegrityError):
        await user_repo.create("admin", hash_password("other"), "installer")


async def test_create_invalid_role(user_repo: UserRepo) -> None:
    with pytest.raises(ValueError, match="Invalid role"):
        await user_repo.create("admin", hash_password("secret"), "superuser")


async def test_hash_not_plaintext(user_repo: UserRepo) -> None:
    raw = "mysecret"
    await user_repo.create("admin", hash_password(raw), "installer")
    row = await user_repo.get_by_username("admin")
    assert row is not None
    assert row["hashed_password"] != raw


async def test_hash_is_phc_format(user_repo: UserRepo) -> None:
    await user_repo.create("admin", hash_password("secret"), "installer")
    row = await user_repo.get_by_username("admin")
    assert row is not None
    assert row["hashed_password"].startswith("$2b$")


async def test_verify_password_correct(user_repo: UserRepo) -> None:
    raw = "correct_password"
    hashed = hash_password(raw)
    assert verify_password(raw, hashed) is True


async def test_verify_password_wrong(user_repo: UserRepo) -> None:
    hashed = hash_password("correct_password")
    assert verify_password("wrong", hashed) is False


async def test_verify_password_malformed_hash_returns_false(user_repo: UserRepo) -> None:
    assert verify_password("any", "not-a-valid-hash") is False


async def test_get_by_username_found(user_repo: UserRepo) -> None:
    await user_repo.create("admin", hash_password("secret"), "installer")
    row = await user_repo.get_by_username("admin")
    assert row is not None
    assert row["username"] == "admin"


async def test_get_by_username_not_found(user_repo: UserRepo) -> None:
    row = await user_repo.get_by_username("nonexistent")
    assert row is None


async def test_must_change_password_default_true(user_repo: UserRepo) -> None:
    await user_repo.create("admin", hash_password("secret"), "installer")
    row = await user_repo.get_by_username("admin")
    assert row is not None
    assert row["must_change_password"] == 1


async def test_update_password_changes_hash(user_repo: UserRepo) -> None:
    uid = await user_repo.create("admin", hash_password("old"), "installer")
    row_before = await user_repo.get_by_username("admin")
    assert row_before is not None
    old_hash = row_before["hashed_password"]

    await user_repo.update_password(uid, hash_password("new"))
    row_after = await user_repo.get_by_username("admin")
    assert row_after is not None
    assert row_after["hashed_password"] != old_hash


async def test_update_password_clears_flag(user_repo: UserRepo) -> None:
    uid = await user_repo.create("admin", hash_password("old"), "installer")
    new_hash = hash_password("new")
    await user_repo.update_password(uid, new_hash)
    row = await user_repo.get_by_username("admin")
    assert row is not None
    assert row["must_change_password"] == 0
    assert row["hashed_password"].startswith("$2b$")


async def test_update_password_nonexistent_user(user_repo: UserRepo) -> None:
    with pytest.raises(ValueError, match="No user found"):
        await user_repo.update_password("nonexistent-id", hash_password("new"))
