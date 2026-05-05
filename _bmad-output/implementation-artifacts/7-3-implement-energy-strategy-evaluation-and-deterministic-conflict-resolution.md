# Story 7.3: Implement Energy Strategy Evaluation and Deterministic Conflict Resolution

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want the decision engine to apply the active homeowner-selected `EnergyStrategy` using local data and to resolve conflicting energy demands with an explicit deterministic priority model,
so that strategy selection produces measurably different system behavior, v1 operates without optional external data (FR40/FR41), and equal-priority conflicts always resolve identically.

## Acceptance Criteria

**AC1 - EnergyStrategy is the only strategy input**
Given the decision engine receives an evaluation input
When strategy evaluation runs
Then it reads the active strategy exclusively from `EvaluationInput.strategy`
And it does not access `config_repo`, `ConfigAuditRepo`, `StateStore`, web/session state, environment variables, files, sockets, adapter objects, or wall-clock time
And it does not require dynamic tariff data (FR40/FR41 are nice-to-have; Epic 13 enriches behavior if configured)
And it does not require solar forecast data (FR40 is nice-to-have).

**AC2 - EnergyStrategy enum and EvaluationInput field**
Given the engine input contract is extended for strategy evaluation
When `EnergyStrategy` is defined
Then it is a `StrEnum` in `src/open_ems/core/state.py` (next to `SystemOperatingMode`) with exactly three members and value strings:
- `minimize_cost = "minimize_cost"`
- `maximize_self_consumption = "maximize_self_consumption"`
- `prioritize_ev = "prioritize_ev"`

And `EvaluationInput` gains a required `strategy: EnergyStrategy` field
And `EvaluationInput.from_snapshot()` accepts a keyword-only `strategy: EnergyStrategy` argument and copies it into the model
And `EvaluationInput` remains frozen with `extra="forbid"`
And the existing required-slot validation for `inverter` and `grid_meter` is preserved
And the existing `peak_context: PeakContext` field is preserved unchanged.

**AC3 - Each strategy produces meaningfully different candidate actions**
Given identical device states and a single overshoot-free `PeakContext`
When `evaluate_energy_strategy()` runs for each `EnergyStrategy` value in turn
Then the three returned `StrategyEvaluation` objects have non-equal `strategy_candidates` tuples
And each strategy's candidates reflect its documented bias:
- `minimize_cost`: emphasizes peak-headroom protection and PV-surplus battery charging; suppresses EV charge candidates outside the configured window unless the configured window is unavailable in this story (Story 7.4 owns EV-window logic; Story 7.3 emits at most a low-priority "permit_ev_charge" candidate at convenience band)
- `maximize_self_consumption`: when PV production exceeds inverter AC export by a positive margin, emits a `battery_charge_from_pv` candidate at optimization band targeting `DeviceRole.battery`; when PV is below current grid import, emits a `battery_discharge_to_avoid_import` candidate at optimization band
- `prioritize_ev`: when the EV charger slot is a healthy `EVChargerState` with `session_active=True`, emits an `ev_charge` candidate at convenience band targeting `DeviceRole.ev_charger` with the strongest weight any strategy emits in the convenience band

And no strategy emits the empty tuple for an input where its triggering preconditions hold
And strategy logic does NOT produce final battery/EV intents (`BatteryIntent`, `EVChargerIntent` are owned by Story 7.4) — it produces typed `CandidateAction` records consumed by the conflict resolver in this story and by Stories 7.4 and 7.5 later.

**AC4 - Deterministic priority bands and conflict resolution contract**
Given the engine receives candidate actions from one or more rules
When `resolve_conflicts()` runs
Then it accepts an iterable of `CandidateAction` records
And it groups candidates by `DeviceRole` and selects exactly one resolved candidate per role
And the resolved candidate is the one with the highest priority under this total order:
1. `PriorityBand.safety` outranks `PriorityBand.optimization` outranks `PriorityBand.convenience`
2. Within the same band, lower numeric `priority_weight` outranks higher `priority_weight` (1 is strongest, 100 is weakest)
3. Within the same band and weight, lexicographically smaller `tiebreaker_key` outranks larger
4. Within identical band, weight, and tiebreaker_key, lexicographically smaller `source_rule` outranks larger

And the result is a `dict[DeviceRole, CandidateAction]` with at most one entry per role and only roles for which at least one candidate was supplied
And `resolve_conflicts()` does not mutate its input
And `resolve_conflicts()` is pure: same input always returns equal output regardless of input ordering and regardless of the surrounding wall clock.

**AC5 - Safety outranks optimization outranks convenience**
Given a `safety`-band candidate and an `optimization`-band candidate target the same `DeviceRole`
When conflict resolution runs
Then the safety candidate is the resolved result regardless of weight values across bands
And given an `optimization`-band candidate and a `convenience`-band candidate target the same `DeviceRole`, the optimization candidate is the resolved result regardless of weight values across bands.

**AC6 - Equal-priority tiebreaker is stable and documented**
Given two candidates target the same `DeviceRole` with identical `priority_band` and identical `priority_weight`
When conflict resolution runs
Then the tiebreaker order documented in AC4 (lexicographic `tiebreaker_key`, then lexicographic `source_rule`) determines the winner
And the tiebreaker MUST NOT depend on wall-clock time, `random`, `id()`, dictionary insertion order, set iteration order, or any other non-deterministic source
And repeated calls with the same set of candidates supplied in any input ordering return equal results.

**AC7 - Operating-mode and degraded-device safety**
Given Story 7.1 already derives `recommended_operating_mode`
When `evaluate_energy_strategy()` runs
Then it reuses `derive_recommended_operating_mode()` from `open_ems.engine.operating_mode`; it does not reimplement the degradation matrix
And if the derived mode is `conservative` or `fail_safe`, `evaluate_energy_strategy()` returns a `StrategyEvaluation` whose `strategy_candidates` is empty and whose `suppressed_by_operating_mode` reflects the suppressing mode
And if `battery` is degraded or absent, no strategy emits a `battery_*` candidate
And if `ev_charger` is degraded or absent or `session_active=False`, no strategy emits an `ev_charge` candidate
And inverter degradation alone (with healthy battery, EV, and grid meter) does not suppress strategy evaluation; `maximize_self_consumption` cannot rely on PV but may still emit `battery_discharge_to_avoid_import` when grid_meter shows import.

