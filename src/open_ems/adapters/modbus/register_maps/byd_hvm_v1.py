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
    _require_reg,
)
from open_ems.adapters.modbus.register_maps.utils import scale, uint16
from open_ems.adapters.protocol import RawModbusState
from open_ems.core.devices import BatteryState

_REG_SOC = 200  # uint16, unit: 0.1 % (divide by 10 → %)  # PLACEHOLDER
_REG_CAPACITY_WH = 201  # uint16, unit: 100 Wh (multiply by 0.1 → kWh)  # PLACEHOLDER
_REG_POWER_W = 202  # uint16, unit: W (unsigned magnitude)  # PLACEHOLDER
_REG_DIRECTION = 204  # uint16, 0 = charging, 1 = discharging  # PLACEHOLDER
_REG_OPERATING_MODE = 205  # uint16 enum: 0=normal, 1=standby, 2=fault  # PLACEHOLDER

_DIRECTION_CHARGE = 0
_OPERATING_MODE_MAP: dict[int, str] = {
    0: "normal",
    1: "standby",
    2: "fault",
}

__all__ = ["BydHvmV1"]


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
