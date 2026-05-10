"""Domain-level inverter adapter: translates RawModbusState → InverterState.

Story 9.0 AC3 / AC10: v1 inverters are read-only across all supported models
(``fronius_gen24_v1``, ``huawei_sun2000_v3``, ``growatt_hybrid_v1``). The
capability profiles in ``open_ems.adapters.capabilities.inverter`` declare an
empty ``write_capabilities`` set, so PolicyGuard's capability gate rejects every
command before it reaches ``send_command``. The ``TypeError`` raised here is
the same defense-in-depth signal used by other controllable adapters when an
unsupported command type slips through (cross-adapter contract clause 2;
see ``open_ems.adapters`` module docstring).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pydantic import ValidationError

from open_ems.adapters.capabilities import get_profile
from open_ems.adapters.modbus.register_maps import (
    FroniusGen24V1,
    GrowattHybridV1,
    HuaweiSun2000V3,
    InvalidRegisterValueError,
    InverterRegisterMap,
    MissingRegisterError,
)
from open_ems.adapters.modbus.tcp import ModbusTcpAdapter
from open_ems.adapters.protocol import ProtocolDegradedState, RawModbusState
from open_ems.core.commands import CommandResult, DeviceCommand
from open_ems.core.devices import (
    DegradedDeviceState,
    DeviceCapabilityProfile,
    DeviceRole,
    InverterState,
)

logger = structlog.get_logger(__name__)

_SUPPORTED_MODELS: dict[str, InverterRegisterMap] = {
    "fronius_gen24_v1": FroniusGen24V1(),
    "huawei_sun2000_v3": HuaweiSun2000V3(),
    "growatt_hybrid_v1": GrowattHybridV1(),
}

# Protocol reasons that indicate a transient connection loss → mapped to "reconnecting"
_RECONNECTING_REASONS: frozenset[str] = frozenset({"modbus_timeout", "modbus_error"})


class InverterAdapter:
    """Domain-level inverter adapter: translates RawModbusState → InverterState.

    Wraps a ModbusTcpAdapter and applies the device model's register map to
    produce typed InverterState. Unsupported models raise ValueError at init time.
    """

    def __init__(
        self,
        device_id: str,
        protocol_adapter: ModbusTcpAdapter,
        model: str,
    ) -> None:
        if model not in _SUPPORTED_MODELS:
            raise ValueError(
                f"Unsupported inverter model: {model!r}. Supported: {sorted(_SUPPORTED_MODELS)}"
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

    async def get_state(self) -> InverterState | DegradedDeviceState:
        """Read inverter state via Modbus and map to domain types.

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
        profile = get_profile(
            device_id=self.device_id,
            model=self._model,
            firmware_version=None,
        )
        if profile.limitation_reason is not None:
            logger.warning(
                "capability_profile_unknown",
                component="adapters",
                device_id=self.device_id,
                model=self._model,
                firmware_version=None,
            )
        return profile

    async def send_command(self, cmd: DeviceCommand) -> CommandResult:
        """v1 inverter has no write surface — every command type raises TypeError.

        Per the cross-adapter command contract (clause 2), this is a programming-
        error signal: PolicyGuard's capability gate should have rejected the
        command before reaching the adapter. Returning ``TypeError`` (not a
        ``CommandResult``) lets test suites surface a routing bug immediately
        instead of silently mapping it to ``failed``.
        """
        raise TypeError(f"InverterAdapter does not support {type(cmd).__name__}")

    def _to_degraded(self, reason: str, occurred_at: datetime) -> DegradedDeviceState:
        domain_reason = "reconnecting" if reason in _RECONNECTING_REASONS else reason
        logger.warning(
            "device_degraded",
            component="adapters",
            device_id=self.device_id,
            role=DeviceRole.inverter.value,
            reason=domain_reason,
        )
        return DegradedDeviceState(
            device_id=self.device_id,
            role=DeviceRole.inverter,
            reason=domain_reason,
            occurred_at=occurred_at,
        )
