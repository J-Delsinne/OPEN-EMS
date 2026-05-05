# Story 8.0b: Implement PartialIntervalTracker Contract and Implementation

Status: done

## Story

As a developer building Epic 8's runtime control loop,
I want a `PartialIntervalTracker` that accumulates grid meter readings within the active 15-minute clock-aligned interval and produces a projected peak value for each evaluation cycle,
so that the control loop can build a `PeakContext` from live data without accessing the billing database, and the finalize-before-evaluate invariant is mechanically enforceable.

## Acceptance Criteria

**AC1 — CompletedInterval type**

**GIVEN** a 15-minute clock-aligned interval has elapsed
**WHEN** the tracker finalizes it
**THEN** it returns a `CompletedInterval` value with:
- `interval_start_utc: datetime` — the UTC clock-aligned start of the completed interval
- `avg_power_kw: float` — arithmetic mean of all power readings recorded during the interval; `0.0` if no readings were recorded
- `sample_count: int` — count of readings recorded; `0` if none

**AND** `CompletedInterval` is a frozen dataclass (not a Pydantic model)
**AND** `interval_start_utc` is always a clock-aligned UTC datetime (minute divisible by 15, zero seconds and microseconds)

---

**AC2 — PartialIntervalTracker construction**

**GIVEN** the control loop starts or restarts
**WHEN** a `PartialIntervalTracker` is created
**THEN** it accepts explicit initialization via `PartialIntervalTracker(interval_start: datetime)`, where `interval_start` must be a clock-aligned UTC datetime — raises `ValueError` if not aligned or not UTC
**AND** it provides a factory method `PartialIntervalTracker.from_current_time(at: datetime) -> PartialIntervalTracker` that floors `at` to the nearest 15-minute clock boundary
**AND** the initial state is: `sample_count = 0`, `power_sum_kw = 0.0`, `latest_power_kw = None`

---

**AC3 — update() accumulates readings and detects interval rollover**

**GIVEN** the control loop calls `update(power_kw, at)` each tick

**WHEN** `at` falls within the current interval (same 15-minute clock boundary)
**THEN** the reading is added to the running sum; `sample_count` increments by one; `latest_power_kw` is updated; returns `None`

**WHEN** `at` crosses into a new 15-minute clock boundary
**THEN** the completed interval is finalized and a `CompletedInterval` is returned:
- `interval_start_utc` = the just-completed interval's start
- `avg_power_kw` = `power_sum / sample_count` if `sample_count > 0`, else `0.0`
- `sample_count` = readings recorded in the completed interval
- The tracker resets to the new interval with `power_sum = power_kw`, `sample_count = 1`, `latest_power_kw = power_kw`

**AND** the reading that triggered the rollover is credited to the **new** interval, not the completed one
**AND** if more than one boundary is crossed (gap in readings), the tracker resets to the current boundary and returns a single `CompletedInterval` for the interval that was being tracked — intermediate intervals are not synthesized; Story 8.1 handles gap marking at persistence time

---

**AC4 — build_peak_context() produces a valid PeakContext from active interval state**

**GIVEN** the control loop calls `build_peak_context(configured_peak_limit_kw, current_monthly_recorded_peak_kw, at)` after any rollover is resolved

**WHEN** the call is valid (`at >= interval_start` and `elapsed_seconds < 900`)
**THEN** returns a `PeakContext` with:
- `current_partial_window_projection_kw` = computed via the projection formula (AC5)
- `configured_peak_limit_kw` = from parameter
- `current_monthly_recorded_peak_kw` = from parameter
- `current_interval_start` = `self.interval_start`
- `current_interval_elapsed_seconds` = `int(elapsed_seconds)`, clamped to `[0, 899]`

**WHEN** `elapsed_seconds >= 900` (interval is complete but caller did not roll over first)
**THEN** raises `ValueError` with a message identifying the invariant violation — the caller (control loop) must call `update()` to trigger rollover before calling `build_peak_context()`

**WHEN** `at < interval_start` (time went backwards)
**THEN** raises `ValueError`

---

**AC5 — Projection formula: time-weighted average**

**GIVEN** the tracker has state at time `at`
**WHEN** projection is computed
**THEN** the formula is:

```
elapsed_fraction = elapsed_seconds / 900.0
remaining_fraction = 1.0 - elapsed_fraction

if sample_count == 0:
    projection_kw = 0.0  # no readings yet; conservative (no spurious peak action)
else:
    avg_so_far_kw = power_sum_kw / sample_count
    projection_kw = avg_so_far_kw * elapsed_fraction + latest_power_kw * remaining_fraction
```

