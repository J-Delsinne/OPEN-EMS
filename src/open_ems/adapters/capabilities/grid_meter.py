"""Capability profiles for supported grid meter models.

Grid meters are strictly read-only devices — FR6b is enforced here:
write_capabilities is always frozenset() for grid meters.
"""

from __future__ import annotations

from open_ems.core.devices import (
    CapabilityStatus,
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)

_FULL_CAPS = frozenset({ReadCapability.state, ReadCapability.power, ReadCapability.energy})
_WRITE_CAPS: frozenset[WriteCapability] = frozenset()  # read-only device — FR6b enforced here

GRID_METER_PROFILES: dict[str, DeviceCapabilityProfile] = {
    "dsmr_p1": DeviceCapabilityProfile(
        device_id="__placeholder__",
        model="dsmr_p1",
        capability_status=CapabilityStatus.full,
        read_capabilities=_FULL_CAPS,
        write_capabilities=_WRITE_CAPS,
    ),
}
