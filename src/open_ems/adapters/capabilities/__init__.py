"""Device capability registry for all supported device models.

Provides ``get_profile()`` — the single entry point for resolving a
``DeviceCapabilityProfile`` by model string and optional firmware version.

Firmware version range matching is deferred to a future story. All known
profiles accept any ``firmware_version`` value in Epic 4 (the value is stored
in the returned profile for future use, but not used for selection).

Unknown models return a REDUCED profile with only basic read access
(``ReadCapability.state``) and no write capabilities, satisfying FR6b.

Story 9.0c (AC5): ``validate_capability_registry_alignment()`` is a startup
gate that fails loud if any adapter's ``_SUPPORTED_MODELS`` references a model
not registered in ``_ALL_PROFILES``. Lifespan invokes it before
``ActiveConstraintsProvider.hydrate()``; drift aborts boot with
``SystemExit(1)``. This prevents the silent REDUCED-profile degradation that
would otherwise present to the installer as "the device is connected but
nothing works".
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
    "BATTERY_PROFILES",
    "CapabilityRegistryDriftError",
    "EV_CHARGER_PROFILES",
    "GRID_METER_PROFILES",
    "INVERTER_PROFILES",
    "get_profile",
    "validate_capability_registry_alignment",
]

_ALL_PROFILES: dict[str, DeviceCapabilityProfile] = {
    **INVERTER_PROFILES,
    **BATTERY_PROFILES,
    **EV_CHARGER_PROFILES,
    **GRID_METER_PROFILES,
}
assert len(_ALL_PROFILES) == (
    len(INVERTER_PROFILES)
    + len(BATTERY_PROFILES)
    + len(EV_CHARGER_PROFILES)
    + len(GRID_METER_PROFILES)
), "Duplicate model key detected across capability profile categories"

_SAFE_READ_CAPS: frozenset[ReadCapability] = frozenset({ReadCapability.state})

# Fixed model strings used by adapters that do not declare a ``_SUPPORTED_MODELS``
# dict (OCPP and DSMR each address exactly one wire-level protocol version).
# Listed here so ``validate_capability_registry_alignment()`` can assert their
# presence without importing the adapter modules.
_FIXED_ADAPTER_MODELS: tuple[str, ...] = ("ocpp_1_6", "dsmr_p1")


class CapabilityRegistryDriftError(Exception):
    """Raised when an adapter's ``_SUPPORTED_MODELS`` references a model absent from the registry.

    The error message lists EVERY missing model on a single fail-loud raise so
    the operator (or CI) can fix all drift in one pass instead of iterating.
    """

    def __init__(self, missing_models: list[str]) -> None:
        self.missing_models = list(missing_models)
        super().__init__(
            "Capability registry drift detected: the following models are referenced by an "
            f"adapter's _SUPPORTED_MODELS but are missing from _ALL_PROFILES: {self.missing_models}"
        )


def validate_capability_registry_alignment() -> None:
    """Assert that every adapter-supported model is registered in ``_ALL_PROFILES``.

    Story 9.0c (AC5). Called at lifespan startup AFTER ``init_database()`` and
    BEFORE ``ActiveConstraintsProvider.hydrate()``. Raises
    ``CapabilityRegistryDriftError`` listing every missing model on a single
    fail-loud raise.

    Imports adapter ``_SUPPORTED_MODELS`` lazily inside the function body to
    avoid the circular import (the adapter modules import from this package).

    Returns ``None`` on success.
    """
    # Deferred imports break the import cycle: battery_adapter / inverter_adapter
    # both import from ``open_ems.adapters.capabilities``.
    from open_ems.adapters.modbus.battery_adapter import (
        _SUPPORTED_MODELS as _BATTERY_MODELS,
    )
    from open_ems.adapters.modbus.inverter_adapter import (
        _SUPPORTED_MODELS as _INVERTER_MODELS,
    )

    missing: list[str] = []
    for model in _BATTERY_MODELS:
        if model not in _ALL_PROFILES:
            missing.append(model)
    for model in _INVERTER_MODELS:
        if model not in _ALL_PROFILES:
            missing.append(model)
    for fixed_model in _FIXED_ADAPTER_MODELS:
        if fixed_model not in _ALL_PROFILES:
            missing.append(fixed_model)

    if missing:
        raise CapabilityRegistryDriftError(missing)


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
