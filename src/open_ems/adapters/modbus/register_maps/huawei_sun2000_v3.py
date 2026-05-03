# Device: Huawei SUN2000 series
# Protocol: Modbus TCP
# Firmware assumed: SUN2000 SDongleA Modbus TCP interface v3 and later (sun2000_v3)
# Source: Huawei SUN2000 Modbus Interface Definition (request from installer)
# PLACEHOLDER — register addresses must be verified against actual device documentation

from __future__ import annotations

from open_ems.adapters.modbus.register_maps._base import _require_reg
from open_ems.adapters.modbus.register_maps.utils import int16, scale, uint16
from open_ems.adapters.protocol import RawModbusState
from open_ems.core.devices import InverterState

_REG_PV_POWER_W = 32064  # uint16, unit: W  # PLACEHOLDER
_REG_AC_POWER_W = 32080  # int16 signed two's complement, unit: W  # PLACEHOLDER
# uint16 enum: 0=standby, 1=grid-tied, 2=off-grid, 4=fault  # PLACEHOLDER
_REG_DEVICE_STATUS = 32089
_REG_FAULT_CODE = 32090  # uint16, 0 = no fault  # PLACEHOLDER

_DEVICE_STATUS_MAP: dict[int, str] = {
    0: "standby",
    1: "grid_tied",
    2: "off_grid",
    4: "fault",
}

__all__ = ["HuaweiSun2000V3"]


class HuaweiSun2000V3:
    """Register map for Huawei SUN2000 inverter (Modbus TCP interface v3)."""

    def map_state(self, raw: RawModbusState) -> InverterState:
        regs = raw.registers
        pv_raw = _require_reg(regs, _REG_PV_POWER_W)
        ac_raw = _require_reg(regs, _REG_AC_POWER_W)
        status_raw = _require_reg(regs, _REG_DEVICE_STATUS)
        fault_raw: int = regs.get(_REG_FAULT_CODE, 0)  # optional — default 0 = no fault
        return InverterState(
            device_id=raw.device_id,
            pv_power_kw=scale(uint16(pv_raw), 1 / 1000.0),
            ac_power_kw=scale(int16(ac_raw), 1 / 1000.0),
            operating_mode=_DEVICE_STATUS_MAP.get(status_raw, "unknown"),
            fault_code=str(fault_raw) if fault_raw != 0 else None,
            read_at=raw.read_at,
        )



