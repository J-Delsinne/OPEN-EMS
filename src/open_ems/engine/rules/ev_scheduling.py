"""Pure EV charging intent evaluation."""

from __future__ import annotations

import enum
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from open_ems.core import DeviceRole, EVChargerState
from open_ems.core.devices import NonEmptyStr, WriteCapability
from open_ems.engine.models import EvaluationInput, EVChargingWindow
from open_ems.engine.rules.energy_balancing import ActionType, CandidateAction


class EVChargerIntentAction(enum.StrEnum):
    """EV charger intent actions emitted by the decision engine."""

    hold = "hold"
    charge = "charge"
    stop = "stop"


class EVChargerIntent(BaseModel):
    """Abstract EV charger intent for later policy and command handling."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: EVChargerIntentAction
    target_charge_rate_kw: float | None
    homeowner_override_active: bool
    reason_code: NonEmptyStr
    source_candidate: CandidateAction | None


def evaluate_ev_scheduling(
    evaluation_input: EvaluationInput,
    resolved_candidate: CandidateAction | None,
) -> EVChargerIntent:
    """Convert a resolved EV candidate into a pure EV charger intent."""
    ev_charger = evaluation_input.ev_charger
    if not isinstance(ev_charger, EVChargerState):
        return _hold(evaluation_input, "ev_charger_unavailable", resolved_candidate)

    if resolved_candidate is None or resolved_candidate.role is not DeviceRole.ev_charger:
        return _hold(evaluation_input, "ev_candidate_unavailable", resolved_candidate)

    action_type = resolved_candidate.action_type
    if action_type is ActionType.reduce_ev_charge_rate:
        # Intentional hold: peak_limiting candidate; the peak-projection guard below would
        # independently block EV charging under peak overshoot anyway.
        return _hold(evaluation_input, "ev_candidate_unavailable", resolved_candidate)
    elif action_type not in (ActionType.permit_ev_charge, ActionType.ev_charge):
        raise ValueError(f"Unhandled EV ActionType: {action_type!r}")

    peak_context = evaluation_input.peak_context
    if peak_context.current_partial_window_projection_kw >= peak_context.configured_peak_limit_kw:
        return _no_charge_intent(
            evaluation_input,
            ev_charger,
            "peak_limit_blocks_ev_charge",
            resolved_candidate,
        )

    scheduling = evaluation_input.ev_scheduling
    if not scheduling.homeowner_override_active:
        if scheduling.charging_window is None:
            return _no_charge_intent(
                evaluation_input,
                ev_charger,
                "ev_charging_window_inactive",
                resolved_candidate,
            )
        if not _is_inside_window(scheduling.charging_window, scheduling.evaluated_at):
            return _no_charge_intent(
                evaluation_input,
                ev_charger,
                "ev_charging_window_inactive",
                resolved_candidate,
            )

    target_charge_rate_kw = _target_charge_rate_kw(evaluation_input, ev_charger)
    if target_charge_rate_kw is not None:
        available_headroom_kw = (
            peak_context.configured_peak_limit_kw
            - peak_context.current_partial_window_projection_kw
        )
        if target_charge_rate_kw > available_headroom_kw:
            return _no_charge_intent(
                evaluation_input,
                ev_charger,
                "peak_headroom_insufficient",
                resolved_candidate,
            )

    return EVChargerIntent(
        action=EVChargerIntentAction.charge,
        target_charge_rate_kw=target_charge_rate_kw,
        homeowner_override_active=scheduling.homeowner_override_active,
        reason_code="ev_charge_allowed",
        source_candidate=resolved_candidate,
    )


def _is_inside_window(window: EVChargingWindow, evaluated_at_utc: datetime) -> bool:
    local_time = evaluated_at_utc.astimezone(ZoneInfo(window.timezone_name)).time()
    if window.start_local_time < window.end_local_time:
        return window.start_local_time <= local_time < window.end_local_time
    return local_time >= window.start_local_time or local_time < window.end_local_time


def _target_charge_rate_kw(
    evaluation_input: EvaluationInput,
    ev_charger: EVChargerState,
) -> float | None:
    scheduling = evaluation_input.ev_scheduling
    if scheduling.target_charge_rate_kw is None:
        return None
    profile = scheduling.capability_profile
    if profile is None or profile.device_id != ev_charger.device_id:
        return None
    if WriteCapability.set_ev_charge_current not in profile.write_capabilities:
        return None
    return scheduling.target_charge_rate_kw


def _hold(
    evaluation_input: EvaluationInput,
    reason_code: str,
    source_candidate: CandidateAction | None,
) -> EVChargerIntent:
    return EVChargerIntent(
        action=EVChargerIntentAction.hold,
        target_charge_rate_kw=None,
        homeowner_override_active=evaluation_input.ev_scheduling.homeowner_override_active,
        reason_code=reason_code,
        source_candidate=source_candidate,
    )


def _no_charge_intent(
    evaluation_input: EvaluationInput,
    ev_charger: EVChargerState,
    reason_code: str,
    source_candidate: CandidateAction | None,
) -> EVChargerIntent:
    action = EVChargerIntentAction.stop if ev_charger.session_active else EVChargerIntentAction.hold
    return EVChargerIntent(
        action=action,
        target_charge_rate_kw=None,
        homeowner_override_active=evaluation_input.ev_scheduling.homeowner_override_active,
        reason_code=reason_code,
        source_candidate=source_candidate,
    )
