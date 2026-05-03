"""Capability profiles for supported EV charger models."""

from __future__ import annotations

from open_ems.core.devices import (
    CapabilityStatus,
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)

_FULL_CAPS = frozenset({ReadCapability.state, ReadCapability.power})
_WRITE_CAPS = frozenset({WriteCapability.set_ev_charge_current})

EV_CHARGER_PROFILES: dict[str, DeviceCapabilityProfile] = {
    "ocpp_1_6": DeviceCapabilityProfile(
        device_id="__placeholder__",
        model="ocpp_1_6",
        capability_status=CapabilityStatus.full,
        read_capabilities=_FULL_CAPS,
        write_capabilities=_WRITE_CAPS,
    ),
}
