"""Step-1 advance gate for the installer wizard (Story 9.1 AC7).

The route handler runs ``evaluate`` before flipping ``WizardState.step_1_complete``.
The gate fails when any registered device is ``validated=0`` AND lacks
``installer_acknowledged_unvalidated_at``. The set of unacknowledged rows is
returned so the inline error banner can list them.
"""

from __future__ import annotations

from dataclasses import dataclass

from open_ems.storage.repositories.device_repo import DeviceRegistryEntry, DeviceRepo


@dataclass(frozen=True)
class WizardGateOutcome:
    """Whether step 1 may advance, and (if not) which rows need acknowledgement."""

    can_advance: bool
    unacknowledged_unvalidated: tuple[DeviceRegistryEntry, ...]


class WizardGateService:
    def __init__(self, device_repo: DeviceRepo) -> None:
        self._device_repo = device_repo

    async def evaluate(self) -> WizardGateOutcome:
        rows = await self._device_repo.list_all()
        blockers: list[DeviceRegistryEntry] = []
        for row in rows:
            if row.validated:
                continue
            if row.installer_acknowledged_unvalidated_at is not None:
                continue
            blockers.append(row)
        return WizardGateOutcome(
            can_advance=not blockers,
            unacknowledged_unvalidated=tuple(blockers),
        )
