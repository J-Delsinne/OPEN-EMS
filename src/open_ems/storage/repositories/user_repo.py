from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

import aiosqlite
import bcrypt as _bcrypt

from open_ems.storage.database import get_connection, get_write_lock

_BCRYPT_MAX_BYTES = 72

_VALID_ROLES: frozenset[str] = frozenset({"installer", "homeowner"})

# Story 11.3 AC7 — server-authoritative password complexity rule.
# Length-only, NIST SP 800-63B §5.1.1.2-aligned (no character-class composition).
MIN_PASSWORD_LENGTH: Final[int] = 12

_PASSWORD_TOO_SHORT_MESSAGE: Final[str] = (
    f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
)


class PasswordTooLongError(ValueError):
    """Raised by :func:`hash_password` when the UTF-8 encoding exceeds bcrypt's
    72-byte ceiling. ValueError subclass so legacy except-ValueError catches
    keep working while new code can narrow to the specific byte-length error.
    """


class RoleAlreadyExistsError(Exception):
    """Raised by :meth:`UserRepo.create` when ``enforce_role_uniqueness`` is set
    and the role-uniqueness re-check inside the write lock finds an existing
    row with the requested role. Closes the TOCTOU window between the route's
    pre-check and the INSERT.
    """


def validate_password_complexity(password: str) -> str | None:
    """Story 11.3 AC7 — server-authoritative password complexity check.

    Returns ``None`` if the password passes; otherwise returns a user-facing
    error message. The v1 rule is length-only ≥ 12 characters; bcrypt's
    72-byte UTF-8 ceiling is checked at hash time (raises ``ValueError`` from
    :func:`hash_password`) and surfaced separately by the route layer.

    All three credential-mutation routes (create-homeowner,
    reset-homeowner-password, change-password) reference this single function
    so the rule has one source of truth.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return _PASSWORD_TOO_SHORT_MESSAGE
    return None


# PHC-prefix → verifier callable. To add a new algorithm, append an entry here.
# Each callable receives (plain_password: str, hashed_password: str) → bool.
_HASHERS: dict[str, Callable[[str, str], bool]] = {
    "$2b$": lambda plain, hashed: bool(_bcrypt.checkpw(plain.encode(), hashed.encode())),
    "$2a$": lambda plain, hashed: bool(_bcrypt.checkpw(plain.encode(), hashed.encode())),
}


def hash_password(password: str) -> str:
    """Hash a password. Returns a bcrypt PHC self-describing string ($2b$...).

    Raises ValueError if the UTF-8 encoding exceeds bcrypt's 72-byte limit.
    """
    encoded = password.encode()
    if len(encoded) > _BCRYPT_MAX_BYTES:
        raise PasswordTooLongError(
            f"Password exceeds bcrypt's {_BCRYPT_MAX_BYTES}-byte limit "
            f"({len(encoded)} bytes encoded)."
        )
    return _bcrypt.hashpw(encoded, _bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plain text against a stored hash. Dispatches by PHC prefix.

    Returns False for unknown prefixes or malformed hashes — never raises.
    To add a new algorithm: insert its PHC prefix and verifier into _HASHERS.
    """
    for prefix, verifier in _HASHERS.items():
        if hashed_password.startswith(prefix):
            try:
                return verifier(plain_password, hashed_password)
            except ValueError:
                # bcrypt.checkpw raises ValueError on malformed/truncated hashes.
                return False
    return False


class UserRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    async def count(self) -> int:
        """Return total number of users."""
        async with self._conn.execute("SELECT COUNT(*) FROM users") as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def create(
        self,
        username: str,
        hashed_password: str,
        role: str,
        must_change_password: bool = True,
        *,
        enforce_role_uniqueness: str | None = None,
    ) -> str:
        """Create a user. Returns the new user ID (UUID4 string).

        Story 11.3 AC6 — write is serialized through the database-module write
        lock to honor the 9.0b "cannot start a transaction within a transaction"
        invariant under concurrent writers.

        ``enforce_role_uniqueness`` (Story 11.3 review-D1): if set to a role
        name, the method re-checks under the write lock that no existing row
        carries that role and raises :class:`RoleAlreadyExistsError` if one
        does. Closes the TOCTOU window between the route's pre-check and the
        INSERT (used by the single-homeowner invariant on
        ``/actions/create-homeowner``).
        """
        if role not in _VALID_ROLES:
            raise ValueError(f"Invalid role {role!r}. Must be one of {sorted(_VALID_ROLES)}")
        user_id = str(uuid.uuid4())
        created_at = datetime.now(UTC).isoformat()
        async with get_write_lock():
            if enforce_role_uniqueness is not None:
                async with self._conn.execute(
                    "SELECT 1 FROM users WHERE role = ? LIMIT 1",
                    (enforce_role_uniqueness,),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        raise RoleAlreadyExistsError(
                            f"A user with role={enforce_role_uniqueness!r} already exists."
                        )
            await self._conn.execute(
                "INSERT INTO users"
                " (id, username, role, hashed_password, must_change_password, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, role, hashed_password, int(must_change_password), created_at),
            )
            await self._conn.commit()
        return user_id

    async def get_by_username(self, username: str) -> aiosqlite.Row | None:
        """Fetch user row by username. Returns None if not found.

        Story 11.3 review-D2: ``COLLATE NOCASE`` makes the lookup case-
        insensitive, so ``Admin`` / ``ADMIN`` / ``admin`` resolve to the same
        row. Uniqueness checks (create-homeowner) and login lookup share this
        helper, so case-insensitive matching is symmetric across the two
        callers.
        """
        async with self._conn.execute(
            "SELECT id, username, role, hashed_password, must_change_password, created_at"
            " FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ) as cursor:
            return await cursor.fetchone()

    async def get_by_id(self, user_id: str) -> aiosqlite.Row | None:
        """Fetch user row by user ID. Returns None if not found."""
        async with self._conn.execute(
            "SELECT id, username, role, hashed_password, must_change_password, created_at"
            " FROM users WHERE id = ?",
            (user_id,),
        ) as cursor:
            return await cursor.fetchone()

    async def get_homeowner(self) -> aiosqlite.Row | None:
        """Story 11.3 AC10 — fetch the single homeowner user row, or None.

        v1 invariant: ``users`` table contains at most one row with
        ``role='homeowner'``. Multi-homeowner scenarios are out of scope for v1
        and would require an explicit migration + UX update — not a silent
        expansion of this method. ``ORDER BY created_at ASC LIMIT 1`` keeps
        the read deterministic if the invariant is violated via direct DB
        manipulation (the route layer rejects creation when one exists).
        """
        async with self._conn.execute(
            "SELECT id, username, role, hashed_password, must_change_password, created_at"
            " FROM users WHERE role = ? ORDER BY created_at ASC LIMIT 1",
            ("homeowner",),
        ) as cursor:
            return await cursor.fetchone()

    async def update_password(
        self,
        user_id: str,
        hashed_password: str,
        *,
        must_change_password: bool = False,
    ) -> None:
        """Replace hashed_password and set the must_change_password flag.

        Caller is responsible for passing a pre-hashed value (see
        :func:`hash_password`). The flag defaults to ``False`` — the historic
        self-change path (Story 11.3's ``/change-password`` route) clears it
        on successful update. Pass ``must_change_password=True`` for
        installer-initiated resets so the homeowner is forced through the
        change-password flow on next login.

        Raises ``ValueError`` if no user with the given user_id exists.

        Story 11.3 AC6 — write is serialized through the database-module write
        lock to honor the 9.0b transaction-isolation invariant.
        """
        async with get_write_lock():
            async with self._conn.execute(
                "UPDATE users SET hashed_password = ?, must_change_password = ? WHERE id = ?",
                (hashed_password, int(must_change_password), user_id),
            ) as cursor:
                if cursor.rowcount == 0:
                    raise ValueError(f"No user found with id={user_id!r}")
            await self._conn.commit()
