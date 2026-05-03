"""OCPP 1.6 Central System raw protocol adapter."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from ocpp.routing import on
from ocpp.v16 import ChargePoint as _BaseChargePoint
from ocpp.v16 import call as ocpp_call
from ocpp.v16 import call_result
from ocpp.v16.enums import Action, RegistrationStatus
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from open_ems.adapters.protocol import (
    ProtocolCommandResult,
    ProtocolDegradedState,
    RawOCPPState,
    RawProtocolCommand,
)

logger = structlog.get_logger(__name__)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class OCPPAdapterConfig(BaseModel):
    """Configuration for one OCPP 1.6 Central System adapter instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    charge_point_id: NonEmptyStr
    heartbeat_timeout_s: float = Field(default=60.0, gt=0)
    command_timeout_s: float = Field(default=10.0, gt=0, le=10.0)

    @field_validator("heartbeat_timeout_s", "command_timeout_s", mode="before")
    @classmethod
    def _float_fields_must_be_numeric(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("value must be a numeric timeout")
        return value


class _ChargerState:
    """Mutable state updated by ocpp @on handlers — NOT a Pydantic model."""

    def __init__(self) -> None:
        self.connected: bool = False
        self.last_status_notification: dict[str, Any] | None = None
        self.last_heartbeat_at: datetime | None = None
        self.last_call_result: dict[str, Any] | None = None
        self.last_call_error: dict[str, Any] | None = None


class _InternalChargePoint(_BaseChargePoint):  # type: ignore[misc]
    def __init__(
        self,
        id: str,
        connection: Any,
        state: _ChargerState,
        response_timeout: float,
    ) -> None:
        super().__init__(id, connection, response_timeout=response_timeout)
        self._state = state

    @on(Action.boot_notification)  # type: ignore[untyped-decorator]
    def on_boot_notification(
        self,
        charge_point_model: str,
        charge_point_vendor: str,
        **kwargs: Any,
    ) -> Any:
        self._state.last_status_notification = {
            "type": "BootNotification",
            "charge_point_model": charge_point_model,
            "charge_point_vendor": charge_point_vendor,
            **kwargs,
        }
        return call_result.BootNotification(
            current_time=datetime.now(UTC).isoformat(),
            interval=10,
            status=RegistrationStatus.accepted,
        )

    @on(Action.heartbeat)  # type: ignore[untyped-decorator]
    def on_heartbeat(self, **kwargs: Any) -> Any:
        now = datetime.now(UTC)
        self._state.last_heartbeat_at = now
        return call_result.Heartbeat(current_time=now.isoformat())

    @on(Action.status_notification)  # type: ignore[untyped-decorator]
    def on_status_notification(
        self,
        connector_id: int,
        error_code: str,
        status: str,
        **kwargs: Any,
    ) -> Any:
        self._state.last_status_notification = {
            "type": "StatusNotification",
            "connector_id": connector_id,
            "error_code": error_code,
            "status": status,
            **kwargs,
        }
        return call_result.StatusNotification()


class OCPPChargerAdapter:
    """Raw OCPP 1.6 adapter implementing the ProtocolAdapter contract for one EV charger."""

    def __init__(self, config: OCPPAdapterConfig) -> None:
        self.config = config
        self._state = _ChargerState()
        self._handler: _InternalChargePoint | None = None

    async def handle_connection(self, connection: Any) -> None:
        """Manage OCPP message loop for one charger connection. Re-callable for reconnect."""
        state = _ChargerState()
        state.connected = True
        handler = _InternalChargePoint(
            self.config.charge_point_id,
            connection,
            state,
            response_timeout=self.config.command_timeout_s + 1.0,
        )
        self._state = state
        self._handler = handler
        try:
            await handler.start()
        except Exception as exc:  # noqa: BLE001 — ocpp.ChargePoint.start() propagates ConnectionClosed, ProtocolError, and any exception from a message handler; all are non-recoverable for this connection and are logged below
            logger.warning(
                "adapter_connection_error",
                device_id=self.config.device_id,
                charge_point_id=self.config.charge_point_id,
                reason=type(exc).__name__,
                detail=str(exc),
            )
        finally:
            state.connected = False
            if self._handler is handler:
                self._handler = None
            logger.warning(
                "adapter_disconnected",
                component="ocpp",
                device_id=self.config.device_id,
                charge_point_id=self.config.charge_point_id,
            )

    async def get_raw_state(self) -> RawOCPPState | ProtocolDegradedState:
        """Return current raw OCPP state or a degraded state if disconnected or heartbeat stale."""
        state = self._state
        if not state.connected:
            return self._degraded("ocpp_disconnected")
        if state.last_heartbeat_at is not None:
            age_s = (datetime.now(UTC) - state.last_heartbeat_at).total_seconds()
            if age_s > self.config.heartbeat_timeout_s:
                return self._degraded("ocpp_disconnected")
        return RawOCPPState(
            device_id=self.config.device_id,
            charge_point_id=self.config.charge_point_id,
            last_status_notification=state.last_status_notification,
            last_heartbeat_at=state.last_heartbeat_at,
            connection_status="connected",
            last_call_result=state.last_call_result,
            last_call_error=state.last_call_error,
        )

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        """Send a raw OCPP CALL and return protocol-level acknowledgement or failure."""
        handler = self._handler
        if handler is None or not self._state.connected:
            return _command_error(command, "not_connected")

        payload = command.payload
        if not isinstance(payload, dict):
            return _command_error(command, "invalid_payload")

        call_cls = getattr(ocpp_call, command.command_name, None)
        if call_cls is None or not dataclasses.is_dataclass(call_cls):
            return _command_error(command, "unknown_action")

        try:
            call_payload = call_cls(**payload)
        except TypeError:
            return _command_error(command, "invalid_payload")

        try:
            result = await asyncio.wait_for(
                handler.call(call_payload, suppress=False),
                timeout=self.config.command_timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "adapter_timeout",
                component="ocpp",
                device_id=self.config.device_id,
                charge_point_id=self.config.charge_point_id,
                command=command.command_name,
                timeout_s=self.config.command_timeout_s,
            )
            return ProtocolCommandResult(
                correlation_id=command.correlation_id,
                device_id=command.device_id,
                protocol_status="timeout",
                raw_response={"error": "ocpp_timeout"},
            )
        except Exception as exc:  # noqa: BLE001 — ocpp.ChargePoint.call() may raise OCPPError, websocket exceptions, or unexpected protocol errors at the boundary; all are logged and returned as error results
            error_dict: dict[str, Any] = {
                "error_code": type(exc).__name__,
                "error_description": getattr(exc, "description", str(exc)),
                "error_details": getattr(exc, "details", {}),
            }
            self._state.last_call_error = error_dict
            logger.warning(
                "adapter_protocol_error",
                component="ocpp",
                device_id=self.config.device_id,
                charge_point_id=self.config.charge_point_id,
                command=command.command_name,
                reason=type(exc).__name__,
            )
            return ProtocolCommandResult(
                correlation_id=command.correlation_id,
                device_id=command.device_id,
                protocol_status="error",
                raw_response=error_dict,
            )

        try:
            result_dict: dict[str, Any] = dataclasses.asdict(result) if result is not None else {}
        except TypeError:
            result_dict = {}
            logger.warning(
                "adapter_unexpected_result_type",
                component="ocpp",
                device_id=self.config.device_id,
                charge_point_id=self.config.charge_point_id,
                command=command.command_name,
                result_type=type(result).__name__,
            )
        self._state.last_call_result = result_dict
        return ProtocolCommandResult(
            correlation_id=command.correlation_id,
            device_id=command.device_id,
            protocol_status="acked",
            raw_response=result_dict,
        )

    def _degraded(self, reason: str) -> ProtocolDegradedState:
        return ProtocolDegradedState(
            device_id=self.config.device_id,
            reason=reason,
            occurred_at=datetime.now(UTC),
        )


class OCPPCentralSystem:
    """Registry of OCPPChargerAdapter instances, one per charge point ID."""

    def __init__(self) -> None:
        self._adapters: dict[str, OCPPChargerAdapter] = {}

    def register(self, config: OCPPAdapterConfig) -> OCPPChargerAdapter:
        """Create and register a new charger adapter, returning it."""
        adapter = OCPPChargerAdapter(config)
        self._adapters[config.charge_point_id] = adapter
        return adapter

    def get_adapter(self, charge_point_id: str) -> OCPPChargerAdapter | None:
        """Look up a registered adapter by OCPP charge point identity."""
        return self._adapters.get(charge_point_id)

    async def handle_charger(self, charge_point_id: str, connection: Any) -> None:
        """Accept an incoming OCPP WebSocket connection. Blocks until charger disconnects.

        Raises KeyError if charge_point_id has not been pre-registered via register().
        """
        adapter = self._adapters.get(charge_point_id)
        if adapter is None:
            raise KeyError(f"No adapter registered for charge_point_id={charge_point_id!r}")
        await adapter.handle_connection(connection)


def _command_error(command: RawProtocolCommand, error: str) -> ProtocolCommandResult:
    return ProtocolCommandResult(
        correlation_id=command.correlation_id,
        device_id=command.device_id,
        protocol_status="error",
        raw_response={"error": error},
    )
