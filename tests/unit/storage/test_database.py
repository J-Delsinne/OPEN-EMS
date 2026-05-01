import pytest

from open_ems.storage.database import (
    close_database,
    get_connection,
    init_database,
)


async def test_wal_mode_enabled(tmp_db_path: str, clean_db_state: None) -> None:
    await init_database(tmp_db_path)
    conn = get_connection()
    async with conn.execute("PRAGMA journal_mode") as cursor:
        row = await cursor.fetchone()
    await close_database()
    assert row is not None
    assert row[0] == "wal"


async def test_foreign_keys_enabled(tmp_db_path: str, clean_db_state: None) -> None:
    await init_database(tmp_db_path)
    conn = get_connection()
    async with conn.execute("PRAGMA foreign_keys") as cursor:
        row = await cursor.fetchone()
    await close_database()
    assert row is not None
    assert row[0] == 1


async def test_get_connection_before_init_raises(clean_db_state: None) -> None:
    with pytest.raises(RuntimeError, match="Database not initialized"):
        get_connection()


async def test_init_close_lifecycle(tmp_db_path: str, clean_db_state: None) -> None:
    await init_database(tmp_db_path)
    conn = get_connection()
    assert conn is not None
    await close_database()
    # After close, get_connection must raise again
    with pytest.raises(RuntimeError):
        get_connection()


async def test_init_database_double_call_raises(tmp_db_path: str, clean_db_state: None) -> None:
    await init_database(tmp_db_path)
    with pytest.raises(RuntimeError, match="already initialized"):
        await init_database(tmp_db_path)
    await close_database()


async def test_wal_validation_fails_for_memory_db(clean_db_state: None) -> None:
    with pytest.raises(RuntimeError, match="Failed to enable WAL mode"):
        await init_database(":memory:")


async def test_row_factory(tmp_db_path: str, clean_db_state: None) -> None:
    await init_database(tmp_db_path)
    conn = get_connection()
    await conn.execute("CREATE TABLE _test (id INTEGER PRIMARY KEY, name TEXT)")
    await conn.execute("INSERT INTO _test (name) VALUES (?)", ("hello",))
    await conn.commit()
    async with conn.execute("SELECT id, name FROM _test") as cursor:
        row = await cursor.fetchone()
    await close_database()
    assert row is not None
    assert row["name"] == "hello"
    assert row["id"] == 1
