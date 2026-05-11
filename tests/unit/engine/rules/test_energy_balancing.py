from __future__ import annotations

import ast
import random
from collections.abc import Iterable
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from open_ems.core import (
    BatteryState,
    ComponentState,
    DegradedDeviceState,
    DeviceRole,
    EnergyStrategy,
    EVChargerState,
    GlobalState,
    GridMeterState,
    InverterState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.engine import EvaluationInput, PeakContext
from open_ems.engine.models import BatteryControlContext, EVSchedulingContext
from open_ems.engine.rules.energy_balancing import (
    ActionType,
    CandidateAction,
    PriorityBand,
    StrategyEvaluation,
    evaluate_energy_strategy,
    resolve_conflicts,
)

_NOW_UTC = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_DEFAULT_SLOT = object()


def _peak_context(
    *,
    projection_kw: float = 3.8,
    limit_kw: float = 5.0,
) -> PeakContext:
    return PeakContext(
        current_partial_window_projection_kw=projection_kw,
        configured_peak_limit_kw=limit_kw,
        current_monthly_recorded_peak_kw=4.2,
        current_interval_start=_NOW_UTC,
        current_interval_elapsed_seconds=120,
    )


def _battery_control_context() -> BatteryControlContext:
    return BatteryControlContext(reserve_floor_percent=20.0, capability_profile=None)


def _ev_scheduling_context() -> EVSchedulingContext:
    return EVSchedulingContext(
        capability_profile=None,
        charging_window=None,
        homeowner_override_active=False,
        evaluated_at=_NOW_UTC,
        target_charge_rate_kw=None,
    )


def _inverter(*, pv_power_kw: float = 3.2, ac_power_kw: float = 3.0) -> InverterState:
    return InverterState(
        device_id="inv-001",
        pv_power_kw=pv_power_kw,
        ac_power_kw=ac_power_kw,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )


def _battery() -> BatteryState:
    return BatteryState(
        device_id="bat-001",
        soc_percent=55.0,
        battery_power_kw=-1.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW_UTC,
    )


def _ev_charger(
    *,
    session_active: bool = True,
    current_power_kw: float | None = 7.0,
) -> EVChargerState:
    return EVChargerState(
        device_id="ev-001",
        status="charging" if session_active else "available",
        session_active=session_active,
        current_power_kw=current_power_kw,
        read_at=_NOW_UTC,
    )


def _grid_meter(*, grid_power_kw: float = 1.5) -> GridMeterState:
    return GridMeterState(
        device_id="grid-001",
        grid_power_kw=grid_power_kw,
        energy_delivered_kwh=100.0,
        energy_returned_kwh=20.0,
        received_at=_NOW_UTC,
    )


def _degraded(role: DeviceRole, reason: str = "reconnecting") -> DegradedDeviceState:
    return DegradedDeviceState(
        device_id=f"{role.value}-001",
        role=role,
        reason=reason,
        occurred_at=_NOW_UTC,
    )


def _input_with(
    degraded_roles: Iterable[DeviceRole] = (),
    *,
    battery: BatteryState | DegradedDeviceState | None | object = _DEFAULT_SLOT,
    ev_charger: EVChargerState | DegradedDeviceState | None | object = _DEFAULT_SLOT,
    inverter: InverterState | DegradedDeviceState | None | object = _DEFAULT_SLOT,
    grid_meter: GridMeterState | DegradedDeviceState | object = _DEFAULT_SLOT,
    peak_context: PeakContext | None = None,
    strategy: EnergyStrategy = EnergyStrategy.minimize_cost,
) -> EvaluationInput:
    degraded = set(degraded_roles)
    return EvaluationInput(
        inverter=(
            _degraded(DeviceRole.inverter)
            if DeviceRole.inverter in degraded
            else inverter
            if inverter is not _DEFAULT_SLOT
            else _inverter()
        ),
        battery=(
            _degraded(DeviceRole.battery)
            if DeviceRole.battery in degraded
            else battery
            if battery is not _DEFAULT_SLOT
            else _battery()
        ),
        ev_charger=(
            _degraded(DeviceRole.ev_charger)
            if DeviceRole.ev_charger in degraded
            else ev_charger
            if ev_charger is not _DEFAULT_SLOT
            else _ev_charger()
        ),
        grid_meter=(
            _degraded(DeviceRole.grid_meter, "dsmr_stale")
            if DeviceRole.grid_meter in degraded
            else grid_meter
            if grid_meter is not _DEFAULT_SLOT
            else _grid_meter()
        ),
        peak_context=peak_context if peak_context is not None else _peak_context(),
        strategy=strategy,
        battery_control=_battery_control_context(),
        ev_scheduling=_ev_scheduling_context(),
    )


def _candidate(
    *,
    role: DeviceRole = DeviceRole.battery,
    action_type: ActionType = ActionType.battery_charge_from_pv,
    band: PriorityBand = PriorityBand.optimization,
    weight: int = 50,
    tiebreaker: str = "tb",
    source: str = "src",
) -> CandidateAction:
    return CandidateAction(
        role=role,
        action_type=action_type,
        priority_band=band,
        priority_weight=weight,
        tiebreaker_key=tiebreaker,
        source_rule=source,
    )


# ---------------------------------------------------------------------------
# EnergyStrategy enum + EvaluationInput field
# ---------------------------------------------------------------------------


def test_energy_strategy_has_three_documented_members() -> None:
    assert {m.value for m in EnergyStrategy} == {
        "minimize_cost",
        "maximize_self_consumption",
        "prioritize_ev",
    }


def test_evaluation_input_requires_strategy() -> None:
    with pytest.raises(ValidationError):
        EvaluationInput(  # type: ignore[call-arg]
            inverter=_inverter(),
            battery=_battery(),
            ev_charger=_ev_charger(),
            grid_meter=_grid_meter(),
            peak_context=_peak_context(),
            battery_control=_battery_control_context(),
            ev_scheduling=_ev_scheduling_context(),
        )


def test_from_snapshot_requires_strategy_keyword_only() -> None:
    snapshot = SystemSnapshot(
        sequence_id=1,
        captured_at=_NOW_UTC,
        global_state=GlobalState.normal,
        operating_mode=SystemOperatingMode.normal,
        active_strategy=EnergyStrategy.maximize_self_consumption,
        inverter=_inverter(),
        battery=_battery(),
        ev_charger=_ev_charger(),
        grid_meter=_grid_meter(),
        component_states={
            DeviceRole.inverter: ComponentState.active,
            DeviceRole.battery: ComponentState.active,
            DeviceRole.ev_charger: ComponentState.active,
            DeviceRole.grid_meter: ComponentState.active,
        },
        data_age_seconds={
            DeviceRole.inverter: 0,
            DeviceRole.battery: 0,
            DeviceRole.ev_charger: 0,
            DeviceRole.grid_meter: 0,
        },
        system_clock_status="valid",
    )

    with pytest.raises(TypeError):
        EvaluationInput.from_snapshot(  # type: ignore[misc]
            snapshot,
            _peak_context(),  # peak_context passed positionally → fails
            EnergyStrategy.minimize_cost,
        )

    with pytest.raises(TypeError):
        EvaluationInput.from_snapshot(snapshot, peak_context=_peak_context())

    evaluation_input = EvaluationInput.from_snapshot(
        snapshot,
        peak_context=_peak_context(),
        strategy=EnergyStrategy.prioritize_ev,
        battery_control=_battery_control_context(),
        ev_scheduling=_ev_scheduling_context(),
    )
    assert evaluation_input.strategy is EnergyStrategy.prioritize_ev


# ---------------------------------------------------------------------------
# Strategy evaluation: distinctness + behavior
# ---------------------------------------------------------------------------


def test_each_strategy_emits_non_empty_candidates_on_healthy_input() -> None:
    for strategy in EnergyStrategy:
        evaluation = evaluate_energy_strategy(_input_with(strategy=strategy))
        assert evaluation.strategy is strategy
        assert evaluation.suppressed_by_operating_mode is None
        assert len(evaluation.strategy_candidates) > 0, strategy


def test_three_strategies_produce_pairwise_non_equal_candidates() -> None:
    minimize = evaluate_energy_strategy(_input_with(strategy=EnergyStrategy.minimize_cost))
    self_cons = evaluate_energy_strategy(
        _input_with(strategy=EnergyStrategy.maximize_self_consumption)
    )
    prioritize = evaluate_energy_strategy(_input_with(strategy=EnergyStrategy.prioritize_ev))

    assert minimize.strategy_candidates != self_cons.strategy_candidates
    assert minimize.strategy_candidates != prioritize.strategy_candidates
    assert self_cons.strategy_candidates != prioritize.strategy_candidates


def test_minimize_cost_emits_battery_charge_from_pv_when_pv_producing() -> None:
    evaluation = evaluate_energy_strategy(
        _input_with(strategy=EnergyStrategy.minimize_cost, inverter=_inverter(pv_power_kw=2.5))
    )
    actions = {(c.role, c.action_type) for c in evaluation.strategy_candidates}
    assert (DeviceRole.battery, "battery_charge_from_pv") in actions
    assert (DeviceRole.ev_charger, "permit_ev_charge") in actions


def test_minimize_cost_skips_battery_when_no_pv() -> None:
    evaluation = evaluate_energy_strategy(
        _input_with(
            strategy=EnergyStrategy.minimize_cost,
            inverter=_inverter(pv_power_kw=0.0, ac_power_kw=0.0),
        )
    )
    actions = {(c.role, c.action_type) for c in evaluation.strategy_candidates}
    assert (DeviceRole.battery, "battery_charge_from_pv") not in actions


def test_maximize_self_consumption_emits_charge_from_pv_when_pv_exceeds_ac() -> None:
    evaluation = evaluate_energy_strategy(
        _input_with(
            strategy=EnergyStrategy.maximize_self_consumption,
            inverter=_inverter(pv_power_kw=4.0, ac_power_kw=2.0),
            grid_meter=_grid_meter(grid_power_kw=-0.5),  # exporting → no discharge candidate
        )
    )
    actions = {c.action_type for c in evaluation.strategy_candidates}
    assert "battery_charge_from_pv" in actions
    assert "battery_discharge_to_avoid_import" not in actions


def test_maximize_self_consumption_requires_positive_pv_for_charge_candidate() -> None:
    evaluation = evaluate_energy_strategy(
        _input_with(
            strategy=EnergyStrategy.maximize_self_consumption,
            inverter=_inverter(pv_power_kw=0.0, ac_power_kw=-0.1),
            grid_meter=_grid_meter(grid_power_kw=-0.5),
        )
    )
    actions = {c.action_type for c in evaluation.strategy_candidates}
    assert "battery_charge_from_pv" not in actions


def test_maximize_self_consumption_emits_discharge_when_importing() -> None:
    evaluation = evaluate_energy_strategy(
        _input_with(
            strategy=EnergyStrategy.maximize_self_consumption,
            inverter=_inverter(pv_power_kw=0.0, ac_power_kw=0.0),
            grid_meter=_grid_meter(grid_power_kw=2.0),
        )
    )
    actions = {c.action_type for c in evaluation.strategy_candidates}
    assert "battery_discharge_to_avoid_import" in actions
    assert "battery_charge_from_pv" not in actions


def test_prioritize_ev_emits_strongest_ev_candidate() -> None:
    evaluation = evaluate_energy_strategy(_input_with(strategy=EnergyStrategy.prioritize_ev))
    ev_candidates = [c for c in evaluation.strategy_candidates if c.role == DeviceRole.ev_charger]
    assert len(ev_candidates) == 1
    ev_candidate = ev_candidates[0]
    assert ev_candidate.action_type == "ev_charge"
    assert ev_candidate.priority_band == PriorityBand.convenience
    assert ev_candidate.priority_weight == 10  # stronger than minimize_cost's 80


# ---------------------------------------------------------------------------
# Suppression under conservative / fail_safe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "degraded,expected_mode",
    [
        ([DeviceRole.grid_meter], SystemOperatingMode.conservative),
        ([DeviceRole.grid_meter, DeviceRole.battery], SystemOperatingMode.fail_safe),
    ],
)
def test_strategy_suppressed_under_conservative_or_fail_safe(
    degraded: list[DeviceRole], expected_mode: SystemOperatingMode
) -> None:
    for strategy in EnergyStrategy:
        evaluation = evaluate_energy_strategy(_input_with(degraded, strategy=strategy))
        assert evaluation.strategy_candidates == ()
        assert evaluation.suppressed_by_operating_mode == expected_mode


