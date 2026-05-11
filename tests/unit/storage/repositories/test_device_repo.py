"""Unit tests for ``DeviceRepo`` — Story 9.1 AC1, AC5, AC6, AC7."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import cast

import aiosqlite
import pytest
import pytest_asyncio
from pydantic import ValidationError

from open_ems.storage.repositories.device_repo import (
    DeviceRegistryEntry,
    DeviceRepo,
    ManualDeviceEntryInput,
)

# Schema mirrors migrations/versions/0009_add_device_registry_table.py
# plus migrations/versions/0010_add_role_to_device_registry.py.
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
        last_capability_status TEXT
            CHECK (last_capability_status IS NULL
                   OR last_capability_status IN ('full', 'reduced', 'unsupported')),
        last_limitation_reason TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT,
        installer_acknowledged_unvalidated_at TEXT,
        role TEXT
            CHECK (role IS NULL
                   OR role IN ('inverter', 'battery', 'ev_charger', 'grid_meter')),
        role_assigned_at TEXT,
        CHECK ((role IS NULL AND role_assigned_at IS NULL)
               OR (role IS NOT NULL AND role_assigned_at IS NOT NULL))
    )
"""

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 11, 12, 30, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def repo_with_conn() -> AsyncGenerator[tuple[DeviceRepo, aiosqlite.Connection], None]:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_DEVICE_REGISTRY)
        await conn.commit()
        yield DeviceRepo(conn), conn


def _manual(device_id: str = "inv-001", protocol: str = "modbus_tcp") -> ManualDeviceEntryInput:
    return ManualDeviceEntryInput(
        device_id=device_id,
        protocol=protocol,  # type: ignore[arg-type]
        address="192.168.1.10:502",
        model="huawei_sun2000_v3",
        firmware_version=None,
    )


async def _count(conn: aiosqlite.Connection) -> int:
    async with conn.execute("SELECT COUNT(*) FROM device_registry") as cur:
        row = await cur.fetchone()
    return int(cast(aiosqlite.Row, row)[0])


