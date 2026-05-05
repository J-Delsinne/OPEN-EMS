"""Domain-level EV charger adapter: translates RawOCPPState → EVChargerState."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import structlog

from open_ems.adapters.capabilities import get_profile
from open_ems.adapters.ocpp.central_system import OCPPChargerAdapter
from open_ems.adapters.protocol import ProtocolDegradedState, RawOCPPState
from open_ems.core.commands import CommandResult, DeviceCommand
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
        """OCPP charger write path — implementation deferred (Epic 9)."""
        raise NotImplementedError("EVChargerAdapter.send_command is not yet implemented")

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