**AC8 - Architecture boundaries**
Given the pure decision-engine import contract from Story 7.1
When this story adds new modules
Then `src/open_ems/engine/rules/energy_balancing.py` may import from `open_ems.core`, `open_ems.engine.models`, `open_ems.engine.operating_mode`, the standard library, and Pydantic only
And it MUST NOT import from `open_ems.web`, `open_ems.storage`, `open_ems.adapters`, or `open_ems.services`
And no `Command`, `DeviceCommand`, `PolicyGuard`, control loop, command dispatch, audit log entry, event log entry, or migration is created in this story
And `StateStore.publish()` and `StateStore.get_snapshot()` are not called from any new module
And the existing `peak_limiting.py` rule and `PeakLimitDecision` are NOT modified by this story (Story 7.5 will integrate `PeakLimitDecision.candidate_actions` into the broader candidate stream when it defines `EvaluationResult`).

**AC9 - Tests**
- Unit tests verify each `EnergyStrategy` produces a non-empty distinct `strategy_candidates` tuple for an input where its triggering preconditions hold, and that the three results are pairwise non-equal.
- Unit tests verify `EvaluationInput` rejects construction without `strategy`.
- Unit tests verify `EvaluationInput.from_snapshot()` requires `strategy` as a keyword argument.
- Unit tests verify priority hierarchy: a safety candidate outranks an optimization candidate at the same role; an optimization candidate outranks a convenience candidate at the same role.
- Unit tests verify documented tiebreakers: identical (band, weight) → smaller `tiebreaker_key` wins; identical (band, weight, tiebreaker_key) → smaller `source_rule` wins.
- Unit tests verify `resolve_conflicts()` returns equal results for the same candidate set supplied in three different input orderings (forward, reversed, shuffled).
- Unit tests verify `resolve_conflicts()` does not mutate its input.
- Unit tests verify suppression: `conservative` and `fail_safe` derived modes empty `strategy_candidates` and set `suppressed_by_operating_mode`.
- Unit tests verify degraded/absent battery and EV slots are excluded from strategy candidates targeting those roles.
- Unit tests verify the rule modules do not import `open_ems.storage`, `open_ems.web`, `open_ems.adapters`, or `open_ems.services` (mirror the AST-based test in `test_peak_limiting.py`).
- Unit tests verify the existing peak-limiting tests still pass with the new required `strategy` field on `EvaluationInput`.

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` and record the baseline pass count.
  - [x] Run `uv run python -m ruff check .`.
  - [x] Run `uv run python -m ruff format --check .`.
  - [x] Run `uv run python -m mypy src/`.
  - [x] Treat any unrelated pre-existing failure as a baseline note; do not hide it by changing unrelated code.

- [x] Task 1: Add `EnergyStrategy` enum in `core/state.py` (AC: AC1, AC2)
  - [x] Update `src/open_ems/core/state.py`.
  - [x] Add `class EnergyStrategy(enum.StrEnum)` with members `minimize_cost`, `maximize_self_consumption`, `prioritize_ev` and value strings exactly equal to the member names.
  - [x] Place it directly after `SystemOperatingMode` for visual locality.
  - [x] Export `EnergyStrategy` from `open_ems.core` (`src/open_ems/core/__init__.py`) keeping `__all__` sorted.
  - [x] Do not change `SystemOperatingMode`, `GlobalState`, `ComponentState`, `SystemSnapshot`, or any existing serializer.

- [x] Task 2: Extend `EvaluationInput` with `strategy` (AC: AC1, AC2, AC9)
  - [x] Update `src/open_ems/engine/models.py`.
  - [x] Add `strategy: EnergyStrategy` as a required field on `EvaluationInput` after `peak_context`.
  - [x] Update `EvaluationInput.from_snapshot()` to accept a keyword-only `strategy: EnergyStrategy` argument and forward it into the model.
  - [x] Preserve the existing `_required_slots_present` model validator and the existing `peak_context` field.
  - [x] Update existing `tests/unit/engine/test_operating_mode.py` and `tests/unit/engine/rules/test_peak_limiting.py` helpers to supply a deterministic default strategy (`EnergyStrategy.minimize_cost`) so 7.1/7.2 tests continue to pass; do not weaken any prior validation.

- [x] Task 3: Define the conflict-resolution contract (AC: AC4, AC5, AC6, AC8)
  - [x] Create `src/open_ems/engine/rules/energy_balancing.py`.
  - [x] Define `class PriorityBand(enum.StrEnum)` with members `safety = "safety"`, `optimization = "optimization"`, `convenience = "convenience"`.
  - [x] Define a private module-level mapping `_BAND_RANK: Mapping[PriorityBand, int]` ordered `safety=0, optimization=1, convenience=2` (lower wins).
  - [x] Define `class CandidateAction(BaseModel)` with `ConfigDict(frozen=True, extra="forbid")` and fields:
    - `role: DeviceRole`
    - `action_type: NonEmptyStr` (a stable string identifier such as `"battery_charge_from_pv"`, `"battery_discharge_to_avoid_import"`, `"ev_charge"`, `"reduce_ev_charge_rate"`, `"discharge_battery"`)
    - `priority_band: PriorityBand`
    - `priority_weight: int = Field(ge=1, le=100)` (1 = strongest within band; 100 = weakest)
    - `tiebreaker_key: NonEmptyStr`
    - `source_rule: NonEmptyStr` (e.g., `"peak_limiting"`, `"strategy_minimize_cost"`)
  - [x] Define `def resolve_conflicts(candidates: Iterable[CandidateAction]) -> dict[DeviceRole, CandidateAction]`.
  - [x] Implementation order: copy iterable into a stable list, sort with key `(_BAND_RANK[c.priority_band], c.priority_weight, c.tiebreaker_key, c.source_rule)` ascending, then iterate and place into a result dict keyed by role only if the role is not already present (first wins after deterministic sort).
  - [x] Do not mutate the input iterable. Do not call `random`, `time`, `datetime.now`, `uuid`, or any non-deterministic API.
  - [x] Reuse `NonEmptyStr` from `open_ems.core.devices` (re-export through `core/__init__.py` only if not already exported; otherwise import from `core.devices` directly with a single comment noting why this exception is needed).

- [x] Task 4: Implement strategy evaluation (AC: AC1, AC3, AC7, AC8)
  - [x] In the same module `energy_balancing.py`, define `class StrategyEvaluation(BaseModel)` with `ConfigDict(frozen=True, extra="forbid")` and fields:
    - `strategy: EnergyStrategy`
    - `strategy_candidates: tuple[CandidateAction, ...]`
    - `suppressed_by_operating_mode: SystemOperatingMode | None = None`
  - [x] Define `def evaluate_energy_strategy(evaluation_input: EvaluationInput) -> StrategyEvaluation`.
  - [x] First step inside the function: derive the operating mode via `derive_recommended_operating_mode(evaluation_input)`. If the mode is `conservative` or `fail_safe`, return `StrategyEvaluation(strategy=evaluation_input.strategy, strategy_candidates=(), suppressed_by_operating_mode=mode)`. Reuse the suppression set pattern from `peak_limiting._SUPPRESSED_MODES` by defining a local `_SUPPRESSED_MODES = frozenset({SystemOperatingMode.conservative, SystemOperatingMode.fail_safe})` (do not import the private symbol from `peak_limiting`).
  - [x] Dispatch to one private helper per strategy: `_evaluate_minimize_cost`, `_evaluate_maximize_self_consumption`, `_evaluate_prioritize_ev`. Each returns `tuple[CandidateAction, ...]`.
  - [x] All three helpers must:
    - Skip a `battery_*` candidate when `evaluation_input.battery` is not an instance of `BatteryState`.
    - Skip an `ev_charge` candidate when `evaluation_input.ev_charger` is not an instance of `EVChargerState` with `session_active=True`.
    - Read PV from `evaluation_input.inverter.pv_power_kw` only if it is an instance of `InverterState`; otherwise treat PV as unavailable.
    - Read grid import from `evaluation_input.grid_meter.grid_power_kw` (always present per `_required_slots_present` validator).
    - Use timezone-aware UTC datetimes only if datetimes are needed (none required for v1 strategy logic).
  - [x] `_evaluate_minimize_cost`:
    - Emit `CandidateAction(role=battery, action_type="battery_charge_from_pv", priority_band=optimization, priority_weight=20, tiebreaker_key="minimize_cost:battery_charge_from_pv", source_rule="strategy_minimize_cost")` when battery is healthy AND `pv_power_kw > 0`.
    - Emit `CandidateAction(role=ev_charger, action_type="permit_ev_charge", priority_band=convenience, priority_weight=80, tiebreaker_key="minimize_cost:permit_ev_charge", source_rule="strategy_minimize_cost")` when EV slot is healthy with `session_active=True`. (Story 7.4 will refine window-aware EV behavior.)
  - [x] `_evaluate_maximize_self_consumption`:
    - When PV is available and `inverter.pv_power_kw > inverter.ac_power_kw`, emit `CandidateAction(role=battery, action_type="battery_charge_from_pv", priority_band=optimization, priority_weight=10, tiebreaker_key="maximize_self_consumption:battery_charge_from_pv", source_rule="strategy_maximize_self_consumption")` when battery is healthy.
    - When `grid_meter.grid_power_kw > 0` (importing) AND battery is healthy, emit `CandidateAction(role=battery, action_type="battery_discharge_to_avoid_import", priority_band=optimization, priority_weight=15, tiebreaker_key="maximize_self_consumption:battery_discharge_to_avoid_import", source_rule="strategy_maximize_self_consumption")`.
    - The two candidates above may both be emitted on the same input; conflict resolution will pick the lower-weight winner per role.
  - [x] `_evaluate_prioritize_ev`:
    - When EV is healthy with `session_active=True`, emit `CandidateAction(role=ev_charger, action_type="ev_charge", priority_band=convenience, priority_weight=10, tiebreaker_key="prioritize_ev:ev_charge", source_rule="strategy_prioritize_ev")`. Weight 10 is intentionally stronger than `minimize_cost`'s `permit_ev_charge` weight 80.
    - When battery is healthy, emit `CandidateAction(role=battery, action_type="battery_support_ev", priority_band=optimization, priority_weight=30, tiebreaker_key="prioritize_ev:battery_support_ev", source_rule="strategy_prioritize_ev")`.
  - [x] Numeric weight choices above are MANDATORY for AC3 to produce non-equal `strategy_candidates` tuples across the three strategies on the same input.

- [x] Task 5: Export the public surface (AC: AC2, AC8)
  - [x] Update `src/open_ems/engine/__init__.py` `__all__` to add: `CandidateAction`, `EnergyStrategy`, `PriorityBand`, `StrategyEvaluation`, `evaluate_energy_strategy`, `resolve_conflicts`.
  - [x] Re-export from the right modules: `EnergyStrategy` from `open_ems.core`; the rest from `open_ems.engine.rules.energy_balancing`.
  - [x] Keep names direct and boring; do not introduce a generic `Decision` or `Intent` symbol that would collide with Stories 7.4 and 7.5.

- [x] Task 6: Add focused unit tests (AC: AC1-AC9)
  - [x] Create `tests/unit/engine/rules/test_energy_balancing.py`.
  - [x] Add helper constructors mirroring `tests/unit/engine/rules/test_peak_limiting.py` style (timezone-aware UTC datetimes, healthy device states, `_input_with(strategy=...)`-style helper).
  - [x] Strategy tests:
    - Each `EnergyStrategy` produces a non-empty `strategy_candidates` tuple on a healthy input where its preconditions hold (PV producing, EV charging, battery healthy, grid importing slightly).
    - The three strategies' `strategy_candidates` tuples are pairwise non-equal on the same input (verifies AC3 directly).
    - `EvaluationInput` cannot be constructed without `strategy` (`pytest.raises(ValidationError)`).
    - `EvaluationInput.from_snapshot()` requires `strategy` as a keyword-only argument.
  - [x] Suppression tests:
    - With `grid_meter` degraded (mode = `conservative`), `evaluate_energy_strategy` returns empty candidates with `suppressed_by_operating_mode == SystemOperatingMode.conservative`.
    - With `grid_meter` and `battery` both degraded (mode = `fail_safe`), `evaluate_energy_strategy` returns empty candidates with `suppressed_by_operating_mode == SystemOperatingMode.fail_safe`.
  - [x] Slot-eligibility tests:
    - Degraded battery → no `battery_*` candidate emitted by any strategy.
    - Degraded EV charger → no `ev_charge` candidate emitted by any strategy.
    - `EVChargerState` with `session_active=False` → no `ev_charge` candidate.
    - Inverter degradation alone → `maximize_self_consumption` may still emit `battery_discharge_to_avoid_import` if grid is importing.
  - [x] Conflict-resolver tests:
    - Empty input returns empty dict.
    - Single candidate input returns a one-entry dict.
    - Two candidates same role, different bands (safety vs optimization) → safety wins regardless of weight.
    - Two candidates same role, different bands (optimization vs convenience) → optimization wins regardless of weight.
    - Two candidates same role, same band, different weights → lower weight wins.
    - Two candidates same role, same band, same weight, different `tiebreaker_key` → lexicographically smaller wins.
    - Two candidates same role, same band, same weight, same `tiebreaker_key`, different `source_rule` → lexicographically smaller wins.
    - Same set of candidates passed in forward / reversed / shuffled order produces equal results across all three orderings (run with `random.Random(seed=0)` for the shuffle so the test itself is deterministic).
    - Mutation safety: `resolve_conflicts(list_of_candidates)` does not mutate the original list (compare to a copy taken before the call).
  - [x] Import-boundary test:
    - AST-walk `open_ems.engine.rules.energy_balancing` and assert no top-level imports from `open_ems.storage`, `open_ems.web`, `open_ems.adapters`, or `open_ems.services`. Mirror the implementation in `test_peak_limiting.test_rule_module_avoids_runtime_infrastructure_imports`.

- [x] Task 7: Final validation (AC: all)
  - [x] Run `uv run python -m ruff check .`.
  - [x] Run `uv run python -m ruff format --check .`.
  - [x] Run `uv run python -m mypy src/`.
  - [x] Run `uv run python -m pytest tests/unit/engine/ --no-cov -q` (focused engine suite).
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` (full suite). Pass count must be ≥ 7.2 baseline (637) plus the new tests added in this story.
  - [x] Confirm story Status is `review` only after all checked items above are true.

