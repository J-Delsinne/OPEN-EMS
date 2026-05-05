"""Energy strategy evaluation and deterministic conflict resolution (FR11, FR11b)."""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from open_ems.core import (
    BatteryState,
    DeviceRole,
    EnergyStrategy,
    EVChargerState,
    GridMeterState,
    InverterState,
    SystemOperatingMode,
)
from open_ems.core.devices import NonEmptyStr
from open_ems.engine.models import EvaluationInput
from open_ems.engine.operating_mode import derive_recommended_operating_mode


class PriorityBand(enum.StrEnum):
    """Conflict-resolution priority band per FR11b: safety > optimization > convenience."""

    safety = "safety"
    optimization = "optimization"
    convenience = "convenience"


_BAND_RANK: Mapping[PriorityBand, int] = MappingProxyType(
    {
        PriorityBand.safety: 0,
        PriorityBand.optimization: 1,
        PriorityBand.convenience: 2,
    }
)


class CandidateAction(BaseModel):
    """Typed candidate action emitted by an engine rule for conflict resolution.

    Total order for resolution: priority_band → priority_weight (1=strongest, 100=weakest)
    → tiebreaker_key (lex) → source_rule (lex). Lower wins at every level.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: DeviceRole
    action_type: NonEmptyStr
    priority_band: PriorityBand
    priority_weight: int = Field(ge=1, le=100)
    tiebreaker_key: NonEmptyStr
    source_rule: NonEmptyStr


class StrategyEvaluation(BaseModel):
    """Strategy evaluator output: per-strategy candidate actions and suppression flag."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: EnergyStrategy
    strategy_candidates: tuple[CandidateAction, ...]
    suppressed_by_operating_mode: SystemOperatingMode | None = None


# Mirrors peak_limiting._SUPPRESSED_MODES; duplicated to avoid coupling private symbols.
_SUPPRESSED_MODES = frozenset({SystemOperatingMode.conservative, SystemOperatingMode.fail_safe})


def evaluate_energy_strategy(evaluation_input: EvaluationInput) -> StrategyEvaluation:
    """Evaluate the active energy strategy and emit typed candidate actions.

    Reuses the Story 7.1 degradation matrix to suppress all strategy candidates under
    `conservative` and `fail_safe` modes (mirrors Story 7.2 AC6 pattern).
    """
    operating_mode = derive_recommended_operating_mode(evaluation_input)
    if operating_mode in _SUPPRESSED_MODES:
        return StrategyEvaluation(
            strategy=evaluation_input.strategy,
            strategy_candidates=(),
            suppressed_by_operating_mode=operating_mode,
        )

    dispatch = {
        EnergyStrategy.minimize_cost: _evaluate_minimize_cost,
        EnergyStrategy.maximize_self_consumption: _evaluate_maximize_self_consumption,
        EnergyStrategy.prioritize_ev: _evaluate_prioritize_ev,
    }
    handler = dispatch.get(evaluation_input.strategy)
    if handler is None:
        return StrategyEvaluation(
            strategy=evaluation_input.strategy,
            strategy_candidates=(),
            suppressed_by_operating_mode=None,
        )
    candidates = handler(evaluation_input)
    return StrategyEvaluation(
        strategy=evaluation_input.strategy,
        strategy_candidates=candidates,
    )


def resolve_conflicts(
    candidates: Iterable[CandidateAction],
) -> dict[DeviceRole, CandidateAction]:
    """Resolve candidate actions to one resolved candidate per device role.

    Total order: band → weight → tiebreaker_key → source_rule → action_type, ascending.
    The first candidate per role after the deterministic sort wins.
    """
    ordered = sorted(
        candidates,
        key=lambda c: (
            _BAND_RANK[c.priority_band],
            c.priority_weight,
            c.tiebreaker_key,
            c.source_rule,
            c.action_type,
        ),
    )
    resolved: dict[DeviceRole, CandidateAction] = {}
    for candidate in ordered:
        resolved.setdefault(candidate.role, candidate)
    return resolved