**AND** the formula is interpreted as: the observed average weighted by elapsed time share + current rate extrapolated for remaining time
**AND** when `elapsed_fraction == 0.0` (interval just started, one reading added), `projection_kw = latest_power_kw` (current rate extrapolated to the full interval — worst-case initial estimate)
**AND** when `elapsed_fraction == 1.0` (impossible under AC4, but if elapsed equals 899/900), `projection_kw ≈ avg_so_far_kw` (converges to actual average as interval completes)
**AND** the result is guaranteed to be a finite float (no NaN or Inf)

---

**AC6 — Finalize-before-evaluate invariant documented and mechanically enforced**

The following invariant must appear in both the module docstring and Story 8.1's dev notes:

> **Finalize-before-evaluate invariant:** `PeakContext` must only be built from an active (non-complete) interval. The control loop must call `tracker.update(power_kw, at=now)` every tick. If `update()` returns a `CompletedInterval`, the loop persists it to `peak_intervals` before calling `tracker.build_peak_context()`. Only after this finalization step may the loop call `evaluate_cycle()`.

The mechanical enforcement is: `build_peak_context()` raises `ValueError` when `elapsed_seconds >= 900` (AC4). This makes it physically impossible for the control loop to pass a complete-interval `PeakContext` to the decision engine without triggering a test failure.

Once this story is complete, update Story 8.1's acceptance criteria to include this invariant as an explicit AC.

---

**AC7 — Engine import boundary preserved**

**GIVEN** `partial_interval_tracker.py` lives in `src/open_ems/engine/`
**WHEN** its imports are inspected
**THEN** it imports only from:
- Python standard library (`datetime`, `math`, `dataclasses`, `typing`)
- `open_ems.engine.models` (for `PeakContext`)
- Nothing from: `open_ems.web`, `open_ems.storage`, `open_ems.adapters`, `open_ems.services`

**AND** an AST import-boundary test verifies this in `tests/unit/engine/test_partial_interval_tracker.py`

---

**AC8 — Public exports**

**GIVEN** `PartialIntervalTracker` and `CompletedInterval` are defined
**WHEN** the engine package is imported
**THEN** both are accessible via `from open_ems.engine import PartialIntervalTracker, CompletedInterval`
**AND** both appear in `engine/__init__.py` `__all__`

---

**AC9 — Tests**

Unit tests cover:

- `test_initial_state_zero_samples()` — new tracker has `sample_count=0`, `latest_power_kw=None`
- `test_from_current_time_floors_to_15_min_boundary()` — verify boundary-flooring for various times
- `test_constructor_rejects_unaligned_interval_start()` — ValueError if minute not divisible by 15
- `test_constructor_rejects_non_utc_interval_start()` — ValueError if not UTC
- `test_update_within_same_interval_returns_none()` — accumulates, returns None
- `test_update_accumulates_multiple_readings()` — power_sum and count track correctly
- `test_update_crossing_boundary_returns_completed_interval()` — correct avg, count, interval_start
- `test_update_rollover_credits_reading_to_new_interval()` — `sample_count == 1` after rollover, sum == new reading
- `test_update_multi_boundary_gap_returns_single_completed_interval()` — if 30+ minutes pass between updates
- `test_build_peak_context_zero_samples_returns_zero_projection()` — no readings → projection = 0.0
- `test_build_peak_context_single_sample_full_projection()` — elapsed_fraction=0; projection = latest_power_kw
- `test_build_peak_context_time_weighted_formula()` — verify the weighted formula numerically (e.g., at 450s elapsed: 50% weight each; avg_so_far=3.0kW, latest=5.0kW → projection=4.0kW)
- `test_build_peak_context_elapsed_seconds_clamped_to_899()` — at exactly 899s, valid PeakContext
- `test_build_peak_context_raises_at_900_elapsed()` — ValueError when elapsed >= 900
- `test_build_peak_context_raises_on_time_backwards()` — ValueError when at < interval_start
- `test_completed_interval_avg_with_multiple_samples()` — avg_power_kw is arithmetic mean
- `test_completed_interval_avg_zero_samples()` — avg_power_kw = 0.0 when no samples recorded before rollover
- `test_projection_is_finite()` — result is finite float for all valid inputs
- `test_ast_import_boundary()` — `partial_interval_tracker.py` does not import from forbidden modules

