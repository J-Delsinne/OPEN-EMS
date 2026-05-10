"""Unit tests for the device capability registry (adapters/capabilities/__init__.py)."""

from __future__ import annotations

import pytest

import open_ems.adapters.capabilities as capabilities_module
from open_ems.adapters.capabilities import (
    CapabilityRegistryDriftError,
    get_profile,
    validate_capability_registry_alignment,
)
from open_ems.core.devices import (
    CapabilityStatus,
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)

# ---------------------------------------------------------------------------
# Known model: inverter
# ---------------------------------------------------------------------------


def test_get_profile_fronius_returns_full_profile() -> None:
    profile = get_profile("dev-1", "fronius_gen24_v1")
    assert profile.capability_status == CapabilityStatus.full
    assert ReadCapability.state in profile.read_capabilities
    assert ReadCapability.power in profile.read_capabilities
    # Story 9.0 AC3: v1 inverter is read-only.
    assert profile.write_capabilities == frozenset()


def test_get_profile_inverter_does_not_have_charge_or_discharge() -> None:
    profile = get_profile("dev-1", "fronius_gen24_v1")
    assert WriteCapability.set_charge_rate not in profile.write_capabilities
    assert WriteCapability.set_discharge_rate not in profile.write_capabilities


def test_get_profile_huawei_returns_full_profile() -> None:
    profile = get_profile("dev-1", "huawei_sun2000_v3")
    assert profile.capability_status == CapabilityStatus.full
    assert profile.write_capabilities == frozenset()


def test_get_profile_growatt_returns_full_profile() -> None:
    profile = get_profile("dev-1", "growatt_hybrid_v1")
    assert profile.capability_status == CapabilityStatus.full
    assert ReadCapability.power in profile.read_capabilities
    assert profile.write_capabilities == frozenset()


# ---------------------------------------------------------------------------
# Known model: battery
# ---------------------------------------------------------------------------


def test_get_profile_byd_hvs_returns_full_profile() -> None:
    profile = get_profile("dev-1", "byd_hvs_v1")
    assert profile.capability_status == CapabilityStatus.full
    assert ReadCapability.soc in profile.read_capabilities
    assert WriteCapability.set_charge_rate in profile.write_capabilities
    assert WriteCapability.set_discharge_rate in profile.write_capabilities
    # Story 9.0 AC10: set_operating_mode removed from BYD profiles for v1.
    assert WriteCapability.set_operating_mode not in profile.write_capabilities


def test_get_profile_byd_hvm_returns_full_profile() -> None:
    profile = get_profile("dev-1", "byd_hvm_v1")
    assert profile.capability_status == CapabilityStatus.full
    assert ReadCapability.soc in profile.read_capabilities


# ---------------------------------------------------------------------------
# Known model: grid meter — FR6b: strictly read-only
# ---------------------------------------------------------------------------


def test_get_profile_dsmr_p1_returns_full_profile_read_only() -> None:
    profile = get_profile("dev-1", "dsmr_p1")
    assert profile.capability_status == CapabilityStatus.full
    assert ReadCapability.state in profile.read_capabilities
    assert ReadCapability.power in profile.read_capabilities
    assert ReadCapability.energy in profile.read_capabilities
    assert profile.write_capabilities == frozenset()


def test_get_profile_grid_meter_no_write_caps_fr6b() -> None:
    profile = get_profile("meter-1", "dsmr_p1")
    assert WriteCapability.set_charge_rate not in profile.write_capabilities
    assert WriteCapability.set_discharge_rate not in profile.write_capabilities
    assert WriteCapability.set_ev_charge_current not in profile.write_capabilities
    assert WriteCapability.set_operating_mode not in profile.write_capabilities


# ---------------------------------------------------------------------------
# Known model: EV charger
# ---------------------------------------------------------------------------


def test_get_profile_ocpp_1_6_returns_full_profile() -> None:
    profile = get_profile("dev-1", "ocpp_1_6")
    assert profile.capability_status == CapabilityStatus.full
    assert WriteCapability.set_ev_charge_current in profile.write_capabilities
    assert ReadCapability.state in profile.read_capabilities
    assert ReadCapability.power in profile.read_capabilities


# ---------------------------------------------------------------------------
# Unknown model
# ---------------------------------------------------------------------------


def test_get_profile_unknown_model_returns_reduced() -> None:
    profile = get_profile("dev-1", "unknown_brand_xyz")
    assert profile.capability_status == CapabilityStatus.reduced
    assert profile.write_capabilities == frozenset()


