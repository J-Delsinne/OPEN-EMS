from __future__ import annotations

import uuid
from datetime import UTC, datetime

import aiosqlite
import bcrypt as _bcrypt

from open_ems.storage.database import get_connection


def hash_password(password: str) -> str:
    """Hash a password. Returns a bcrypt PHC self-describing string ($2b$...)."""
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plain text against a stored bcrypt hash."""
    return bool(_bcrypt.checkpw(plain_password.encode(), hashed_password.encode()))


class UserRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    async def count(self) -> int:
        """Return total number of users."""
        cursor = await self._conn.execute("SELECT COUNT(*) FROM users")
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
        cursor = await self._conn.execute(
            "SELECT id, username, role, hashed_password, must_change_password, created_at"
            " FROM users WHERE username = ?",
            (username,),
        )
        return await cursor.fetchone()

    async def update_password(self, user_id: str, hashed_password: str) -> None:
        """Replace hashed_password and clear must_change_password flag."""
        await self._conn.execute(
            "UPDATE users SET hashed_password = ?, must_change_password = 0 WHERE id = ?",
            (hashed_password, user_id),
        )
        await self._conn.commit()
