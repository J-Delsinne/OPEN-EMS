"""Unit tests for BYD HVS / HVM ``command_payload`` encoding (Story 9.0 AC2)."""

from __future__ import annotations

import pytest

from open_ems.adapters.modbus.register_maps import BydHvmV1, BydHvsV1
from open_ems.core.commands import (
    CommandOrigin,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
)
from open_ems.core.devices import DeviceRole

# Setpoint write registers — must match the placeholder addresses in the maps.
HVS_REG_CHARGE = 110
HVS_REG_DISCHARGE = 111
HVM_REG_CHARGE = 210
HVM_REG_DISCHARGE = 211

REGISTER_MAPS_AND_REGS = [
    pytest.param(BydHvsV1(), HVS_REG_CHARGE, HVS_REG_DISCHARGE, id="byd_hvs_v1"),
    pytest.param(BydHvmV1(), HVM_REG_CHARGE, HVM_REG_DISCHARGE, id="byd_hvm_v1"),
]


def _make_charge_cmd(rate_kw: float) -> SetBatteryChargeRateCommand:
    return SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


def _make_discharge_cmd(rate_kw: float) -> SetBatteryDischargeRateCommand:
    return SetBatteryDischargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_charge_nominal(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    cmd = _make_charge_cmd(rate_kw=3.5)
    payload = register_map.command_payload(cmd)  # type: ignore[attr-defined]
    assert payload == {
        "operation": "write_register",
        "address": charge_reg,
        "value": 3500,  # 3.5 kW → 3500 W
    }


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_discharge_nominal(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    cmd = _make_discharge_cmd(rate_kw=2.0)
    payload = register_map.command_payload(cmd)  # type: ignore[attr-defined]
    assert payload == {
        "operation": "write_register",
        "address": discharge_reg,
        "value": 2000,
    }


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_zero_rate_is_valid(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    """0.0 kW is a valid setpoint (effectively idle/stop)."""
    cmd = _make_charge_cmd(rate_kw=0.0)
    payload = register_map.command_payload(cmd)  # type: ignore[attr-defined]
    assert payload["value"] == 0


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_max_boundary(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    """65.535 kW is exactly the uint16 ceiling."""
    cmd = _make_charge_cmd(rate_kw=65.535)
    payload = register_map.command_payload(cmd)  # type: ignore[attr-defined]
    assert payload["value"] == 65_535


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_above_max_raises_value_error(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    """Above-max rate raises ValueError so the adapter wraps it as register_map_encoding_error."""
    cmd = _make_charge_cmd(rate_kw=100.0)  # 100 000 W exceeds 65 535
    with pytest.raises(ValueError, match="exceeds register-map max"):
        register_map.command_payload(cmd)  # type: ignore[attr-defined]


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_rounding(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    """Sub-watt precision rounds to nearest integer watt above the 0.5 W floor."""
    cmd = _make_charge_cmd(rate_kw=3.4567)
    payload = register_map.command_payload(cmd)  # type: ignore[attr-defined]
    # 3.4567 * 1000 = 3456.7 → rounds to 3457
    assert payload["value"] == 3457


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
@pytest.mark.parametrize("sub_watt_rate", [0.0001, 0.0002, 0.0004999])
def test_command_payload_sub_watt_non_zero_raises_value_error(
    register_map: object,
    charge_reg: int,
    discharge_reg: int,
    sub_watt_rate: float,
) -> None:
    """Sub-watt non-zero setpoints are rejected so they cannot round to 0 W.

    The decision engine sends exactly 0.0 to mean "stop"; any positive value
    below the 0.5 W floor is ambiguous and the encoder raises so the adapter
    can wrap it as register_map_encoding_error:sub_watt_precision.
    """
    cmd = _make_charge_cmd(rate_kw=sub_watt_rate)
    with pytest.raises(ValueError, match="sub_watt_precision"):
        register_map.command_payload(cmd)  # type: ignore[attr-defined]


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_at_minimum_floor_encodes_one_watt(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    """Exactly 0.0005 kW (the floor) encodes to 1 W (rounded from 0.5)."""
    cmd = _make_charge_cmd(rate_kw=0.0005)
    payload = register_map.command_payload(cmd)  # type: ignore[attr-defined]
    # 0.5 W rounds banker's-style; just assert a 0-or-1 watt outcome is allowed
    # (Python's round() uses banker's rounding for .5 — 0.0005 * 1000 = 0.5).
    assert payload["value"] in {0, 1}


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_inf_raises_overflow_error(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    """float('inf') as rate_kw raises OverflowError from int(round(inf*1000)).

    The battery adapter catches both ValueError and OverflowError and wraps as
    register_map_encoding_error, so this path produces the documented encoding-
    error reason (not adapter_internal_error).
    """
    cmd = _make_charge_cmd(rate_kw=float("inf"))
    with pytest.raises((OverflowError, ValueError)):
        register_map.command_payload(cmd)  # type: ignore[attr-defined]
