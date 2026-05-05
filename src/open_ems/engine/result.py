"""Decision-engine evaluation output contract."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from open_ems.core import DeviceRole, SystemOperatingMode
from open_ems.engine.models import require_utc
from open_ems.engine.rules.battery_control import BatteryIntent
from open_ems.engine.rules.ev_scheduling import EVChargerIntent


class EvaluationResult(BaseModel):
    """Fully annotated output from one decision-engine evaluation cycle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cycle_id: uuid.UUID
    evaluated_at: datetime
    recommended_operating_mode: SystemOperatingMode
    intents: tuple[BatteryIntent | EVChargerIntent, ...]
    decision_reasons: tuple[str, ...]
    cycle_duration_ms: int = Field(ge=0)

    @field_validator("evaluated_at")
    @classmethod
    def _evaluated_at_must_be_utc(cls, value: datetime) -> datetime:
        return require_utc(value, "evaluated_at")

    @field_validator("decision_reasons")
    @classmethod
    def _reasons_must_be_non_empty_strings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for reason in value:
            if not reason.strip():
                raise ValueError(
                    "decision_reasons must not contain empty or whitespace-only strings"
                )
        return value

    @model_validator(mode="after")
    def _reason_count_must_match_intent_count(self) -> EvaluationResult:
        if len(self.intents) != len(self.decision_reasons):
            raise ValueError("decision_reasons must contain one entry per intent")
        return self

    @model_validator(mode="after")
    def _intents_must_be_unique_per_device_role(self) -> EvaluationResult:
        seen_roles: set[DeviceRole] = set()
        for intent in self.intents:
            role = _role_for_intent(intent)
            if role in seen_roles:
                raise ValueError("intents must contain at most one intent per device role")
            seen_roles.add(role)
        return self

    @model_validator(mode="after")
    def _intents_must_not_be_empty(self) -> EvaluationResult:
        if len(self.intents) == 0:
            raise ValueError("intents must not be empty")
        return self


def _role_for_intent(intent: BatteryIntent | EVChargerIntent) -> DeviceRole:
    if isinstance(intent, BatteryIntent):
        return DeviceRole.battery
    if isinstance(intent, EVChargerIntent):
        return DeviceRole.ev_charger
    raise TypeError(f"Unhandled intent type: {type(intent).__name__}")