### Review Findings

- [x] [Review][Patch] Conflict resolver is order-dependent for fully tied same-role candidates [src/open_ems/engine/rules/energy_balancing.py:107] — fixed
- [x] [Review][Patch] Self-consumption can emit charge-from-PV with zero PV when AC power is negative [src/open_ems/engine/rules/energy_balancing.py:167] — fixed
- [x] [Review][Patch] Missing direct test that from_snapshot rejects omitted strategy keyword [tests/unit/engine/rules/test_energy_balancing.py:218] — fixed

## Dev Notes

### Scope and Non-Scope

This story adds the engine-level energy-strategy evaluator and the deterministic conflict-resolution primitive. It does NOT introduce concrete typed final intents (Story 7.4) and does NOT define the final `EvaluationResult` contract (Story 7.5).

In scope:
- `EnergyStrategy` enum in `core/state.py` (next to `SystemOperatingMode`).
- A required `strategy: EnergyStrategy` field on `EvaluationInput`.
- A pure deterministic `evaluate_energy_strategy()` that emits typed `CandidateAction` records biased per strategy, suppressed under conservative/fail_safe.
- A pure deterministic `resolve_conflicts()` that picks one resolved candidate per `DeviceRole` using a documented total order: band → weight → tiebreaker_key → source_rule.
- Unit tests proving strategy distinctness, conflict-resolution determinism, suppression, and import-boundary safety.

