"""``RoleAssignmentService`` — Step 2 single evaluator (Story 9.2 AC3).

The service owns conflict detection and Step-2 gate evaluation. Route
handlers, templates, and downstream consumers (Step 4 validation in 9.4) all
call into this surface so the rule set has exactly one implementation.

Hard block vs. acknowledgeable is a property of *role*:

    - DeviceRole.grid_meter → 'hard_block' when missing; never bypassable.
    - DeviceRole.battery / inverter / ev_charger → 'acknowledgeable_warn'
      when missing; the gate clears when the corresponding label is in
      ``acknowledged_gaps``.

Conflicts (two rows sharing a non-NULL role) are *always* blocking,
regardless of acknowledgments. The only resolution is to change one of the
colliding rows' roles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from open_ems.core.devices import _VALID_GAP_LABELS, DeviceRole
from open_ems.storage.repositories.device_repo import DeviceRepo

# Story 9.2 AC3 — ``_VALID_GAP_LABELS`` is the single authoritative whitelist
# of acknowledgeable role gaps. It lives in ``core.devices`` so storage,
# service, model-validator, and route-handler readers can all import the SAME
# frozenset without crossing architectural layers. ``grid_meter_missing`` is
# deliberately absent — the grid meter is a hard block (AC5) and admitting it
# to the whitelist would silently allow the safety property to be bypassed.
# Re-exported here so existing consumers ``from open_ems.services.role_assignment
# import _VALID_GAP_LABELS`` keep working.


_GapSeverity = Literal["hard_block", "acknowledgeable_warn"]


def _label_for_role(role: DeviceRole) -> str:
    return f"{role.value}_missing"


def _severity_for_missing_role(role: DeviceRole) -> _GapSeverity:
    if role is DeviceRole.grid_meter:
        return "hard_block"
    return "acknowledgeable_warn"


class RoleConflict(BaseModel):
    """Two or more registry rows sharing the same non-NULL role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: DeviceRole
    device_ids: tuple[str, ...]

    def __hash__(self) -> int:  # frozen + tuple fields → hashable for frozenset
        return hash((self.role, self.device_ids))


class RoleGap(BaseModel):
    """A required-or-warn role with no device assigned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: DeviceRole
    severity: _GapSeverity
    label: str

    def __hash__(self) -> int:
        return hash((self.role, self.severity, self.label))


class RoleAssignmentRow(BaseModel):
    """One device-registry row as it appears in the Step-2 list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    model: str | None
    role: DeviceRole | None
    conflicting_device_ids: tuple[str, ...]


class RoleAssignmentSnapshot(BaseModel):
    """Pure-function product of a registry read at a single instant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assignments: tuple[RoleAssignmentRow, ...]
    conflicts: frozenset[RoleConflict]
    gaps: frozenset[RoleGap]
    unassigned_count: int


@dataclass(frozen=True)
class RoleGateOutcome:
    """Composed gate result. ``can_advance`` AND-folds the three sub-conditions."""

    can_advance: bool
    blocking_gaps: tuple[RoleGap, ...]
    unacknowledged_warnings: tuple[RoleGap, ...]
    conflicts: tuple[RoleConflict, ...]
    effective_acknowledged_gaps: frozenset[str]


class RoleAssignmentService:
    def __init__(self, device_repo: DeviceRepo) -> None:
        self._device_repo = device_repo

    async def evaluate_assignments(self) -> RoleAssignmentSnapshot:
        rows = await self._device_repo.list_all()
        # Group device_ids by role to detect duplicates. The grouping happens
        # over the same ordered list, so collision-order is deterministic.
        by_role: dict[DeviceRole, list[str]] = {}
        for row in rows:
            if row.role is None:
                continue
            by_role.setdefault(row.role, []).append(row.device_id)

        conflicts: set[RoleConflict] = set()
        conflicting_peers: dict[str, tuple[str, ...]] = {}
        for role, device_ids in by_role.items():
            if len(device_ids) < 2:
                continue
            conflicts.add(RoleConflict(role=role, device_ids=tuple(device_ids)))
            for dev in device_ids:
                conflicting_peers[dev] = tuple(other for other in device_ids if other != dev)

        assignments = tuple(
            RoleAssignmentRow(
                device_id=row.device_id,
                model=row.model,
                role=row.role,
                conflicting_device_ids=conflicting_peers.get(row.device_id, ()),
            )
            for row in rows
        )

        present_roles = set(by_role.keys())
        gaps: set[RoleGap] = set()
        for role in DeviceRole:
            if role in present_roles:
                continue
            severity = _severity_for_missing_role(role)
            gaps.add(RoleGap(role=role, severity=severity, label=_label_for_role(role)))

        unassigned_count = sum(1 for row in rows if row.role is None)

        return RoleAssignmentSnapshot(
            assignments=assignments,
            conflicts=frozenset(conflicts),
            gaps=frozenset(gaps),
            unassigned_count=unassigned_count,
        )

    async def evaluate_gate(
        self,
        *,
        acknowledged_gaps: frozenset[str],
    ) -> RoleGateOutcome:
        snapshot = await self.evaluate_assignments()
        currently_open_labels = {gap.label for gap in snapshot.gaps}
        # Stale-acknowledgment filter (AC6 step 4 + R6 drift call-out):
        # only project acknowledgments onto labels that correspond to *currently
        # open* gaps. The route handler writes back this filtered set so the
        # persisted state matches what any subsequent reader will see.
        effective = frozenset(
            label for label in acknowledged_gaps if label in currently_open_labels
        )

        blocking_gaps = tuple(
            sorted(
                (gap for gap in snapshot.gaps if gap.severity == "hard_block"),
                key=lambda g: g.role.value,
            )
        )
        unacknowledged_warnings = tuple(
            sorted(
                (
                    gap
                    for gap in snapshot.gaps
                    if gap.severity == "acknowledgeable_warn" and gap.label not in effective
                ),
                key=lambda g: g.role.value,
            )
        )
        conflicts = tuple(sorted(snapshot.conflicts, key=lambda c: c.role.value))

        can_advance = not blocking_gaps and not unacknowledged_warnings and not conflicts
        return RoleGateOutcome(
            can_advance=can_advance,
            blocking_gaps=blocking_gaps,
            unacknowledged_warnings=unacknowledged_warnings,
            conflicts=conflicts,
            effective_acknowledged_gaps=effective,
        )


__all__ = [
    "RoleAssignmentRow",
    "RoleAssignmentService",
    "RoleAssignmentSnapshot",
    "RoleConflict",
    "RoleGap",
    "RoleGateOutcome",
    "_VALID_GAP_LABELS",
]
