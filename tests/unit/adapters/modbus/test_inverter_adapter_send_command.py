"""Unit tests for ``InverterAdapter.send_command`` (Story 9.0 AC3, AC10, AC11).

v1 inverters are read-only — every command type raises ``TypeError`` directly.
PolicyGuard's capability gate is the primary rejection point; the adapter's
TypeError is defense in depth.
"""

from __future__ import annotations

import pytest

from open_ems.adapters.modbus.inverter_adapter import InverterAdapter
from open_ems.core.commands import (
    CommandOrigin,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.core.devices import DeviceRole


class FakeModbusProtocolAdapter:
    """Minimal fake — InverterAdapter.send_command never reaches the protocol layer."""

    async def get_raw_state(self) -> None:  # pragma: no cover
        raise AssertionError("send_command must not call protocol layer")

    async def send_raw_command(self, _: object) -> None:  # pragma: no cover
        raise AssertionError("InverterAdapter.send_command must not call send_raw_command")

    async def close(self) -> None:
        pass


def _make_adapter(model: str = "fronius_gen24_v1") -> InverterAdapter:
    return InverterAdapter(
        device_id="inv-001",
        protocol_adapter=FakeModbusProtocolAdapter(),  # type: ignore[arg-type]
        model=model,
    )


def _battery_charge_cmd() -> SetBatteryChargeRateCommand:
    return SetBatteryChargeRateCommand(
        device_id="inv-001",
        device_role=DeviceRole.inverter,
        origin=CommandOrigin.decision_engine,
        rate_kw=2.0,
    )


def _battery_discharge_cmd() -> SetBatteryDischargeRateCommand:
    return SetBatteryDischargeRateCommand(
        device_id="inv-001",
        device_role=DeviceRole.inverter,
        origin=CommandOrigin.decision_engine,
        rate_kw=2.0,
    )


def _ev_rate_cmd() -> SetEVChargingRateCommand:
    return SetEVChargingRateCommand(
        device_id="inv-001",
        device_role=DeviceRole.inverter,
        origin=CommandOrigin.decision_engine,
        rate_kw=2.0,
    )


def _stop_cmd() -> StopEVChargingCommand:
    return StopEVChargingCommand(
        device_id="inv-001",
        device_role=DeviceRole.inverter,
        origin=CommandOrigin.decision_engine,
    )


@pytest.mark.parametrize(
    "command_factory",
    [_battery_charge_cmd, _battery_discharge_cmd, _ev_rate_cmd, _stop_cmd],
)
@pytest.mark.parametrize(
    "model",
    ["fronius_gen24_v1", "huawei_sun2000_v3", "growatt_hybrid_v1"],
)
async def test_inverter_send_command_raises_typeerror_for_every_command_type(
    command_factory: object, model: str
) -> None:
    adapter = _make_adapter(model=model)
    cmd = command_factory()  # type: ignore[operator]
    with pytest.raises(TypeError, match="InverterAdapter does not support"):
        await adapter.send_command(cmd)