Out of scope:
- `BatteryIntent`, `EVChargerIntent`, capability-summary-bound intents — Story 7.4 owns these.
- Battery reserve-floor and EV charging-window logic — Story 7.4 owns these.
- Final `EvaluationResult` with `decision_reasons` and `cycle_duration_ms` — Story 7.5 owns this.
- Wiring `PeakLimitDecision.candidate_actions` into the conflict-resolver pipeline — Story 7.5 owns this when defining `EvaluationResult`.
- Integration with `config_repo` to read the active strategy — Epic 8 runtime control loop owns this.
- `PolicyGuard`, `DeviceCommand`, command dispatch, audit/event logging — Epic 8 owns these.
- Dynamic tariff (FR41) and solar forecast (FR40) integrations — Epic 13 owns these.
- Migrations — none in this story.

### Existing Code to Preserve

`src/open_ems/core/state.py`
- Current state: defines `GlobalState`, `ComponentState`, `SystemOperatingMode`, `OPERATING_MODE_GLOBAL_STATE`, `derive_global_state`, `SystemSnapshot`, `_FAILED_DEGRADED_REASONS`, `_is_failed_reason`, `derive_component_state`, `derive_data_age_seconds`, role tuples and required/optional role frozensets.
- Change expected: add `class EnergyStrategy(enum.StrEnum)` immediately after `SystemOperatingMode`. No other changes.
- Preserve: every other public symbol, all serializers, all validators, the `_FAILED_DEGRADED_REASONS` set, all role frozensets and tuples.

`src/open_ems/core/__init__.py`
- Current state: re-exports core domain symbols and `StateStore` with a sorted `__all__`.
- Change expected: add `EnergyStrategy` to imports and `__all__` (sorted).
- Preserve: every existing entry; do not remove `StateStore`.

`src/open_ems/engine/models.py`
- Current state: defines `PeakContext`, `PositiveKw`, `NonNegativeKw`, `IntervalElapsedSeconds`, `_require_utc`, and frozen `EvaluationInput` with `inverter`/`battery`/`ev_charger`/`grid_meter`/`peak_context`, the `_required_slots_present` model validator, and `from_snapshot(cls, snapshot, *, peak_context)`.
- Change expected: add `strategy: EnergyStrategy` after `peak_context`; update `from_snapshot()` to take `strategy` as a keyword-only argument and forward it.
- Preserve: every existing field, validator, and the keyword-only `peak_context` argument shape on `from_snapshot()`. Keep `extra="forbid"` and `frozen=True`. Keep the `__future__` annotations import and Pydantic v2 idioms.

`src/open_ems/engine/operating_mode.py`
- Current state: pure `derive_recommended_operating_mode()` consuming `EvaluationInput`.
- Change expected: none.
- Preserve: do not modify the matrix or the function signature.

`src/open_ems/engine/rules/peak_limiting.py`
- Current state: `LoadReductionAction`, `PeakLimitDecision`, `_SUPPRESSED_MODES`, `evaluate_peak_limiting()`.
- Change expected: none.
- Preserve: do not edit, do not import its private `_SUPPRESSED_MODES` symbol from another module — duplicate the small set inside `energy_balancing.py` to keep public surface clean.

`src/open_ems/engine/__init__.py`
- Current state: exports `EvaluationInput`, `LoadReductionAction`, `PeakContext`, `PeakLimitDecision`, `derive_recommended_operating_mode`, `evaluate_peak_limiting`.
- Change expected: add `CandidateAction`, `EnergyStrategy`, `PriorityBand`, `StrategyEvaluation`, `evaluate_energy_strategy`, `resolve_conflicts`. Keep `__all__` sorted.

`src/open_ems/core/devices.py`
- Current state: `BatteryState`, `EVChargerState`, `GridMeterState`, `InverterState`, `DegradedDeviceState`, `DeviceRole`, `NonEmptyStr`, energy sign conventions.
- Change expected: none.
- Preserve: energy sign convention `grid_power_kw > 0 = import`. Do not add engine-specific behavior to core device models.

`src/open_ems/core/state_store.py`
- Current state: holds immutable `SystemSnapshot` snapshots; ownership boundary already established.
- Change expected: none.
- Preserve: pure-engine code must not touch the store.

