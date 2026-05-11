"""Unit tests for ``RoleAssignmentService`` — Story 9.2 AC3, AC5, AC6, AC9."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import aiosqlite
import pytest_asyncio

from open_ems.core.devices import DeviceRole
from open_ems.services.role_assignment import (
    RoleAssignmentService,
    RoleConflict,
    RoleGap,
)
from open_ems.storage.repositories.device_repo import (
    DeviceRepo,
    ManualDeviceEntryInput,
)

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


def _t(seconds: int) -> datetime:
    return datetime(2026, 5, 11, 12, 0, seconds, tzinfo=UTC)


@pytest_asyncio.fixture
async def repo_and_service() -> AsyncGenerator[tuple[DeviceRepo, RoleAssignmentService], None]:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_DEVICE_REGISTRY)
        await conn.commit()
        repo = DeviceRepo(conn)
        yield repo, RoleAssignmentService(repo)


async def _insert_row(
    repo: DeviceRepo,
    device_id: str,
    *,
    first_seen_at: datetime,
    role: DeviceRole | None = None,
) -> None:
    await repo.upsert_manual(
        ManualDeviceEntryInput(
            device_id=device_id,
            protocol="modbus_tcp",
            address="10.0.0.1:502",
        ),
        first_seen_at=first_seen_at,
    )
    if role is not None:
        await repo.assign_role(device_id, role=role, assigned_at=first_seen_at)


async def test_empty_registry_reports_all_gaps_no_conflicts(
    repo_and_service: tuple[DeviceRepo, RoleAssignmentService],
) -> None:
    _, service = repo_and_service
    snapshot = await service.evaluate_assignments()
    assert snapshot.assignments == ()
    assert snapshot.conflicts == frozenset()
    assert snapshot.unassigned_count == 0

    # All four roles missing — exactly one gap each, grid_meter is hard_block.
    by_role = {gap.role: gap for gap in snapshot.gaps}
    assert set(by_role.keys()) == {
        DeviceRole.inverter,
        DeviceRole.battery,
        DeviceRole.ev_charger,
        DeviceRole.grid_meter,
    }
    assert by_role[DeviceRole.grid_meter].severity == "hard_block"
    assert by_role[DeviceRole.grid_meter].label == "grid_meter_missing"
    for role in (DeviceRole.inverter, DeviceRole.battery, DeviceRole.ev_charger):
        assert by_role[role].severity == "acknowledgeable_warn"
        assert by_role[role].label == f"{role.value}_missing"


async def test_gate_blocked_when_grid_meter_missing_even_if_others_acknowledged(
    repo_and_service: tuple[DeviceRepo, RoleAssignmentService],
) -> None:
    _, service = repo_and_service
    outcome = await service.evaluate_gate(
        acknowledged_gaps=frozenset({"battery_missing", "inverter_missing", "ev_charger_missing"})
    )
    assert outcome.can_advance is False
    assert any(
        g.role is DeviceRole.grid_meter and g.severity == "hard_block"
        for g in outcome.blocking_gaps
    )


async def test_only_grid_meter_assigned_advances_when_others_acknowledged(
    repo_and_service: tuple[DeviceRepo, RoleAssignmentService],
) -> None:
    repo, service = repo_and_service
    await _insert_row(repo, "meter-1", first_seen_at=_t(0), role=DeviceRole.grid_meter)

    # Without acknowledgments the three warns still block.
    outcome = await service.evaluate_gate(acknowledged_gaps=frozenset())
    assert outcome.can_advance is False
    assert {g.label for g in outcome.unacknowledged_warnings} == {
        "battery_missing",
        "inverter_missing",
        "ev_charger_missing",
    }

    # Acknowledged → can advance.
    outcome = await service.evaluate_gate(
        acknowledged_gaps=frozenset({"battery_missing", "inverter_missing", "ev_charger_missing"})
    )
    assert outcome.can_advance is True
    assert outcome.blocking_gaps == ()
    assert outcome.unacknowledged_warnings == ()
    assert outcome.conflicts == ()


async def test_all_four_roles_assigned_advances_without_acknowledgments(
    repo_and_service: tuple[DeviceRepo, RoleAssignmentService],
) -> None:
    repo, service = repo_and_service
    await _insert_row(repo, "inv", first_seen_at=_t(0), role=DeviceRole.inverter)
    await _insert_row(repo, "bat", first_seen_at=_t(1), role=DeviceRole.battery)
    await _insert_row(repo, "evc", first_seen_at=_t(2), role=DeviceRole.ev_charger)
    await _insert_row(repo, "gm", first_seen_at=_t(3), role=DeviceRole.grid_meter)
    outcome = await service.evaluate_gate(acknowledged_gaps=frozenset())
    assert outcome.can_advance is True


async def test_duplicate_role_yields_conflict_with_both_device_ids(
    repo_and_service: tuple[DeviceRepo, RoleAssignmentService],
) -> None:
    repo, service = repo_and_service
    await _insert_row(repo, "inv-a", first_seen_at=_t(0), role=DeviceRole.inverter)
    await _insert_row(repo, "inv-b", first_seen_at=_t(1), role=DeviceRole.inverter)
    snapshot = await service.evaluate_assignments()
    conflict = next(iter(snapshot.conflicts))
    assert conflict.role is DeviceRole.inverter
    assert set(conflict.device_ids) == {"inv-a", "inv-b"}

    # Each row carries its peer device_ids for the inline indicator.
    by_id = {row.device_id: row for row in snapshot.assignments}
    assert by_id["inv-a"].conflicting_device_ids == ("inv-b",)
    assert by_id["inv-b"].conflicting_device_ids == ("inv-a",)


async def test_conflict_blocks_gate_regardless_of_acknowledgments(
    repo_and_service: tuple[DeviceRepo, RoleAssignmentService],
) -> None:
    repo, service = repo_and_service
    # Two inverters, one grid_meter → no missing gaps but a conflict.
    await _insert_row(repo, "inv-a", first_seen_at=_t(0), role=DeviceRole.inverter)
    await _insert_row(repo, "inv-b", first_seen_at=_t(1), role=DeviceRole.inverter)
    await _insert_row(repo, "gm", first_seen_at=_t(2), role=DeviceRole.grid_meter)
    outcome = await service.evaluate_gate(
        acknowledged_gaps=frozenset({"battery_missing", "ev_charger_missing"})
    )
    assert outcome.can_advance is False
    assert any(c.role is DeviceRole.inverter for c in outcome.conflicts)


async def test_evaluate_gate_filters_stale_acknowledgments(
    repo_and_service: tuple[DeviceRepo, RoleAssignmentService],
) -> None:
    """Story 9.2 AC6 / R3 #5: an acknowledgment for a gap that has since
    closed is filtered out of ``effective_acknowledged_gaps`` at every read.
    """
    repo, service = repo_and_service
    await _insert_row(repo, "bat", first_seen_at=_t(0), role=DeviceRole.battery)
    # ``battery_missing`` is no longer an open gap, but the caller still
    # passes it in.
    outcome = await service.evaluate_gate(
        acknowledged_gaps=frozenset({"battery_missing", "inverter_missing", "ev_charger_missing"})
    )
    # The closed-gap label is dropped.
    assert "battery_missing" not in outcome.effective_acknowledged_gaps
    # The still-open labels remain.
    assert "inverter_missing" in outcome.effective_acknowledged_gaps
    assert "ev_charger_missing" in outcome.effective_acknowledged_gaps


async def test_assignments_ordered_by_first_seen_then_device_id(
    repo_and_service: tuple[DeviceRepo, RoleAssignmentService],
) -> None:
    repo, service = repo_and_service
    await _insert_row(repo, "z-late", first_seen_at=_t(5))
    await _insert_row(repo, "a-late", first_seen_at=_t(5))
    await _insert_row(repo, "m-early", first_seen_at=_t(1))
    snapshot = await service.evaluate_assignments()
    assert [row.device_id for row in snapshot.assignments] == ["m-early", "a-late", "z-late"]


async def test_no_role_device_compatibility_check_in_v1(
    repo_and_service: tuple[DeviceRepo, RoleAssignmentService],
) -> None:
    """Story 9.2 Dev Notes: v1 implements duplicate-role detection only —
    role/device-class compatibility is deliberately out of scope (the data
    model permits any role on any device row; the decision engine is the
    one that enforces role-based behavior). Test pinned to keep the v1
    scope decision honest."""
    repo, service = repo_and_service
    # A modbus row assigned the ev_charger role. The data model allows it;
    # no compatibility error is surfaced.
    await _insert_row(repo, "modbus-as-evc", first_seen_at=_t(0), role=DeviceRole.ev_charger)
    snapshot = await service.evaluate_assignments()
    assert snapshot.conflicts == frozenset()


def test_role_conflict_and_gap_are_hashable() -> None:
    """Both types live inside ``frozenset[...]`` fields and MUST hash stably."""
    c = RoleConflict(role=DeviceRole.inverter, device_ids=("a", "b"))
    g = RoleGap(role=DeviceRole.battery, severity="acknowledgeable_warn", label="battery_missing")
    assert {c} == {RoleConflict(role=DeviceRole.inverter, device_ids=("a", "b"))}
    assert {g} == {
        RoleGap(
            role=DeviceRole.battery,
            severity="acknowledgeable_warn",
            label="battery_missing",
        )
    }
