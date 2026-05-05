# Story 7.5: Define EvaluationResult Contract with Decision Reasons and Cycle Timing

Status: review

<!-- Completion note: Ultimate context engine analysis completed - comprehensive developer guide created. -->

## Story

As a developer,
I want the decision engine to return a typed `EvaluationResult` with one resolved intent per device role, plain-language decision reasons per intent, and cycle timing,
so that the runtime loop in Epic 8 has an unambiguous, fully annotated output to act on, and the event log can explain every decision in human-readable form.

## Acceptance Criteria

**AC1 — EvaluationResult type contract**

**GIVEN** an evaluation cycle completes
**WHEN** the engine returns its output
**THEN** it returns a typed `EvaluationResult` containing:
- `cycle_id`: UUID (unique per evaluation cycle)
- `evaluated_at`: timezone-aware datetime (UTC)
- `recommended_operating_mode`: `SystemOperatingMode` enum value
- `intents`: ordered sequence of typed intent objects — one final resolved intent per device role (battery + EV charger)
- `decision_reasons`: sequence of reason strings, one per corresponding intent
- `cycle_duration_ms`: int (elapsed milliseconds for evaluation execution)

**AND** each intent is a typed object (`BatteryIntent`, `EVChargerIntent`) — not a raw dict or string
**AND** no `Command` object is produced by the decision engine — intent-to-command translation happens in Epic 8

---

**AC2 — Intent uniqueness per device role**

**GIVEN** multiple candidate intents per role may be considered internally during conflict resolution
**WHEN** the resolution completes
**THEN** only the final resolved intent per role is included in `EvaluationResult.intents`
**AND** the output contains no conflicting or redundant intents for the same device role

---

**AC3 — Decision reasons are specific, not generic**

**GIVEN** each intent in `EvaluationResult.intents` has been resolved
**WHEN** the result is constructed
**THEN** each intent has a corresponding entry in `decision_reasons` — a plain-language explanation specific enough to produce a meaningful installer-visible audit log summary
- Example: `"Battery discharge blocked: SoC 22% is at or below configured reserve floor of 25%"`
- Example: `"Battery charge approved by maximize_self_consumption strategy"`
- Example: `"EV charge suppressed: outside 22:00–06:00 window (Europe/Brussels) and no active session"`

**AND** `decision_reasons` entries must never be generic placeholders — they must reference specific values and constraints that drove the decision (units where applicable: kW, %, hours)

---

**AC4 — Cycle timing**

**GIVEN** the evaluation cycle measurement runs
**WHEN** the cycle completes
**THEN** `cycle_duration_ms` reflects the actual elapsed evaluation time
**AND** the runtime loop in Epic 8 is responsible for detecting slow cycles and triggering watchdog recovery — the decision engine reports timing in the result but does not self-recover

---

**AC5 — Peak-limit candidates integrated into conflict resolution**

**GIVEN** `evaluate_peak_limiting()` returns `action_required=True` with `candidate_actions`
**WHEN** the evaluation cycle assembles all candidates
**THEN** peak-limit candidates are promoted to `CandidateAction` objects with `PriorityBand.safety`
**AND** they are passed to `resolve_conflicts()` together with strategy candidates
**AND** safety-band candidates win over optimization and convenience candidates for the same device role

---

**AC6 — No Command production**

**GIVEN** an `EvaluationResult` is constructed
**WHEN** its intents are inspected
**THEN** it contains no `Command`, `DeviceCommand`, or `CommandResult` objects
**AND** all intents are abstract intent objects consumed only by Epic 8's `IntentExecutor`

---

**AC7 — Tests**

