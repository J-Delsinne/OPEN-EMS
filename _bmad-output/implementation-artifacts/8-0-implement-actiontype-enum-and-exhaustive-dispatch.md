# Story 8.0: Implement ActionType Enum and Exhaustive Dispatch

Status: done

## Story

As a developer building Epic 8's IntentExecutor and command dispatch pipeline,
I want all action type values in the decision engine to be typed, exhaustive enum members rather than raw strings,
so that any invalid or unrecognized action type fails immediately with an exception rather than silently producing a hold intent at runtime.

## Acceptance Criteria

**AC1 — ActionType enum defined**

**GIVEN** the decision engine produces `CandidateAction` objects with typed action types
**WHEN** `ActionType` is defined in `energy_balancing.py`
**THEN** it is a `StrEnum` with exactly these six members:
- `battery_charge_from_pv`
- `battery_discharge_to_avoid_import`
- `battery_support_ev`
- `permit_ev_charge`
- `ev_charge`
- `reduce_ev_charge_rate`

**AND** `CandidateAction.action_type` is typed `ActionType`, replacing the previous `NonEmptyStr` field
**AND** `ActionType` is exported from `src/open_ems/engine/__init__.py` in `__all__`

---

**AC2 — Battery dispatch is exhaustive over ActionType**

**GIVEN** `evaluate_battery_control()` receives a resolved battery candidate
**WHEN** the candidate's `action_type` is inspected
**THEN** the dispatch covers:
- `ActionType.battery_charge_from_pv` → charge path
- `ActionType.battery_discharge_to_avoid_import` → discharge path
- `ActionType.battery_support_ev` → discharge path

**AND** any other `ActionType` value raises `ValueError` immediately — does not silently produce a hold
**AND** the `_CHARGE_ACTIONS` and `_DISCHARGE_ACTIONS` frozensets are removed

---

**AC3 — EV dispatch is exhaustive with documented reduce_ev_charge_rate hold**

**GIVEN** `evaluate_ev_scheduling()` receives a resolved EV charger candidate
**WHEN** the candidate's `action_type` is inspected
**THEN** the dispatch covers:
- `ActionType.permit_ev_charge` → proceed to EV charging logic
- `ActionType.ev_charge` → proceed to EV charging logic
- `ActionType.reduce_ev_charge_rate` → explicit hold with `reason_code="ev_candidate_unavailable"` (intentional fall-through; peak suppression already handled internally)

**AND** any other `ActionType` value raises `ValueError` immediately
**AND** the `_CHARGE_ACTIONS` frozenset is removed
**AND** the `reduce_ev_charge_rate` → hold case is annotated with a comment identifying it as intentional

---

**AC4 — EnergyStrategy dispatch raises on unknown strategy**

**GIVEN** `evaluate_energy_strategy()` dispatches on `evaluation_input.strategy`
**WHEN** a strategy value is not in the dispatch table
**THEN** `ValueError` is raised immediately — does not return an empty `StrategyEvaluation` silently

---

**AC5 — evaluator.py string literals replaced with ActionType members**

**GIVEN** `_peak_candidates_to_candidate_actions()` constructs `CandidateAction` objects
**WHEN** the action type is set
**THEN** `ActionType.battery_discharge_to_avoid_import` and `ActionType.reduce_ev_charge_rate` are used — no string literals

---

**AC6 — Exhaustive dispatch tests**

Unit tests verify that every `ActionType` member is handled deterministically by battery and EV dispatch:
- Iterating all `ActionType` members against `evaluate_battery_control()` and `evaluate_ev_scheduling()` produces no unhandled exceptions and no silent fall-throughs — each member produces the documented behavior
- `evaluate_energy_strategy()` handles all `EnergyStrategy` members without raising
- Any hypothetical unknown `ActionType` reaching battery or EV dispatch raises `ValueError`

---

**AC7 — All existing tests pass; type checks clean**

