"""Unit tests for the device capability registry (adapters/capabilities/__init__.py)."""

from __future__ import annotations

from open_ems.adapters.capabilities import (
    get_profile,
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
    assert WriteCapability.set_operating_mode in profile.write_capabilities


def test_get_profile_inverter_does_not_have_charge_or_discharge() -> None:
    profile = get_profile("dev-1", "fronius_gen24_v1")
    assert WriteCapability.set_charge_rate not in profile.write_capabilities
    assert WriteCapability.set_discharge_rate not in profile.write_capabilities


def test_get_profile_huawei_returns_full_profile() -> None:
    profile = get_profile("dev-1", "huawei_sun2000_v3")
    assert profile.capability_status == CapabilityStatus.full
    assert WriteCapability.set_operating_mode in profile.write_capabilities


def test_get_profile_growatt_returns_full_profile() -> None:
    profile = get_profile("dev-1", "growatt_hybrid_v1")
    assert profile.capability_status == CapabilityStatus.full
    assert ReadCapability.power in profile.read_capabilities


# ---------------------------------------------------------------------------
# Known model: battery
# ---------------------------------------------------------------------------


def test_get_profile_byd_hvs_returns_full_profile() -> None:
    profile = get_profile("dev-1", "byd_hvs_v1")
    assert profile.capability_status == CapabilityStatus.full
    assert ReadCapability.soc in profile.read_capabilities
    assert WriteCapability.set_charge_rate in profile.write_capabilities
    assert WriteCapability.set_discharge_rate in profile.write_capabilities
    assert WriteCapability.set_operating_mode in profile.write_capabilities


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