# ---------------------------------------------------------------------------
# Slot eligibility: no candidates targeting degraded/absent slots
# ---------------------------------------------------------------------------


def test_degraded_battery_excludes_battery_candidates() -> None:
    for strategy in EnergyStrategy:
        evaluation = evaluate_energy_strategy(_input_with([DeviceRole.battery], strategy=strategy))
        battery_candidates = [
            c for c in evaluation.strategy_candidates if c.role == DeviceRole.battery
        ]
        assert battery_candidates == [], strategy


def test_degraded_ev_charger_excludes_ev_candidates() -> None:
    for strategy in EnergyStrategy:
        evaluation = evaluate_energy_strategy(
            _input_with([DeviceRole.ev_charger], strategy=strategy)
        )
        ev_candidates = [
            c for c in evaluation.strategy_candidates if c.role == DeviceRole.ev_charger
        ]
        assert ev_candidates == [], strategy


def test_inactive_ev_session_excludes_ev_candidates() -> None:
    for strategy in EnergyStrategy:
        evaluation = evaluate_energy_strategy(
            _input_with(ev_charger=_ev_charger(session_active=False), strategy=strategy)
        )
        ev_candidates = [
            c for c in evaluation.strategy_candidates if c.role == DeviceRole.ev_charger
        ]
        assert ev_candidates == [], strategy


