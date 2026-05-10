"""Domain-level battery adapter: translates RawModbusState → BatteryState.

``send_command`` honors the cross-adapter command contract documented in
``open_ems.adapters`` (the canonical reference). Per-clause behavior is enforced
here without restating the contract; see that module's docstring for clauses
1–10 (authorization, command surface, status mapping, correlation_id round-trip,
cancellation, timeout boundary, observed_state, AR16 enforcement, idempotency,
audit ownership).

Battery power sign convention (system-wide, per Story 4.1 / devices.py):
    battery_power_kw > 0  →  charging (consuming power from PV or grid)
    battery_power_kw < 0  →  discharging (providing power to loads or grid)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from pydantic import ValidationError

from open_ems.adapters._helpers import failed_result
from open_ems.adapters.capabilities import get_profile
from open_ems.adapters.modbus.register_maps import (
    BatteryRegisterMap,
    BydHvmV1,
    BydHvsV1,
    InvalidRegisterValueError,
    MissingRegisterError,
)
from open_ems.adapters.modbus.tcp import ModbusTcpAdapter
from open_ems.adapters.protocol import (
    ProtocolCommandResult,
    ProtocolDegradedState,
    RawModbusState,
    RawProtocolCommand,
)
from open_ems.core.commands import (
    CommandResult,
    CommandStatus,
    DeviceCommand,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
)
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

# Protocol reasons that indicate a transient connection loss → mapped to "reconnecting"
_RECONNECTING_REASONS: frozenset[str] = frozenset({"modbus_timeout", "modbus_error"})

_COMMAND_NAME_BY_TYPE: dict[type, str] = {
    SetBatteryChargeRateCommand: "set_battery_charge_rate",
    SetBatteryDischargeRateCommand: "set_battery_discharge_rate",
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
                f"Unsupported battery model: {model!r}. Supported: {sorted(_SUPPORTED_MODELS)}"
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
        """Modbus battery write path.

        Honors the cross-adapter command contract in ``open_ems.adapters``.
        ``asyncio.CancelledError`` and ``TypeError`` propagate (clauses 5 and 2).
        Every other exception is caught and wrapped (clause 8 / AR16).
        """
        if not isinstance(cmd, (SetBatteryChargeRateCommand, SetBatteryDischargeRateCommand)):
            raise TypeError(f"BatteryAdapter does not support {type(cmd).__name__}")

        try:
            payload = self._register_map.command_payload(cmd)
        except (ValueError, OverflowError) as exc:
            # OverflowError can arise from int(round(float('inf') * 1000)); treat
            # it the same as ValueError — it is an encoding-domain failure, not
            # an adapter bug. ValueError covers explicit range checks plus any
            # numeric domain violation the register-map encoder raises.
            return failed_result(cmd, f"register_map_encoding_error:{exc}")
        except Exception as exc:  # noqa: BLE001 — AR16: wrap unexpected exceptions
            return self._wrap_internal_error(cmd, exc, where="register_map.command_payload")

        try:
            raw_command = RawProtocolCommand(
                correlation_id=str(cmd.correlation_id),
                device_id=self.device_id,
                command_name=_COMMAND_NAME_BY_TYPE[type(cmd)],
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 — AR16
            return self._wrap_internal_error(cmd, exc, where="raw_command_construction")

        try:
            protocol_result = await self._protocol_adapter.send_raw_command(raw_command)
        except Exception as exc:  # noqa: BLE001 — AR16
            # CancelledError is BaseException in 3.11+; not caught here, propagates.
            return self._wrap_internal_error(cmd, exc, where="protocol_dispatch")

        # correlation_id round-trip integrity (contract clause 4).
        try:
            returned_uuid = uuid.UUID(protocol_result.correlation_id)
        except (ValueError, AttributeError, TypeError):
            self._log_correlation_mismatch(cmd, protocol_result)
            return failed_result(cmd, "correlation_id_mismatch")
        if returned_uuid != cmd.correlation_id:
            self._log_correlation_mismatch(cmd, protocol_result)
            return failed_result(cmd, "correlation_id_mismatch")

        return _map_protocol_result(cmd, protocol_result, protocol_prefix="modbus")

    def _log_correlation_mismatch(
        self, cmd: DeviceCommand, protocol_result: ProtocolCommandResult
    ) -> None:
        # The returned correlation id is untrusted input from the device. Cap and
        # strip control characters so a hostile payload cannot inject newlines or
        # structlog field markers into downstream log streams.
        received_raw = str(protocol_result.correlation_id)
        received_sanitized = "".join(
            c if c.isprintable() and c not in "\r\n" else "?" for c in received_raw[:64]
        )
        logger.warning(
            "adapter_correlation_mismatch",
            component="adapters",
            device_id=self.device_id,
            expected=str(cmd.correlation_id),
            received=received_sanitized,
            received_length=len(received_raw),
        )

    def _wrap_internal_error(
        self, cmd: DeviceCommand, exc: BaseException, *, where: str
    ) -> CommandResult:
        logger.warning(
            "adapter_internal_error",
            component="adapters",
            device_id=self.device_id,
            command_type=type(cmd).__name__,
            phase=where,
            error=repr(exc),
            exc_info=True,
        )
        return CommandResult(
            correlation_id=cmd.correlation_id,
            device_id=cmd.device_id,
            status=CommandStatus.failed,
            applied=False,
            reason=f"adapter_internal_error:{type(exc).__name__}",
        )

    def _to_degraded(self, reason: str, occurred_at: datetime) -> DegradedDeviceState:
        domain_reason = "reconnecting" if reason in _RECONNECTING_REASONS else reason
        logger.warning(
            "device_degraded",
            component="adapters",
            device_id=self.device_id,
            role=DeviceRole.battery.value,
            reason=domain_reason,
        )
        return DegradedDeviceState(
            device_id=self.device_id,
            role=DeviceRole.battery,
            reason=domain_reason,
            occurred_at=occurred_at,
        )


def _map_protocol_result(
    cmd: DeviceCommand,
    protocol_result: ProtocolCommandResult,
    *,
    protocol_prefix: str,
) -> CommandResult:
    """Map a ``ProtocolCommandResult`` to a ``CommandResult`` per the contract clause 3."""
    status = protocol_result.protocol_status
    raw_response = protocol_result.raw_response

    if status == "acked":
        return CommandResult(
            correlation_id=cmd.correlation_id,
            device_id=cmd.device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )
    if status == "timeout":
        return CommandResult(
            correlation_id=cmd.correlation_id,
            device_id=cmd.device_id,
            status=CommandStatus.timeout,
            applied=False,
            reason=f"{protocol_prefix}_command_timeout",
        )
    if status == "error":
        error_code = "unknown"
        if isinstance(raw_response, dict):
            error_code = str(
                raw_response.get("error") or raw_response.get("error_code") or "unknown"
            )
        return failed_result(cmd, f"{protocol_prefix}_error:{error_code}")

    # Defensive — protocol_status is a Literal in ProtocolCommandResult, so reaching
    # here would mean a bug at the protocol layer.
    return failed_result(cmd, f"adapter_internal_error:unexpected_protocol_status:{status}")
