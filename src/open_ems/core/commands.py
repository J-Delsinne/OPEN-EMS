"""Device command and result models for the Epic 8 control execution layer.

Three-stage pipeline (Story 8.3 adds RetryPolicy on top of Story 8.2):

    EvaluationResult.intents
        ↓ IntentExecutor.translate()
    list[DeviceCommand]
        ↓ RetryPolicy.execute()                 (calls PolicyGuard 1..N times)
            ↓ PolicyGuard.authorize_and_dispatch()
                ↓ adapter.send_command()
    CommandResult (final, after all retries)

PolicyGuard is the single mandatory dispatch path (AR15). Adapters never receive
``send_command()`` calls from any other component.

As of Story 9.0, every controllable adapter (Battery, Inverter, EV Charger)
honors the cross-adapter command contract documented in
``open_ems.adapters`` (single source of truth) — closing the AR16 gap that
existed during Epic 8 (where ``send_command`` was a stub raising
``NotImplementedError``). DSMR P1 remains read-only (FR6b) and is excluded
from the controllable-adapter set.

Idempotency classification (``is_idempotent: ClassVar[bool]``) is intrinsic to
the command type and lives at the class level — NOT as a Pydantic field — so it
cannot be overridden per instance and so ``ConfigDict(extra="forbid")`` does not
reject it as an unexpected init kwarg. Setpoint commands (battery / EV rate)
are idempotent: re-applying converges to the same physical state. Session
transitions (``StopEVChargingCommand``) are NOT idempotent: re-issuing a stop
risks affecting a subsequent OCPP session that started between attempts. Any
new command subtype MUST declare ``is_idempotent`` explicitly — the base does
not provide a default to force the author to think about it.

CommandResult.status uses these values per Story 8.2 acceptance criteria:
``success``, ``failed``, ``timeout``, ``rejected``.
"""

from __future__ import annotations

import enum
import uuid
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    # Story 9.0c (D4): post-dispatch correlation_id mismatch (P5). Indeterminate
    # outcome — the adapter may have applied the command but returned a bogus
    # correlation_id. Distinct from ``failed`` so RetryPolicy can refuse to
    # retry (re-issuing a non-idempotent command after a partial dispatch is
    # unsafe).
    correlation_broken = "correlation_broken"


class DeviceCommandBase(BaseModel):
    """Common fields shared by every device command subtype."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    device_role: DeviceRole
    origin: CommandOrigin
    correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # Force every concrete command subtype to declare ``is_idempotent`` in its OWN
        # ``__dict__`` — silent inheritance from a sibling subtype would let a future
        # "stop-like" command auto-classify as idempotent if it inherited from a
        # rate-setpoint command. Idempotency is intrinsic to each command type and
        # must be a deliberate choice.
        super().__init_subclass__(**kwargs)
        if "is_idempotent" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} must declare ``is_idempotent: ClassVar[bool]`` in its own body"
            )


class SetBatteryChargeRateCommand(DeviceCommandBase):
    is_idempotent: ClassVar[bool] = True
    command_type: Literal["set_battery_charge_rate"] = "set_battery_charge_rate"
    rate_kw: Annotated[float, Field(ge=0.0)]


class SetBatteryDischargeRateCommand(DeviceCommandBase):
    is_idempotent: ClassVar[bool] = True
    command_type: Literal["set_battery_discharge_rate"] = "set_battery_discharge_rate"
    rate_kw: Annotated[float, Field(ge=0.0)]


class SetEVChargingRateCommand(DeviceCommandBase):
    is_idempotent: ClassVar[bool] = True
    command_type: Literal["set_ev_charging_rate"] = "set_ev_charging_rate"
    rate_kw: Annotated[float, Field(ge=0.0)]


class StopEVChargingCommand(DeviceCommandBase):
    is_idempotent: ClassVar[bool] = False
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

    @model_validator(mode="after")
    def _applied_iff_success(self) -> CommandResult:
        # ``applied`` and ``status==success`` must agree. A malformed adapter result
        # like ``applied=True, status=failed`` would otherwise short-circuit RetryPolicy
        # into treating the command as successful.
        if self.applied and self.status is not CommandStatus.success:
            raise ValueError(
                f"applied=True requires status=success (got status={self.status.value!r})"
            )
        if not self.applied and self.status is CommandStatus.success:
            raise ValueError("status=success requires applied=True")
        return self