- Regression: `uv run python -m pytest tests/ --no-cov -q` — all 736+ tests pass, no new failures
- `uv run python -m mypy src/` — no new type errors
- `uv run python -m ruff check .` — no new linting errors
- `uv run python -m ruff format --check .` — format clean

---

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — record the exact passing count as baseline.
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Note any pre-existing failures; do not hide them by touching unrelated code.

- [x] Task 1: Define ActionType enum and update CandidateAction (AC: AC1)
  - [x] Open `src/open_ems/engine/rules/energy_balancing.py`.
  - [x] Add `ActionType` as a new `StrEnum` class immediately before `PriorityBand` (around line 25). Use `enum.StrEnum` — the module already imports `enum`. Members (in this order):
    - `battery_charge_from_pv = "battery_charge_from_pv"`
    - `battery_discharge_to_avoid_import = "battery_discharge_to_avoid_import"`
    - `battery_support_ev = "battery_support_ev"`
    - `permit_ev_charge = "permit_ev_charge"`
    - `ev_charge = "ev_charge"`
    - `reduce_ev_charge_rate = "reduce_ev_charge_rate"`
  - [x] In `CandidateAction` (line 42), change `action_type: NonEmptyStr` → `action_type: ActionType`.
  - [x] Remove the `from open_ems.core.devices import NonEmptyStr` import line **only if** no other field in `energy_balancing.py` uses `NonEmptyStr`. Check: `tiebreaker_key: NonEmptyStr` and `source_rule: NonEmptyStr` still use it — keep the import.
  - [x] Replace all six string literal `action_type=` values in `_evaluate_minimize_cost`, `_evaluate_maximize_self_consumption`, and `_evaluate_prioritize_ev` with their `ActionType` member equivalents.
  - [x] Fix `EnergyStrategy` dispatch (AC4): replace the `handler is None` silent-return path with a raise.
  - [x] Verify `resolve_conflicts`'s sort key still works: `c.action_type` is now an `ActionType` (StrEnum), which is still a `str` — sort order is preserved. No change needed to `resolve_conflicts`.

- [x] Task 2: Update battery_control.py — exhaustive enum dispatch (AC: AC2)
  - [x] Open `src/open_ems/engine/rules/battery_control.py`.
  - [x] Add `ActionType` to the import from `energy_balancing`.
  - [x] Remove `_CHARGE_ACTIONS` and `_DISCHARGE_ACTIONS` frozensets entirely.
  - [x] Replace the two frozenset membership checks with exhaustive enum dispatch with `else: raise ValueError(...)`.
  - [x] The silent fallthrough hold is replaced by the `else: raise` branch. Function no longer has an unreachable fallthrough.
  - [x] Verify that `NonEmptyStr` is still imported (for `BatteryIntent.reason_code: NonEmptyStr`). Keep the import.

- [x] Task 3: Update ev_scheduling.py — exhaustive enum dispatch with documented hold (AC: AC3)
  - [x] Open `src/open_ems/engine/rules/ev_scheduling.py`.
  - [x] Add `ActionType` to the import from `energy_balancing`.
  - [x] Remove the `_CHARGE_ACTIONS` frozenset entirely.
  - [x] Replace the compound early-return guard with two-part check: role guard then explicit ActionType dispatch.
  - [x] `reduce_ev_charge_rate` → hold with documented comment. Other unhandled types → raise ValueError.
  - [x] The rest of the function body (peak-projection check, window check, target rate check, charge return) is unchanged.
  - [x] Verify that `NonEmptyStr` is still imported. Keep the import.

- [x] Task 4: Update evaluator.py — replace string literals with ActionType (AC: AC5)
  - [x] Open `src/open_ems/engine/evaluator.py`.
  - [x] Add `ActionType` to the import from `energy_balancing`.
  - [x] In `_peak_candidates_to_candidate_actions()`, replace string literals with ActionType members.

- [x] Task 5: Export ActionType from engine __init__.py (AC: AC1)
  - [x] Open `src/open_ems/engine/__init__.py`.
  - [x] Add `ActionType` to the import from `energy_balancing`.
  - [x] Add `"ActionType"` to `__all__` in alphabetical position (before `"BatteryControlContext"`).