### Implementation Guardrails

- Put new code under `src/open_ems/engine/rules/energy_balancing.py`. Tests under `tests/unit/engine/rules/test_energy_balancing.py`. The architecture's project-tree document calls this file out by exact name and intent: "FR11, FR11b: strategy-based priority resolution."
- The architecture's tree also names a future `engine/strategies/` directory for per-strategy parameter sets. This story does NOT create that directory; it inlines three private helpers in `energy_balancing.py`. Splitting to per-file modules is a future refactor when strategy logic grows beyond ~30 lines per strategy.
- Keep `engine.rules` pure: it may import from `open_ems.core`, `open_ems.engine.models`, `open_ems.engine.operating_mode`, plus standard library and Pydantic only. Forbidden imports for AC8: `open_ems.web`, `open_ems.storage`, `open_ems.adapters`, `open_ems.services`.
- Numeric weight constants (`10`, `15`, `20`, `30`, `80`) are explicit in this story to make AC3 tests provable. Do not parameterize them through configuration in v1.
- `priority_weight` semantics: `1 = strongest`, `100 = weakest`. This direction (lower = stronger) matches the conventional "priority 1" scheduling idiom and is aligned with how `_BAND_RANK` orders bands (lower rank = higher priority). Document this on the `CandidateAction` field docstring.
- Do not use raw dicts, tuples, or strings for rule outputs. Use the typed `StrategyEvaluation` and `CandidateAction` models.
- Do not return `None` to mean "no action." Return `StrategyEvaluation(..., strategy_candidates=())` with `suppressed_by_operating_mode` set when suppressed; otherwise `suppressed_by_operating_mode=None`.
- Do not call `random`, `time.time`, `datetime.now`, `uuid.uuid4`, or any environment access inside the rule code or its helpers. The conflict resolver and strategy evaluator must be 100% pure.
- Reuse `derive_recommended_operating_mode()`. Do not duplicate the degradation matrix. (Mirror Story 7.2 AC6 pattern.)
- The architecture's `enums.py` aspirational location for `Strategy` is not currently realized; existing convention puts `SystemOperatingMode` in `core/state.py`. Follow established convention — put `EnergyStrategy` in `core/state.py`. If `core/enums.py` is created later, both enums migrate together as a single dedicated refactor.
- The new module has zero async I/O, zero transport, zero adapters, zero new subsystems beyond pure typed models, and no cross-cutting adapter changes. Per the Epic 4 retro A1 gate, this story does NOT meet the "2+ complexity dimensions" trigger and therefore does not require an explicit error-path table in Dev Notes.
- The Epic 4 retro A2 adapter scaling counter is unaffected by this story (no adapter changes).

### Suggested Module Shape

```python
# src/open_ems/core/state.py  (additions only)
class EnergyStrategy(enum.StrEnum):
    """Homeowner-selected energy optimization strategy."""

    minimize_cost = "minimize_cost"
    maximize_self_consumption = "maximize_self_consumption"
    prioritize_ev = "prioritize_ev"
```

```python
# src/open_ems/engine/models.py  (only the EvaluationInput change)
class EvaluationInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    inverter: DeviceSlot
    battery: DeviceSlot
    ev_charger: DeviceSlot
    grid_meter: DeviceSlot
    peak_context: PeakContext
    strategy: EnergyStrategy

    @classmethod
    def from_snapshot(
        cls,
        snapshot: SystemSnapshot,
        *,
        peak_context: PeakContext,
        strategy: EnergyStrategy,
    ) -> EvaluationInput:
        return cls(
            inverter=snapshot.inverter,
            battery=snapshot.battery,
            ev_charger=snapshot.ev_charger,
            grid_meter=snapshot.grid_meter,
            peak_context=peak_context,
            strategy=strategy,
        )
```

```python
# src/open_ems/engine/rules/energy_balancing.py
class PriorityBand(enum.StrEnum):
    safety = "safety"
    optimization = "optimization"
    convenience = "convenience"


_BAND_RANK: Mapping[PriorityBand, int] = MappingProxyType({
    PriorityBand.safety: 0,
    PriorityBand.optimization: 1,
    PriorityBand.convenience: 2,
})


class CandidateAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: DeviceRole
    action_type: NonEmptyStr
    priority_band: PriorityBand
    priority_weight: int = Field(ge=1, le=100)  # 1 = strongest within band
    tiebreaker_key: NonEmptyStr
    source_rule: NonEmptyStr


class StrategyEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: EnergyStrategy
    strategy_candidates: tuple[CandidateAction, ...]
    suppressed_by_operating_mode: SystemOperatingMode | None = None


_SUPPRESSED_MODES = frozenset({
    SystemOperatingMode.conservative,
    SystemOperatingMode.fail_safe,
})


def evaluate_energy_strategy(evaluation_input: EvaluationInput) -> StrategyEvaluation:
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
    candidates = dispatch[evaluation_input.strategy](evaluation_input)
    return StrategyEvaluation(
        strategy=evaluation_input.strategy,
        strategy_candidates=candidates,
    )


def resolve_conflicts(
    candidates: Iterable[CandidateAction],
) -> dict[DeviceRole, CandidateAction]:
    ordered = sorted(
        candidates,
        key=lambda c: (
            _BAND_RANK[c.priority_band],
            c.priority_weight,
            c.tiebreaker_key,
            c.source_rule,
        ),
    )
    resolved: dict[DeviceRole, CandidateAction] = {}
    for candidate in ordered:
        resolved.setdefault(candidate.role, candidate)
    return resolved
```

This shape is a guide, not a requirement, but the final code must have equivalent type safety, equivalent purity, and exactly the AC3 numeric weights so that the strategy-distinctness tests pass.

### Previous Story Intelligence

Story 7.2 established the `engine/rules/` directory and the `EvaluationInput` extension pattern.

Actionable learnings carried forward:
- Reuse `derive_recommended_operating_mode()` for suppression. Do not duplicate the degradation matrix. (AC7 mirrors 7.2 AC6.)
- Extend `EvaluationInput` deliberately. The pattern: add a required field, update `from_snapshot()` keyword-only signature, update prior-story tests to supply a default. This is exactly the same shape Story 7.2 used to add `peak_context`.
- Keep `engine.rules` pure. The AST-based import-boundary test in `tests/unit/engine/rules/test_peak_limiting.py::test_rule_module_avoids_runtime_infrastructure_imports` is the canonical test pattern; copy it for `energy_balancing.py`.
- Numeric weight ordering and explicit `tiebreaker_key` strings are critical for AC4–AC6. Story 7.2's `LoadReductionAction` was an opaque enum and ordered by source-code ordering; this story formalizes ordering as data because conflict resolution requires explicit rule-independent comparability.