def test_absent_optional_slots_emit_no_candidates_for_those_roles() -> None:
    for strategy in EnergyStrategy:
        evaluation = evaluate_energy_strategy(
            _input_with(battery=None, ev_charger=None, strategy=strategy)
        )
        roles = {c.role for c in evaluation.strategy_candidates}
        assert DeviceRole.battery not in roles
        assert DeviceRole.ev_charger not in roles


def test_inverter_degradation_alone_does_not_suppress_strategy() -> None:
    evaluation = evaluate_energy_strategy(
        _input_with(
            [DeviceRole.inverter],
            strategy=EnergyStrategy.maximize_self_consumption,
            grid_meter=_grid_meter(grid_power_kw=2.0),
        )
    )
    assert evaluation.suppressed_by_operating_mode is None
    actions = {c.action_type for c in evaluation.strategy_candidates}
    assert "battery_discharge_to_avoid_import" in actions
    assert "battery_charge_from_pv" not in actions  # no PV available


# ---------------------------------------------------------------------------
# Conflict resolution: contract behavior
# ---------------------------------------------------------------------------


def test_resolve_conflicts_returns_empty_dict_for_empty_input() -> None:
    assert resolve_conflicts([]) == {}


def test_resolve_conflicts_returns_single_candidate() -> None:
    candidate = _candidate()
    assert resolve_conflicts([candidate]) == {DeviceRole.battery: candidate}


