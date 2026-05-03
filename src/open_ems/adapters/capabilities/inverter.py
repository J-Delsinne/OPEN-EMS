"""Capability profiles for supported PV inverter models."""

from __future__ import annotations

from open_ems.core.devices import (
    CapabilityStatus,
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)

_FULL_CAPS = frozenset({ReadCapability.state, ReadCapability.power})
_WRITE_CAPS = frozenset({WriteCapability.set_operating_mode})

INVERTER_PROFILES: dict[str, DeviceCapabilityProfile] = {
    "fronius_gen24_v1": DeviceCapabilityProfile(
        device_id="__placeholder__",
        model="fronius_gen24_v1",
        capability_status=CapabilityStatus.full,
        read_capabilities=_FULL_CAPS,
        write_capabilities=_WRITE_CAPS,
    ),
    "huawei_sun2000_v3": DeviceCapabilityProfile(
        device_id="__placeholder__",
        model="huawei_sun2000_v3",
        capability_status=CapabilityStatus.full,
        read_capabilities=_FULL_CAPS,
        write_capabilities=_WRITE_CAPS,
    ),
    "growatt_hybrid_v1": DeviceCapabilityProfile(
        device_id="__placeholder__",
        model="growatt_hybrid_v1",
        capability_status=CapabilityStatus.full,
        read_capabilities=_FULL_CAPS,
        write_capabilities=_WRITE_CAPS,
    ),
}