def test_get_profile_unknown_model_limitation_reason_contains_model_and_firmware() -> None:
    profile = get_profile("dev-1", "unknown_brand_xyz")
    assert profile.limitation_reason is not None
    assert "model=unknown_brand_xyz firmware=None" in profile.limitation_reason


def test_get_profile_unknown_model_with_firmware_version_in_reason() -> None:
    profile = get_profile("dev-1", "unknown_brand_xyz", "3.1.0")
    assert profile.limitation_reason is not None
    assert "model=unknown_brand_xyz" in profile.limitation_reason
    assert "firmware=3.1.0" in profile.limitation_reason


def test_get_profile_unknown_model_still_has_state_read_cap() -> None:
    profile = get_profile("dev-1", "unknown_brand_xyz")
    assert ReadCapability.state in profile.read_capabilities


# ---------------------------------------------------------------------------
# device_id substitution
# ---------------------------------------------------------------------------


def test_get_profile_substitutes_device_id() -> None:
    profile = get_profile("my-real-device-id", "fronius_gen24_v1")
    assert profile.device_id == "my-real-device-id"
    assert profile.device_id != "__placeholder__"


def test_get_profile_unknown_substitutes_device_id() -> None:
    profile = get_profile("real-id-99", "no_such_model")
    assert profile.device_id == "real-id-99"


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------


def test_get_profile_write_capabilities_is_frozenset() -> None:
    profile = get_profile("dev-1", "byd_hvs_v1")
    assert isinstance(profile.write_capabilities, frozenset)


def test_get_profile_read_capabilities_is_frozenset() -> None:
    profile = get_profile("dev-1", "fronius_gen24_v1")
    assert isinstance(profile.read_capabilities, frozenset)


def test_get_profile_returns_device_capability_profile_instance() -> None:
    profile = get_profile("dev-1", "ocpp_1_6")
    assert isinstance(profile, DeviceCapabilityProfile)


# ---------------------------------------------------------------------------
# Enum member validity
# ---------------------------------------------------------------------------


def test_capability_status_members_are_valid() -> None:
    assert CapabilityStatus.full == "full"
    assert CapabilityStatus.reduced == "reduced"
    assert CapabilityStatus.unsupported == "unsupported"


# ---------------------------------------------------------------------------
# firmware_version stored in returned profile (Epic 4 placeholder)
# ---------------------------------------------------------------------------


def test_get_profile_stores_firmware_version_in_returned_profile() -> None:
    profile = get_profile("dev-1", "fronius_gen24_v1", "1.2.3")
    assert profile.firmware_version == "1.2.3"


def test_get_profile_known_model_with_any_firmware_returns_full_profile() -> None:
    """Known model + any firmware version must still return FULL (deferred firmware matching)."""
    profile = get_profile("dev-1", "fronius_gen24_v1", "99.99.99-unknown")
    assert profile.capability_status == CapabilityStatus.full
    assert ReadCapability.state in profile.read_capabilities


def test_get_profile_none_firmware_version_stored() -> None:
    profile = get_profile("dev-1", "fronius_gen24_v1")
    assert profile.firmware_version is None


# ---------------------------------------------------------------------------
# AC5 (Story 9.0c) — validate_capability_registry_alignment()
# ---------------------------------------------------------------------------


def test_validate_capability_registry_alignment_success() -> None:
    """With current ``_SUPPORTED_MODELS`` and registry state, returns None and does not raise."""
    assert validate_capability_registry_alignment() is None


def test_validate_capability_registry_alignment_detects_single_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkey-patch the battery adapter's _SUPPORTED_MODELS to inject a missing model."""
    from open_ems.adapters.modbus import battery_adapter

    # Snapshot the original dict and inject a bogus model not in the registry.
    original = dict(battery_adapter._SUPPORTED_MODELS)
    patched = dict(original)
    # The value type doesn't matter for this check — the validator only inspects keys.
    patched["future_byd_model_v2"] = next(iter(original.values()))
    monkeypatch.setattr(battery_adapter, "_SUPPORTED_MODELS", patched)

    with pytest.raises(CapabilityRegistryDriftError) as exc_info:
        validate_capability_registry_alignment()

    assert "future_byd_model_v2" in exc_info.value.missing_models
    assert "future_byd_model_v2" in str(exc_info.value)


