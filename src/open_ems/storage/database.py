from __future__ import annotations

import asyncio

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

_connection: aiosqlite.Connection | None = None
_write_lock: asyncio.Lock | None = None


async def init_database(db_path: str) -> None:
    """Open aiosqlite connection and enable WAL mode + foreign keys."""
    global _connection, _write_lock
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
    _write_lock = asyncio.Lock()
    logger.info("database_initialized", db_path=db_path, component="storage.database")


async def close_database() -> None:
    """Close the shared connection."""
    global _connection, _write_lock
    if _connection is not None:
        await _connection.close()
        _connection = None
        _write_lock = None
        logger.info("database_closed", component="storage.database")


def get_connection() -> aiosqlite.Connection:
    """Return the shared connection. Raises RuntimeError if not yet initialized."""
    if _connection is None:
        raise RuntimeError(
            "Database not initialized. Call init_database() before using get_connection()."
        )
    return _connection


def get_write_lock() -> asyncio.Lock:
    """Return the process-wide write lock for the shared aiosqlite connection.

    Story 9.0b / D1: aiosqlite shares one connection across all coroutines.
    SQLite cannot start a transaction (``BEGIN IMMEDIATE`` or auto-begin from
    DML) while another transaction is open on the same connection — so every
    writer must serialize through this lock. Reads are not subject to the lock.

    Holding the lock for the entire ``execute → commit`` window prevents the
    "cannot start a transaction within a transaction" race that was empirically
    reproduced by ``tests/integration/storage/test_config_repo_concurrent_writers``.

    Lazy initialization: unit tests that bypass ``init_database`` and pass
    their own ``aiosqlite.Connection`` to a repo still call into the writers
    and so still hit ``get_write_lock``. We lazy-create a process-local lock
    in that case — tests run sequentially within a worker, so a single
    fresh lock is fine; production replaces it inside ``init_database``.
    """
    global _write_lock
    if _write_lock is None:
        _write_lock = asyncio.Lock()
    return _write_lock
