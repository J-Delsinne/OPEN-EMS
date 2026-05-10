"""Domain-level EV charger adapter: translates RawOCPPState → EVChargerState.

``send_command`` honors the cross-adapter command contract documented in
``open_ems.adapters`` (the canonical reference). OCPP-specific clauses on top
of the cross-adapter contract:

- ``SetEVChargingRateCommand`` → OCPP ``SetChargingProfile`` CALL with a single
  ``TxProfile`` setting ``chargingRateUnit="W"`` and ``limit=int(rate_kw*1000)``.
  Response ``status="Accepted"`` ⇒ ``CommandResult(success)``.
  Response ``status="Rejected"`` ⇒ ``CommandResult(rejected, "ocpp_rejected:Rejected")``.
  Response ``status="NotSupported"`` or any other non-{Accepted,Rejected} ⇒
  ``CommandResult(failed, f"ocpp_unsupported:<status>")`` — permanent capability
  error (the charger cannot honor this CALL); RetryPolicy must not retry.
- ``StopEVChargingCommand`` → OCPP ``RemoteStopTransaction`` with the active
  transaction id observed from the charger's most recent ``StartTransaction``.
  No active transaction ⇒ ``CommandResult(failed, "no_active_ocpp_transaction")``
  (do NOT send a stop with a stale id — Story 8.0 retro hazard). The transaction
  id read here is not atomic with the dispatch await; if the charger has
  independently stopped the transaction between the two, the OCPP response will
  be ``ocpp_unsupported:*`` or ``ocpp_rejected:*`` and the result is correctly
  marked non-applied. RetryPolicy will not retry (StopEVChargingCommand is not
  idempotent).

OCPP CALL ``acked`` ≠ command applied; the response payload's ``status`` field
is the authoritative signal. The protocol layer reports both alike as
``protocol_status="acked"``; this adapter inspects the response payload to
disambiguate (cross-adapter contract clause 3, OCPP-specific rows).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import structlog

from open_ems.adapters._helpers import failed_result
from open_ems.adapters.capabilities import get_profile
from open_ems.adapters.ocpp.central_system import OCPPChargerAdapter
from open_ems.adapters.protocol import (
    ProtocolCommandResult,
    ProtocolDegradedState,
    RawOCPPState,
    RawProtocolCommand,
)
from open_ems.core.commands import (
    CommandResult,
    CommandStatus,
    DeviceCommand,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.core.devices import (
    DegradedDeviceState,
    DeviceCapabilityProfile,
    DeviceRole,
    EVChargerState,
)

logger = structlog.get_logger(__name__)

_OCPP_POWER_STALE_SECONDS: float = 90.0

_DomainStatus = Literal["available", "charging", "faulted", "unavailable"]

# Protocol reasons that indicate a transient connection loss → mapped to "reconnecting"
_RECONNECTING_REASONS: frozenset[str] = frozenset({"ocpp_disconnected"})

_OCPP_STATUS_MAP: dict[str, tuple[_DomainStatus, bool]] = {
    "Available": ("available", False),
    "Preparing": ("available", True),
    "Charging": ("charging", True),
    "SuspendedEVSE": ("charging", True),
    "SuspendedEV": ("charging", True),
    "Finishing": ("charging", True),
    "Reserved": ("unavailable", False),
    "Unavailable": ("unavailable", False),
    "Faulted": ("faulted", False),
}


class EVChargerAdapter:
    """Domain-level EV charger adapter: translates RawOCPPState → EVChargerState."""

    def __init__(
        self,
        device_id: str,
        protocol_adapter: OCPPChargerAdapter,
    ) -> None:
        self.device_id = device_id
        self._protocol_adapter = protocol_adapter

    async def connect(self) -> None:
        """No-op: OCPP connections are charger-initiated.
        Connection lifecycle is managed by OCPPCentralSystem.handle_charger()
        via the FastAPI WebSocket endpoint at /ocpp/{charge_point_id}.
        """

    async def disconnect(self) -> None:
        """No-op: OCPP disconnect is charger-driven.
        The OCPPChargerAdapter has no close() method; connection teardown
        is handled by the WebSocket server task lifecycle.
        """

    async def get_state(self) -> EVChargerState | DegradedDeviceState:
        raw = await self._protocol_adapter.get_raw_state()
        if isinstance(raw, ProtocolDegradedState):
            return self._to_degraded(raw.reason, raw.occurred_at)

        if not isinstance(raw, RawOCPPState):
            return self._to_degraded("unexpected_raw_state_type", datetime.now(UTC))

        if raw.last_status_notification is None:
            if raw.connection_status == "disconnected":
                return self._to_degraded("ocpp_disconnected", datetime.now(UTC))
            return self._to_degraded("ocpp_no_status", datetime.now(UTC))

        raw_status: str = raw.last_status_notification.get("status", "")
        mapping = _OCPP_STATUS_MAP.get(raw_status)
        if mapping is None:
            return self._to_degraded(f"ocpp_unknown_status:{raw_status}", datetime.now(UTC))

        domain_status, session_active = mapping

        meter_at = raw.last_meter_values_at
        power_kw = raw.last_meter_values_power_kw
        if meter_at is not None and power_kw is not None:
            if power_kw < 0:
                return self._to_degraded("ocpp_invalid_power", datetime.now(UTC))
            age_s = (datetime.now(UTC) - meter_at).total_seconds()
            if age_s <= _OCPP_POWER_STALE_SECONDS:
                current_power_kw: float | None = power_kw
                power_source: Literal["meter_values"] | None = "meter_values"
                power_measured_at: datetime | None = meter_at
            else:
                current_power_kw = None
                power_source = None
                power_measured_at = None
        else:
            current_power_kw = None
            power_source = None
            power_measured_at = None

        return EVChargerState(
            device_id=self.device_id,
            status=domain_status,
            session_active=session_active,
            current_power_kw=current_power_kw,
            power_source=power_source,
            power_measured_at=power_measured_at,
            read_at=datetime.now(UTC),
        )

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        profile = get_profile(
            device_id=self.device_id,
            model="ocpp_1_6",
            firmware_version=None,
        )
        if profile.limitation_reason is not None:
            logger.warning(
                "capability_profile_unknown",
                component="adapters",
                device_id=self.device_id,
                model="ocpp_1_6",
                firmware_version=None,
            )
        return profile

    async def send_command(self, cmd: DeviceCommand) -> CommandResult:
        """OCPP charger write path.

        Honors the cross-adapter command contract in ``open_ems.adapters``.
        ``asyncio.CancelledError`` and ``TypeError`` propagate (clauses 5 and 2).
        Every other exception is caught and wrapped (clause 8 / AR16).
        """
        if isinstance(cmd, SetEVChargingRateCommand):
            command_name = "SetChargingProfile"
            try:
                payload = self._compose_set_charging_profile_payload(cmd.rate_kw)
            except Exception as exc:  # noqa: BLE001 — AR16
                return self._wrap_internal_error(cmd, exc, where="ocpp_payload_composition")
        elif isinstance(cmd, StopEVChargingCommand):
            command_name = "RemoteStopTransaction"
            transaction_id = self._protocol_adapter.active_transaction_id
            if transaction_id is None:
                return failed_result(cmd, "no_active_ocpp_transaction")
            payload = {"transaction_id": transaction_id}
        else:
            raise TypeError(f"EVChargerAdapter does not support {type(cmd).__name__}")

        try:
            raw_command = RawProtocolCommand(
                correlation_id=str(cmd.correlation_id),
                device_id=self.device_id,
                command_name=command_name,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 — AR16
            return self._wrap_internal_error(cmd, exc, where="raw_command_construction")

        try:
            protocol_result = await self._protocol_adapter.send_raw_command(raw_command)
        except Exception as exc:  # noqa: BLE001 — AR16. CancelledError is BaseException; not caught here.
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

        return self._map_ocpp_result(cmd, protocol_result)

    def _compose_set_charging_profile_payload(self, rate_kw: float) -> dict[str, Any]:
        """Build an OCPP 1.6 SetChargingProfile payload for an EMS rate setpoint.

        See Dev Notes § "OCPP charging profile composition" (Story 9.0).
        """
        if rate_kw < 0.0:
            raise ValueError(f"rate_kw must be >= 0 (got {rate_kw})")
        limit_w = int(round(rate_kw * 1000))
        profile_id = self._protocol_adapter.reserve_charging_profile_id()
        return {
            "connector_id": 1,
            "cs_charging_profiles": {
                "charging_profile_id": profile_id,
                "stack_level": 0,
                "charging_profile_purpose": "TxProfile",
                "charging_profile_kind": "Absolute",
                "charging_schedule": {
                    "charging_rate_unit": "W",
                    "charging_schedule_period": [
                        {"start_period": 0, "limit": limit_w},
                    ],
                },
            },
        }

    def _map_ocpp_result(
        self,
        cmd: DeviceCommand,
        protocol_result: ProtocolCommandResult,
    ) -> CommandResult:
        """Map ``ProtocolCommandResult`` → ``CommandResult`` per OCPP semantics.

        OCPP ``acked`` does NOT mean "applied"; the response payload's ``status``
        field is the authoritative signal. ``Accepted`` ⇒ success; ``Rejected``
        ⇒ rejected (soft refusal — charger could grant later); anything else
        (notably ``NotSupported``) ⇒ failed/``ocpp_unsupported:<status>`` (the
        charger cannot honor this CALL — permanent error).
        """
        status = protocol_result.protocol_status
        raw_response = protocol_result.raw_response

        if status == "acked":
            response_status = None
            if isinstance(raw_response, dict):
                response_status = raw_response.get("status")
            if response_status == "Accepted":
                return CommandResult(
                    correlation_id=cmd.correlation_id,
                    device_id=cmd.device_id,
                    status=CommandStatus.success,
                    applied=True,
                    reason="ok",
                )
            if response_status == "Rejected":
                return CommandResult(
                    correlation_id=cmd.correlation_id,
                    device_id=cmd.device_id,
                    status=CommandStatus.rejected,
                    applied=False,
                    reason="ocpp_rejected:Rejected",
                )
            # NotSupported, missing/non-string status field, or any value other
            # than Accepted/Rejected: treat as a permanent capability error so
            # RetryPolicy does not waste retries. The raw value is preserved for
            # audit visibility.
            reason_status = response_status if isinstance(response_status, str) else "Unknown"
            return failed_result(cmd, f"ocpp_unsupported:{reason_status}")

        if status == "timeout":
            return CommandResult(
                correlation_id=cmd.correlation_id,
                device_id=cmd.device_id,
                status=CommandStatus.timeout,
                applied=False,
                reason="ocpp_command_timeout",
            )

        if status == "error":
            error_code = "unknown"
            if isinstance(raw_response, dict):
                error_code = str(
                    raw_response.get("error") or raw_response.get("error_code") or "unknown"
                )
            return failed_result(cmd, f"ocpp_error:{error_code}")

        return failed_result(cmd, f"adapter_internal_error:unexpected_protocol_status:{status}")

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

    def _to_degraded(self, reason: str, occurred_at: datetime) -> DegradedDeviceState:
        domain_reason = "reconnecting" if reason in _RECONNECTING_REASONS else reason
        logger.warning(
            "device_degraded",
            component="adapters",
            device_id=self.device_id,
            role=DeviceRole.ev_charger.value,
            reason=domain_reason,
        )
        return DegradedDeviceState(
            device_id=self.device_id,
            role=DeviceRole.ev_charger,
            reason=domain_reason,
            occurred_at=occurred_at,
        )