async def test_list_all_on_empty_returns_empty_list(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    assert await repo.list_all() == []


async def test_get_by_device_id_on_missing_returns_none(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    assert await repo.get_by_device_id("missing") is None


async def test_upsert_manual_persists_as_unvalidated(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    entry = await repo.upsert_manual(_manual(), first_seen_at=_NOW)
    assert entry.device_id == "inv-001"
    assert entry.protocol == "modbus_tcp"
    assert entry.source == "manual_entry"
    assert entry.validated is False
    assert entry.last_capability_status is None
    assert entry.last_limitation_reason is None
    assert entry.first_seen_at == _NOW
    assert entry.last_seen_at is None
    assert entry.installer_acknowledged_unvalidated_at is None


async def test_upsert_manual_rejects_duplicate_device_id(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    repo, conn = repo_with_conn
    await repo.upsert_manual(_manual(), first_seen_at=_NOW)
    with pytest.raises(ValueError, match="device_id already registered"):
        await repo.upsert_manual(_manual(), first_seen_at=_LATER)
    assert await _count(conn) == 1


async def test_upsert_manual_rejects_naive_first_seen_at(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    naive = datetime(2026, 5, 11, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="must be timezone-aware UTC"):
        await repo.upsert_manual(_manual(), first_seen_at=naive)


async def test_mark_validated_one_way_transition(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.upsert_manual(_manual(), first_seen_at=_NOW)
    await repo.mark_validated(
        "inv-001",
        last_capability_status="full",
        last_limitation_reason=None,
        last_seen_at=_LATER,
    )
    entry = await repo.get_by_device_id("inv-001")
    assert entry is not None
    assert entry.validated is True
    assert entry.last_capability_status == "full"
    assert entry.last_seen_at == _LATER


async def test_mark_validated_raises_when_device_missing(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    with pytest.raises(ValueError, match="No device_registry row"):
        await repo.mark_validated(
            "missing",
            last_capability_status="full",
            last_limitation_reason=None,
            last_seen_at=_LATER,
        )


async def test_repo_has_no_mark_unvalidated_method() -> None:
    """AC6 structural guard: the validated bit is one-way per row."""
    assert not hasattr(DeviceRepo, "mark_unvalidated")


def test_device_repo_validated_requires_capability_status() -> None:
    """R3 #1: ``DeviceRegistryEntry(validated=True)`` MUST carry a resolved
    ``last_capability_status``. A row-level model-validator rejects the
    impossible combination on read so a manually tampered DB row cannot
    surface as "validated but uncategorized".
    """
    with pytest.raises(ValidationError, match="validated=True requires"):
        DeviceRegistryEntry(
            device_id="inv-001",
            protocol="modbus_tcp",
            address="192.168.1.10:502",
            model=None,
            firmware_version=None,
            source="manual_entry",
            validated=True,
            last_capability_status=None,
            last_limitation_reason=None,
            first_seen_at=_NOW,
            last_seen_at=None,
            installer_acknowledged_unvalidated_at=None,
        )


async def test_mark_validated_clears_acknowledged_unvalidated_at(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    """Audit-trail integrity: once a row genuinely validates, the prior
    "installer waved this through unvalidated" timestamp no longer describes
    the current state and is cleared by ``mark_validated``.
    """
    repo, _ = repo_with_conn
    await repo.upsert_manual(_manual(), first_seen_at=_NOW)
    ack_at = datetime(2026, 5, 11, 12, 15, 0, tzinfo=UTC)
    await repo.acknowledge_unvalidated("inv-001", acknowledged_at=ack_at)
    before = await repo.get_by_device_id("inv-001")
    assert before is not None
    assert before.installer_acknowledged_unvalidated_at == ack_at
    await repo.mark_validated(
        "inv-001",
        last_capability_status="full",
        last_limitation_reason=None,
        last_seen_at=_LATER,
    )
    after = await repo.get_by_device_id("inv-001")
    assert after is not None
    assert after.validated is True
    assert after.installer_acknowledged_unvalidated_at is None


async def test_update_last_seen_preserves_validated(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.upsert_manual(_manual(), first_seen_at=_NOW)
    await repo.mark_validated(
        "inv-001",
        last_capability_status="full",
        last_limitation_reason=None,
        last_seen_at=_LATER,
    )
    later2 = datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC)
    await repo.update_last_seen(
        "inv-001",
        last_capability_status="reduced",
        last_limitation_reason="firmware_unknown",
        last_seen_at=later2,
    )
    entry = await repo.get_by_device_id("inv-001")
    assert entry is not None
    assert entry.validated is True  # preserved
    assert entry.last_capability_status == "reduced"
    assert entry.last_limitation_reason == "firmware_unknown"
    assert entry.last_seen_at == later2


async def test_update_last_seen_raises_when_device_missing(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    with pytest.raises(ValueError, match="No device_registry row"):
        await repo.update_last_seen(
            "missing",
            last_capability_status="full",
            last_limitation_reason=None,
            last_seen_at=_LATER,
        )


async def test_acknowledge_unvalidated_round_trip(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.upsert_manual(_manual(), first_seen_at=_NOW)
    await repo.acknowledge_unvalidated("inv-001", acknowledged_at=_LATER)
    entry = await repo.get_by_device_id("inv-001")
    assert entry is not None
    assert entry.installer_acknowledged_unvalidated_at == _LATER
    assert entry.validated is False  # still unvalidated, just acknowledged


async def test_acknowledge_unvalidated_refuses_validated_rows(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    await repo.upsert_manual(_manual(), first_seen_at=_NOW)
    await repo.mark_validated(
        "inv-001",
        last_capability_status="full",
        last_limitation_reason=None,
        last_seen_at=_LATER,
    )
    with pytest.raises(ValueError, match="No unvalidated device_registry row"):
        await repo.acknowledge_unvalidated("inv-001", acknowledged_at=_LATER)


async def test_db_check_constraint_rejects_unknown_protocol(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    _, conn = repo_with_conn
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(
            "INSERT INTO device_registry"
            " (device_id, protocol, address, source, first_seen_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("bad", "carrier_pigeon", "192.168.1.1:1", "manual_entry", _NOW.isoformat()),
        )


async def test_manual_entry_input_rejects_blank_device_id() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ManualDeviceEntryInput(
            device_id="   ",
            protocol="modbus_tcp",
            address="192.168.1.10:502",
        )


async def test_manual_entry_input_rejects_device_id_over_64_chars() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ManualDeviceEntryInput(
            device_id="x" * 65,
            protocol="modbus_tcp",
            address="192.168.1.10:502",
        )


async def test_list_all_orders_by_first_seen_then_device_id(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    repo, _ = repo_with_conn
    t0 = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 5, 11, 12, 1, 0, tzinfo=UTC)
    await repo.upsert_manual(
        ManualDeviceEntryInput(
            device_id="dev-b",
            protocol="modbus_tcp",
            address="10.0.0.2:502",
        ),
        first_seen_at=t1,
    )
    await repo.upsert_manual(
        ManualDeviceEntryInput(
            device_id="dev-a",
            protocol="modbus_tcp",
            address="10.0.0.1:502",
        ),
        first_seen_at=t0,
    )
    entries = await repo.list_all()
    assert [e.device_id for e in entries] == ["dev-a", "dev-b"]


# ---------------------------------------------------------------------------
# Story 9.2 — assign_role + role / role_assigned_at paired invariant
# ---------------------------------------------------------------------------


async def test_assign_role_round_trip_none_to_role_to_none(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    """Setting → reassigning → clearing keeps role and role_assigned_at paired."""
    from open_ems.core.devices import DeviceRole

    repo, _ = repo_with_conn
    await repo.upsert_manual(_manual(), first_seen_at=_NOW)
    # None → DeviceRole.inverter
    await repo.assign_role("inv-001", role=DeviceRole.inverter, assigned_at=_LATER)
    entry = await repo.get_by_device_id("inv-001")
    assert entry is not None
    assert entry.role is DeviceRole.inverter
    assert entry.role_assigned_at == _LATER

    # Reassign → battery
    later2 = datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC)
    await repo.assign_role("inv-001", role=DeviceRole.battery, assigned_at=later2)
    entry = await repo.get_by_device_id("inv-001")
    assert entry is not None
    assert entry.role is DeviceRole.battery
    assert entry.role_assigned_at == later2

    # DeviceRole → None
    later3 = datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC)
    await repo.assign_role("inv-001", role=None, assigned_at=later3)
    entry = await repo.get_by_device_id("inv-001")
    assert entry is not None
    assert entry.role is None
    assert entry.role_assigned_at is None


async def test_assign_role_raises_when_device_missing(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    from open_ems.core.devices import DeviceRole

    repo, _ = repo_with_conn
    with pytest.raises(ValueError) as exc_info:
        await repo.assign_role("missing", role=DeviceRole.inverter, assigned_at=_LATER)
    assert str(exc_info.value) == "No device_registry row for device_id='missing'"


async def test_assign_role_rejects_naive_assigned_at(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    """Mirrors ``test_upsert_manual_rejects_naive_first_seen_at``. The
    ``_require_utc(assigned_at, "assigned_at")`` guard at the top of
    ``assign_role`` must reject a naive datetime before any DB write.
    """
    from open_ems.core.devices import DeviceRole

    repo, _ = repo_with_conn
    await repo.upsert_manual(_manual(), first_seen_at=_NOW)
    naive = datetime(2026, 5, 11, 12, 30, 0)  # no tzinfo
    with pytest.raises(ValueError, match="must be timezone-aware UTC"):
        await repo.assign_role("inv-001", role=DeviceRole.inverter, assigned_at=naive)


def test_role_and_role_assigned_at_must_be_paired_pydantic_side() -> None:
    """R3 #1 (Story 9.2): the impossible state is rejected at every read."""
    from open_ems.core.devices import DeviceRole

    with pytest.raises(ValidationError, match="role and role_assigned_at must both"):
        DeviceRegistryEntry(
            device_id="inv-001",
            protocol="modbus_tcp",
            address="10.0.0.1:502",
            model=None,
            firmware_version=None,
            source="manual_entry",
            validated=False,
            last_capability_status=None,
            last_limitation_reason=None,
            first_seen_at=_NOW,
            last_seen_at=None,
            installer_acknowledged_unvalidated_at=None,
            role=DeviceRole.inverter,
            role_assigned_at=None,
        )
    with pytest.raises(ValidationError, match="role and role_assigned_at must both"):
        DeviceRegistryEntry(
            device_id="inv-001",
            protocol="modbus_tcp",
            address="10.0.0.1:502",
            model=None,
            firmware_version=None,
            source="manual_entry",
            validated=False,
            last_capability_status=None,
            last_limitation_reason=None,
            first_seen_at=_NOW,
            last_seen_at=None,
            installer_acknowledged_unvalidated_at=None,
            role=None,
            role_assigned_at=_LATER,
        )


async def test_db_check_rejects_role_with_no_assigned_at(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    """Raw DB INSERT bypassing the repo: the CHECK constraint must reject."""
    _, conn = repo_with_conn
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(
            "INSERT INTO device_registry"
            " (device_id, protocol, address, source, first_seen_at, role)"
            " VALUES (?, 'modbus_tcp', '10.0.0.1:502', 'manual_entry', ?, 'inverter')",
            ("orphan", _NOW.isoformat()),
        )


async def test_db_check_rejects_unknown_role_value(
    repo_with_conn: tuple[DeviceRepo, aiosqlite.Connection],
) -> None:
    _, conn = repo_with_conn
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(
            "INSERT INTO device_registry"
            " (device_id, protocol, address, source, first_seen_at, role, role_assigned_at)"
            " VALUES (?, 'modbus_tcp', '10.0.0.1:502', 'manual_entry', ?, 'banana', ?)",
            ("bad", _NOW.isoformat(), _NOW.isoformat()),
        )
