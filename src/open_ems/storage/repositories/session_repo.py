from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

import aiosqlite

from open_ems.storage.database import get_connection

SESSION_TOKEN_BYTES = 32  # 256 bits; architecture requires >= 128 bits


def generate_session_token() -> str:
    """Generate a cryptographically random session token. Returns raw hex string."""
    return secrets.token_hex(SESSION_TOKEN_BYTES)


def hash_token(raw_token: str) -> str:
    """SHA-256 hash of raw token. Only the hash is stored in the database."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


class SessionRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    async def create(
        self,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
    ) -> str:
        """Create a session. Returns the new session ID (UUID4 string)."""
        session_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            "INSERT INTO sessions (id, user_id, token_hash, created_at, last_active_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, user_id, token_hash, now, now, expires_at.isoformat()),
        )
        await self._conn.commit()
        return session_id

    async def get_by_token_hash(self, token_hash: str) -> aiosqlite.Row | None:
        """Fetch session by token hash. Returns None if not found."""
        async with self._conn.execute(
            "SELECT id, user_id, token_hash, created_at, last_active_at, expires_at"
            " FROM sessions WHERE token_hash = ?",
            (token_hash,),
        ) as cursor:
            return await cursor.fetchone()

    async def delete_by_id(self, session_id: str) -> None:
        """Delete a session by ID."""
        await self._conn.execute(
            "DELETE FROM sessions WHERE id = ?",
            (session_id,),
        )
        await self._conn.commit()

    async def delete_all_for_user(self, user_id: str) -> None:
        """Delete all sessions for a given user (session fixation prevention on re-login)."""
        await self._conn.execute(
            "DELETE FROM sessions WHERE user_id = ?",
            (user_id,),
        )
        await self._conn.commit()

    async def update_last_active(self, session_id: str) -> None:
        """Update last_active_at to now.

        Raises ValueError if session_id does not exist.
        Caller is responsible for passing a valid session ID (see get_by_token_hash()).
        """
        now = datetime.now(UTC).isoformat()
        async with self._conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE id = ?",
            (now, session_id),
        ) as cursor:
            if cursor.rowcount == 0:
                raise ValueError(f"No session found with id={session_id!r}")
        await self._conn.commit()
