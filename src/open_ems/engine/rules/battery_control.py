"""Pure battery control intent evaluation."""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict

from open_ems.core import BatteryState, DeviceRole
from open_ems.core.devices import NonEmptyStr, WriteCapability
from open_ems.engine.models import EvaluationInput
from open_ems.engine.rules.energy_balancing import CandidateAction


class BatteryIntentAction(enum.StrEnum):
    """Battery intent actions emitted by the decision engine."""

    hold = "hold"
    charge = "charge"
    discharge = "discharge"


class BatteryIntent(BaseModel):
    """Abstract battery intent for later policy and command handling."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: BatteryIntentAction
    target_power_kw: float | None
    reserve_floor_percent: float
    reason_code: NonEmptyStr
    source_candidate: CandidateAction | None


_CHARGE_ACTIONS = frozenset({"battery_charge_from_pv"})
_DISCHARGE_ACTIONS = frozenset(
    {
        "battery_discharge_to_avoid_import",
        "battery_support_ev",
    }
)


def evaluate_battery_control(
    evaluation_input: EvaluationInput,
    resolved_candidate: CandidateAction | None,
) -> BatteryIntent:
    """Convert a resolved battery candidate into a pure battery intent."""
    battery = evaluation_input.battery
    if not isinstance(battery, BatteryState):
        return _hold(evaluation_input, "battery_unavailable", resolved_candidate)

    if resolved_candidate is None or resolved_candidate.role is not DeviceRole.battery:
        return _hold(evaluation_input, "battery_candidate_unavailable", resolved_candidate)

    if resolved_candidate.action_type in _CHARGE_ACTIONS:
        if not _has_capability(evaluation_input, battery, WriteCapability.set_charge_rate):
            return _hold(evaluation_input, "battery_charge_capability_missing", resolved_candidate)
        return BatteryIntent(
            action=BatteryIntentAction.charge,
            target_power_kw=None,
            reserve_floor_percent=evaluation_input.battery_control.reserve_floor_percent,
            reason_code="battery_charge_allowed",
            source_candidate=resolved_candidate,
        )

    if resolved_candidate.action_type in _DISCHARGE_ACTIONS:
        if battery.soc_percent <= evaluation_input.battery_control.reserve_floor_percent:
            return _hold(
                evaluation_input,
                "battery_reserve_floor_reached",
                resolved_candidate,
            )
        if not _has_capability(evaluation_input, battery, WriteCapability.set_discharge_rate):
            return _hold(
                evaluation_input,
                "battery_discharge_capability_missing",
                resolved_candidate,
            )
        return BatteryIntent(
            action=BatteryIntentAction.discharge,
            target_power_kw=None,
            reserve_floor_percent=evaluation_input.battery_control.reserve_floor_percent,
            reason_code="battery_discharge_allowed",
            source_candidate=resolved_candidate,
        )

    return _hold(evaluation_input, "battery_candidate_unavailable", resolved_candidate)


def _has_capability(
    evaluation_input: EvaluationInput,
    battery: BatteryState,
    capability: WriteCapability,
) -> bool:
    profile = evaluation_input.battery_control.capability_profile
    if profile is None or profile.device_id != battery.device_id:
        return False
    return capability in profile.write_capabilities


def _hold(
    evaluation_input: EvaluationInput,
    reason_code: str,
    source_candidate: CandidateAction | None,
) -> BatteryIntent:
    return BatteryIntent(
        action=BatteryIntentAction.hold,
        target_power_kw=None,
        reserve_floor_percent=evaluation_input.battery_control.reserve_floor_percent,
        reason_code=reason_code,
        source_candidate=source_candidate,
    )