---

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — record exact passing count as baseline (≥ 736).
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`

- [x] Task 1: Define CompletedInterval (AC: AC1)
  - [x] Create `src/open_ems/engine/partial_interval_tracker.py`.
  - [x] Add `from __future__ import annotations`.
  - [x] Import: `import math`, `from dataclasses import dataclass`, `from datetime import datetime, timedelta`.
  - [x] Define `CompletedInterval` as a frozen dataclass.
  - [x] No Pydantic, no validators — this is a plain data carrier.

- [x] Task 2: Implement PartialIntervalTracker (AC: AC2, AC3, AC4, AC5, AC6)
  - [x] Define helper `_floor_to_15_min(dt: datetime) -> datetime`.
  - [x] Define helper `_assert_clock_aligned_utc(dt: datetime, name: str) -> None`.
  - [x] Implement `PartialIntervalTracker` with `__init__`, `from_current_time`, `update`, `build_peak_context`, and properties.
  - [x] Add the finalize-before-evaluate invariant to the module docstring and class docstring (AC6 verbatim).

- [x] Task 3: Add exports to engine __init__.py (AC: AC8)
  - [x] Added `from open_ems.engine.partial_interval_tracker import CompletedInterval, PartialIntervalTracker`.
  - [x] Added `"CompletedInterval"` after `"CandidateAction"` in `__all__`.
  - [x] Added `"PartialIntervalTracker"` before `"PeakContext"` in `__all__`.

- [x] Task 4: Add unit tests (AC: AC9)
  - [x] Created `tests/unit/engine/test_partial_interval_tracker.py` with all 23 tests covering all AC9 requirements.

- [x] Task 5: Final validation (AC: all)
  - [x] 23/23 new tests pass.
  - [x] ruff check clean.
  - [x] ruff format clean.
  - [x] mypy clean (72 source files).
  - [x] Full suite: 766 passed (baseline 743, +23 new tests).

---

## Dev Notes

### Context: Why This Story Exists Before Story 8.1

`PartialIntervalTracker` is the data contract between the runtime control loop (Story 8.1) and the decision engine (Epic 7). Story 8.1 assembles `EvaluationInput` each tick using `PeakContext` — which must come from the tracker. If the tracker interface is defined inside Story 8.1 itself, the developer faces two distinct design problems simultaneously: interval tracking semantics + control loop architecture. This story separates them cleanly.

Additionally, the deferred `current_interval_elapsed_seconds=900` boundary question (identified in Story 7.5 code review) is only resolvable with the finalize-before-evaluate invariant as a mechanical enforcement point. That invariant requires the tracker to exist first.

Owner: Winston (tracker design) + Jordan (invariant governance).

---

### Architecture Context: Two Tracking Mechanisms (BAD-1)

Architecture Decision BAD-1 establishes two distinct tracking mechanisms:

| Mechanism | Purpose | Owner | Lifecycle |
|---|---|---|---|
| `peak_intervals` table | Authoritative billing record | `energy_repo` | Persistent; written once per completed interval |
| `PartialIntervalTracker` | Real-time control signal | `control_loop.py` | In-memory; reset each 15-min boundary |

`PartialIntervalTracker` **is not** the billing record. It is the operational control signal. Conflating these would allow control actions to affect billing accuracy. The tracker outputs `CompletedInterval` data; the control loop (Story 8.1) persists it to `peak_intervals`. The tracker has no dependency on `storage/`.

---

### Module Placement: engine/partial_interval_tracker.py

The tracker lives in `src/open_ems/engine/partial_interval_tracker.py` alongside `control_loop.py` (the component that owns it at runtime). It is pure computation — no IO, no Pydantic models except the `PeakContext` it returns. This keeps it inside the engine import boundary.

The architecture shows `engine/control_loop.py` as the main asyncio task and evaluation cycle owner. `PartialIntervalTracker` is an in-memory helper owned by that task.

Do NOT place `PartialIntervalTracker` in `services/` (services are cross-cutting infrastructure with potential IO); do NOT place it in `engine/rules/` (rules are pure evaluation functions, not stateful trackers); do NOT place it in `engine/models.py` (models are pure Pydantic input types, not stateful objects).

---

### Clock Alignment: Critical for Correctness

The 15-minute peak tariff is billed on **clock-aligned** intervals: 00:00–00:15, 00:15–00:30, etc. A rolling interval starting from the first observation would produce incorrect billing records. The `PartialIntervalTracker` always uses `_floor_to_15_min(dt)` to determine the current interval boundary — it never uses the first-observation timestamp as the interval start.

This means: if the system starts at 12:07 UTC, `from_current_time()` initializes with `interval_start = 12:00`. The tracker begins accumulating from 12:07 onward, with `elapsed_seconds ≈ 420` at that first reading. This is correct: the billing interval started at 12:00, and we observe only a partial window.

The `PeakContext.current_interval_start` validator in `models.py` already enforces the 15-minute alignment:
```python
if value.minute % 15 != 0 or value.second != 0 or value.microsecond != 0:
    raise ValueError("current_interval_start must be aligned to a 15-minute boundary")
