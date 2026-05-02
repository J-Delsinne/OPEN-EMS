from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import aiosqlite
import bcrypt as _bcrypt

from open_ems.storage.database import get_connection

_BCRYPT_MAX_BYTES = 72

_VALID_ROLES: frozenset[str] = frozenset({"installer", "homeowner"})

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
        raise ValueError(
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
            except Exception:
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
    ) -> str:
        """Create a user. Returns the new user ID (UUID4 string)."""
        if role not in _VALID_ROLES:
            raise ValueError(f"Invalid role {role!r}. Must be one of {sorted(_VALID_ROLES)}")
        user_id = str(uuid.uuid4())
        created_at = datetime.now(UTC).isoformat()
        await self._conn.execute(
            "INSERT INTO users"
            " (id, username, role, hashed_password, must_change_password, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, role, hashed_password, int(must_change_password), created_at),
        )
        await self._conn.commit()
        return user_id

    async def get_by_username(self, username: str) -> aiosqlite.Row | None:
        """Fetch user row by username. Returns None if not found."""
        async with self._conn.execute(
            "SELECT id, username, role, hashed_password, must_change_password, created_at"
            " FROM users WHERE username = ?",
            (username,),
        ) as cursor:
            return await cursor.fetchone()

    async def update_password(self, user_id: str, hashed_password: str) -> None:
        """Replace hashed_password and clear must_change_password flag.

        Caller is responsible for passing a pre-hashed value (see hash_password()).
        Raises ValueError if no user with the given user_id exists.
        """
        async with self._conn.execute(
            "UPDATE users SET hashed_password = ?, must_change_password = 0 WHERE id = ?",
            (hashed_password, user_id),
        ) as cursor:
            if cursor.rowcount == 0:
                raise ValueError(f"No user found with id={user_id!r}")
        await self._conn.commit()