def test_safety_outranks_optimization_at_same_role_regardless_of_weight() -> None:
    safety = _candidate(band=PriorityBand.safety, weight=100, source="safety_rule")
    optimization = _candidate(band=PriorityBand.optimization, weight=1, source="optim_rule")
    resolved = resolve_conflicts([optimization, safety])
    assert resolved[DeviceRole.battery] is safety


def test_optimization_outranks_convenience_at_same_role_regardless_of_weight() -> None:
    optimization = _candidate(band=PriorityBand.optimization, weight=100, source="optim_rule")
    convenience = _candidate(band=PriorityBand.convenience, weight=1, source="conv_rule")
    resolved = resolve_conflicts([convenience, optimization])
    assert resolved[DeviceRole.battery] is optimization


def test_lower_weight_wins_within_same_band() -> None:
    weak = _candidate(weight=50, source="weak")
    strong = _candidate(weight=10, source="strong")
    resolved = resolve_conflicts([weak, strong])
    assert resolved[DeviceRole.battery] is strong


def test_smaller_tiebreaker_key_wins_within_same_band_and_weight() -> None:
    later = _candidate(weight=20, tiebreaker="zzz", source="later")
    earlier = _candidate(weight=20, tiebreaker="aaa", source="earlier")
    resolved = resolve_conflicts([later, earlier])
    assert resolved[DeviceRole.battery] is earlier


def test_smaller_source_rule_wins_when_band_weight_and_tiebreaker_equal() -> None:
    later = _candidate(weight=20, tiebreaker="tb", source="zzz")
    earlier = _candidate(weight=20, tiebreaker="tb", source="aaa")
    resolved = resolve_conflicts([later, earlier])
    assert resolved[DeviceRole.battery] is earlier


def test_smaller_action_type_wins_when_priority_metadata_is_identical() -> None:
    later = _candidate(
        action_type=ActionType.reduce_ev_charge_rate, weight=20, tiebreaker="tb", source="same"
    )
    earlier = _candidate(
        action_type=ActionType.battery_charge_from_pv, weight=20, tiebreaker="tb", source="same"
    )

    forward = resolve_conflicts([later, earlier])
    reverse = resolve_conflicts([earlier, later])

    assert forward == reverse == {DeviceRole.battery: earlier}


