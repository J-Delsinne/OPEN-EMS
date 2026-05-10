"""Capability profiles for supported battery storage models.

Story 9.0 decision (AC10): ``set_operating_mode`` is removed from BYD HVS/HVM
profiles for v1. ``BatteryAdapter.send_command`` only implements rate setpoints
(``set_charge_rate``, ``set_discharge_rate``); declaring an operating-mode
capability without an implementation would violate the profile↔implementation
lockstep invariant. If a future BYD model exposes a discrete operating-mode
register write, add it back together with the corresponding command type.
"""

from __future__ import annotations

from open_ems.core.devices import (
    CapabilityStatus,
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)

_FULL_CAPS = frozenset({ReadCapability.state, ReadCapability.power, ReadCapability.soc})
_WRITE_CAPS = frozenset(
    {
        WriteCapability.set_charge_rate,
        WriteCapability.set_discharge_rate,
    }
)

BATTERY_PROFILES: dict[str, DeviceCapabilityProfile] = {
    "byd_hvs_v1": DeviceCapabilityProfile(
        device_id="__placeholder__",
        model="byd_hvs_v1",
        capability_status=CapabilityStatus.full,
        read_capabilities=_FULL_CAPS,
        write_capabilities=_WRITE_CAPS,
    ),
    "byd_hvm_v1": DeviceCapabilityProfile(
        device_id="__placeholder__",
        model="byd_hvm_v1",
        capability_status=CapabilityStatus.full,
        read_capabilities=_FULL_CAPS,
        write_capabilities=_WRITE_CAPS,
    ),
}