def _evaluate_minimize_cost(evaluation_input: EvaluationInput) -> tuple[CandidateAction, ...]:
    actions: list[CandidateAction] = []
    inverter = evaluation_input.inverter
    pv_power_kw = inverter.pv_power_kw if isinstance(inverter, InverterState) else 0.0

    if isinstance(evaluation_input.battery, BatteryState) and pv_power_kw > 0:
        actions.append(
            CandidateAction(
                role=DeviceRole.battery,
                action_type="battery_charge_from_pv",
                priority_band=PriorityBand.optimization,
                priority_weight=20,
                tiebreaker_key="minimize_cost:battery_charge_from_pv",
                source_rule="strategy_minimize_cost",
            )
        )

    ev_charger = evaluation_input.ev_charger
    if isinstance(ev_charger, EVChargerState) and ev_charger.session_active:
        actions.append(
            CandidateAction(
                role=DeviceRole.ev_charger,
                action_type="permit_ev_charge",
                priority_band=PriorityBand.convenience,
                priority_weight=80,
                tiebreaker_key="minimize_cost:permit_ev_charge",
                source_rule="strategy_minimize_cost",
            )
        )

    return tuple(actions)


def _evaluate_maximize_self_consumption(
    evaluation_input: EvaluationInput,
) -> tuple[CandidateAction, ...]:
    actions: list[CandidateAction] = []
    inverter = evaluation_input.inverter
    pv_power_kw = inverter.pv_power_kw if isinstance(inverter, InverterState) else 0.0
    ac_power_kw = inverter.ac_power_kw if isinstance(inverter, InverterState) else 0.0
    grid_meter = evaluation_input.grid_meter
    grid_power_kw = grid_meter.grid_power_kw if isinstance(grid_meter, GridMeterState) else 0.0

    battery_healthy = isinstance(evaluation_input.battery, BatteryState)

    if battery_healthy and pv_power_kw > 0 and pv_power_kw > ac_power_kw:
        actions.append(
            CandidateAction(
                role=DeviceRole.battery,
                action_type="battery_charge_from_pv",
                priority_band=PriorityBand.optimization,
                priority_weight=10,
                tiebreaker_key="maximize_self_consumption:battery_charge_from_pv",
                source_rule="strategy_maximize_self_consumption",
            )
        )

    if battery_healthy and grid_power_kw > 0:
        actions.append(
            CandidateAction(
                role=DeviceRole.battery,
                action_type="battery_discharge_to_avoid_import",
                priority_band=PriorityBand.optimization,
                priority_weight=15,
                tiebreaker_key="maximize_self_consumption:battery_discharge_to_avoid_import",
                source_rule="strategy_maximize_self_consumption",
            )
        )

    return tuple(actions)


def _evaluate_prioritize_ev(evaluation_input: EvaluationInput) -> tuple[CandidateAction, ...]:
    actions: list[CandidateAction] = []
    ev_charger = evaluation_input.ev_charger
    ev_session_active = isinstance(ev_charger, EVChargerState) and ev_charger.session_active

    if ev_session_active:
        actions.append(
            CandidateAction(
                role=DeviceRole.ev_charger,
                action_type="ev_charge",
                priority_band=PriorityBand.convenience,
                priority_weight=10,
                tiebreaker_key="prioritize_ev:ev_charge",
                source_rule="strategy_prioritize_ev",
            )
        )

    if ev_session_active and isinstance(evaluation_input.battery, BatteryState):
        actions.append(
            CandidateAction(
                role=DeviceRole.battery,
                action_type="battery_support_ev",
                priority_band=PriorityBand.optimization,
                priority_weight=30,
                tiebreaker_key="prioritize_ev:battery_support_ev",
                source_rule="strategy_prioritize_ev",
            )
        )

    return tuple(actions)