- [x] Task 6: Add / update tests (AC: AC6, AC7)
  - [x] Open `tests/unit/engine/rules/test_energy_balancing.py`.
  - [x] Add `ActionType` to the import from `energy_balancing`.
  - [x] Add `test_candidate_action_requires_actiontype_for_action_type_field()`.
  - [x] Add `test_all_actiontype_members_have_expected_count()`.
  - [x] Add `test_evaluate_energy_strategy_handles_all_known_strategies()`.
  - [x] Fix `test_smaller_action_type_wins_when_priority_metadata_is_identical` — invalid string literals replaced with ActionType members.
  - [x] Fix `test_resolve_conflicts_groups_per_role` and `test_resolve_conflicts_is_deterministic_across_input_ordering`.

  - [x] Open `tests/unit/engine/rules/test_battery_control.py`.
  - [x] Add `ActionType` to imports.
  - [x] Update all existing tests to use `ActionType` enum members instead of string literals.
  - [x] Removed the `"unknown_battery_action"` parametrize case (now raises ValueError not hold).
  - [x] Add `test_battery_dispatch_exhaustive_all_battery_actiontypes()`.
  - [x] Add `test_battery_dispatch_raises_on_ev_action_type()`.

  - [x] Open `tests/unit/engine/rules/test_ev_scheduling.py`.
  - [x] Add `ActionType` to imports.
  - [x] Update all existing tests to use `ActionType` enum members instead of string literals.
  - [x] Removed the `"unknown_ev_action"` parametrize case (now raises ValueError not hold).
  - [x] Add `test_ev_dispatch_reduce_ev_charge_rate_produces_hold()`.
  - [x] Add `test_ev_dispatch_raises_on_battery_action_type()`.

  - [x] Open `tests/unit/engine/test_evaluator.py`.
  - [x] Add `ActionType` to imports.
  - [x] Update `_candidate`, `_ev_intent`, `_battery_intent_with`, `_ev_intent_with` to use ActionType members.

- [x] Task 7: Final validation (AC: all)
  - [x] Run `uv run python -m pytest tests/unit/engine/ --no-cov -q` — all engine tests pass.
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — full suite: 743 passed (baseline 738, +5 new tests).

---

## File List

- `src/open_ems/engine/rules/energy_balancing.py` — Added `ActionType` StrEnum; changed `CandidateAction.action_type` from `NonEmptyStr` to `ActionType`; replaced 6 string literals with enum members; EnergyStrategy dispatch now raises on unknown strategy
- `src/open_ems/engine/rules/battery_control.py` — Removed `_CHARGE_ACTIONS` / `_DISCHARGE_ACTIONS` frozensets; replaced frozenset membership checks with exhaustive `if/elif/else: raise` dispatch; imported `ActionType`
- `src/open_ems/engine/rules/ev_scheduling.py` — Removed `_CHARGE_ACTIONS` frozenset; replaced compound guard with role check + explicit ActionType dispatch (documented `reduce_ev_charge_rate` hold, `ValueError` on unknown); imported `ActionType`
- `src/open_ems/engine/evaluator.py` — Imported `ActionType`; replaced 2 string literals in `_peak_candidates_to_candidate_actions()`
- `src/open_ems/engine/__init__.py` — Added `ActionType` to import and `__all__`
- `tests/unit/engine/rules/test_energy_balancing.py` — Added `ActionType` import; updated `_candidate` helper; fixed tests using invalid string literals; added 3 new tests
- `tests/unit/engine/rules/test_battery_control.py` — Added `ActionType` import; updated all string literals to enum members; removed invalid parametrize case; added 2 new exhaustive dispatch tests
- `tests/unit/engine/rules/test_ev_scheduling.py` — Added `ActionType` import; updated all string literals to enum members; removed invalid parametrize case; added 2 new exhaustive dispatch tests
- `tests/unit/engine/test_evaluator.py` — Added `ActionType` import; updated `_candidate`, `_ev_intent`, `_battery_intent_with`, `_ev_intent_with` helpers