Story 7.2 review/deferred items relevant to 7.3:
- `current_monthly_recorded_peak_kw`, `current_interval_start`, `current_interval_elapsed_seconds` are still unused in the rule layer — Story 7.5 / Epic 8 may consume them later. This story does NOT need to consume them.
- `current_interval_elapsed_seconds=900` boundary differentiation is deferred to Story 7.5 / Epic 8.
- `_require_utc` private helper used exactly once — deferred. Do NOT promote it in this story unless a second UTC-validated datetime field is added (it is not).
- `LoadReductionAction` redundant explicit string values — deferred cosmetic fix; ignore in this story.
- `PeakLimitDecision.required_reduction_kw` already has `Field(ge=0.0)` from the 7.2 review patch — no action needed.

Story 7.1 review/deferred:
- `DegradedDeviceState.role` is not cross-validated against the positional `EvaluationInput` slot. Same constraint applies in 7.3. Do not expand this story to fix it; `from_snapshot` is the canonical construction path.

### Recent Git History

- `1d651bb 7.2` added `PeakContext`, extended `EvaluationInput`, created `engine/rules/peak_limiting.py`, added `LoadReductionAction`, `PeakLimitDecision`, `evaluate_peak_limiting`, AST import-boundary test.
- `6f96944 7.1` created `src/open_ems/engine/`, the initial frozen `EvaluationInput` and `derive_recommended_operating_mode()`, plus the engine test directory.
- `2ddc23f 6.3` and `98cbc7a 6.2` are observability/audit foundations; do NOT add audit or event-log calls in this story.
- `03ff345 5.3` and earlier Epic 5 work established `StateStore` and SSE/HTMX serialization. Do NOT touch web serializers, SSE event payloads, or the `system_status_update` event contract — homeowner serialization of `active_strategy` is a future story.

### Architecture Compliance

- Architecture project-tree (architecture.md lines 810–824) names `engine/rules/energy_balancing.py` for FR11/FR11b; this story creates that exact file.
- Architecture BAD-2 (lines 1080–1107) defines the conservative/fail_safe behavior per device. The strategy evaluator mirrors the engine-purity expectation: stop emitting load-increasing candidates under `conservative`; emit no candidates under `fail_safe`. Implementation reuses Story 7.1's matrix.
- Architecture's PolicyGuard pattern (lines 381–433) mandates that no `DeviceCommand` is created or dispatched outside `PolicyGuard.authorize_and_dispatch()`. This story produces NO `DeviceCommand`s — only typed `CandidateAction` records. Concrete commands are Epic 8's responsibility.
- FR11 names exactly the three strategies; the enum value strings (`minimize_cost`, `maximize_self_consumption`, `prioritize_ev`) match the SSE payload in `ux-design-specification.md` (line 1714) for forward compatibility with Epic 10/11 UI.
- FR40/FR41 (dynamic tariff and solar forecast) are explicitly nice-to-have; AC1 forbids dependency on them. Strategy evaluator uses only the local snapshot data and `EvaluationInput` fields.
- Energy sign conventions: `grid_power_kw > 0` is grid import; `pv_power_kw >= 0` is PV production only. Strategy logic respects these signs.
- All datetimes must be timezone-aware UTC (no naive datetimes anywhere). Strategy evaluation does not introduce new datetime fields, so this is preserved by reusing existing models.
- Decision-engine logic must be testable without physical devices, a database, a web app, or async runtime. AC8 + the AST import-boundary test enforce this.

### Testing Standards

- Mirror source structure: `src/open_ems/engine/rules/energy_balancing.py` ↔ `tests/unit/engine/rules/test_energy_balancing.py`.
- Use `datetime.UTC` and explicit fixed datetimes when datetimes are constructed.
- Reuse helper-constructor style from `tests/unit/engine/rules/test_peak_limiting.py` (`_inverter()`, `_battery()`, `_ev_charger()`, `_grid_meter()`, `_degraded()`, `_input_with()`).
- `_input_with()` in the new test module must accept a `strategy: EnergyStrategy = EnergyStrategy.minimize_cost` keyword default.
- All tests synchronous; no `pytest.mark.asyncio`; no async fixtures.
- Line length 100 chars; honor `pyproject.toml` Ruff/mypy settings.
- No DB/app/HTTP fixtures.
- For the conflict-resolver determinism test, use `random.Random(seed=0)` to shuffle so the test itself is reproducible.

### Latest Technical Information

Checked on 2026-05-05:
- The lockfile already resolves Pydantic `2.13.3`, pytest `9.0.3`, Ruff `0.15.12`, and mypy `1.20.2`. No new dependency needed for this story.
- Pydantic `2.13.3`'s `StrEnum` integration is stable — `EnergyStrategy(enum.StrEnum)` round-trips correctly through `model_dump()` and `model_validate()` without custom serializers.
- Python 3.12+ is enforced via `requires-python = ">=3.12"` in `pyproject.toml`. `enum.StrEnum` (3.11+) and PEP 695 type aliases are available.
- `MappingProxyType` from `types` is already imported and used in `core/state.py` for `OPERATING_MODE_GLOBAL_STATE` — reuse that import pattern for `_BAND_RANK`.
- No external API, no new third-party dependency.

### Project Structure Notes

Expected new files:
- `src/open_ems/engine/rules/energy_balancing.py`
- `tests/unit/engine/rules/test_energy_balancing.py`

Expected updated files:
- `src/open_ems/core/state.py` (add `EnergyStrategy`)
- `src/open_ems/core/__init__.py` (re-export `EnergyStrategy`)
- `src/open_ems/engine/models.py` (add `strategy` field; update `from_snapshot()`)
- `src/open_ems/engine/__init__.py` (re-export new public symbols)
- `tests/unit/engine/test_operating_mode.py` (supply default `strategy` in helpers)
- `tests/unit/engine/rules/test_peak_limiting.py` (supply default `strategy` in helpers)

Files to read and preserve before editing:
- `src/open_ems/core/state.py` — existing enums, derivation helpers, snapshot model, role frozensets
- `src/open_ems/core/devices.py` — `NonEmptyStr`, `BatteryState`, `EVChargerState`, `GridMeterState`, `InverterState`, `DeviceRole`, `DegradedDeviceState`
- `src/open_ems/engine/models.py` — `EvaluationInput`, `PeakContext`, `_required_slots_present`, `from_snapshot`
- `src/open_ems/engine/operating_mode.py` — `derive_recommended_operating_mode`
- `src/open_ems/engine/rules/peak_limiting.py` — pattern reference for `_SUPPRESSED_MODES`, suppression behavior, candidate ordering
- `tests/unit/engine/rules/test_peak_limiting.py` — helper-constructor patterns, AST import-boundary test pattern