Unit tests verify:
- `test_evaluation_result_contains_all_required_fields()`: all required fields with correct types
- `test_intents_contains_at_most_one_per_role()`: battery and EV charger each appear at most once
- `test_each_intent_has_corresponding_reason()`: `len(intents) == len(decision_reasons)`, each reason is a non-empty str
- `test_decision_reasons_are_specific_not_generic()`: reasons include specific values (SoC %, kW, floor %, window times, strategy name)
- `test_cycle_duration_recorded_and_reflects_elapsed_time()`: `cycle_duration_ms` is a non-negative int
- `test_no_command_objects_in_result()`: EvaluationResult contains no Command-type objects
- `test_recommended_operating_mode_is_system_operating_mode_enum()`: valid SystemOperatingMode value
- `test_cycle_id_is_uuid()`: valid UUID4
- `test_evaluated_at_is_timezone_aware_utc()`: timezone-aware UTC datetime
- `test_peak_limit_candidate_wins_over_strategy_candidate()`: safety-band peak candidate resolves to battery discharge, overriding optimization-band strategy result
- `test_decision_reason_generation_for_all_reason_codes()`: every known reason_code on `BatteryIntent` and `EVChargerIntent` produces a specific, non-placeholder reason string
- `test_repeated_identical_inputs_produce_equal_results()`: deterministic — same `EvaluationInput` always returns identical `EvaluationResult` fields (excluding `cycle_id` and `cycle_duration_ms`)

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` and record the baseline pass count.
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Treat unrelated pre-existing failures as baseline notes; do not hide them by changing unrelated code.

- [x] Task 1: Define EvaluationResult model (AC: AC1, AC2, AC4, AC6, AC7)
  - [x] Create `src/open_ems/engine/result.py` — new module for the engine output contract.
  - [x] Add `from __future__ import annotations` for forward refs.
  - [x] Define `EvaluationResult` as a frozen Pydantic v2 model (`ConfigDict(frozen=True, extra="forbid")`).
  - [x] Fields:
    - `cycle_id: uuid.UUID`
    - `evaluated_at: datetime` (validated UTC, timezone-aware — reuse `_require_utc` from `models.py`)
    - `recommended_operating_mode: SystemOperatingMode`
    - `intents: tuple[BatteryIntent | EVChargerIntent, ...]`
    - `decision_reasons: tuple[str, ...]`
    - `cycle_duration_ms: int` (validated `>= 0`)
  - [x] Add `@model_validator(mode="after")` to verify `len(intents) == len(decision_reasons)`.
  - [x] Add a second validator to assert no two intents have the same device role (compare via `BatteryIntent → DeviceRole.battery`, `EVChargerIntent → DeviceRole.ev_charger`).
  - [x] Import `BatteryIntent` from `open_ems.engine.rules.battery_control` and `EVChargerIntent` from `open_ems.engine.rules.ev_scheduling`. Do NOT create a shared `Intent` base class.
  - [x] Keep this module free of stdlib `uuid` generation at import time — only type-hint `uuid.UUID`, do not generate UUIDs here.

- [x] Task 2: Implement reason-generation helpers (AC: AC3, AC7)
  - [x] Create private `_reason_for_battery_intent(intent: BatteryIntent, evaluation_input: EvaluationInput) -> str` in `engine/evaluator.py`.
  - [x] Implement a mapping from each `BatteryIntent.reason_code` to a format string that inserts specific values:
    - `"battery_unavailable"` → `"Battery device unavailable or degraded"`
    - `"battery_candidate_unavailable"` → `"No battery candidate from strategy or peak-limit evaluation"`
    - `"battery_charge_capability_missing"` → `f"Battery charge blocked: write capability 'set_charge_rate' missing for device {battery.device_id}"` (extract `battery.device_id` from `evaluation_input.battery` if it is a `BatteryState`, else use `"unknown"`)
    - `"battery_discharge_capability_missing"` → `f"Battery discharge blocked: write capability 'set_discharge_rate' missing for device {battery.device_id}"`
    - `"battery_reserve_floor_reached"` → `f"Battery discharge blocked: SoC {battery.soc_percent:.0f}% is at or below configured reserve floor of {intent.reserve_floor_percent:.0f}%"`
    - `"battery_charge_allowed"` → `f"Battery charge approved (strategy: {evaluation_input.strategy.value})"`
    - `"battery_discharge_allowed"` → `f"Battery discharge approved: SoC {battery.soc_percent:.0f}% exceeds reserve floor of {intent.reserve_floor_percent:.0f}%"`
  - [x] For unknown reason codes: return `f"Battery intent '{intent.action.value}': {intent.reason_code}"` as a safe fallback (not a generic placeholder — includes the actual code).
  - [x] Create private `_reason_for_ev_intent(intent: EVChargerIntent, evaluation_input: EvaluationInput) -> str` in `engine/evaluator.py`.
  - [x] Map each `EVChargerIntent.reason_code`:
    - `"ev_charger_unavailable"` → `"EV charger unavailable or degraded"`
    - `"ev_candidate_unavailable"` → `"No EV charger candidate from strategy or peak-limit evaluation"`
    - `"ev_charge_outside_window_no_session"` → derive window times from `evaluation_input.ev_scheduling.charging_window`; format: `f"EV hold: outside charging window {window.start_local_time.strftime('%H:%M')}–{window.end_local_time.strftime('%H:%M')} ({window.timezone_name}) and no active session"`; if window is None, use `"EV hold: no charging window configured and homeowner override is off"`
    - `"ev_stop_outside_window_session_active"` → same window format, append `"— stopping active session"`
    - `"peak_limit_blocks_ev_charge"` → `f"EV charge suppressed: peak projection {evaluation_input.peak_context.current_partial_window_projection_kw:.2f} kW at or above limit of {evaluation_input.peak_context.configured_peak_limit_kw:.2f} kW"`
    - `"peak_headroom_insufficient"` → `f"EV dynamic rate suppressed: peak headroom {(evaluation_input.peak_context.configured_peak_limit_kw - evaluation_input.peak_context.current_partial_window_projection_kw):.2f} kW is below target {evaluation_input.ev_scheduling.target_charge_rate_kw} kW"`
    - `"ev_charge_allowed"` → `f"EV charge approved (strategy: {evaluation_input.strategy.value}, override: {intent.homeowner_override_active})"`
    - `"ev_charge_allowed_override"` → `f"EV charge approved via homeowner override (window bypassed, strategy: {evaluation_input.strategy.value})"`
    - `"no_window_no_override"` → `"EV hold: no charging window configured and homeowner override is off"`
  - [x] For unknown EV reason codes: `f"EV charger intent '{intent.action.value}': {intent.reason_code}"`.
  - [x] **IMPORTANT**: Check actual `reason_code` values produced by `ev_scheduling.py` at implementation time — use `grep` to read the exact string literals in `src/open_ems/engine/rules/ev_scheduling.py` before writing this mapping. The reason_codes above are approximate from context; the exact strings must match the actual code.

- [x] Task 3: Implement evaluate_cycle() orchestrator (AC: AC1–AC6, AC7)
  - [x] Create `src/open_ems/engine/evaluator.py` — new module for the full-cycle orchestrator.
  - [x] Add `from __future__ import annotations` for forward refs.
  - [x] Import `time` (or `datetime`) for elapsed-time measurement using `time.perf_counter()` (monotonic, high-resolution — preferred over `datetime.now()` for duration measurement).
  - [x] Import `uuid` for `uuid.uuid4()`.
  - [x] Implement `evaluate_cycle(evaluation_input: EvaluationInput) -> EvaluationResult`:
    ```
    1. start_ns = time.perf_counter_ns()
    2. evaluated_at = datetime.now(UTC)   # one wall-clock read per cycle
    3. operating_mode = derive_recommended_operating_mode(evaluation_input)
    4. peak_decision = evaluate_peak_limiting(evaluation_input)
    5. strategy_eval = evaluate_energy_strategy(evaluation_input)
    6. peak_candidates = _peak_candidates_to_candidate_actions(peak_decision)
    7. all_candidates = list(peak_candidates) + list(strategy_eval.strategy_candidates)
    8. resolved: dict[DeviceRole, CandidateAction] = resolve_conflicts(all_candidates)
    9. battery_intent = evaluate_battery_control(evaluation_input, resolved.get(DeviceRole.battery))
    10. ev_intent = evaluate_ev_scheduling(evaluation_input, resolved.get(DeviceRole.ev_charger))
    11. intents = (battery_intent, ev_intent)
    12. reasons = (
            _reason_for_battery_intent(battery_intent, evaluation_input),
            _reason_for_ev_intent(ev_intent, evaluation_input),
        )
    13. elapsed_ns = time.perf_counter_ns() - start_ns
    14. cycle_duration_ms = elapsed_ns // 1_000_000
    15. return EvaluationResult(
            cycle_id=uuid.uuid4(),
            evaluated_at=evaluated_at,
            recommended_operating_mode=operating_mode,
            intents=intents,
            decision_reasons=reasons,
            cycle_duration_ms=int(cycle_duration_ms),
        )
    ```
  - [x] Implement `_peak_candidates_to_candidate_actions(peak_decision: PeakLimitDecision) -> list[CandidateAction]`:
    - If `not peak_decision.action_required`, return `[]`.
    - For each `action` in `peak_decision.candidate_actions`:
      - `LoadReductionAction.discharge_battery` → `CandidateAction(role=DeviceRole.battery, action_type="battery_discharge_to_avoid_import", priority_band=PriorityBand.safety, priority_weight=1, tiebreaker_key="peak_limit:discharge_battery", source_rule="peak_limiting")`
      - `LoadReductionAction.reduce_ev_charge_rate` → `CandidateAction(role=DeviceRole.ev_charger, action_type="reduce_ev_charge_rate", priority_band=PriorityBand.safety, priority_weight=1, tiebreaker_key="peak_limit:reduce_ev_charge_rate", source_rule="peak_limiting")`
    - Return the list.
  - [x] **IMPORTANT**: `"reduce_ev_charge_rate"` is a new `action_type` for `ev_scheduling.py`. When `evaluate_ev_scheduling()` receives a candidate with `action_type="reduce_ev_charge_rate"`, the current `_CHARGE_ACTIONS` frozenset does NOT include it, so it falls through to a hold intent. This is intentional for this story — the safety suppression of EV charging already happens inside `ev_scheduling.py` via the peak-projection check. The `reduce_ev_charge_rate` candidate entering `resolve_conflicts()` is documentation of intent, and the existing internal peak guard in `ev_scheduling.py` handles suppression. Do NOT modify `ev_scheduling.py` in this story to handle this new action type.
  - [x] **Import boundary**: `evaluator.py` may import only:
    - Python standard library (`time`, `uuid`, `datetime`, `datetime.UTC`)
    - Pydantic (if needed)
    - `open_ems.core` or `open_ems.core.devices` (for `DeviceRole`, `BatteryState`, `SystemOperatingMode`)
    - `open_ems.engine.models` (for `EvaluationInput`)
    - `open_ems.engine.result` (for `EvaluationResult`)
    - `open_ems.engine.operating_mode` (for `derive_recommended_operating_mode`)
    - `open_ems.engine.rules.peak_limiting` (for `evaluate_peak_limiting`, `PeakLimitDecision`, `LoadReductionAction`)
    - `open_ems.engine.rules.energy_balancing` (for `evaluate_energy_strategy`, `resolve_conflicts`, `CandidateAction`, `PriorityBand`)
    - `open_ems.engine.rules.battery_control` (for `evaluate_battery_control`, `BatteryIntent`)
    - `open_ems.engine.rules.ev_scheduling` (for `evaluate_ev_scheduling`, `EVChargerIntent`)
  - [x] Must NOT import from: `open_ems.web`, `open_ems.storage`, `open_ems.adapters`, `open_ems.services`, `StateStore`, `PolicyGuard`, `Command`, `DeviceCommand`, FastAPI request/session objects.
  - [x] Functions must be synchronous and pure (except for `time.perf_counter_ns()` and `uuid.uuid4()` side effects which are acceptable).

- [x] Task 4: Update public engine exports (AC: AC1, AC7)
  - [x] Update `src/open_ems/engine/__init__.py`:
    - Import `EvaluationResult` from `open_ems.engine.result`
    - Import `evaluate_cycle` from `open_ems.engine.evaluator`
    - Add both to `__all__`
  - [x] Update `src/open_ems/engine/rules/__init__.py` only if the project currently exports from it. Check first — from story 7.4, the rules __init__ may be empty or minimal.

- [x] Task 5: Add unit tests (AC: AC1–AC7)
  - [x] Create `tests/unit/engine/test_evaluator.py`.
  - [x] Mirror helper-constructor style from `test_battery_control.py` and `test_ev_scheduling.py`.
  - [x] Provide a `_full_input()` helper that returns a valid `EvaluationInput` with:
    - A healthy `BatteryState` with moderate SoC (e.g., 55%)
    - A healthy `EVChargerState` with `session_active=True`
    - A healthy `InverterState` with positive PV
    - A healthy `GridMeterState`
    - `PeakContext` with projection BELOW the limit (e.g., `3.8 kW` vs `5.0 kW` limit)
    - `EnergyStrategy.minimize_cost`
    - `BatteryControlContext(reserve_floor_percent=20.0, capability_profile=None)`
    - `EVSchedulingContext(homeowner_override_active=False, charging_window=None, evaluated_at=_NOW_UTC)`
  - [x] Test: `test_evaluation_result_contains_all_required_fields()` — call `evaluate_cycle(_full_input())` and assert all six fields exist and are of correct type.
  - [x] Test: `test_cycle_id_is_uuid()` — `isinstance(result.cycle_id, uuid.UUID)`.
  - [x] Test: `test_evaluated_at_is_timezone_aware_utc()` — `result.evaluated_at.tzinfo is not None` and `result.evaluated_at.utcoffset() == timedelta(0)`.
  - [x] Test: `test_recommended_operating_mode_is_system_operating_mode_enum()` — `isinstance(result.recommended_operating_mode, SystemOperatingMode)`.
  - [x] Test: `test_intents_contains_at_most_one_per_role()` — result has exactly one `BatteryIntent` and one `EVChargerIntent` in `intents`.
  - [x] Test: `test_each_intent_has_corresponding_reason()` — `len(result.intents) == len(result.decision_reasons)` and each reason is a non-empty `str`.
  - [x] Test: `test_cycle_duration_recorded_and_reflects_elapsed_time()` — `isinstance(result.cycle_duration_ms, int)` and `result.cycle_duration_ms >= 0`.
  - [x] Test: `test_no_command_objects_in_result()` — inspect `result.intents`; assert no element is an instance of any Command-like type.
  - [x] Test: `test_decision_reasons_are_specific_not_generic()`:
    - Use a `BatteryControlContext` with `reserve_floor_percent=25.0` and a `BatteryState` with `soc_percent=22.0`.
    - Assert the battery reason contains `"22"` (SoC), `"25"` (floor), and is not `""`.
    - Assert the EV reason is not `""` and not a single-word placeholder.
  - [x] Test: `test_peak_limit_candidate_wins_over_strategy_candidate()`:
    - Construct `EvaluationInput` with peak projection ABOVE the limit (e.g., `6.0 kW` vs `5.0 kW`).
    - Include a `BatteryState` with SoC above floor, and a `BatteryControlContext` with `capability_profile` that includes `WriteCapability.set_discharge_rate`.
    - Use `EnergyStrategy.minimize_cost` (which produces no battery candidate in normal conditions).
    - Call `evaluate_cycle()` and assert the `BatteryIntent.action == BatteryIntentAction.discharge`.
    - Assert the battery decision reason references discharge and NOT a generic hold.
  - [x] Test: `test_two_calls_with_same_input_produce_equal_resolved_fields()`:
    - Call `evaluate_cycle(_full_input())` twice.
    - Assert both results have equal `recommended_operating_mode`, `intents`, `decision_reasons`.
    - Assert `cycle_id` values are DIFFERENT (UUIDs are unique per call).
  - [x] Add an AST import-boundary test for `evaluator.py` mirroring the pattern in `test_peak_limiting.py`:
    - `test_evaluator_does_not_import_forbidden_modules()` — parse `engine/evaluator.py` with `ast`, assert no import of `web`, `storage`, `adapters`, `services`.
  - [x] Add an AST import-boundary test for `result.py` as well.

- [x] Task 6: Final validation (AC: all)
  - [x] Run `uv run python -m pytest tests/unit/engine/ --no-cov -q` — all tests pass.
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — full suite, no regressions.
  - [x] Leave this story in `review` only after all checked items are true.

## Dev Notes

### Critical Implementation Decision: EvaluationResult File Location

- **`EvaluationResult` lives in `src/open_ems/engine/result.py`** (new file). Do NOT add it to `models.py` — `models.py` is documented as "typed inputs" and `EvaluationResult` is an output type.
- **Orchestrator lives in `src/open_ems/engine/evaluator.py`** (new file). Do NOT put it in `rules/` — it is not a single rule but the full-cycle orchestrator.
- Tests live in `tests/unit/engine/test_evaluator.py` (not in `rules/`).
- Architecture file lists `engine/` as the home for engine logic but does not specify an exact file for the result type — use `result.py` for clean separation by convention.

### Critical Implementation Decision: PeakLimitDecision Integration

Story 7-3 deferred `PeakLimitDecision.candidate_actions` integration to Story 7-5. Here is the resolution:

**`_peak_candidates_to_candidate_actions()` maps:**
- `LoadReductionAction.discharge_battery` → `CandidateAction` with `action_type="battery_discharge_to_avoid_import"` and `PriorityBand.safety`. This is already in `battery_control._DISCHARGE_ACTIONS`, so no change to `battery_control.py` is needed.
- `LoadReductionAction.reduce_ev_charge_rate` → `CandidateAction` with `action_type="reduce_ev_charge_rate"` and `PriorityBand.safety`. This action_type is NOT in `ev_scheduling._CHARGE_ACTIONS`, so it falls through to `_hold()` inside `ev_scheduling.py`. This is INTENTIONAL — `ev_scheduling.py` already guards against peak overshoot internally via its peak-projection check (AC from Story 7-4: "Given the current peak projection is already at or above the configured peak limit, the EV scheduling returns no charge intent"). Do NOT add `"reduce_ev_charge_rate"` to `ev_scheduling._CHARGE_ACTIONS` in this story.

**IMPORTANT**: Do NOT modify `battery_control.py` or `ev_scheduling.py` in this story. They are complete as of Story 7-4.

### Critical: _reason_for_ev_intent() — Read ev_scheduling.py Exactly Before Writing

Before implementing the EV reason mapping in Task 2, **read `src/open_ems/engine/rules/ev_scheduling.py` completely** and grep for all `reason_code=` string literals. The reason_codes in this story document are derived from context (Story 7-4 dev notes) and may not match exactly. Use only the actual string values from the source code.

Known reason codes from `battery_control.py` (confirmed from source):
- `"battery_unavailable"` (line 51)
- `"battery_candidate_unavailable"` (lines 54, 82)
- `"battery_charge_capability_missing"` (line 57)
- `"battery_reserve_floor_reached"` (line 65)
- `"battery_discharge_capability_missing"` (line 72)
- `"battery_charge_allowed"` (line 60)
- `"battery_discharge_allowed"` (line 76)

Read `ev_scheduling.py` to extract EV reason codes before writing `_reason_for_ev_intent()`.

### Critical: _require_utc Re-use

`_require_utc` is private in `engine/models.py`. For `EvaluationResult.evaluated_at` validation, either:
a) Duplicate the one-liner inline in `result.py` (acceptable for one use), or
b) Promote `_require_utc` to a module-level export if this gives it a second legitimate use.

The deferred note from Story 7-2 says "_require_utc private helper used exactly once — inline or promote to shared utility when a second UTC datetime field is validated." Story 7-4 added `EVSchedulingContext.evaluated_at` as a second use. Now `EvaluationResult.evaluated_at` is a third. **Promote `_require_utc` to a public function `require_utc()` in `engine/models.py`** (or keep it private and call it from `result.py` via a direct import — but cross-module access to private helpers is a code smell). Recommended: promote to `require_utc()` in `engine/models.py` and update the two existing call sites.

Actually — re-check: do `BatteryControlContext`, `EVChargingWindow`, and `EVSchedulingContext` use `_require_utc`? Check `models.py` lines 103-105. If `_require_utc` is already used by `EVSchedulingContext.evaluated_at`, then Story 7-5's `EvaluationResult.evaluated_at` would be the third use and promotion is justified.

### EvaluationResult Model: intents Union Type

`intents: tuple[BatteryIntent | EVChargerIntent, ...]` — Pydantic v2 handles discriminated unions via `Annotated` with a discriminator if the models have a shared discriminator field. Since `BatteryIntent` and `EVChargerIntent` have no shared discriminator field, Pydantic v2 will use left-to-right union matching. This is fine for this story since we always construct intents explicitly (not deserializing from untrusted JSON). If mypy complains about the union, add a `Union` annotation or cast.

Alternative if Pydantic v2 union causes issues: use `tuple[Any, ...]` with a model validator that checks types explicitly. But prefer the typed union.

### Timing: time.perf_counter_ns() vs datetime

- **Use `time.perf_counter_ns()`** for elapsed-time measurement (monotonic, nanosecond precision, no wall-clock skew).
- **Use `datetime.now(UTC)`** for `evaluated_at` (wall-clock timestamp for event-log readability).
- `cycle_duration_ms = (time.perf_counter_ns() - start_ns) // 1_000_000` — integer division gives whole milliseconds.
- The `from datetime import UTC` constant is available from Python 3.11+. The project uses Python ≥3.12 (see `pyproject.toml`). Safe to use.