## Dev Agent Record

### Completion Notes

Baseline: 738 tests passing, ruff clean, mypy clean.

Implemented all 7 tasks in one session:

- `ActionType` StrEnum (6 members) defined in `energy_balancing.py` before `PriorityBand`. `CandidateAction.action_type` changed from `NonEmptyStr` to `ActionType`. All 6 string literals in strategy evaluation functions replaced. `EnergyStrategy` dispatch now raises `ValueError` on unknown strategy instead of silently returning empty `StrategyEvaluation`.
- `battery_control.py`: `_CHARGE_ACTIONS` / `_DISCHARGE_ACTIONS` frozensets removed. Dispatch rewritten as `if/elif (charge)/elif (discharge)/else: raise ValueError(...)`. No more silent fallthrough hold for unrecognised action types.
- `ev_scheduling.py`: `_CHARGE_ACTIONS` frozenset removed. Role guard separated from action_type guard. `reduce_ev_charge_rate` produces documented intentional hold; any other non-charge ActionType raises `ValueError`.
- `evaluator.py` and `engine/__init__.py` updated to import and export `ActionType`.
- 7 new tests added (3 energy_balancing, 2 battery_control, 2 ev_scheduling). Existing tests using invalid string literals (`"unknown_battery_action"`, `"unknown_ev_action"`, `"z_action"`, `"a_action"`) updated to valid ActionType members.
- Final: 743 tests pass, ruff clean, mypy clean (no new type errors).

### Change Log

- 2026-05-05: Implemented story 8-0 — ActionType StrEnum, exhaustive dispatch in battery/EV evaluators, EnergyStrategy raise on unknown, 5 net new tests (743 total).

---

## Dev Notes

### Context: Why This Story Exists

This story closes a deferred risk identified across Stories 7.3, 7.4, and 7.5. When action type strings were unregistered plain `str` values, a typo or drift in one module produced a silent hold intent rather than a traceable failure. In Epic 8, those intents drive real device commands. The Epic 7 retrospective classified this as crossing Jordan's runtime-risk threshold: any deferred issue that can produce a misleading intent, unsafe dispatch, or ambiguous watchdog behavior must be fixed before Story 8.1.

