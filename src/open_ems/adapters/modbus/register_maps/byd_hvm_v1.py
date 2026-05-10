# Device: BYD Battery-Box Premium HVM (High Voltage Module)
# Protocol: Modbus TCP
# Firmware assumed: BYD Battery-Box HVM Modbus TCP interface v1 (byd_hvm_v1)
# Source: BYD Battery-Box Modbus Protocol Specification (request from installer)
# PLACEHOLDER — register addresses must be verified against actual device documentation
#
# Battery power sign convention (system-wide, per Story 4.1):
#   battery_power_kw > 0  →  charging (consuming power from PV or grid)
#   battery_power_kw < 0  →  discharging (providing power to loads or grid)
#
# BYD HVM native convention: power is unsigned (magnitude only); direction is
# reported in a separate register (0 = charge, 1 = discharge).
# Conversion: battery_power_kw = +magnitude if direction == 0 else -magnitude

from __future__ import annotations

from open_ems.adapters.modbus.register_maps._base import (
    InvalidRegisterValueError,
    ModbusCommandPayload,
    _require_reg,
)
from open_ems.adapters.modbus.register_maps.utils import scale, uint16
from open_ems.adapters.protocol import RawModbusState
from open_ems.core.commands import (
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
)
from open_ems.core.devices import BatteryState

_REG_SOC = 200  # uint16, unit: 0.1 % (divide by 10 → %)  # PLACEHOLDER
_REG_CAPACITY_WH = 201  # uint16, unit: 100 Wh (multiply by 0.1 → kWh)  # PLACEHOLDER
_REG_POWER_W = 202  # uint16, unit: W (unsigned magnitude)  # PLACEHOLDER
_REG_DIRECTION = 204  # uint16, 0 = charging, 1 = discharging  # PLACEHOLDER
_REG_OPERATING_MODE = 205  # uint16 enum: 0=normal, 1=standby, 2=fault  # PLACEHOLDER

# Setpoint write registers — placeholder addresses; verify against BYD docs at
# installer commissioning. Encoded value is unsigned watts (uint16, 0..65 535).
_REG_CHARGE_RATE_SETPOINT_W = 210  # PLACEHOLDER
_REG_DISCHARGE_RATE_SETPOINT_W = 211  # PLACEHOLDER

_MAX_SETPOINT_W = 65_535  # uint16 ceiling
_MAX_SETPOINT_KW = _MAX_SETPOINT_W / 1000.0  # 65.535 kW
# Sub-watt setpoints would round to 0 W; rejected so the caller cannot
# accidentally request a stop via rounding. See byd_hvs_v1._encode_setpoint_w.
_MIN_NONZERO_SETPOINT_KW = 0.0005  # 0.5 W — half the register's unit step

_DIRECTION_CHARGE = 0
_OPERATING_MODE_MAP: dict[int, str] = {
    0: "normal",
    1: "standby",
    2: "fault",
}

__all__ = ["BydHvmV1"]


def _encode_setpoint_w(rate_kw: float, *, command_type: str) -> int:
    """Encode rate_kw → unsigned-watts uint16 register value, raising on overflow."""
    if rate_kw < 0.0:
        # Pydantic Field(ge=0) on the command catches this earlier — defense in depth.
        raise ValueError(f"{command_type} rate_kw must be >= 0 (got {rate_kw})")
    if 0.0 < rate_kw < _MIN_NONZERO_SETPOINT_KW:
        raise ValueError(
            f"sub_watt_precision: {command_type} rate_kw={rate_kw} below "
            f"{_MIN_NONZERO_SETPOINT_KW} kW minimum (send 0.0 to stop)"
        )
    watts = int(round(rate_kw * 1000))
    if watts > _MAX_SETPOINT_W:
        raise ValueError(
            f"{command_type} rate_kw={rate_kw} exceeds register-map max ({_MAX_SETPOINT_KW} kW)"
        )
    return watts


class BydHvmV1:
    """Register map for BYD Battery-Box Premium HVM (Modbus TCP, byd_hvm_v1)."""

    def map_state(self, raw: RawModbusState) -> BatteryState:
        regs = raw.registers
        soc_raw = _require_reg(regs, _REG_SOC)
        capacity_raw = _require_reg(regs, _REG_CAPACITY_WH)
        power_raw = _require_reg(regs, _REG_POWER_W)
        direction_raw = _require_reg(regs, _REG_DIRECTION)
        mode_raw = _require_reg(regs, _REG_OPERATING_MODE)

        power_kw = scale(uint16(power_raw), 1 / 1000.0)
        if direction_raw not in {0, 1}:
            raise InvalidRegisterValueError(
                _REG_DIRECTION, direction_raw, "expected 0=charge or 1=discharge"
            )
        battery_power_kw = power_kw if direction_raw == _DIRECTION_CHARGE else -power_kw

        return BatteryState(
            device_id=raw.device_id,
            soc_percent=scale(uint16(soc_raw), 1 / 10.0),
            battery_power_kw=battery_power_kw,
            capacity_kwh=scale(uint16(capacity_raw), 1 / 10.0),
            operating_mode=_OPERATING_MODE_MAP.get(mode_raw, "unknown"),
            read_at=raw.read_at,
        )

    def command_payload(
        self,
        cmd: SetBatteryChargeRateCommand | SetBatteryDischargeRateCommand,
    ) -> ModbusCommandPayload:
        """Encode a battery setpoint into a Modbus ``write_register`` payload."""
        if isinstance(cmd, SetBatteryChargeRateCommand):
            address = _REG_CHARGE_RATE_SETPOINT_W
            command_type = "set_battery_charge_rate"
        elif isinstance(cmd, SetBatteryDischargeRateCommand):
            address = _REG_DISCHARGE_RATE_SETPOINT_W
            command_type = "set_battery_discharge_rate"
        else:  # pragma: no cover — caller ensures command type
            raise TypeError(f"BydHvmV1.command_payload does not support {type(cmd).__name__}")
        value = _encode_setpoint_w(cmd.rate_kw, command_type=command_type)
        return {
            "operation": "write_register",
            "address": address,
            "value": value,
        }