### uuid.UUID and Pydantic v2

Pydantic v2 natively supports `uuid.UUID` field types without a custom validator. `uuid.uuid4()` returns a `uuid.UUID` object. No special annotation needed.

### Architecture Compliance

- **Decision engine produces NO commands**: `EvaluationResult.intents` contains abstract intent objects only. `BatteryIntent` and `EVChargerIntent` are pure data — no adapter calls, no database writes, no web/session access.
- **PolicyGuard (Epic 8)**: Every `BatteryIntent` or `EVChargerIntent` that results in a device action must pass through `PolicyGuard.authorize_and_dispatch()` in Epic 8 before reaching any adapter. Story 7-5 must not implement or invoke PolicyGuard.
- **StateStore**: `evaluate_cycle()` must not access `StateStore` directly. The caller (Epic 8's control loop) assembles `EvaluationInput.from_snapshot()` and passes it in.
- **Import boundary**: `evaluator.py` and `result.py` must not import from `open_ems.web`, `open_ems.storage`, `open_ems.adapters`, or `open_ems.services`. Verify with the AST pattern from Story 7-2's `test_peak_limiting.py`.
- **Immutability**: `EvaluationResult` is frozen (`ConfigDict(frozen=True, extra="forbid")`). Every field is a value type or another frozen model.
- **No `Command` objects**: The `EvaluationResult` must not contain or reference `Command`, `DeviceCommand`, or `CommandResult` from `open_ems.core.commands`. This is enforced by import boundary — `evaluator.py` must not import from `open_ems.core.commands`.

### Source File Locations (All Current)

| Symbol | Source File | Notes |
|--------|-------------|-------|
| `EvaluationInput` | `src/open_ems/engine/models.py` | input contract, frozen |
| `PeakContext` | `src/open_ems/engine/models.py` | peak signal |
| `BatteryControlContext` | `src/open_ems/engine/models.py` | battery context |
| `EVSchedulingContext` | `src/open_ems/engine/models.py` | EV context |
| `EVChargingWindow` | `src/open_ems/engine/models.py` | window config |
| `derive_recommended_operating_mode` | `src/open_ems/engine/operating_mode.py` | Story 7.1 |
| `PeakLimitDecision`, `LoadReductionAction`, `evaluate_peak_limiting` | `src/open_ems/engine/rules/peak_limiting.py` | Story 7.2 |
| `CandidateAction`, `PriorityBand`, `StrategyEvaluation`, `evaluate_energy_strategy`, `resolve_conflicts` | `src/open_ems/engine/rules/energy_balancing.py` | Story 7.3 |
| `BatteryIntent`, `BatteryIntentAction`, `evaluate_battery_control` | `src/open_ems/engine/rules/battery_control.py` | Story 7.4 |
| `EVChargerIntent`, `EVChargerIntentAction`, `evaluate_ev_scheduling` | `src/open_ems/engine/rules/ev_scheduling.py` | Story 7.4 |
| `SystemOperatingMode` | `src/open_ems/core/state.py` | also exported via `open_ems.core` |
| `DeviceRole` | `src/open_ems/core/devices.py` | also exported via `open_ems.core` |
| `BatteryState` | `src/open_ems/core/devices.py` | also exported via `open_ems.core` |
| `EVChargerState` | `src/open_ems/core/devices.py` | also exported via `open_ems.core` |

**New files to create:**
| Symbol | New File | Notes |
|--------|----------|-------|
| `EvaluationResult` | `src/open_ems/engine/result.py` | output contract |
| `evaluate_cycle` | `src/open_ems/engine/evaluator.py` | full-cycle orchestrator |
| Tests | `tests/unit/engine/test_evaluator.py` | unit tests for AC7 |

### Previous Story Intelligence (Story 7.4)

- Story 7-4 explicitly prohibited creating `EvaluationResult` — that ownership is confirmed for Story 7-5.
- `BatteryIntent.reason_code` is a `NonEmptyStr`, not an enum — so the reason-generation function uses `str` matching (not enum dispatch). Handle unknown reason codes gracefully.
- `EVChargerIntent.reason_code` is similarly a `NonEmptyStr`. Read `ev_scheduling.py` completely before writing the reason-generation mapping.
- Story 7-4 review patches: `battery_support_ev` now has a `ev_charger.session_active` guard in `energy_balancing.py` (already applied). No issue for Story 7-5.
- Deferred from 7-4: `"Action type strings are untyped literals with no central registry"` — do not fix this in Story 7-5. Do not create a registry or ActionType enum.

### Deferred Items NOT for This Story

- **SOC upper-bound**: No charge ceiling on battery — BMS responsibility. Don't add it.
- **DST fold handling** in `ev_scheduling.py` — do not touch.
- **`current_interval_elapsed_seconds=900` boundary**: Not handled in Story 7-5. `cycle_duration_ms` is the evaluation timing; the 900s boundary is Epic 8 territory.
- **`resolve_conflicts` sort direction / tiebreaker**: Pre-existing design. Do not change.
- **`DegradedDeviceState.role` cross-validation**: Do not broaden.

### Recent Git Intelligence

- `4ed727e (HEAD)` — 7.4: added `BatteryControlContext`, `EVSchedulingContext`, `EVChargingWindow` in `models.py`; created `battery_control.py` and `ev_scheduling.py`; expanded `engine/__init__.py` to export all new symbols; updated all prior engine tests with deterministic default contexts.
- `1d651bb` — 7.2: established AST import-boundary test pattern in `test_peak_limiting.py`. Mirror this in `test_evaluator.py`.
- The project uses `uv` as its package manager. Run `uv run python -m pytest ...`, not `python -m pytest ...`.

### Test Helper Pattern

Follow the factory-function pattern established in `test_battery_control.py`:
```python
_NOW_UTC = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_DEFAULT_SLOT = object()  # sentinel

def _peak_context(*, projection_kw: float = 3.8, limit_kw: float = 5.0) -> PeakContext: ...
def _battery(*, soc_percent: float = 55.0) -> BatteryState: ...
def _full_input(**overrides) -> EvaluationInput: ...
```

Use keyword-only args for test-variant parameters. For the peak-limit-overrides test, construct a variant `_full_input()` with `peak_context=_peak_context(projection_kw=6.0, limit_kw=5.0)` and battery capability profile.

### AST Import Boundary Test Pattern

From `test_peak_limiting.py` (Story 7.2):
```python
def test_peak_limiting_does_not_import_forbidden_modules() -> None:
    import ast
    import pathlib
    src = pathlib.Path("src/open_ems/engine/rules/peak_limiting.py").read_text()
    tree = ast.parse(src)
    forbidden = {"web", "storage", "adapters", "services"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            for f in forbidden:
                assert f not in module, f"peak_limiting.py must not import from {f}"
```

Apply the same pattern for `evaluator.py` and `result.py`.

## Dev Agent Record

### Agent Model Used

Claude Sonnet 4.6 (GitHub Copilot)

### Debug Log References

- 2026-05-05: Baseline `uv run python -m pytest tests/ --no-cov -q` passed: 717 passed, 43 warnings.
- 2026-05-05: Baseline `uv run python -m ruff check .` passed.
- 2026-05-05: Baseline `uv run python -m ruff format --check .` passed: 144 files already formatted.
- 2026-05-05: Baseline `uv run python -m mypy src/` passed: no issues in 69 source files.
- 2026-05-05: Task 1 red test failed as expected: missing `open_ems.engine.result`.
- 2026-05-05: Task 1 focused tests passed: 2 passed.
- 2026-05-05: Task 1 full validation passed: 719 passed, 43 warnings; Ruff check passed; Ruff format check passed; mypy passed.
- 2026-05-05: Task 2 red test failed as expected: missing `open_ems.engine.evaluator`.
- 2026-05-05: Confirmed actual EV reason codes from `ev_scheduling.py`: `ev_charger_unavailable`, `ev_candidate_unavailable`, `ev_charging_window_inactive`, `peak_limit_blocks_ev_charge`, `peak_headroom_insufficient`, `ev_charge_allowed`.
- 2026-05-05: Task 2 focused tests passed: 4 passed.
- 2026-05-05: Task 2 full validation passed: 721 passed, 43 warnings; Ruff check passed; Ruff format check passed; mypy passed.
- 2026-05-05: Task 3 red test failed as expected: `evaluate_cycle` import missing.
- 2026-05-05: Task 3 focused tests passed: 15 passed.
- 2026-05-05: Task 3 full validation passed: 732 passed, 43 warnings; Ruff check passed; Ruff format check passed; mypy passed.
- 2026-05-05: Task 4 red test failed as expected: `open_ems.engine.EvaluationResult` missing.
- 2026-05-05: Task 4 focused tests passed: 16 passed.
- 2026-05-05: Task 4 full validation passed: 733 passed, 43 warnings; Ruff check passed after import-order fix; Ruff format check passed; mypy passed.
- 2026-05-05: Task 5 expanded evaluator tests passed: 19 passed.
- 2026-05-05: Task 5 full validation passed: 736 passed, 43 warnings; Ruff check passed after AST helper fix; Ruff format check passed; mypy passed.
- 2026-05-05: Task 6 final validation passed: `tests/unit/engine/` 138 passed; Ruff check passed; Ruff format check passed; mypy passed; full suite 736 passed, 43 warnings.
- 2026-05-05: Completion-gate full regression passed: 736 passed, 43 warnings.

### Completion Notes List

- Completed pre-story quality gate with no baseline failures.
- Added frozen `EvaluationResult` output contract with UTC validation, non-negative cycle timing, reason-count validation, and per-role intent uniqueness.
- Promoted engine UTC validation helper to `require_utc()` and updated existing engine input validators to reuse it.
- Added battery and EV decision reason helpers with source-value-specific messages and safe unknown-code fallbacks.
- Used actual `ev_scheduling.py` reason code `ev_charging_window_inactive` for both no-window and outside-window cases, with message text differentiated by intent action and configured window.
- Implemented `evaluate_cycle()` orchestration with operating-mode derivation, peak-limit and strategy candidate merging, deterministic conflict resolution, typed intent emission, decision reasons, UUID cycle IDs, UTC evaluation timestamps, and measured cycle duration.
- Promoted peak-limit load-reduction candidates into safety-band `CandidateAction` objects without modifying EV scheduling action handling.
- Exported `EvaluationResult` and `evaluate_cycle` from `open_ems.engine`; checked `rules/__init__.py` and left it unchanged because no new rule-level symbol was added.
- Completed evaluator unit coverage for AC1-AC7, including result field types, UUID/timestamp validation, per-role uniqueness, reason pairing and specificity, no command-like objects, safety-band peak conflict resolution, deterministic resolved fields, and AST import boundaries.
- Completed final validation with no regressions.
- Story completion gates passed; status set to review.

### File List

- _bmad-output/implementation-artifacts/7-5-define-evaluationresult-contract-with-decision-reasons-and-cycle-timing.md
- _bmad-output/implementation-artifacts/sprint-status.yaml
- src/open_ems/engine/evaluator.py
- src/open_ems/engine/__init__.py
- src/open_ems/engine/models.py
- src/open_ems/engine/result.py
- tests/unit/engine/test_evaluator.py

### Change Log

- 2026-05-05: Implemented EvaluationResult contract, full-cycle evaluator orchestration, decision reason generation, public engine exports, and AC1-AC7 evaluator tests.

### Review Findings

- [x] [Review][Decision] D1 — `require_utc` now public: resolved — keep as-is; promotion was intentional and documented in Dev Notes. [src/open_ems/engine/models.py, src/open_ems/engine/result.py]
- [x] [Review][Patch] P1 — `_role_for_intent` silently returns `ev_charger` for unknown intent types — should raise `TypeError` [src/open_ems/engine/result.py:_role_for_intent]
- [x] [Review][Patch] P2 — `decision_reasons` validator does not reject empty or whitespace-only strings (AC3 requires non-placeholder reasons) [src/open_ems/engine/result.py:EvaluationResult]
- [x] [Review][Patch] P3 — Unknown `LoadReductionAction` variants silently dropped in `_peak_candidates_to_candidate_actions` — should raise `ValueError` [src/open_ems/engine/evaluator.py:_peak_candidates_to_candidate_actions]
- [x] [Review][Patch] P4 — `battery_unavailable` / `ev_charger_unavailable` reason strings contain no device-specific value, violating AC3 — include device_id [src/open_ems/engine/evaluator.py:_reason_for_battery_intent, _reason_for_ev_intent]
- [x] [Review][Patch] P5 — `battery_candidate_unavailable` / `ev_candidate_unavailable` strings missing strategy context, violating AC3 — include `evaluation_input.strategy.value` [src/open_ems/engine/evaluator.py:_reason_for_battery_intent, _reason_for_ev_intent]
- [x] [Review][Patch] P6 — `EvaluationResult(intents=(), decision_reasons=())` passes all validators; spec requires at least one intent per device role — add non-empty validator [src/open_ems/engine/result.py:EvaluationResult]
- [x] [Review][Defer] D-a — `frozen=True` shallow immutability: `BatteryIntent`/`EVChargerIntent` are not frozen; tuple contents can be mutated — pre-existing design [src/open_ems/engine/result.py] — deferred, pre-existing
- [x] [Review][Defer] D-b — `isinstance(battery, BatteryState)` guard implies `EvaluationInput.battery` may be non-`BatteryState`; pre-existing type ambiguity in `EvaluationInput` — deferred, pre-existing [src/open_ems/engine/evaluator.py:_reason_for_battery_intent] — deferred, pre-existing
- [x] [Review][Defer] D-c — `cycle_id` not validated as v4 at model construction; spec says "UUID", not "UUID4"; design choice — deferred, pre-existing [src/open_ems/engine/result.py] — deferred, pre-existing
- [x] [Review][Defer] D-d — Future `EVChargerIntentAction` variants in `_ev_window_inactive_reason` silently labelled "hold"; low-risk for current scope — deferred, pre-existing [src/open_ems/engine/evaluator.py:_ev_window_inactive_reason] — deferred, pre-existing
