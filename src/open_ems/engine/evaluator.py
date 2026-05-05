"""Full-cycle decision-engine evaluation helpers."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from open_ems.core import BatteryState, DeviceRole
from open_ems.engine.models import EvaluationInput
from open_ems.engine.operating_mode import derive_recommended_operating_mode
from open_ems.engine.result import EvaluationResult
from open_ems.engine.rules.battery_control import BatteryIntent, evaluate_battery_control
from open_ems.engine.rules.energy_balancing import (
    CandidateAction,
    PriorityBand,
    evaluate_energy_strategy,
    resolve_conflicts,
)
from open_ems.engine.rules.ev_scheduling import (
    EVChargerIntent,
    EVChargerIntentAction,
    evaluate_ev_scheduling,
)
from open_ems.engine.rules.peak_limiting import (
    LoadReductionAction,
    PeakLimitDecision,
    evaluate_peak_limiting,
)


def evaluate_cycle(evaluation_input: EvaluationInput) -> EvaluationResult:
    """Evaluate one complete decision-engine cycle from an explicit input."""
    start_ns = time.perf_counter_ns()
    evaluated_at = datetime.now(UTC)

    operating_mode = derive_recommended_operating_mode(evaluation_input)
    peak_decision = evaluate_peak_limiting(evaluation_input)
    strategy_eval = evaluate_energy_strategy(evaluation_input)
    peak_candidates = _peak_candidates_to_candidate_actions(peak_decision)
    all_candidates = list(peak_candidates) + list(strategy_eval.strategy_candidates)
    resolved: dict[DeviceRole, CandidateAction] = resolve_conflicts(all_candidates)

    battery_intent = evaluate_battery_control(
        evaluation_input,
        resolved.get(DeviceRole.battery),
    )
    ev_intent = evaluate_ev_scheduling(
        evaluation_input,
        resolved.get(DeviceRole.ev_charger),
    )
    intents = (battery_intent, ev_intent)
    reasons = (
        _reason_for_battery_intent(battery_intent, evaluation_input),
        _reason_for_ev_intent(ev_intent, evaluation_input),
    )

    elapsed_ns = time.perf_counter_ns() - start_ns
    return EvaluationResult(
        cycle_id=uuid.uuid4(),
        evaluated_at=evaluated_at,
        recommended_operating_mode=operating_mode,
        intents=intents,
        decision_reasons=reasons,
        cycle_duration_ms=int(elapsed_ns // 1_000_000),
    )


def _peak_candidates_to_candidate_actions(
    peak_decision: PeakLimitDecision,
) -> list[CandidateAction]:
    if not peak_decision.action_required:
        return []

    candidates: list[CandidateAction] = []
    for action in peak_decision.candidate_actions:
        if action is LoadReductionAction.discharge_battery:
            candidates.append(
                CandidateAction(
                    role=DeviceRole.battery,
                    action_type="battery_discharge_to_avoid_import",
                    priority_band=PriorityBand.safety,
                    priority_weight=1,
                    tiebreaker_key="peak_limit:discharge_battery",
                    source_rule="peak_limiting",
                )
            )
        elif action is LoadReductionAction.reduce_ev_charge_rate:
            candidates.append(
                CandidateAction(
                    role=DeviceRole.ev_charger,
                    action_type="reduce_ev_charge_rate",
                    priority_band=PriorityBand.safety,
                    priority_weight=1,
                    tiebreaker_key="peak_limit:reduce_ev_charge_rate",
                    source_rule="peak_limiting",
                )
            )
        else:
            raise ValueError(f"Unhandled LoadReductionAction: {action!r}")
    return candidates


def _reason_for_battery_intent(
    intent: BatteryIntent,
    evaluation_input: EvaluationInput,
) -> str:
    battery = evaluation_input.battery
    battery_id = battery.device_id if isinstance(battery, BatteryState) else "unknown"
    soc_percent = battery.soc_percent if isinstance(battery, BatteryState) else None

    if intent.reason_code == "battery_unavailable":
        return f"Battery device {battery_id} unavailable or degraded"
    if intent.reason_code == "battery_candidate_unavailable":
        return (
            "No battery candidate from strategy or peak-limit evaluation "
            f"(strategy: {evaluation_input.strategy.value})"
        )
    if intent.reason_code == "battery_charge_capability_missing":
        return (
            "Battery charge blocked: write capability 'set_charge_rate' missing "
            f"for device {battery_id}"
        )
    if intent.reason_code == "battery_discharge_capability_missing":
        return (
            "Battery discharge blocked: write capability 'set_discharge_rate' missing "
            f"for device {battery_id}"
        )
    if intent.reason_code == "battery_reserve_floor_reached":
        return (
            f"Battery discharge blocked: SoC {_format_percent(soc_percent)} is at or below "
            f"configured reserve floor of {intent.reserve_floor_percent:.0f}%"
        )
    if intent.reason_code == "battery_charge_allowed":
        return f"Battery charge approved (strategy: {evaluation_input.strategy.value})"
    if intent.reason_code == "battery_discharge_allowed":
        return (
            f"Battery discharge approved: SoC {_format_percent(soc_percent)} exceeds "
            f"reserve floor of {intent.reserve_floor_percent:.0f}%"
        )
    return f"Battery intent '{intent.action.value}': {intent.reason_code}"


def _reason_for_ev_intent(
    intent: EVChargerIntent,
    evaluation_input: EvaluationInput,
) -> str:
    ev_id = getattr(evaluation_input.ev_charger, "device_id", "unknown")
    if intent.reason_code == "ev_charger_unavailable":
        return f"EV charger {ev_id} unavailable or degraded"
    if intent.reason_code == "ev_candidate_unavailable":
        return (
            "No EV charger candidate from strategy or peak-limit evaluation "
            f"(strategy: {evaluation_input.strategy.value})"
        )
    if intent.reason_code == "ev_charging_window_inactive":
        return _ev_window_inactive_reason(intent, evaluation_input)
    if intent.reason_code == "peak_limit_blocks_ev_charge":
        peak_context = evaluation_input.peak_context
        return (
            "EV charge suppressed: peak projection "
            f"{peak_context.current_partial_window_projection_kw:.2f} kW at or above "
            f"limit of {peak_context.configured_peak_limit_kw:.2f} kW"
        )
    if intent.reason_code == "peak_headroom_insufficient":
        peak_context = evaluation_input.peak_context
        headroom_kw = (
            peak_context.configured_peak_limit_kw
            - peak_context.current_partial_window_projection_kw
        )
        target_kw = evaluation_input.ev_scheduling.target_charge_rate_kw
        target = f"{target_kw} kW" if target_kw is not None else "unknown target"
        return (
            f"EV dynamic rate suppressed: peak headroom {headroom_kw:.2f} kW "
            f"is below target {target}"
        )
    if intent.reason_code == "ev_charge_allowed":
        return (
            f"EV charge approved (strategy: {evaluation_input.strategy.value}, "
            f"override: {intent.homeowner_override_active})"
        )
    return f"EV charger intent '{intent.action.value}': {intent.reason_code}"


def _ev_window_inactive_reason(
    intent: EVChargerIntent,
    evaluation_input: EvaluationInput,
) -> str:
    window = evaluation_input.ev_scheduling.charging_window
    if window is None:
        action = "stop" if intent.action is EVChargerIntentAction.stop else "hold"
        return f"EV {action}: no charging window configured and homeowner override is off"

    start = window.start_local_time.strftime("%H:%M")
    end = window.end_local_time.strftime("%H:%M")
    if intent.action is EVChargerIntentAction.stop:
        return (
            f"EV stop: outside charging window {start}-{end} "
            f"({window.timezone_name}) and active session is being stopped"
        )
    return (
        f"EV hold: outside charging window {start}-{end} "
        f"({window.timezone_name}) and no active session"
    )


def _format_percent(value: float | None) -> str:
    if value is None:
        return "unknown%"
    return f"{value:.0f}%"
