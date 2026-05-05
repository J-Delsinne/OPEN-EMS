"""Device command and result models for the Epic 8 control execution layer.

Two-stage pipeline (Story 8.2):

    EvaluationResult.intents
        ↓ IntentExecutor.translate()
    list[DeviceCommand]
        ↓ PolicyGuard.authorize_and_dispatch()
    CommandResult

PolicyGuard is the single mandatory dispatch path (AR15). Adapters never receive
``send_command()`` calls from any other component.

CommandResult.status uses these values per Story 8.2 acceptance criteria:
``success``, ``failed``, ``timeout``, ``rejected``.
"""

from __future__ import annotations

import enum
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from open_ems.core.devices import DegradedDeviceState, DeviceRole, DeviceState, NonEmptyStr


class CommandOrigin(enum.StrEnum):
    """Originator of a device command."""

    decision_engine = "decision_engine"
    installer = "installer"
    homeowner = "homeowner"
    system = "system"


class CommandStatus(enum.StrEnum):
    """Terminal status of a device command after PolicyGuard dispatch."""

    success = "success"
    failed = "failed"
    timeout = "timeout"
    rejected = "rejected"


class DeviceCommandBase(BaseModel):
    """Common fields shared by every device command subtype."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    device_role: DeviceRole
    origin: CommandOrigin
    correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)


class SetBatteryChargeRateCommand(DeviceCommandBase):
    command_type: Literal["set_battery_charge_rate"] = "set_battery_charge_rate"
    rate_kw: Annotated[float, Field(ge=0.0)]


class SetBatteryDischargeRateCommand(DeviceCommandBase):
    command_type: Literal["set_battery_discharge_rate"] = "set_battery_discharge_rate"
    rate_kw: Annotated[float, Field(ge=0.0)]


class SetEVChargingRateCommand(DeviceCommandBase):
    command_type: Literal["set_ev_charging_rate"] = "set_ev_charging_rate"
    rate_kw: Annotated[float, Field(ge=0.0)]


class StopEVChargingCommand(DeviceCommandBase):
    command_type: Literal["stop_ev_charging"] = "stop_ev_charging"


DeviceCommand = (
    SetBatteryChargeRateCommand
    | SetBatteryDischargeRateCommand
    | SetEVChargingRateCommand
    | StopEVChargingCommand
)


class CommandResult(BaseModel):
    """Terminal outcome of a device command after PolicyGuard dispatch.

    ``applied`` is the source of truth for whether the device state changed.
    Callers MUST NOT assume mutation when ``applied`` is False.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: uuid.UUID
    device_id: NonEmptyStr
    status: CommandStatus
    applied: bool
    reason: str
    observed_state: DeviceState | DegradedDeviceState | None = None
