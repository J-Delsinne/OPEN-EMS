"""Domain-level battery adapter: translates RawModbusState → BatteryState.

Battery power sign convention (system-wide, per Story 4.1 / devices.py):
    battery_power_kw > 0  →  charging (consuming power from PV or grid)
    battery_power_kw < 0  →  discharging (providing power to loads or grid)
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pydantic import ValidationError

from open_ems.adapters.modbus.register_maps import (
    BatteryRegisterMap,
    BydHvmV1,
    BydHvsV1,
    InvalidRegisterValueError,
    MissingRegisterError,
)
from open_ems.adapters.modbus.tcp import ModbusTcpAdapter
from open_ems.adapters.protocol import ProtocolDegradedState, RawModbusState
from open_ems.core.devices import (
    BatteryState,
    DegradedDeviceState,
    DeviceCapabilityProfile,
    DeviceRole,
)

logger = structlog.get_logger(__name__)

_SUPPORTED_MODELS: dict[str, BatteryRegisterMap] = {
    "byd_hvs_v1": BydHvsV1(),
    "byd_hvm_v1": BydHvmV1(),
}


class BatteryAdapter:
    """Domain-level battery adapter: translates RawModbusState → BatteryState.

    Wraps a ModbusTcpAdapter and applies the device model's register map to
    produce typed BatteryState. Unsupported models raise ValueError at init time.
    """

    def __init__(
        self,
        device_id: str,
        protocol_adapter: ModbusTcpAdapter,
        model: str,
    ) -> None:
        if model not in _SUPPORTED_MODELS:
            raise ValueError(
                f"Unsupported battery model: {model!r}. "
                f"Supported: {sorted(_SUPPORTED_MODELS)}"
            )
        self.device_id = device_id  # explicit — NOT derived from protocol_adapter.config
        self._protocol_adapter = protocol_adapter
        self._register_map = _SUPPORTED_MODELS[model]
        self._model = model

    async def connect(self) -> None:
        """No-op: ModbusTcpAdapter connects lazily on first get_raw_state() call."""

    async def disconnect(self) -> None:
        """Close the underlying protocol adapter connection."""
        await self._protocol_adapter.close()

    async def get_state(self) -> BatteryState | DegradedDeviceState:
        """Read battery state via Modbus and map to domain types.

        Returns DegradedDeviceState if the protocol layer is degraded, a required
        register is missing, or raw values fail Pydantic validation.
        """
        raw = await self._protocol_adapter.get_raw_state()
        if isinstance(raw, ProtocolDegradedState):
            return self._to_degraded(raw.reason, raw.occurred_at)
        if not isinstance(raw, RawModbusState):
            return self._to_degraded("unexpected_raw_state_type", datetime.now(UTC))
        if raw.device_id != self.device_id:
            return self._to_degraded("device_id_mismatch", raw.read_at)
        try:
            return self._register_map.map_state(raw)
        except (MissingRegisterError, InvalidRegisterValueError, ValidationError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, (MissingRegisterError, InvalidRegisterValueError))
                else f"validation_error:{exc}"
            )
            return self._to_degraded(reason, raw.read_at)

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        """Return a stub capability profile (expanded in Story 4.4)."""
        return DeviceCapabilityProfile(
            device_id=self.device_id,
            model=self._model,
            firmware_version=None,
            capability_status="full",
        )

    def _to_degraded(self, reason: str, occurred_at: datetime) -> DegradedDeviceState:
        logger.warning(
            "device_degraded",
            component="adapters",
            device_id=self.device_id,
            role=DeviceRole.battery.value,
            reason=reason,
        )
        return DegradedDeviceState(
            device_id=self.device_id,
            role=DeviceRole.battery,
            reason=reason,
            occurred_at=occurred_at,
        )
