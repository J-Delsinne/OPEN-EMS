from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite
import pytest
import pytest_asyncio
from structlog.testing import capture_logs

from open_ems.services.audit_log import (
    EVENT_LOG_SCHEMA_VERSION,
    MAX_INSTALLER_NOTE_LENGTH,
    ObservabilityService,
)
from open_ems.storage.repositories.event_log_repo import EventLogRepo
from tests.helpers.fake_audit_log import FakeAuditLog

_CREATE_EVENT_LOG = """
    CREATE TABLE event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schema_version INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        actor TEXT NOT NULL,
        event_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        detail TEXT,
        device_id TEXT,
        config_version TEXT
    )
"""


@pytest_asyncio.fixture
async def mem_svc():
    """ObservabilityService backed by an in-memory SQLite database."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_EVENT_LOG)
        await conn.commit()
        repo = EventLogRepo(conn)
        yield ObservabilityService(repo=repo), conn


# ── AC1 / AC5: dual-sink writes ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_writes_structlog(mem_svc):
    svc, _ = mem_svc
    with capture_logs() as logs:
        await svc.audit(actor="system", event_type="SYSTEM", summary="boot complete")
    audit_logs = [e for e in logs if e.get("audit") is True]
    assert len(audit_logs) == 1
    entry = audit_logs[0]
    assert entry["event_type"] == "SYSTEM"
    assert entry["actor"] == "system"
    assert entry["summary"] == "boot complete"


@pytest.mark.asyncio
async def test_audit_writes_db_row(mem_svc):
    svc, conn = mem_svc
    await svc.audit(
        actor="installer",
        event_type="DEVICE",
        summary="device connected",
        detail={"device": "bat-01"},
        device_id="bat-01",
        config_version="v3",
    )
    async with conn.execute("SELECT * FROM event_log") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["actor"] == "installer"
    assert row["event_type"] == "DEVICE"
    assert row["summary"] == "device connected"
    assert row["device_id"] == "bat-01"
    assert row["config_version"] == "v3"


@pytest.mark.asyncio
async def test_audit_schema_version_in_row(mem_svc):
    svc, conn = mem_svc
    await svc.audit(actor="system", event_type="SYSTEM", summary="test")
    async with conn.execute("SELECT schema_version FROM event_log") as cur:
        row = await cur.fetchone()
    assert row["schema_version"] == EVENT_LOG_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_audit_detail_null_when_not_provided(mem_svc):
    svc, conn = mem_svc
    await svc.audit(actor="system", event_type="SYSTEM", summary="no detail")
    async with conn.execute("SELECT detail FROM event_log") as cur:
        row = await cur.fetchone()
    assert row["detail"] is None


@pytest.mark.asyncio
async def test_audit_detail_stored_as_json(mem_svc):
    svc, conn = mem_svc
    await svc.audit(
        actor="system", event_type="SYSTEM", summary="with detail", detail={"key": "val"}
    )
    async with conn.execute("SELECT detail FROM event_log") as cur:
        row = await cur.fetchone()
    assert json.loads(row["detail"]) == {"key": "val"}


@pytest.mark.asyncio
async def test_non_json_detail_raises_before_any_write(mem_svc):
    svc, conn = mem_svc
    with capture_logs() as logs:
        with pytest.raises(ValueError, match="detail must be JSON-serializable"):
            await svc.audit(
                actor="system",
                event_type="SYSTEM",
                summary="bad detail",
                detail={"at": datetime.now(UTC)},
            )
    assert not any(e.get("audit") is True for e in logs)
    async with conn.execute("SELECT COUNT(*) FROM event_log") as cur:
        row = await cur.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_nan_detail_raises_before_any_write(mem_svc):
    svc, conn = mem_svc
    with capture_logs() as logs:
        with pytest.raises(ValueError, match="detail must be JSON-serializable"):
            await svc.audit(
                actor="system",
                event_type="SYSTEM",
                summary="bad detail",
                detail={"value": float("nan")},
            )
    assert not any(e.get("audit") is True for e in logs)
    async with conn.execute("SELECT COUNT(*) FROM event_log") as cur:
        row = await cur.fetchone()
    assert row[0] == 0


# ── AC3: write-time validation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blank_summary_raises_before_write(mem_svc):
    svc, conn = mem_svc
    with pytest.raises(ValueError, match="summary must be non-empty"):
        await svc.audit(actor="system", event_type="SYSTEM", summary="")
    async with conn.execute("SELECT COUNT(*) FROM event_log") as cur:
        row = await cur.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_whitespace_only_summary_raises(mem_svc):
    svc, _ = mem_svc
    with pytest.raises(ValueError, match="summary must be non-empty"):
        await svc.audit(actor="system", event_type="SYSTEM", summary="   ")


@pytest.mark.asyncio
async def test_invalid_actor_raises(mem_svc):
    svc, conn = mem_svc
    with pytest.raises(ValueError, match="Invalid actor"):
        await svc.audit(actor="robot", event_type="SYSTEM", summary="test")
    async with conn.execute("SELECT COUNT(*) FROM event_log") as cur:
        row = await cur.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_invalid_event_type_raises(mem_svc):
    svc, conn = mem_svc
    with pytest.raises(ValueError, match="Invalid event_type"):
        await svc.audit(actor="system", event_type="UNKNOWN", summary="test")
    async with conn.execute("SELECT COUNT(*) FROM event_log") as cur:
        row = await cur.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_validation_fires_before_structlog(mem_svc):
    svc, _ = mem_svc
    with capture_logs() as logs:
        with pytest.raises(ValueError):
            await svc.audit(actor="system", event_type="SYSTEM", summary="")
    assert not any(e.get("audit") is True for e in logs)


# ── AC5: all valid actors and event types accepted ────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["system", "installer", "homeowner"])
async def test_valid_actors_accepted(actor):
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_EVENT_LOG)
        await conn.commit()
        svc = ObservabilityService(repo=EventLogRepo(conn))
        await svc.audit(actor=actor, event_type="SYSTEM", summary="ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["DECISION", "DEVICE", "SYSTEM", "CONSTRAINT", "INSTALLER"])
async def test_valid_event_types_accepted(event_type):
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_EVENT_LOG)
        await conn.commit()
        svc = ObservabilityService(repo=EventLogRepo(conn))
        await svc.audit(actor="system", event_type=event_type, summary="ok")


# ── AC4 / AC5: FakeAuditLog ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fake_captures_event():
    fake = FakeAuditLog()
    await fake.audit(actor="system", event_type="SYSTEM", summary="boot")
    fake.assert_has_event(event_type="SYSTEM", actor="system")


@pytest.mark.asyncio
async def test_fake_assert_has_event_fails_when_missing():
    fake = FakeAuditLog()
    await fake.audit(actor="system", event_type="SYSTEM", summary="boot")
    with pytest.raises(AssertionError):
        fake.assert_has_event(event_type="DECISION", actor="system")


@pytest.mark.asyncio
async def test_fake_assert_event_count():
    fake = FakeAuditLog()
    await fake.audit(actor="system", event_type="SYSTEM", summary="a")
    await fake.audit(actor="installer", event_type="DEVICE", summary="b")
    fake.assert_event_count(2)


@pytest.mark.asyncio
async def test_fake_clear():
    fake = FakeAuditLog()
    await fake.audit(actor="system", event_type="SYSTEM", summary="a")
    fake.clear()
    fake.assert_event_count(0)


@pytest.mark.asyncio
async def test_fake_snapshots_detail():
    fake = FakeAuditLog()
    detail = {"nested": {"value": "before"}}
    await fake.audit(actor="system", event_type="SYSTEM", summary="a", detail=detail)
    detail["nested"]["value"] = "after"  # type: ignore[index]
    assert fake.events[0]["detail"] == {"nested": {"value": "before"}}


@pytest.mark.asyncio
async def test_fake_validates_blank_summary():
    fake = FakeAuditLog()
    with pytest.raises(ValueError, match="summary must be non-empty"):
        await fake.audit(actor="system", event_type="SYSTEM", summary="")


@pytest.mark.asyncio
async def test_fake_validates_actor():
    fake = FakeAuditLog()
    with pytest.raises(ValueError, match="Invalid actor"):
        await fake.audit(actor="alien", event_type="SYSTEM", summary="test")


@pytest.mark.asyncio
async def test_fake_validates_event_type():
    fake = FakeAuditLog()
    with pytest.raises(ValueError, match="Invalid event_type"):
        await fake.audit(actor="system", event_type="BOGUS", summary="test")


@pytest.mark.asyncio
async def test_fake_validates_detail_serializable():
    fake = FakeAuditLog()
    with pytest.raises(ValueError, match="detail must be JSON-serializable"):
        await fake.audit(
            actor="system",
            event_type="SYSTEM",
            summary="bad detail",
            detail={"at": datetime.now(UTC)},
        )


# ── Story 6.3: installer note storage ────────────────────────────────────────


@pytest.mark.asyncio
async def test_installer_note_writes_sanitized_installer_event(mem_svc):
    svc, conn = mem_svc

    await svc.installer_note('  <script>alert("x")</script>  ')

    async with conn.execute("SELECT actor, event_type, summary FROM event_log") as cur:
        row = await cur.fetchone()
    assert row["actor"] == "installer"
    assert row["event_type"] == "INSTALLER"
    assert row["summary"] == "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"


@pytest.mark.asyncio
async def test_installer_note_rejects_over_length_before_write(mem_svc):
    svc, conn = mem_svc

    with pytest.raises(
        ValueError,
        match=f"installer note must be <= {MAX_INSTALLER_NOTE_LENGTH} characters",
    ):
        await svc.installer_note("x" * (MAX_INSTALLER_NOTE_LENGTH + 1))

    async with conn.execute("SELECT COUNT(*) FROM event_log") as cur:
        row = await cur.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_installer_note_rejects_whitespace_before_write(mem_svc):
    svc, conn = mem_svc

    with pytest.raises(ValueError, match="installer note must be non-empty"):
        await svc.installer_note(" \n\t ")

    async with conn.execute("SELECT COUNT(*) FROM event_log") as cur:
        row = await cur.fetchone()
    assert row[0] == 0