def test_resolve_conflicts_groups_per_role() -> None:
    battery = _candidate(role=DeviceRole.battery)
    ev = _candidate(role=DeviceRole.ev_charger, action_type=ActionType.ev_charge)
    resolved = resolve_conflicts([battery, ev])
    assert resolved == {DeviceRole.battery: battery, DeviceRole.ev_charger: ev}


def test_resolve_conflicts_is_deterministic_across_input_ordering() -> None:
    candidates = [
        _candidate(weight=20, tiebreaker="b", source="src1"),
        _candidate(weight=10, tiebreaker="a", source="src2"),
        _candidate(
            role=DeviceRole.ev_charger,
            action_type=ActionType.ev_charge,
            weight=10,
            source="src3",
        ),
        _candidate(weight=10, tiebreaker="a", source="src4"),
        _candidate(band=PriorityBand.safety, weight=50, source="safety"),
    ]
    forward = resolve_conflicts(candidates)
    reverse = resolve_conflicts(list(reversed(candidates)))
    rng = random.Random(0)
    shuffled = list(candidates)
    rng.shuffle(shuffled)
    shuffled_result = resolve_conflicts(shuffled)

    assert forward == reverse == shuffled_result


def test_resolve_conflicts_does_not_mutate_input_list() -> None:
    candidates = [
        _candidate(weight=20, tiebreaker="b"),
        _candidate(weight=10, tiebreaker="a"),
    ]
    snapshot = list(candidates)
    resolve_conflicts(candidates)
    assert candidates == snapshot


def test_repeated_calls_return_equal_strategy_evaluations() -> None:
    evaluation_input = _input_with(strategy=EnergyStrategy.maximize_self_consumption)
    results = [evaluate_energy_strategy(evaluation_input) for _ in range(5)]
    assert all(r == results[0] for r in results)


# ---------------------------------------------------------------------------
# Import boundary
# ---------------------------------------------------------------------------


def test_rule_module_avoids_runtime_infrastructure_imports() -> None:
    import open_ems.engine.rules.energy_balancing as energy_balancing

    imported_modules = set()
    tree = ast.parse(energy_balancing.__loader__.get_source(energy_balancing.__name__) or "")  # type: ignore[union-attr]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    forbidden_modules = (
        "open_ems.storage",
        "open_ems.web",
        "open_ems.adapters",
        "open_ems.services",
    )

    assert not any(
        name == module or name.startswith(f"{module}.")
        for module in forbidden_modules
        for name in imported_modules
    )


# ---------------------------------------------------------------------------
# StrategyEvaluation typing sanity
# ---------------------------------------------------------------------------


def test_strategy_evaluation_is_frozen() -> None:
    evaluation = evaluate_energy_strategy(_input_with(strategy=EnergyStrategy.minimize_cost))
    assert isinstance(evaluation, StrategyEvaluation)
    with pytest.raises(ValidationError):
        evaluation.strategy_candidates = ()  # type: ignore[misc]


def test_candidate_action_rejects_out_of_range_weight() -> None:
    with pytest.raises(ValidationError):
        _candidate(weight=0)
    with pytest.raises(ValidationError):
        _candidate(weight=101)


def test_candidate_action_requires_actiontype_for_action_type_field() -> None:
    valid = _candidate(action_type=ActionType.battery_charge_from_pv)
    assert valid.action_type is ActionType.battery_charge_from_pv

    with pytest.raises(ValidationError):
        CandidateAction(
            role=DeviceRole.battery,
            action_type="completely_unknown_type",  # type: ignore[arg-type]
            priority_band=PriorityBand.optimization,
            priority_weight=20,
            tiebreaker_key="tb",
            source_rule="src",
        )


def test_all_actiontype_members_have_expected_membership() -> None:
    assert set(ActionType) == {
        ActionType.battery_charge_from_pv,
        ActionType.battery_discharge_to_avoid_import,
        ActionType.battery_support_ev,
        ActionType.permit_ev_charge,
        ActionType.ev_charge,
        ActionType.reduce_ev_charge_rate,
    }


def test_evaluate_energy_strategy_handles_all_known_strategies() -> None:
    for strategy in EnergyStrategy:
        result = evaluate_energy_strategy(_input_with(strategy=strategy))
        assert isinstance(result, StrategyEvaluation)
        assert result.strategy is strategy