```
The tracker's `_assert_clock_aligned_utc` provides the same check at construction time.

---

### update() Rollover: Single CompletedInterval Return

When `update()` is called and `_floor_to_15_min(at) > self._interval_start`, exactly one `CompletedInterval` is returned — the interval that was actively tracked. If multiple boundaries were crossed (e.g., system was paused for 30 minutes), only the single tracked interval is finalized. Intermediate intervals (00:30, 00:45 if the tracker was at 00:15 and `at` is now 01:02) are **not** synthesized.

Story 8.1 is responsible for detecting these gaps and writing `peak_intervals` rows with `data_quality=incomplete` for missed intervals. The tracker does not need to know about persistence or gap handling.

The reading that caused the rollover goes to the **new** interval. This is the only sensible choice: the reading's timestamp (`at`) is inside the new boundary, so it belongs there. After rollover: `power_sum = power_kw`, `sample_count = 1`, `latest_power_kw = power_kw`.

---

### Projection Formula: Rationale

The formula `projection = avg_so_far * elapsed_fraction + latest_kw * remaining_fraction` has this interpretation:

- For time already elapsed: use the actual observed average (that is the power that was drawn)
- For time not yet elapsed: assume the current rate continues (forward projection)
- The result is the projected average over the full 900-second interval

Example (AC9 test): 300 seconds elapsed (one-third of interval), observed avg = 3.0 kW, current = 9.0 kW:
```
projection = 3.0 * (300/900) + 9.0 * (600/900)
           = 3.0 * 0.333 + 9.0 * 0.667
           = 1.0 + 6.0 = 7.0 kW
