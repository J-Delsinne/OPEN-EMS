from __future__ import annotations

import asyncio
import contextlib

import aiosqlite
import pytest

from open_ems.storage.repositories.user_repo import (
    MIN_PASSWORD_LENGTH,
    UserRepo,
    hash_password,
    validate_password_complexity,
    verify_password,
)


class _CountingLock:
    """Async-context-manager lock proxy that counts acquire/release pairs.

    Wraps the real :class:`asyncio.Lock` so behavior is unchanged (locking still
    serializes); tests inspect ``acquire_count`` after the action.
    """

    def __init__(self) -> None:
        self._inner = asyncio.Lock()
        self.acquire_count = 0
        self.release_count = 0

    async def __aenter__(self) -> _CountingLock:
        await self._inner.acquire()
        self.acquire_count += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._inner.release()
        self.release_count += 1


@contextlib.contextmanager
def _patched_write_lock(monkeypatch: pytest.MonkeyPatch, module: object) -> object:
    """Monkeypatch the module's ``get_write_lock`` to return a counting lock.

    Yields the counting lock so the test can assert acquire/release counts.
    """
    counter = _CountingLock()
    monkeypatch.setattr(module, "get_write_lock", lambda: counter)
    yield counter


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


async def test_get_by_id_returns_row(user_repo: UserRepo) -> None:
    uid = await user_repo.create("admin", hash_password("secret"), "installer")
    row = await user_repo.get_by_id(uid)
    assert row is not None
    assert str(row["id"]) == uid
    assert str(row["username"]) == "admin"
    assert str(row["role"]) == "installer"


async def test_get_by_id_unknown_returns_none(user_repo: UserRepo) -> None:
    row = await user_repo.get_by_id("nonexistent-id")
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


# ─── Story 11.3 — password complexity helper (AC7, AC22 #1-3) ─────────────────


def test_validate_password_complexity_accepts_exactly_12_chars() -> None:
    """AC7: a 12-character password passes the v1 complexity rule."""
    assert validate_password_complexity("a" * MIN_PASSWORD_LENGTH) is None


def test_validate_password_complexity_rejects_11_chars_with_clear_message() -> None:
    """AC7: an 11-character password fails with a user-facing error message."""
    result = validate_password_complexity("a" * (MIN_PASSWORD_LENGTH - 1))
    assert result is not None
    assert "12 characters" in result


def test_validate_password_complexity_accepts_long_unicode() -> None:
    """AC7: a 12+ codepoint Unicode password passes — character count, not byte count.

    The bcrypt 72-byte ceiling is a separate check at hash time. The complexity
    helper counts ``len(password)`` (Python ``str`` = codepoints).
    """
    assert validate_password_complexity("héllo-wörld!" + "x" * 5) is None


# ─── Story 11.3 — get_homeowner (AC10, AC22 #4-6) ─────────────────────────────


async def test_get_homeowner_returns_none_when_no_homeowner_exists(
    user_repo: UserRepo,
) -> None:
    """AC10: empty users table → ``get_homeowner()`` is None."""
    await user_repo.create("admin", hash_password("password1234"), "installer")
    assert await user_repo.get_homeowner() is None


async def test_get_homeowner_returns_row_when_one_exists(user_repo: UserRepo) -> None:
    """AC10: a single homeowner row is returned with the expected username."""
    await user_repo.create("admin", hash_password("password1234"), "installer")
    await user_repo.create("home", hash_password("password1234"), "homeowner")
    row = await user_repo.get_homeowner()
    assert row is not None
    assert row["username"] == "home"
    assert row["role"] == "homeowner"


async def test_get_homeowner_returns_first_by_created_at_when_multiple_exist(
    user_repo: UserRepo,
) -> None:
    """AC10: defensive — when the v1 single-homeowner invariant is violated via
    direct DB manipulation, ``get_homeowner()`` returns the earliest row
    deterministically (``ORDER BY created_at ASC LIMIT 1``).
    """
    # First homeowner — created_at = '2026-01-01T00:00:00+00:00' via fixture timing
    conn = user_repo._conn  # noqa: SLF001 — direct INSERT bypasses the route guard
    await conn.execute(
        "INSERT INTO users"
        " (id, username, role, hashed_password, must_change_password, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("h1", "first", "homeowner", "$2b$x", 0, "2026-01-01T00:00:00+00:00"),
    )
    await conn.execute(
        "INSERT INTO users"
        " (id, username, role, hashed_password, must_change_password, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("h2", "second", "homeowner", "$2b$y", 0, "2026-02-01T00:00:00+00:00"),
    )
    await conn.commit()

    row = await user_repo.get_homeowner()
    assert row is not None
    assert row["username"] == "first"


# ─── Story 11.3 — update_password kwarg (AC9, AC22 #7-8) ──────────────────────


async def test_update_password_clears_must_change_password_by_default(
    user_repo: UserRepo,
) -> None:
    """AC9: ``update_password()`` without the kwarg clears the flag (self-change path)."""
    uid = await user_repo.create("user", hash_password("password1234"), "homeowner")
    await user_repo.update_password(uid, hash_password("newpassword12"))
    row = await user_repo.get_by_id(uid)
    assert row is not None
    assert row["must_change_password"] == 0


async def test_update_password_sets_must_change_password_when_kwarg_true(
    user_repo: UserRepo,
) -> None:
    """AC9: passing ``must_change_password=True`` re-applies the flag (reset path)."""
    uid = await user_repo.create("user", hash_password("password1234"), "homeowner")
    # Simulate prior self-change clearing the flag
    await user_repo.update_password(uid, hash_password("midpassword12"))
    row_after_self_change = await user_repo.get_by_id(uid)
    assert row_after_self_change is not None
    assert row_after_self_change["must_change_password"] == 0

    # Installer reset re-applies the flag
    await user_repo.update_password(
        uid, hash_password("resetpassword12"), must_change_password=True
    )
    row_after_reset = await user_repo.get_by_id(uid)
    assert row_after_reset is not None
    assert row_after_reset["must_change_password"] == 1


# ─── Story 11.3 — write-lock discipline (AC6, AC22 #9-10) ─────────────────────


async def test_create_user_acquires_write_lock(
    user_repo: UserRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6: ``UserRepo.create`` serialises through ``get_write_lock()``."""
    from open_ems.storage.repositories import user_repo as user_repo_module

    with _patched_write_lock(monkeypatch, user_repo_module) as counter:
        await user_repo.create("a", hash_password("password1234"), "installer")
    assert counter.acquire_count == 1
    assert counter.release_count == 1


async def test_update_password_acquires_write_lock(
    user_repo: UserRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6: ``UserRepo.update_password`` serialises through ``get_write_lock()``."""
    from open_ems.storage.repositories import user_repo as user_repo_module

    uid = await user_repo.create("a", hash_password("password1234"), "installer")

    with _patched_write_lock(monkeypatch, user_repo_module) as counter:
        await user_repo.update_password(uid, hash_password("newpassword12"))
    assert counter.acquire_count == 1
    assert counter.release_count == 1
