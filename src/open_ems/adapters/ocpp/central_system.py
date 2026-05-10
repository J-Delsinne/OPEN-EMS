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
        self.last_meter_values_at: datetime | None = None
        self.last_meter_values_power_kw: float | None = None
        # OCPP transaction id observed via StartTransaction. Cleared by
        # on_stop_transaction (matching id) and on_boot_notification (charger reset).
        # Preserved across WebSocket reconnects without a boot, since OCPP 1.6
        # transactions survive a WebSocket flap.
        self.last_known_transaction_id: int | None = None
        # Connector that the active transaction is on. Tracked so on_start_transaction
        # can warn when a second StartTransaction arrives before StopTransaction.
        self.active_transaction_connector_id: int | None = None
        # Monotonic counter feeding chargingProfileId in SetChargingProfile payloads.
        # Preserved across reconnect (avoiding silent overwrite of charger-persisted
        # profiles); reset only on BootNotification (charger reset wipes profiles).
        self.next_charging_profile_id: int = 1


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
        # Charger reset — any prior transaction and any charger-side persisted
        # charging profiles are gone. Reset the tracking state so the next
        # StartTransaction issues a fresh id and SetChargingProfile starts at 1.
        self._state.last_known_transaction_id = None
        self._state.active_transaction_connector_id = None
        self._state.next_charging_profile_id = 1
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

    @on(Action.start_transaction)  # type: ignore[untyped-decorator]
    def on_start_transaction(
        self,
        connector_id: int,
        id_tag: str,
        meter_start: int,
        timestamp: str,
        **kwargs: Any,
    ) -> Any:
        # Issue a transaction id and remember it so EVChargerAdapter.send_command can
        # construct a valid RemoteStopTransaction payload. Overlapping
        # StartTransactions (without an intervening StopTransaction) are unusual:
        # multi-connector chargers or buggy firmware. Log a warning before
        # overwriting so the operator can see the lifecycle anomaly.
        previous_id = self._state.last_known_transaction_id
        if previous_id is not None:
            logger.warning(
                "ocpp_overlapping_start_transaction",
                component="ocpp",
                charge_point_id=self.id,
                previous_transaction_id=previous_id,
                previous_connector_id=self._state.active_transaction_connector_id,
                new_connector_id=connector_id,
            )
        transaction_id = (previous_id or 0) + 1
        self._state.last_known_transaction_id = transaction_id
        self._state.active_transaction_connector_id = connector_id
        return call_result.StartTransaction(
            transaction_id=transaction_id,
            id_tag_info={"status": "Accepted"},
        )

    @on(Action.stop_transaction)  # type: ignore[untyped-decorator]
    def on_stop_transaction(
        self,
        meter_stop: int,
        timestamp: str,
        transaction_id: int,
        **kwargs: Any,
    ) -> Any:
        # Charger-initiated stop. When the wire id matches the tracked active id
        # (the common path), clear local state so a subsequent StopEVChargingCommand
        # correctly returns no_active_ocpp_transaction. A mismatch indicates the
        # charger and EMS disagree on which transaction is current — log a warning
        # and DO NOT clear local state so the EMS continues to track what it
        # believes is active (the charger may be sending a late notification).
        expected = self._state.last_known_transaction_id
        if expected == transaction_id:
            self._state.last_known_transaction_id = None
            self._state.active_transaction_connector_id = None
        elif expected is not None:
            logger.warning(
                "ocpp_stop_transaction_id_mismatch",
                component="ocpp",
                charge_point_id=self.id,
                expected_transaction_id=expected,
                received_transaction_id=transaction_id,
            )
        return call_result.StopTransaction(id_tag_info={"status": "Accepted"})

    @on(Action.meter_values)  # type: ignore[untyped-decorator]
    def on_meter_values(
        self,
        connector_id: int,
        meter_value: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        power_kw: float | None = None
        measurement_ts: datetime | None = None
        for mv in reversed(meter_value):
            raw_ts: str | None = mv.get("timestamp")
            parsed_ts: datetime | None = None
            if raw_ts:
                try:
                    dt = datetime.fromisoformat(raw_ts)
                    parsed_ts = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
                except (ValueError, TypeError, OverflowError):
                    parsed_ts = None
            for sv in mv.get("sampled_value", []):
                if sv.get("measurand") == "Power.Active.Import":
                    try:
                        raw_val = float(sv["value"])
                        unit = sv.get("unit", "W")
                        if unit not in ("W", "kW"):
                            logger.warning(
                                "meter_values_unknown_unit",
                                component="ocpp",
                                charge_point_id=self.id,
                                unit=unit,
                                measurand="Power.Active.Import",
                            )
                        power_kw = raw_val / 1000.0 if unit != "kW" else raw_val
                        measurement_ts = parsed_ts
                    except (ValueError, KeyError):
                        continue
                    break
            if power_kw is not None:
                break
        if power_kw is not None:
            self._state.last_meter_values_at = measurement_ts or datetime.now(UTC)
            self._state.last_meter_values_power_kw = power_kw
        return call_result.MeterValues()


class OCPPChargerAdapter:
    """Raw OCPP 1.6 adapter implementing the ProtocolAdapter contract for one EV charger."""

    def __init__(self, config: OCPPAdapterConfig) -> None:
        self.config = config
        self._state = _ChargerState()
        self._handler: _InternalChargePoint | None = None
        # Serializes concurrent send_raw_command calls over the same WebSocket.
        # python-ocpp ChargePoint.call is request/response by message id; overlapping
        # calls on one connection can race the response queue. Modbus has the
        # equivalent guard via ModbusTcpAdapter._lock.
        self._send_lock = asyncio.Lock()

    async def handle_connection(self, connection: Any) -> None:
        """Manage OCPP message loop for one charger connection. Re-callable for reconnect."""
        previous_state = self._state
        state = _ChargerState()
        state.connected = True
        # Preserve OCPP-session state across a WebSocket reconnect that did not
        # involve a BootNotification. OCPP 1.6 transactions and any charger-side
        # SetChargingProfile entries survive a transport flap; the EMS must not
        # silently lose track of the active transaction or reuse a chargingProfileId
        # that already exists on the charger. on_boot_notification resets these
        # fields when the charger actually reboots.
        state.last_known_transaction_id = previous_state.last_known_transaction_id
        state.active_transaction_connector_id = previous_state.active_transaction_connector_id
        state.next_charging_profile_id = previous_state.next_charging_profile_id
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

    @property
    def active_transaction_id(self) -> int | None:
        """The most recent OCPP transaction id observed via StartTransaction.

        Cleared on matching StopTransaction or on BootNotification (charger reset).
        Preserved across WebSocket reconnects without a boot, since OCPP 1.6
        transactions survive a transport flap. Read by EVChargerAdapter to
        compose a RemoteStopTransaction payload. Returns ``None`` if no
        transaction is currently active.
        """
        return self._state.last_known_transaction_id

    def reserve_charging_profile_id(self) -> int:
        """Reserve the next chargingProfileId for SetChargingProfile.

        Monotonic across the lifetime of the charger session — preserved across
        WebSocket reconnects so the EMS does not reuse a profile id that the
        charger still has persisted (which would silently overwrite an existing
        profile). Resets to 1 only on BootNotification (charger reset wipes
        persisted profiles).
        """
        profile_id = self._state.next_charging_profile_id
        self._state.next_charging_profile_id = profile_id + 1
        return profile_id

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
            last_meter_values_at=state.last_meter_values_at,
            last_meter_values_power_kw=state.last_meter_values_power_kw,
        )

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        """Send a raw OCPP CALL and return protocol-level acknowledgement or failure."""
        async with self._send_lock:
            return await self._send_raw_command_locked(command)

    async def _send_raw_command_locked(self, command: RawProtocolCommand) -> ProtocolCommandResult:
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
        self._state.last_call_error = None
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