Expected files NOT to modify:
- `src/open_ems/engine/rules/peak_limiting.py`
- `src/open_ems/web/**`
- `src/open_ems/storage/**`
- `src/open_ems/adapters/**`
- `src/open_ems/services/**`
- `src/open_ems/core/devices.py`
- `src/open_ems/core/state_store.py`
- `migrations/**`

### Anti-Patterns to Avoid

- Do NOT add `EnergyStrategy` to `src/open_ems/core/devices.py`. It belongs in `core/state.py` per established convention.
- Do NOT create a new `core/enums.py` for this story even though architecture.md aspirationally names it. Follow current convention; refactor later as a single dedicated change covering both `SystemOperatingMode` and `EnergyStrategy`.
- Do NOT import `_SUPPRESSED_MODES` from `peak_limiting.py`. Duplicate the three-line constant. Coupling private symbols across modules is a fragility cost; six lines of duplicated frozenset is cheaper.
- Do NOT pass strategy through `PeakContext`. `PeakContext` is the peak-rule input; `strategy` is a separate top-level `EvaluationInput` field.
- Do NOT introduce `BatteryIntent`, `EVChargerIntent`, `Intent` base class, or `DeviceIntent` typed contracts in this story. Story 7.4 owns those names.
- Do NOT modify `PeakLimitDecision`, `LoadReductionAction`, or `evaluate_peak_limiting()`. Story 7.5 will integrate them with `CandidateAction` later.
- Do NOT use `dataclass` — use Pydantic v2 `BaseModel` with `ConfigDict(frozen=True, extra="forbid")` to match existing engine and core convention.
- Do NOT use `@functools.lru_cache` or any caching on the rule functions. Inputs are frozen Pydantic models; hashing is supported but the cache adds non-deterministic memory pressure with no value for unit-cycle latency.
- Do NOT log from rule code. Logging belongs to Epic 8 runtime via `ObservabilityService`.
- Do NOT validate `priority_weight` against `priority_band` (e.g., "safety must have weight ≤ 10"). Bands and weights are independent dimensions; the existing `Field(ge=1, le=100)` constraint is the only weight check.

### References

- `_bmad-output/planning-artifacts/epics.md` — Epic 7 overview, Story 7.3 acceptance criteria, cross-story constraints (lines 1528–1693).
- `_bmad-output/planning-artifacts/prd.md` — FR7, FR8, FR9, FR10, FR11, FR11b, FR12, FR13, FR40 (nice-to-have), FR41 (nice-to-have), FR42, NFR-P1, NFR-R2, NFR-I3.
- `_bmad-output/planning-artifacts/architecture.md` — Project-tree pattern (line 819 names `energy_balancing.py`), BAD-2 Conservative Fallback Matrix (lines 1080–1107), PolicyGuard pattern (lines 381–433), Safety-Critical Patterns and energy sign conventions.
- `_bmad-output/planning-artifacts/ux-design-specification.md` — `system_status_update` SSE payload with `"active_strategy": "minimize_cost"` (line 1714); strategy selector UX (lines 1120–1133).
- `_bmad-output/implementation-artifacts/7-1-implement-systemoperatingmode-derivation-and-degradation-matrix.md` — previous-but-one story; `derive_recommended_operating_mode` contract.
- `_bmad-output/implementation-artifacts/7-2-implement-peak-limiting-logic-consuming-peakcontext.md` — previous story; `EvaluationInput` extension pattern, `_SUPPRESSED_MODES` pattern, AST import-boundary test pattern.
- `_bmad-output/implementation-artifacts/epic-4-retro-2026-05-03.md` — A1 error-path gate (this story does NOT trigger it), A2 adapter scaling counter (unaffected), P1 reason contract (unaffected).
- `src/open_ems/core/state.py` — `SystemOperatingMode`, `SystemSnapshot`, role frozensets, derivation helpers; `EnergyStrategy` will be added here.
- `src/open_ems/core/devices.py` — `NonEmptyStr`, `DeviceRole`, all device state models, energy sign conventions.
- `src/open_ems/engine/models.py` — `EvaluationInput`, `PeakContext`, `from_snapshot()` keyword-only pattern.
- `src/open_ems/engine/operating_mode.py` — `derive_recommended_operating_mode` (reuse, do not duplicate).
- `src/open_ems/engine/rules/peak_limiting.py` — `_SUPPRESSED_MODES` pattern, `evaluate_peak_limiting` reference, suppression behavior reference.
- `tests/unit/engine/rules/test_peak_limiting.py` — helper-constructor patterns, `_input_with`, AST import-boundary test.
- `pyproject.toml` and `uv.lock` — Python ≥ 3.12, Pydantic 2.13.3, pytest 9.0.3, Ruff 0.15.12, mypy 1.20.2.
- Pydantic `StrEnum` docs: https://pydantic.dev/docs/validation/latest/api/standard_library_types/#enum
- Python `enum.StrEnum` docs: https://docs.python.org/3.12/library/enum.html#enum.StrEnum

## Dev Agent Record

### Agent Model Used

Claude Opus 4.7

### Implementation Plan

