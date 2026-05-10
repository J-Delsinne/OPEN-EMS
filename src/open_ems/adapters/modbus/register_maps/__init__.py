"""Modbus device register map definitions.

Exports:
- Protocol types: InverterRegisterMap, BatteryRegisterMap
- Error type: MissingRegisterError
- Concrete map classes: FroniusGen24V1, HuaweiSun2000V3, GrowattHybridV1,
  BydHvsV1, BydHvmV1
"""

from __future__ import annotations

from open_ems.adapters.modbus.register_maps._base import (
    BatteryRegisterMap,
    InvalidRegisterValueError,
    InverterRegisterMap,
    MissingRegisterError,
    ModbusCommandPayload,
)
from open_ems.adapters.modbus.register_maps.byd_hvm_v1 import BydHvmV1
from open_ems.adapters.modbus.register_maps.byd_hvs_v1 import BydHvsV1
from open_ems.adapters.modbus.register_maps.fronius_gen24_v1 import FroniusGen24V1
from open_ems.adapters.modbus.register_maps.growatt_hybrid_v1 import GrowattHybridV1
from open_ems.adapters.modbus.register_maps.huawei_sun2000_v3 import HuaweiSun2000V3

__all__ = [
    "BatteryRegisterMap",
    "BydHvmV1",
    "BydHvsV1",
    "FroniusGen24V1",
    "GrowattHybridV1",
    "HuaweiSun2000V3",
    "InvalidRegisterValueError",
    "InverterRegisterMap",
    "MissingRegisterError",
    "ModbusCommandPayload",
]
