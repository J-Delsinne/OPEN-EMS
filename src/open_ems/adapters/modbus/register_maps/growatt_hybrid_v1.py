# Device: Growatt Hybrid Inverter series (SPH / MID / MAX)
# Protocol: Modbus TCP
# Firmware assumed: Growatt Modbus RTU/TCP protocol v1 (growatt_hybrid_v1)
# Source: Growatt Inverter Modbus RS485 RTU Protocol (request from installer)
# PLACEHOLDER — register addresses must be verified against actual device documentation

from __future__ import annotations

from open_ems.adapters.modbus.register_maps._base import _require_reg
from open_ems.adapters.modbus.register_maps.utils import int16, scale, uint16
from open_ems.adapters.protocol import RawModbusState
from open_ems.core.devices import InverterState

_REG_PV_POWER_W = 3001  # uint16, unit: W  # PLACEHOLDER
_REG_AC_POWER_W = 3002  # int16 signed two's complement, unit: W  # PLACEHOLDER
_REG_WORKING_MODE = 3003  # uint16 enum: 0=waiting, 1=normal, 2=fault  # PLACEHOLDER
_REG_FAULT_CODE = 3004  # uint16, 0 = no fault  # PLACEHOLDER

_WORKING_MODE_MAP: dict[int, str] = {
    0: "waiting",
    1: "normal",
    2: "fault",
}

__all__ = ["GrowattHybridV1"]


class GrowattHybridV1:
    """Register map for Growatt Hybrid inverter (Modbus TCP, growatt_hybrid_v1)."""

    def map_state(self, raw: RawModbusState) -> InverterState:
        regs = raw.registers
        pv_raw = _require_reg(regs, _REG_PV_POWER_W)
        ac_raw = _require_reg(regs, _REG_AC_POWER_W)
        mode_raw = _require_reg(regs, _REG_WORKING_MODE)
        fault_raw: int = regs.get(_REG_FAULT_CODE, 0)  # optional — default 0 = no fault
        return InverterState(
            device_id=raw.device_id,
            pv_power_kw=scale(uint16(pv_raw), 1 / 1000.0),
            ac_power_kw=scale(int16(ac_raw), 1 / 1000.0),
            operating_mode=_WORKING_MODE_MAP.get(mode_raw, "unknown"),
            fault_code=str(fault_raw) if fault_raw != 0 else None,
            read_at=raw.read_at,
        )
