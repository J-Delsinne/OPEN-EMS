"""Capability profiles for supported PV inverter models.

Story 9.0 AC3 / AC10: v1 inverters are read-only. ``InverterAdapter.send_command``
raises ``TypeError`` for any command type, and these profiles correspondingly
declare an empty ``write_capabilities``. PolicyGuard's capability gate rejects
every command type before it reaches the adapter; the adapter's TypeError is
defense in depth. If a future inverter model exposes a Modbus write surface,
add the corresponding ``WriteCapability`` member here together with the
``InverterAdapter.send_command`` implementation in lockstep.
"""

from __future__ import annotations

from open_ems.core.devices import (
    CapabilityStatus,
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)

_FULL_CAPS = frozenset({ReadCapability.state, ReadCapability.power})
_WRITE_CAPS: frozenset[WriteCapability] = frozenset()

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