```
This correctly signals: "if the 9.0 kW rate continues, the interval average will be 7.0 kW." The engine responds immediately to the current rate spike, not just the historical average.

**Edge case: elapsed = 0** (interval just started):
- `elapsed_fraction = 0.0`, `remaining_fraction = 1.0`
- `projection = 0.0 * avg_so_far + latest_kw * 1.0 = latest_kw`
- Correct: the entire remaining interval is projected at the current rate

**Edge case: sample_count = 0** (no readings yet):
- `projection = 0.0`
- This is the conservative choice. No readings = no observed demand. The decision engine will not trigger a false peak action.
- In practice, `sample_count = 0` should be rare: the control loop polls the meter every 5–15 seconds, so by the time `build_peak_context()` is called, at least one reading should exist for any interval except the very first tick after startup.

---

### The 900-Second Guard in build_peak_context()

`PeakContext.current_interval_elapsed_seconds` has validator `IntervalElapsedSeconds = Annotated[int, Field(ge=0, le=900)]`. The value 900 is technically valid by the Pydantic model, but represents a **complete** interval — a state the control loop must never pass to `evaluate_cycle()`.

Story 7.5 code review noted this ambiguity: "a fully-elapsed interval is accepted and treated identically to a partial one." P1's resolution: `build_peak_context()` raises before the `PeakContext` is even constructed if `elapsed >= 900`. This makes the invariant a hard runtime error, not a documentation note.

The `le=900` bound in `IntervalElapsedSeconds` remains valid for other potential consumers, but the tracker enforces the stricter `< 900` rule.

---

### Story 8.1 AC Update (After Completion)

Once this story is reviewed and done, update Story 8.1's acceptance criteria to add:

> **Finalize-before-evaluate invariant (AC from P1):** Each control loop tick must call `tracker.update(power_kw=grid_meter.grid_power_kw, at=now)`. If the return is a `CompletedInterval`, persist it to `peak_intervals` via `energy_repo.write_peak_interval(completed)` before proceeding. Then call `tracker.build_peak_context(configured_peak_limit_kw=..., current_monthly_recorded_peak_kw=..., at=now)` to obtain the `PeakContext` for `EvaluationInput`. Only then call `evaluate_cycle()`. This ordering must be enforced by the loop's tick logic, not left to convention.

Also update Story 8.1 tests to include: "unit test verifies that a completed interval returned by `tracker.update()` is persisted before `evaluate_cycle()` is called (via mock/spy on `energy_repo.write_peak_interval`)."

---

### Pre-Completion Checklist for Reviewer

Before marking `Status: review`, verify:
- [x] `CompletedInterval` is a frozen dataclass, not a Pydantic model.
- [x] `_floor_to_15_min()` handles all minutes: 0, 7, 14, 15, 29, 30, 44, 45, 59.
- [x] `update()` rollover credits the new reading to the **new** interval (sample_count=1 after rollover, not 0).
- [x] `build_peak_context()` raises `ValueError` when `elapsed_seconds >= 900`.
- [x] `build_peak_context()` raises `ValueError` when `at < interval_start`.
- [x] Zero-sample projection is `0.0` (not `None`, not an error).
- [x] Projection formula uses `latest_power_kw` (most recent reading) for remaining fraction — NOT `power_sum / sample_count`.
- [x] `partial_interval_tracker.py` has no imports from web/storage/adapters/services.
- [x] Both `PartialIntervalTracker` and `CompletedInterval` are in `engine/__init__.py` `__all__`.
- [x] Full test suite passes at >= 736 tests.

---

## Dev Agent Record

### Completion Notes

Baseline: 743 tests passing, ruff clean, mypy clean.

Implemented all 5 tasks in one session:

- `CompletedInterval` defined as frozen dataclass in `src/open_ems/engine/partial_interval_tracker.py`. No Pydantic, no validators.
- `_floor_to_15_min()` floors any datetime to the nearest 15-minute UTC boundary.
- `_assert_clock_aligned_utc()` validates UTC timezone and 15-minute alignment; raises `ValueError` for either violation.
- `PartialIntervalTracker` fully implemented: `__init__`, `from_current_time` classmethod, `update` (accumulation + rollover), `build_peak_context` (projection formula + `>= 900` and `< 0` guards), and three read-only properties.
- Finalize-before-evaluate invariant documented in module-level docstring and class docstring (AC6 verbatim).
- Both types exported from `engine/__init__.py` and added to `__all__` in alphabetical position.
- 23 new tests: construction, accumulation, rollover, projection formula, guard conditions, finiteness, export checks, and AST import boundary.
- `type: ignore[operator]` on one line where mypy cannot see that `_latest_power_kw` is non-None after `_sample_count > 0` check — invariant is enforced by logic.

Final: 766 tests pass (baseline 743, +23), ruff clean, mypy clean.

---

## Senior Developer Review (AI) — 2026-05-05

**Outcome:** Changes Requested → Approved after patches

**Action Items:**
- [x] P1 (High): Add UTC validation in `update(at=)` — missing guard corrupted `_interval_start` on rollover with non-UTC `at`; `from_current_time()` had the check but `update()` did not. Added `if at.tzinfo is None or at.utcoffset() != timedelta(0): raise ValueError(...)` and test `test_update_rejects_non_utc_at`.
- [x] P2 (Low): Replace `# type: ignore[operator]` with explicit `assert self._latest_power_kw is not None` to make the sample_count/latest invariant visible to future readers and mypy.

**Deferred Items:**
- [x] D1: `assert` vs `raise ValueError` for finiteness guard — Pydantic validator provides second line of defence; `python -O` not used in production. Defer.
- [x] D2: AC9 spec example text has wrong numbers in parenthetical (says avg=3.0, correct is avg=4.0). Test is correct; spec text is a documentation error. Defer to Story 8.1 context cleanup.
- [x] D3: Negative projection undocumented (net-export sites). Correct behavior; document in Story 8.1 PeakContext contract.
- [x] D4: Thread safety undocumented on class. Story 8.1 owns the asyncio task boundary documentation.
- [x] D5: BH-1 false positive — multi-boundary single CompletedInterval is by-spec design (AC3 explicit).

**Final:** 767 tests pass (766 + 1 new from P1), ruff clean, mypy clean.

### Change Log

- 2026-05-05: Implemented story 8-0b — PartialIntervalTracker, CompletedInterval, 23 tests (766 total).
- 2026-05-05: Code review patches applied — UTC guard in update() + explicit None assertion (767 total).
