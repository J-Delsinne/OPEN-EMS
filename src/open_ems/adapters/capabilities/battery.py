"""Capability profiles for supported battery storage models."""

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
        WriteCapability.set_operating_mode,
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