def test_validate_capability_registry_alignment_detects_multi_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inject TWO missing models across the two Modbus adapters; both must be named."""
    from open_ems.adapters.modbus import battery_adapter, inverter_adapter

    bat_patched = dict(battery_adapter._SUPPORTED_MODELS)
    bat_patched["mystery_battery_v9"] = next(iter(bat_patched.values()))
    monkeypatch.setattr(battery_adapter, "_SUPPORTED_MODELS", bat_patched)

    inv_patched = dict(inverter_adapter._SUPPORTED_MODELS)
    inv_patched["mystery_inverter_v9"] = next(iter(inv_patched.values()))
    monkeypatch.setattr(inverter_adapter, "_SUPPORTED_MODELS", inv_patched)

    with pytest.raises(CapabilityRegistryDriftError) as exc_info:
        validate_capability_registry_alignment()

    assert "mystery_battery_v9" in exc_info.value.missing_models
    assert "mystery_inverter_v9" in exc_info.value.missing_models


def test_validate_capability_registry_alignment_detects_fixed_model_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove ``ocpp_1_6`` from ``_ALL_PROFILES``; the validator must name it as missing."""
    patched_profiles = {
        k: v for k, v in capabilities_module._ALL_PROFILES.items() if k != "ocpp_1_6"
    }
    monkeypatch.setattr(capabilities_module, "_ALL_PROFILES", patched_profiles)

    with pytest.raises(CapabilityRegistryDriftError) as exc_info:
        validate_capability_registry_alignment()

    assert "ocpp_1_6" in exc_info.value.missing_models


def test_capability_registry_drift_error_lists_all_missing() -> None:
    """The exception message contains every missing model in a single fail-loud raise."""
    err = CapabilityRegistryDriftError(["model_a", "model_b", "model_c"])
    msg = str(err)
    assert "model_a" in msg
    assert "model_b" in msg
    assert "model_c" in msg
    assert err.missing_models == ["model_a", "model_b", "model_c"]


# ---------------------------------------------------------------------------
# D1 (Story 9.0c review): maintenance gate against ``_FIXED_ADAPTER_MODELS`` drift
#
# The startup gate ``validate_capability_registry_alignment()`` iterates
# ``_FIXED_ADAPTER_MODELS`` to know which fixed-model adapters to validate.
# If a new fixed-model adapter is added without updating this tuple, the
# startup gate silently passes. This test discovers ``_FIXED_MODEL`` constants
# across the ``open_ems.adapters`` package tree and asserts each is registered.
# ---------------------------------------------------------------------------


def test_maintenance_every_adapter_fixed_model_is_registered() -> None:
    """Every adapter module declaring ``_FIXED_MODEL`` must be in ``_FIXED_ADAPTER_MODELS``.

    Maintenance gate: if a new fixed-model adapter is added (declaring its model
    via ``_FIXED_MODEL = "..."``) without updating ``_FIXED_ADAPTER_MODELS`` in
    ``adapters/capabilities/__init__.py``, AC5's startup gate silently approves
    boot. This test discovers the constants by walking the adapter package and
    fails CI when the registration is missing.
    """
    import importlib
    import pkgutil

    import open_ems.adapters

    declared: dict[str, str] = {}
    for module_info in pkgutil.walk_packages(
        open_ems.adapters.__path__,
        prefix="open_ems.adapters.",
    ):
        try:
            module = importlib.import_module(module_info.name)
        except ImportError:
            # Adapter modules with optional dependencies may fail to import in
            # the unit-test environment; skip them — they will fail elsewhere
            # if actually used.
            continue
        fixed_model = getattr(module, "_FIXED_MODEL", None)
        if fixed_model is not None:
            declared[module_info.name] = fixed_model

    # Sanity: the known OCPP and DSMR fixed-model adapters must be discovered.
    discovered_values = set(declared.values())
    assert "ocpp_1_6" in discovered_values, (
        "OCPP charger adapter should declare ``_FIXED_MODEL``; test cannot "
        "validate the maintenance invariant if discovery returns nothing."
    )
    assert "dsmr_p1" in discovered_values

    # Every declared ``_FIXED_MODEL`` must be in ``_FIXED_ADAPTER_MODELS``.
    for module_name, fixed_model in declared.items():
        assert fixed_model in capabilities_module._FIXED_ADAPTER_MODELS, (
            f"{module_name}._FIXED_MODEL={fixed_model!r} is not registered in "
            f"``adapters.capabilities._FIXED_ADAPTER_MODELS``. Add it so AC5's "
            f"startup gate validates this model against the capability registry."
        )
