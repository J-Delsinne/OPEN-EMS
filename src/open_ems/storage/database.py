from __future__ import annotations

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

_connection: aiosqlite.Connection | None = None


async def init_database(db_path: str) -> None:
    """Open aiosqlite connection and enable WAL mode + foreign keys."""
    global _connection
    if _connection is not None:
        raise RuntimeError("Database already initialized. Call close_database() first.")
    conn = await aiosqlite.connect(db_path)
    try:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("PRAGMA journal_mode=WAL")
        row = await cursor.fetchone()
        if row is None or row[0] != "wal":
            raise RuntimeError(
                f"Failed to enable WAL mode: journal_mode={row[0] if row else 'unknown'}"
            )
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.commit()
    except Exception:
        await conn.close()
        raise
    _connection = conn
    logger.info("database_initialized", db_path=db_path, component="storage.database")


async def close_database() -> None:
    """Close the shared connection."""
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None
        logger.info("database_closed", component="storage.database")


def get_connection() -> aiosqlite.Connection:
    """Return the shared connection. Raises RuntimeError if not yet initialized."""
    if _connection is None:
        raise RuntimeError(
            "Database not initialized. Call init_database() before using get_connection()."
        )
    return _connection