This story is a **hard gate before Story 8.2** (IntentExecutor must dispatch on `ActionType`, not strings) and **strongly preferred before Story 8.1** (runtime loop assembles EvaluationInput; knowing ActionType members are stable avoids last-minute changes in 8.1's dev notes).

---

### Decision: ActionType Placement in energy_balancing.py

`ActionType` is defined in `energy_balancing.py` because `CandidateAction.action_type` is defined there and all action type strings originate there. Downstream modules (`battery_control.py`, `ev_scheduling.py`, `evaluator.py`) already import `CandidateAction` from `energy_balancing` — adding `ActionType` to those same imports introduces no new coupling.

Do NOT create a separate `action_types.py` module. Do NOT define `ActionType` in `evaluator.py` or `models.py`. The type lives with the model it types.

---

### Decision: StrEnum, Not IntEnum

`ActionType` is a `StrEnum` for three reasons:
1. Event log entries use `intent.source_candidate.action_type` values as human-readable strings. StrEnum preserves that without a conversion step.
2. The `resolve_conflicts` sort key (`c.action_type`) already sorts as a string. No change required.
3. Pydantic v2 serializes StrEnum to its string value by default — no `model_config` change needed.

---

### CandidateAction.action_type Type Change — What Changes at Call Sites

`CandidateAction.action_type: NonEmptyStr` → `CandidateAction.action_type: ActionType`.

Pydantic v2 accepts `ActionType` members (which are `str` instances) for StrEnum fields. Any site currently passing a bare string like `action_type="battery_charge_from_pv"` will raise `ValidationError` unless it passes the enum member instead. All such sites are inside the engine:

| File | Line range | String literal | Replacement |
|------|-----------|----------------|-------------|
| `energy_balancing.py` | ~139 | `"battery_charge_from_pv"` | `ActionType.battery_charge_from_pv` |
| `energy_balancing.py` | ~152 | `"permit_ev_charge"` | `ActionType.permit_ev_charge` |
| `energy_balancing.py` | ~179 | `"battery_charge_from_pv"` | `ActionType.battery_charge_from_pv` |
| `energy_balancing.py` | ~191 | `"battery_discharge_to_avoid_import"` | `ActionType.battery_discharge_to_avoid_import` |
| `energy_balancing.py` | ~209 | `"ev_charge"` | `ActionType.ev_charge` |
| `energy_balancing.py` | ~222 | `"battery_support_ev"` | `ActionType.battery_support_ev` |
| `evaluator.py` | ~83 | `"battery_discharge_to_avoid_import"` | `ActionType.battery_discharge_to_avoid_import` |
| `evaluator.py` | ~93 | `"reduce_ev_charge_rate"` | `ActionType.reduce_ev_charge_rate` |

Test files that construct `CandidateAction` directly also need updating — search `tests/` for `action_type=` string arguments.

---

### battery_control.py — Exhaustive Dispatch Detail

The current code has two frozenset membership checks followed by a silent fallback hold:

```python
# BEFORE (remove this):
_CHARGE_ACTIONS = frozenset({"battery_charge_from_pv"})
_DISCHARGE_ACTIONS = frozenset({"battery_discharge_to_avoid_import", "battery_support_ev"})
...
if resolved_candidate.action_type in _CHARGE_ACTIONS:
    ...
if resolved_candidate.action_type in _DISCHARGE_ACTIONS:
    ...
return _hold(evaluation_input, "battery_candidate_unavailable", resolved_candidate)  # silent fallback
```

The final silent hold (line 88) was the bug surface: an unrecognized action type arrived at battery dispatch and produced a silent `battery_candidate_unavailable` hold with no log trace. Replace with an explicit `else: raise ValueError(...)`. The raise is intentional and desired — if a new `ActionType` member is added later without updating battery dispatch, the test suite catches it immediately.

The `battery_candidate_unavailable` reason code is still valid — it is returned when the candidate is `None` or the role is wrong (early guard on line 53–54). The late fallthrough case is eliminated entirely.

---

### ev_scheduling.py — reduce_ev_charge_rate Is Intentional

`reduce_ev_charge_rate` is a peak-limiting action type that carries `DeviceRole.ev_charger`. When conflict resolution resolves it as the EV candidate, `evaluate_ev_scheduling()` receives it. The correct behavior is to hold (not charge), because:

1. The peak-limiting subsystem is already suppressing EV activity by emitting this candidate at `PriorityBand.safety`.
2. `ev_scheduling.py` has its own internal peak-projection guard (lines 56–63) that independently blocks charge if projection ≥ limit.
3. `reduce_ev_charge_rate` does not mean "set a lower charge rate" in the current engine — it means "do not permit EV charging." A future `PartialIntervalTracker`-based rate modulation path would require a new `ActionType` member and a new EV dispatch case.

The updated dispatch explicitly documents this:
```python
if action_type is ActionType.reduce_ev_charge_rate:
    # Intentional hold: peak_limiting candidate; EV suppression already
    # handled by ev_scheduling's internal peak-projection guard.
    return _hold(evaluation_input, "ev_candidate_unavailable", resolved_candidate)
```

Do NOT change `reduce_ev_charge_rate` to charge, rate-limit, or stop. Hold is correct.

---

### EnergyStrategy Dispatch — Raise, Not Silent Return

Current code (lines 92–98 of `energy_balancing.py`):
```python
handler = dispatch.get(evaluation_input.strategy)
if handler is None:
    return StrategyEvaluation(
        strategy=evaluation_input.strategy,
        strategy_candidates=(),
        suppressed_by_operating_mode=None,
    )
```

This was flagged in the Epic 7 retro: an unknown `EnergyStrategy` silently produces an empty `StrategyEvaluation`, which then causes the engine to output a hold for all devices without logging why. Replace with:
```python
handler = dispatch.get(evaluation_input.strategy)
if handler is None:
    raise ValueError(f"Unhandled EnergyStrategy: {evaluation_input.strategy!r}")
```

Since `EnergyStrategy` is an enum, Pydantic validates it at `EvaluationInput` construction time. An unknown strategy can only arrive if Pydantic validation is bypassed (e.g., a future enum member added without updating dispatch). The raise gives a clear traceback instead of a silent misbehavior.

---

### Import Chain — No New Coupling

All downstream modules already import from `energy_balancing`. After this story:

- `battery_control.py`: `from open_ems.engine.rules.energy_balancing import ActionType, CandidateAction`
- `ev_scheduling.py`: `from open_ems.engine.rules.energy_balancing import ActionType, CandidateAction`
- `evaluator.py`: `from open_ems.engine.rules.energy_balancing import ActionType, CandidateAction, PriorityBand, evaluate_energy_strategy, resolve_conflicts`

No new inter-module dependencies are introduced. The engine import boundary (no imports from `web`, `storage`, `adapters`, `services`) is unchanged.

---

### resolve_conflicts Sort Key — No Change Needed

`resolve_conflicts` sorts candidates by `c.action_type` as the last tiebreaker. Since `ActionType` is a `StrEnum`, comparison is by string value. The sort order is identical to sorting by the raw string. No change to `resolve_conflicts` is needed.

---

### Testing the EnergyStrategy Raise

Since `EnergyStrategy` is a `StrEnum`, Pydantic will reject any non-member value at `EvaluationInput` construction. Constructing a test scenario with a truly unknown strategy requires bypassing Pydantic validation. The practical approach:

Option A (recommended): Test all three known strategies return non-raising results. Mypy + Pydantic enforce the constraint at construction; the raise branch is defense-in-depth.

Option B: Use `model_copy(update={"strategy": object()})` with `model_config` `validate_assignment=False` (if the model allows). Only do this if it doesn't require changing production model config. If Option B would require touching the Pydantic model config, use Option A and add a comment in the test.

---

### Exhaustive-Dispatch Test Pattern

The exhaustive dispatch tests iterate all `ActionType` members and assert documented behavior. Pattern from `test_battery_control.py`:

```python
import pytest
from open_ems.engine.rules.energy_balancing import ActionType

BATTERY_CHARGE_TYPES = {ActionType.battery_charge_from_pv}
BATTERY_DISCHARGE_TYPES = {ActionType.battery_discharge_to_avoid_import, ActionType.battery_support_ev}
BATTERY_HANDLED_TYPES = BATTERY_CHARGE_TYPES | BATTERY_DISCHARGE_TYPES
EV_CHARGE_TYPES = {ActionType.permit_ev_charge, ActionType.ev_charge}
EV_HOLD_TYPES = {ActionType.reduce_ev_charge_rate}
EV_HANDLED_TYPES = EV_CHARGE_TYPES | EV_HOLD_TYPES

def test_battery_dispatch_exhaustive_all_battery_actiontypes():
    for action_type in BATTERY_HANDLED_TYPES:
        # construct candidate with this action_type + appropriate input
        # assert non-raising result with expected BatteryIntentAction

def test_battery_dispatch_raises_on_ev_action_type():
    for action_type in EV_CHARGE_TYPES | EV_HOLD_TYPES:
        # construct candidate with role=DeviceRole.battery + this action_type
        # assert ValueError is raised
```

This pattern makes it structurally impossible to add a new `ActionType` member without the test failing if dispatch is not updated.

---

### Story 8.2 AC Update (Post-Completion)

Once this story is done (merged, tests green), update Story 8.2's acceptance criteria to require `ActionType` enum dispatch in `IntentExecutor`. Specifically: IntentExecutor must dispatch on `ActionType` members, not string literals, and unknown members must raise — not silently dispatch a no-op command. This is the design contract Story 8.2 will implement.

---

### Pre-Completion Checklist for Reviewer

Before marking `Status: review`, verify:
- [ ] `ActionType` has exactly 6 members — no more, no fewer.
- [ ] `CandidateAction.action_type` is `ActionType`, not `NonEmptyStr` or `str`.
- [ ] No `_CHARGE_ACTIONS` or `_DISCHARGE_ACTIONS` frozensets remain in any engine file.
- [ ] No bare string `action_type=` literals remain in engine source (search for `action_type="battery_` and `action_type="ev_` and `action_type="permit_` and `action_type="reduce_`).
- [ ] Battery dispatch `else` branch raises `ValueError`.
- [ ] EV dispatch `reduce_ev_charge_rate` case has the documented comment.
- [ ] EV dispatch `else` branch raises `ValueError`.
- [ ] EnergyStrategy dispatch raises on unknown (no silent empty `StrategyEvaluation`).
- [ ] `ActionType` is in `engine/__init__.py` `__all__`.
- [ ] Full test suite passes at >= 736 tests.

---

### Review Findings

- [x] [Review][Patch] Misleading comment in `evaluate_ev_scheduling` — "EV charge suppression under peak overshoot is handled by the peak-projection guard below" is incorrect; the function returns a hold immediately at that line, before the guard is reached [`src/open_ems/engine/rules/ev_scheduling.py:52-53`]
- [x] [Review][Patch] Battery exhaustive test covers only a local 3-member subset, not all `ActionType` members — added `_BATTERY_RAISES_TYPES = set(ActionType) - _BATTERY_HANDLED_TYPES`; raises test now iterates this derived set [`tests/unit/engine/rules/test_battery_control.py:305`]
- [x] [Review][Patch] EV dispatch exhaustive tests are piecemeal with no structural completeness assertion — added `_EV_RAISES_TYPES = set(ActionType) - _EV_HANDLED_TYPES`; raises test now iterates this derived set [`tests/unit/engine/rules/test_ev_scheduling.py`]
- [x] [Review][Patch] `test_all_actiontype_members_have_expected_count` uses hardcoded count `6` — replaced with full set membership assertion `assert set(ActionType) == {...}` [`tests/unit/engine/rules/test_energy_balancing.py`]
- [x] [Review][Defer] Flat `ActionType` enum has no role-to-action-type consistency validation on `CandidateAction` — `role=DeviceRole.ev_charger, action_type=ActionType.battery_charge_from_pv` is accepted by Pydantic; cross-domain combination is a latent data-integrity risk; pre-existing design decision per Dev Notes [`src/open_ems/engine/rules/energy_balancing.py:CandidateAction`] — deferred, pre-existing
- [x] [Review][Defer] Exhaustive dispatch guarantee is purely social — no `__init_subclass__`, assertion, or CI guard ensures battery/EV dispatch stays in sync when a new `ActionType` member is added; tests catch it today but only via manual set maintenance; pre-existing design choice [`src/open_ems/engine/rules/battery_control.py`, `ev_scheduling.py`] — deferred, pre-existing
- [x] [Review][Defer] `ev_charge` and `permit_ev_charge` produce identical dispatch behavior in `evaluate_ev_scheduling` with no explanation of the semantic distinction — pre-existing; both currently proceed to the same EV charging logic [`src/open_ems/engine/rules/ev_scheduling.py`] — deferred, pre-existing
- [x] [Review][Defer] Discharge SoC boundary just above `reserve_floor_percent` (e.g. `floor + 0.01`) not covered by a positive-case test — pre-existing gap [`tests/unit/engine/rules/test_battery_control.py`] — deferred, pre-existing
- [x] [Review][Defer] Peak-projection guard equality boundary (`projection_kw == limit_kw`) and negative headroom formatting untested — pre-existing gap [`tests/unit/engine/rules/test_ev_scheduling.py`] — deferred, pre-existing