- Add `EnergyStrategy` enum to `core/state.py` (next to `SystemOperatingMode`) and re-export from `core/__init__.py`.
- Extend `EvaluationInput` with required `strategy: EnergyStrategy` field; update `from_snapshot()` to require it as keyword-only (mirrors the 7.2 `peak_context` extension pattern).
- Update existing 7.1 (`test_operating_mode.py`) and 7.2 (`test_peak_limiting.py`) helpers to supply `EnergyStrategy.minimize_cost` so prior tests keep passing.
- Create `src/open_ems/engine/rules/energy_balancing.py` containing: `PriorityBand` enum + private `_BAND_RANK`, frozen `CandidateAction` model, frozen `StrategyEvaluation` model, `evaluate_energy_strategy()`, and `resolve_conflicts()`. Reuse `derive_recommended_operating_mode()` for conservative/fail_safe suppression (duplicate the small `_SUPPRESSED_MODES` frozenset rather than couple to peak_limiting's private symbol).
- Per-strategy candidate emission with hardcoded numeric weights chosen so AC3 distinctness holds: `minimize_cost` uses 20/80, `maximize_self_consumption` uses 10/15, `prioritize_ev` uses 10/30. Slot eligibility: skip battery candidates if not `BatteryState`; skip EV candidates if not `EVChargerState` with `session_active=True`.
- Deterministic conflict resolver: sort by `(_BAND_RANK[band], priority_weight, tiebreaker_key, source_rule)` ascending, then `dict.setdefault(role, candidate)` so first-after-sort wins per role. No `random`, no `time`, no `datetime.now`, no `uuid`.
- Re-export new public symbols from `engine/__init__.py` keeping `__all__` sorted.
- Tests (31 new) cover AC1-AC9: enum membership, required-strategy validation, three-way distinctness, per-strategy behavior under varying PV/grid/battery conditions, conservative/fail_safe suppression for all three strategies, slot eligibility for degraded/absent slots, all conflict-resolver tiebreaker layers, deterministic resolution across forward/reverse/shuffled input orderings, mutation safety, AST-based import-boundary, and frozen-model invariants.

### Debug Log References

- Baseline: `uv run python -m pytest tests/ --no-cov -q` — 637 passed, 43 warnings.
- Baseline: `uv run python -m ruff check .` — passed.
- Baseline: `uv run python -m ruff format --check .` — passed (137 files).
- Baseline: `uv run python -m mypy src/` — passed (66 files).
- After Tasks 1-2 (enum + EvaluationInput field, with 7.1/7.2 helpers updated): `uv run python -m pytest tests/unit/engine/ --no-cov -q` — 39 passed.
- After Tasks 1-5 (production code complete): `uv run python -m mypy src/` — passed (67 files); `uv run python -m ruff check .` — passed.
- After Task 6 (initial test file): `uv run python -m pytest tests/unit/engine/ --no-cov -q` — 70 passed; `uv run python -m ruff check .` — 3 E501 line-length errors in new test file.
- After E501 fixes + `ruff format`: all checks green.
- Final: `uv run python -m mypy src/` — passed (67 files).
- Final: `uv run python -m pytest tests/unit/engine/ --no-cov -q` — 70 passed in 0.42s.
- Final: `uv run python -m ruff check .` — passed.
- Final: `uv run python -m ruff format --check .` — passed (139 files).
- Final: `uv run python -m pytest tests/ --no-cov -q` — 668 passed, 43 warnings (= baseline 637 + 31 new).
- Review patch validation: `uv run python -m pytest tests/unit/engine/ --no-cov -q` — 72 passed.
- Review patch validation: `uv run python -m ruff check .` — passed.
- Review patch validation: `uv run python -m ruff format --check .` — passed (139 files).
- Review patch validation: `uv run python -m mypy src/` — passed (67 files).
- Review patch validation: `uv run python -m pytest tests/ --no-cov -q` — 670 passed, 43 warnings.

### Completion Notes List

- Added `EnergyStrategy` StrEnum in `core/state.py` (next to `SystemOperatingMode`) with the three FR11 values matching the SSE `active_strategy` payload contract.
- Extended `EvaluationInput` with required `strategy: EnergyStrategy`; `from_snapshot()` requires it as a keyword-only argument (consistent with the existing keyword-only `peak_context` parameter).
- Updated 7.1 and 7.2 test helpers to supply `EnergyStrategy.minimize_cost` so all 39 prior engine tests continue to pass unchanged. While doing so, fixed a latent test bug in `test_required_slots_reject_none` where `peak_context` was missing from the `slots` dict — the test was passing for the wrong reason; it now properly tests the None-slot rejection in the `_required_slots_present` validator.
- Created `engine/rules/energy_balancing.py` with `PriorityBand`, `CandidateAction`, `StrategyEvaluation`, `evaluate_energy_strategy()`, and `resolve_conflicts()`. Reused `derive_recommended_operating_mode()` for conservative/fail_safe suppression. Duplicated the three-line `_SUPPRESSED_MODES` frozenset rather than coupling to `peak_limiting.py`'s private symbol (per anti-pattern guidance in story Dev Notes).
- Per-strategy candidate weights are hardcoded as documented in story AC3 to make distinctness provable: minimize_cost (battery=20, ev=80), maximize_self_consumption (charge_from_pv=10, discharge=15), prioritize_ev (ev=10, battery=30).
- Conflict resolver uses pure `sorted()` + `dict.setdefault()` — fully deterministic across input orderings, no calls to `random`/`time`/`datetime.now`/`uuid`.
- Added 31 new unit tests covering AC1-AC9: enum membership, validation, strategy distinctness, per-strategy behavior, conservative/fail_safe suppression for all three strategies, slot eligibility, every conflict-resolver tiebreaker layer, determinism across forward/reverse/shuffled orderings (using `random.Random(seed=0)` for reproducibility), mutation safety, and AST-based import-boundary check.
- Code review patches resolved: added `action_type` as the final deterministic conflict-resolution tie-breaker, required positive PV before self-consumption emits `battery_charge_from_pv`, and added regression coverage for omitted `from_snapshot(strategy=...)`, fully tied resolver inputs, and zero-PV/negative-AC inverter state.
- Architecture boundaries preserved: `engine.rules.energy_balancing` imports only from `open_ems.core`, `open_ems.engine.models`, `open_ems.engine.operating_mode`, stdlib, and Pydantic. No `Command`, `DeviceCommand`, `PolicyGuard`, control loop, command dispatch, audit/event log entry, or migration created. `peak_limiting.py` and `PeakLimitDecision` are unmodified — Story 7.5 will integrate them with `CandidateAction` later.
- Full regression: 668 tests passed (baseline 637 + 31 new). Zero regressions, ruff/mypy/format all clean.
- Post-review full regression: 670 tests passed (baseline 637 + 33 new). Zero regressions, ruff/mypy/format all clean.

### File List

- `_bmad-output/implementation-artifacts/7-3-implement-energy-strategy-evaluation-and-deterministic-conflict-resolution.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`
- `src/open_ems/core/__init__.py`
- `src/open_ems/core/state.py`
- `src/open_ems/engine/__init__.py`
- `src/open_ems/engine/models.py`
- `src/open_ems/engine/rules/energy_balancing.py`
- `tests/unit/engine/rules/test_energy_balancing.py`
- `tests/unit/engine/rules/test_peak_limiting.py`
- `tests/unit/engine/test_operating_mode.py`

### Change Log

- 2026-05-05: Story created and marked ready for dev.
- 2026-05-05: Implemented `EnergyStrategy` enum, extended `EvaluationInput` with `strategy` field, added `engine/rules/energy_balancing.py` with `PriorityBand`/`CandidateAction`/`StrategyEvaluation`/`evaluate_energy_strategy`/`resolve_conflicts`, added 31 focused unit tests, updated 7.1 and 7.2 test helpers; full validation green (668 passed); story marked ready for review.
- 2026-05-05: Code review complete; 3 patch findings fixed; full validation green (670 passed); story marked done.
