"""Unit tests for ``WizardGateService`` — Story 9.1 AC7."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import aiosqlite
import pytest
import pytest_asyncio

from open_ems.services.wizard_gate import WizardGateService
from open_ems.storage.repositories.device_repo import DeviceRepo, ManualDeviceEntryInput

_CREATE_DEVICE_REGISTRY = """
    CREATE TABLE device_registry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL UNIQUE,
        protocol TEXT NOT NULL
            CHECK (protocol IN ('modbus_tcp', 'ocpp_1_6', 'dsmr_p1')),
        address TEXT NOT NULL,
        model TEXT,
        firmware_version TEXT,
        source TEXT NOT NULL
            CHECK (source IN ('manual_entry', 'ocpp_self_registration')),
        validated INTEGER NOT NULL DEFAULT 0
            CHECK (validated IN (0, 1)),
        last_capability_status TEXT,
        last_limitation_reason TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT,
        installer_acknowledged_unvalidated_at TEXT
    )
"""

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 11, 12, 30, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def repo() -> AsyncGenerator[DeviceRepo, None]:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_DEVICE_REGISTRY)
        await conn.commit()
        yield DeviceRepo(conn)


def _manual(device_id: str = "inv-001") -> ManualDeviceEntryInput:
    return ManualDeviceEntryInput(
        device_id=device_id,
        protocol="modbus_tcp",
        address="10.0.0.1:502",
    )


@pytest.mark.asyncio
async def test_gate_allows_advance_when_registry_empty(repo: DeviceRepo) -> None:
    """AC7 cold-start: empty registry trivially passes the gate."""
    svc = WizardGateService(repo)
    outcome = await svc.evaluate()
    assert outcome.can_advance is True
    assert outcome.unacknowledged_unvalidated == ()


@pytest.mark.asyncio
async def test_gate_allows_advance_when_all_validated(repo: DeviceRepo) -> None:
    svc = WizardGateService(repo)
    await repo.upsert_manual(_manual(), first_seen_at=_NOW)
    await repo.mark_validated(
        "inv-001",
        last_capability_status="full",
        last_limitation_reason=None,
        last_seen_at=_LATER,
    )
    outcome = await svc.evaluate()
    assert outcome.can_advance is True


@pytest.mark.asyncio
async def test_gate_blocks_advance_when_unvalidated_unacknowledged(
    repo: DeviceRepo,
) -> None:
    svc = WizardGateService(repo)
    await repo.upsert_manual(_manual(), first_seen_at=_NOW)
    outcome = await svc.evaluate()
    assert outcome.can_advance is False
    assert [r.device_id for r in outcome.unacknowledged_unvalidated] == ["inv-001"]


@pytest.mark.asyncio
async def test_gate_allows_advance_when_unvalidated_acknowledged(
    repo: DeviceRepo,
) -> None:
    svc = WizardGateService(repo)
    await repo.upsert_manual(_manual(), first_seen_at=_NOW)
    await repo.acknowledge_unvalidated("inv-001", acknowledged_at=_LATER)
    outcome = await svc.evaluate()
    assert outcome.can_advance is True


@pytest.mark.asyncio
async def test_gate_returns_only_unacknowledged_unvalidated_blockers(
    repo: DeviceRepo,
) -> None:
    svc = WizardGateService(repo)
    # row A: unvalidated, NOT acknowledged → blocker
    await repo.upsert_manual(_manual("inv-A"), first_seen_at=_NOW)
    # row B: unvalidated, acknowledged → not a blocker
    await repo.upsert_manual(_manual("inv-B"), first_seen_at=_NOW)
    await repo.acknowledge_unvalidated("inv-B", acknowledged_at=_LATER)
    # row C: validated → not a blocker
    await repo.upsert_manual(_manual("inv-C"), first_seen_at=_NOW)
    await repo.mark_validated(
        "inv-C",
        last_capability_status="full",
        last_limitation_reason=None,
        last_seen_at=_LATER,
    )
    outcome = await svc.evaluate()
    assert outcome.can_advance is False
    assert [r.device_id for r in outcome.unacknowledged_unvalidated] == ["inv-A"]
