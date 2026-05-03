"""Device capability registry for all supported device models.

Provides ``get_profile()`` — the single entry point for resolving a
``DeviceCapabilityProfile`` by model string and optional firmware version.

Firmware version range matching is deferred to a future story. All known
profiles accept any ``firmware_version`` value in Epic 4 (the value is stored
in the returned profile for future use, but not used for selection).

Unknown models return a REDUCED profile with only basic read access
(``ReadCapability.state``) and no write capabilities, satisfying FR6b.
"""

from __future__ import annotations

from open_ems.adapters.capabilities.battery import BATTERY_PROFILES
from open_ems.adapters.capabilities.ev_charger import EV_CHARGER_PROFILES
from open_ems.adapters.capabilities.grid_meter import GRID_METER_PROFILES
from open_ems.adapters.capabilities.inverter import INVERTER_PROFILES
from open_ems.core.devices import (
    CapabilityStatus,
    DeviceCapabilityProfile,
    ReadCapability,
)

__all__ = [
    "get_profile",
    "INVERTER_PROFILES",
    "BATTERY_PROFILES",
    "EV_CHARGER_PROFILES",
    "GRID_METER_PROFILES",
]

_ALL_PROFILES: dict[str, DeviceCapabilityProfile] = {
    **INVERTER_PROFILES,
    **BATTERY_PROFILES,
    **EV_CHARGER_PROFILES,
    **GRID_METER_PROFILES,
}
assert len(_ALL_PROFILES) == (
    len(INVERTER_PROFILES) + len(BATTERY_PROFILES) + len(EV_CHARGER_PROFILES) + len(GRID_METER_PROFILES)
), "Duplicate model key detected across capability profile categories"

_SAFE_READ_CAPS: frozenset[ReadCapability] = frozenset({ReadCapability.state})


def get_profile(
    device_id: str,
    model: str,
    firmware_version: str | None = None,
) -> DeviceCapabilityProfile:
    """Return the capability profile for the given device model.

    If the model is unknown, returns a REDUCED profile with write capabilities
    blocked and only basic state read exposed. Never raises.

    Args:
        device_id: The device's unique identifier (substituted into returned profile).
        model: The device model string (registry key).
        firmware_version: Optional firmware version — stored in profile but not
            used for selection in Epic 4 (firmware range matching is deferred).
    """
    template = _ALL_PROFILES.get(model)
    if template is None:
        return _unknown_profile(device_id, model, firmware_version)
    # Firmware version matching is deferred (Epic 4 placeholder: all known profiles
    # accept any firmware_version). Store the value for future use only.
    return template.model_copy(
        update={"device_id": device_id, "firmware_version": firmware_version}
    )


def _unknown_profile(
    device_id: str,
    model: str,
    firmware_version: str | None,
) -> DeviceCapabilityProfile:
    reason = f"capability_profile_unknown: model={model} firmware={firmware_version}"
    return DeviceCapabilityProfile(
        device_id=device_id,
        model=model,
        firmware_version=firmware_version,
        capability_status=CapabilityStatus.reduced,
        read_capabilities=_SAFE_READ_CAPS,
        write_capabilities=frozenset(),
        known_limitations=(reason,),
        limitation_reason=reason,
    )
