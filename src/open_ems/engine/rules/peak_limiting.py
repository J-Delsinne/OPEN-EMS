"""Peak-limiting rule for pure decision-engine evaluation."""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field

from open_ems.core import BatteryState, EVChargerState, SystemOperatingMode
from open_ems.engine.models import EvaluationInput
from open_ems.engine.operating_mode import derive_recommended_operating_mode


class LoadReductionAction(enum.StrEnum):
    """Rule-level candidate actions for reducing projected peak load."""

    reduce_ev_charge_rate = "reduce_ev_charge_rate"
    discharge_battery = "discharge_battery"


class PeakLimitDecision(BaseModel):
    """Peak-limiting rule output before final intent resolution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_required: bool
    required_reduction_kw: float = Field(ge=0.0)
    candidate_actions: tuple[LoadReductionAction, ...]
    suppressed_by_operating_mode: SystemOperatingMode | None = None


# AC6: grid-meter degradation produces conservative or fail_safe — peak rule must not emit commands.
_SUPPRESSED_MODES = frozenset({SystemOperatingMode.conservative, SystemOperatingMode.fail_safe})


def evaluate_peak_limiting(evaluation_input: EvaluationInput) -> PeakLimitDecision:
    """Evaluate the peak-limiting rule from explicit engine input only."""
    operating_mode = derive_recommended_operating_mode(evaluation_input)
    if operating_mode in _SUPPRESSED_MODES:
        return _no_action(suppressed_by_operating_mode=operating_mode)

    peak_context = evaluation_input.peak_context
    required_reduction_kw = (
        peak_context.current_partial_window_projection_kw - peak_context.configured_peak_limit_kw
    )
    if required_reduction_kw <= 0:
        return _no_action()

    return PeakLimitDecision(
        action_required=True,
        required_reduction_kw=required_reduction_kw,
        candidate_actions=_candidate_actions(evaluation_input),
    )


def _candidate_actions(evaluation_input: EvaluationInput) -> tuple[LoadReductionAction, ...]:
    actions: list[LoadReductionAction] = []
    ev_charger = evaluation_input.ev_charger
    if (
        isinstance(ev_charger, EVChargerState)
        and ev_charger.session_active
        and ev_charger.current_power_kw is not None
        and ev_charger.current_power_kw > 0
    ):
        actions.append(LoadReductionAction.reduce_ev_charge_rate)

    if isinstance(evaluation_input.battery, BatteryState):
        actions.append(LoadReductionAction.discharge_battery)

    return tuple(actions)


def _no_action(
    *,
    suppressed_by_operating_mode: SystemOperatingMode | None = None,
) -> PeakLimitDecision:
    return PeakLimitDecision(
        action_required=False,
        required_reduction_kw=0.0,
        candidate_actions=(),
        suppressed_by_operating_mode=suppressed_by_operating_mode,
    )
