# Story 7.2: Implement Peak Limiting Logic Consuming PeakContext

Status: done

<!-- Completion note: Ultimate context engine analysis completed - comprehensive developer guide created. -->

## Story

As a developer,
I want the decision engine to enforce peak consumption limits using a `PeakContext` object supplied as part of the evaluation input,
so that peak limiting logic is testable without a database and the authoritative billing record remains outside the pure decision engine.

## Acceptance Criteria

**AC1 - PeakContext is the only peak input**
Given the decision engine receives an evaluation input containing a `PeakContext`
When peak limiting logic runs
Then it reads peak data exclusively from `EvaluationInput.peak_context`
And it does not access `peak_intervals`, `energy_readings`, `PartialIntervalTracker`, repository classes, SQLite connections, `StateStore`, adapter objects, or wall-clock time.

**AC2 - Required PeakContext fields and validation**
Given `PeakContext` is constructed
When validation runs
Then it contains:
- `current_partial_window_projection_kw`
- `configured_peak_limit_kw`
- `current_monthly_recorded_peak_kw`
- `current_interval_start`
- `current_interval_elapsed_seconds`

And `current_interval_start` is timezone-aware UTC and aligned to a 15-minute clock boundary (`:00`, `:15`, `:30`, or `:45`, with zero seconds and microseconds)
And `configured_peak_limit_kw` is greater than zero
And `current_monthly_recorded_peak_kw` is non-negative
And `current_interval_elapsed_seconds` is between 0 and 900 inclusive.

**AC3 - Overshoot produces deterministic load-reduction recommendation**
Given `current_partial_window_projection_kw` is greater than `configured_peak_limit_kw`
When peak limiting logic evaluates
Then it produces a typed load-reduction recommendation containing the required reduction in kW
And the required reduction is exactly `current_partial_window_projection_kw - configured_peak_limit_kw`
And the recommendation includes deterministic candidate actions for available healthy controllable loads:
- reduce EV charge rate when a healthy EV charger is present and charging
- request battery discharge support when a healthy battery is present
And candidate actions are returned in a documented stable order.

**AC4 - No overshoot produces no load reduction**
Given `current_partial_window_projection_kw` is less than or equal to `configured_peak_limit_kw`
When peak limiting logic evaluates
Then it returns a typed no-action or empty recommendation
And it does not create candidate battery or EV actions.

**AC5 - Dual tracking contract is preserved**
Given the architecture separates the in-memory operational signal from the billing record
When peak limiting logic runs
Then `PartialIntervalTracker` and `peak_intervals` remain distinct mechanisms outside the pure decision engine
And the decision engine never writes to `peak_intervals`
And completed interval aggregation and persistence remain runtime or storage-layer responsibilities outside this story.

**AC6 - Operating mode and degraded-device safety are respected**
Given Story 7.1 already derives `recommended_operating_mode`
When peak limiting logic evaluates
Then it reuses the existing `derive_recommended_operating_mode()` helper or an explicitly supplied `SystemOperatingMode`; it does not reimplement the degradation matrix
And if grid-meter degradation produces `conservative` or `fail_safe`, the peak rule does not emit new battery-discharge or EV-charge commands
And if battery or EV charger state is degraded or absent, that device is not included as a candidate load-reduction lever
And inverter degradation does not disable peak limiting when grid-meter data is healthy.

**AC7 - Tests**
- Unit tests verify overshoot produces a load-reduction recommendation.
- Unit tests verify no overshoot returns no action.
- Unit tests verify all `PeakContext` validation rules.
- Unit tests verify the logic reads only `PeakContext` fields and performs no database, adapter, web, or `StateStore` access.
- Unit tests verify clock-aligned interval handling uses `current_interval_start` from `PeakContext`.
- Unit tests verify deterministic action ordering across repeated calls.
- Unit tests verify degraded or absent battery/EV slots are not included as candidate levers.
- Unit tests verify grid-meter degradation suppresses peak-rule candidates and leaves mode handling to the runtime.

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` and record the baseline.
  - [x] Run `uv run python -m ruff check .`.
  - [x] Run `uv run python -m mypy src/`.
  - [x] Treat any unrelated pre-existing failure as a baseline note; do not hide it by changing unrelated code.

- [x] Task 1: Extend engine input with explicit peak context (AC: AC1, AC2, AC5)
  - [x] Update `src/open_ems/engine/models.py`.
  - [x] Add a frozen Pydantic v2 `PeakContext` model using `ConfigDict(frozen=True, extra="forbid")`.
  - [x] Use field names with units exactly as listed in AC2.
  - [x] Validate `current_interval_start` with the same UTC discipline used by core device models.
  - [x] Validate 15-minute clock alignment on `current_interval_start`.
  - [x] Validate elapsed seconds as `0 <= current_interval_elapsed_seconds <= 900`.
  - [x] Add `peak_context: PeakContext` to `EvaluationInput`.
  - [x] Update `EvaluationInput.from_snapshot()` to require a keyword-only `peak_context: PeakContext` parameter and copy it into the model.
  - [x] Update existing operating-mode tests to supply a small valid `PeakContext` fixture; do not weaken Story 7.1 required-slot validation.

- [x] Task 2: Add a pure peak-limiting rules module (AC: AC1, AC3, AC4, AC6)
  - [x] Create `src/open_ems/engine/rules/__init__.py`.
  - [x] Create `src/open_ems/engine/rules/peak_limiting.py`.
  - [x] Implement a pure synchronous function with an explicit name such as `evaluate_peak_limiting(evaluation_input: EvaluationInput) -> PeakLimitDecision`.
  - [x] Define a small typed result contract in engine code, such as `PeakLimitDecision` and `LoadReductionAction`.
  - [x] Keep this result a rule-level recommendation, not the final `EvaluationResult` and not a `Command`.
  - [x] Return no-action when `current_partial_window_projection_kw <= configured_peak_limit_kw`.
  - [x] For overshoot, calculate `required_reduction_kw` exactly as projection minus limit.
  - [x] Use deterministic candidate ordering. Recommended order for this story: EV charge-rate reduction first, battery discharge support second.
  - [x] Include EV reduction only when `ev_charger` is a healthy `EVChargerState` with `session_active=True` and `current_power_kw` greater than zero.
  - [x] Include battery discharge support only when `battery` is a healthy `BatteryState`.
  - [x] Do not inspect battery reserve floor, charger dynamic-rate capability, strategy, homeowner override, or final command authorization; later Epic 7 stories and Epic 8 own those concerns.

- [x] Task 3: Preserve operating-mode and architecture boundaries (AC: AC5, AC6)
  - [x] Reuse `derive_recommended_operating_mode()` instead of copying the degradation matrix.
  - [x] If the derived mode is `conservative` or `fail_safe`, return a no-action recommendation that makes the suppression explicit in the typed result.
  - [x] Keep `StateStore` ownership unchanged; do not call `StateStore.get_snapshot()` or `StateStore.publish()` from peak-limiting code.
  - [x] Do not create `engine/control_loop.py`, `engine/policy_guard.py`, command queues, runtime command dispatch, event logging, or database repositories in this story.
  - [x] Do not add an Alembic migration for `peak_intervals`; storage for completed billing intervals is not implemented here.
  - [x] Do not import from `open_ems.web`, `open_ems.storage`, `open_ems.adapters`, or `open_ems.services` in `open_ems.engine.rules.peak_limiting`.

- [x] Task 4: Export only stable engine symbols needed by tests and later stories (AC: AC1, AC3)
  - [x] Update `src/open_ems/engine/__init__.py` to export `PeakContext`.
  - [x] Export the public peak-limiting function and typed result/action names if they are intended as cross-module contracts.
  - [x] Keep names direct and boring; avoid generic names such as `Decision` or `Intent` that could collide with Story 7.5.

- [x] Task 5: Add focused unit tests (AC: AC1-AC7)
  - [x] Create `tests/unit/engine/rules/__init__.py`.
  - [x] Create `tests/unit/engine/rules/test_peak_limiting.py`.
  - [x] Test valid `PeakContext` construction with a UTC `current_interval_start` at a `:00`, `:15`, `:30`, or `:45` boundary.
  - [x] Parametrize invalid `current_interval_start` values: naive datetime, non-UTC timezone, non-boundary minute, nonzero seconds, and nonzero microseconds.
  - [x] Parametrize invalid numeric values: zero/negative `configured_peak_limit_kw`, negative `current_monthly_recorded_peak_kw`, negative elapsed seconds, and elapsed seconds above 900.
  - [x] Test projected overshoot with healthy charging EV and healthy battery returns required reduction and both candidates in stable order.
  - [x] Test projection exactly equal to the configured peak limit returns no action.
  - [x] Test projection below the configured peak limit returns no action.
  - [x] Test healthy inverter plus degraded battery returns only the EV candidate.
  - [x] Test healthy battery plus degraded EV charger returns only the battery candidate.
  - [x] Test degraded inverter with healthy grid meter still evaluates peak limiting.
  - [x] Test degraded grid meter returns no peak-rule candidates even if `PeakContext` contains an overshoot.
  - [x] Test repeated calls with the same input produce equal results.
  - [x] Test the module can run without database/app fixtures and does not import `open_ems.storage`, `open_ems.web`, `open_ems.adapters`, or `open_ems.services`.
  - [x] Update `tests/unit/engine/test_operating_mode.py` for the new required `peak_context` argument without changing its expected mode outcomes.

- [x] Task 6: Final validation (AC: all)
  - [x] Run `uv run python -m ruff check .`.
  - [x] Run `uv run python -m ruff format --check .`.
  - [x] Run `uv run python -m mypy src/`.
  - [x] Run `uv run python -m pytest tests/unit/engine/test_operating_mode.py tests/unit/engine/rules/test_peak_limiting.py --no-cov -q`.
  - [x] Run `uv run python -m pytest tests/ --no-cov -q`.
  - [x] Leave this story in `review` only after implementation is complete and all checked items are true.

## Dev Notes

### Scope and Non-Scope

This story adds the first peak-safety rule to the pure decision engine. It does not add the runtime loop that gathers peak data or dispatches commands.

In scope:
- A typed `PeakContext` supplied through `EvaluationInput`.
- A pure deterministic peak-limiting rule.
- A typed rule-level load-reduction recommendation.
- Unit tests that prove no database, tracker, adapter, web, or `StateStore` dependency is used.

Out of scope:
- `PartialIntervalTracker` implementation. Epic 8 runtime infrastructure owns it.
- `peak_intervals` table, `energy_repo`, completed interval persistence, or monthly peak aggregation.
- Final `EvaluationResult`, final per-role resolved intents, cycle timing, and human-readable decision reasons. Story 7.5 owns those contracts.
- Battery reserve-floor enforcement and EV scheduling-window logic. Stories 7.3 and 7.4 own those.
- `PolicyGuard`, `DeviceCommand`, command dispatch, command ACK/retry/idempotency, and audit logging. Epic 8 owns runtime enforcement.

### Existing Code to Preserve

`src/open_ems/engine/models.py`
- Current state: defines frozen `EvaluationInput` with device slots for inverter, battery, EV charger, and grid meter; validates required inverter and grid-meter slots; `from_snapshot()` copies slots from `SystemSnapshot`.
- Change expected: add `PeakContext`, add `peak_context` to `EvaluationInput`, and update `from_snapshot()` to accept `peak_context`.
- Preserve: frozen Pydantic model style, `extra="forbid"`, required-slot validation, no `StateStore` reference retention.

`src/open_ems/engine/operating_mode.py`
- Current state: pure `derive_recommended_operating_mode()` maps `DegradedDeviceState` role sets to `SystemOperatingMode`.
- Change expected: none unless import/export cleanup is needed.
- Preserve: deterministic role-set matrix and no side effects.

`src/open_ems/engine/__init__.py`
- Current state: exports `EvaluationInput` and `derive_recommended_operating_mode`.
- Change expected: export `PeakContext` and public peak-limiting rule symbols.
- Preserve: small public surface.

`src/open_ems/core/devices.py`
- Current state: defines energy sign conventions. `grid_power_kw > 0` means grid import and `battery_power_kw < 0` means battery discharge. Defines `BatteryState`, `EVChargerState`, `GridMeterState`, `DeviceRole`, and `DegradedDeviceState`.
- Change expected: none.
- Preserve: do not add engine-specific rule behavior to core device models.

`src/open_ems/core/state.py` and `src/open_ems/core/state_store.py`
- Current state: `SystemSnapshot` is immutable; `StateStore` publishes current UI-readable operating mode and data age/component state.
- Change expected: none.
- Preserve: peak limiting must not read or mutate the store.

### Implementation Guardrails

- Put rule code under `src/open_ems/engine/rules/peak_limiting.py`.
- Keep `engine.rules` pure: it may import from `open_ems.core` and `open_ems.engine`, plus standard library and Pydantic only.
- The rule must not compute clock boundaries from `datetime.now()`. The boundary already arrives as `PeakContext.current_interval_start`.
- Do not normalize, persist, or "complete" intervals in this story. `current_partial_window_projection_kw` is an operational control signal only.
- Do not write, update, or query a billing record. `current_monthly_recorded_peak_kw` is informational context for later result explanations and dashboards; it is not the trigger for load reduction in this story.
- Do not clamp negative projection values to zero unless the model explicitly documents why. The overshoot condition is strictly `projection > configured_peak_limit_kw`; export or low import simply produces no peak action.
- Use unit-bearing names (`*_kw`, `*_seconds`) and timezone-aware UTC datetimes.
- Keep no-action typed and explicit. Avoid returning `None` if it makes later composition ambiguous.
- Do not use raw dicts for rule results. Use frozen Pydantic models or enums consistent with the existing domain style.

### Suggested Module Shape

```python
# src/open_ems/engine/models.py
class PeakContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    current_partial_window_projection_kw: float
    configured_peak_limit_kw: float
    current_monthly_recorded_peak_kw: float
    current_interval_start: datetime
    current_interval_elapsed_seconds: int
```

```python
# src/open_ems/engine/rules/peak_limiting.py
class LoadReductionAction(enum.StrEnum):
    reduce_ev_charge_rate = "reduce_ev_charge_rate"
    discharge_battery = "discharge_battery"


class PeakLimitDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_required: bool
    required_reduction_kw: float
    candidate_actions: tuple[LoadReductionAction, ...]
    suppressed_by_operating_mode: SystemOperatingMode | None = None


def evaluate_peak_limiting(evaluation_input: EvaluationInput) -> PeakLimitDecision:
    ...
```

This shape is a guide, not a requirement, but the final code needs equivalent type safety and behavior.

### Previous Story Intelligence

Story 7.1 established the engine package and the first pure rule boundary.

Actionable learnings:
- Reuse `open_ems.core.SystemOperatingMode`; do not create duplicate mode strings or enums.
- `EvaluationInput` is the engine input object. Extend it deliberately rather than creating a parallel peak-only input shape.
- Keep pure engine code under `src/open_ems/engine/`.
- Keep engine imports one-way: `engine` can import `core`; it must not import `web`, `storage`, `services`, or concrete adapters.
- Existing tests use small helper constructors with timezone-aware UTC datetimes. Follow that style.
- Existing validation commands are `ruff check`, `ruff format --check`, `mypy src/`, focused pytest, then full pytest.

Story 7.1 review/deferred work:
- `DegradedDeviceState.role` is not cross-validated against the positional `EvaluationInput` slot in direct construction. Do not expand this story to solve that unless it blocks peak-limiting tests. `StateStore.publish()` already validates role-to-slot alignment, and `from_snapshot()` is the canonical construction path.

Recent git history:
- `6f96944 7.1` created `src/open_ems/engine/`, `EvaluationInput`, `derive_recommended_operating_mode()`, and engine tests.
- `2ddc23f 6.3` and `98cbc7a 6.2` added audit/config/event-log foundations. Peak limiting should not add logging here.
- `03ff345 5.3` and `b17b6f1 5.2` established StateStore-backed web serialization and state distribution. Peak limiting should not change UI serializers.

### Architecture Compliance

- Architecture BAD-1 separates the operational partial-window projection from the authoritative `peak_intervals` billing record. This story consumes only the operational signal through `PeakContext`.
- Architecture BAD-2 says grid-meter loss removes the peak safety guarantee and produces conservative mode at minimum. Peak limiting must not pretend stale/missing grid data is safe just because a `PeakContext` object exists.
- All device commands eventually pass through `PolicyGuard.authorize_and_dispatch()`, but this story must not create commands or dispatch anything.
- Energy sign conventions matter: `grid_power_kw > 0` is import from the grid. Peak limiting is about projected import exceeding a configured limit.
- Timestamps must be timezone-aware UTC. Clock-aligned means fixed 15-minute boundaries such as `12:00`, `12:15`, `12:30`, and `12:45`.
- Decision-engine logic must be testable without physical devices, a database, a web app, an async runtime, or running adapters.

### Testing Standards

- Mirror source structure: `src/open_ems/engine/rules/peak_limiting.py` maps to `tests/unit/engine/rules/test_peak_limiting.py`.
- Use `datetime.UTC` and explicit fixed datetimes in tests.
- Use helper constructors for healthy `BatteryState`, `EVChargerState`, `GridMeterState`, `InverterState`, and `DegradedDeviceState`.
- Keep tests synchronous unless the implementation has a strong reason otherwise. There should be no async runtime requirement.
- Keep line length at 100 characters and use project Ruff/mypy settings from `pyproject.toml`.
- No database/app fixtures should be required for these tests.

### Latest Technical Information

Checked on 2026-05-04:
- The lockfile already resolves Pydantic `2.13.3`, pytest `9.0.3`, Ruff `0.15.12`, and mypy `1.20.2`.
- Pydantic `2.13.3` is the latest PyPI release as of Apr 20, 2026. Continue using Pydantic v2 APIs such as `ConfigDict(frozen=True, extra="forbid")`; do not add another validation library.
- Pydantic's current config docs support both `extra="forbid"` and `frozen=True`, matching the existing model style.
- pytest `9.0.3` includes a security fix for CVE-2025-71176. The project is already on that version; do not downgrade.
- Ruff `0.15.12` is the latest GitHub release as of Apr 24, 2026. Keep using `ruff check` and `ruff format --check` with the repository config.
- No new third-party dependency or external API is required for this story.

### Project Structure Notes

Expected new files:
- `src/open_ems/engine/rules/__init__.py`
- `src/open_ems/engine/rules/peak_limiting.py`
- `tests/unit/engine/rules/__init__.py`
- `tests/unit/engine/rules/test_peak_limiting.py`

Expected updated files:
- `src/open_ems/engine/models.py`
- `src/open_ems/engine/__init__.py`
- `tests/unit/engine/test_operating_mode.py`

Files to read and preserve before editing:
- `src/open_ems/engine/models.py`
- `src/open_ems/engine/operating_mode.py`
- `src/open_ems/core/devices.py`
- `src/open_ems/core/state.py`
- `src/open_ems/core/state_store.py`

Expected files not to modify:
- `src/open_ems/web/**`
- `src/open_ems/storage/**`
- `src/open_ems/adapters/**`
- `src/open_ems/services/**`
- `migrations/**`

### References

- `_bmad-output/planning-artifacts/epics.md` - Epic 7 overview and Story 7.2 acceptance criteria.
- `_bmad-output/planning-artifacts/epics.md` - Epic 7 cross-story constraints.
- `_bmad-output/planning-artifacts/architecture.md` - BAD-1 15-Minute Peak Window Calculation.
- `_bmad-output/planning-artifacts/architecture.md` - BAD-2 Conservative Fallback Behavior Matrix.
- `_bmad-output/planning-artifacts/architecture.md` - Safety-Critical Patterns and energy sign conventions.
- `_bmad-output/planning-artifacts/prd.md` - FR7, FR8, FR9, FR10, FR11b, FR13, FR15, NFR-P1, NFR-R2, NFR-I3.
- `_bmad-output/planning-artifacts/ux-design-specification.md` - event-log guarantee that later decisions must explain peak-limit enforcement.
- `_bmad-output/implementation-artifacts/7-1-implement-systemoperatingmode-derivation-and-degradation-matrix.md` - previous story implementation context and review findings.
- `src/open_ems/engine/models.py` - current `EvaluationInput`.
- `src/open_ems/engine/operating_mode.py` - existing operating-mode derivation helper.
- `src/open_ems/core/devices.py` - energy sign conventions and device state models.
- `src/open_ems/core/state.py` - `SystemOperatingMode` and `SystemSnapshot`.
- `src/open_ems/core/state_store.py` - immutable snapshot ownership boundary.
- `pyproject.toml` and `uv.lock` - dependency and quality-tool versions.
- Pydantic PyPI release history: https://pypi.org/project/pydantic/
- Pydantic config docs: https://pydantic.dev/docs/validation/latest/api/pydantic/config/
- pytest changelog: https://docs.pytest.org/en/latest/changelog.html
- Ruff docs: https://docs.astral.sh/ruff/
- Ruff 0.15.12 release: https://github.com/astral-sh/ruff/releases/tag/0.15.12

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Implementation Plan

- Extend the existing `EvaluationInput` contract with a validated, frozen `PeakContext`.
- Add a pure `engine.rules.peak_limiting` module that returns typed rule-level recommendations only.
- Reuse Story 7.1 operating-mode derivation for conservative/fail-safe suppression.
- Keep all runtime responsibilities out of scope: no `StateStore`, storage, adapters, web, services, commands, or logging.

### Debug Log References

- Baseline: `uv run python -m pytest tests/ --no-cov -q` - 615 passed, 43 warnings.
- Baseline: `uv run python -m ruff check .` - passed.
- Baseline: `uv run python -m mypy src/` - passed.
- Red phase: `uv run python -m pytest tests/unit/engine/test_operating_mode.py tests/unit/engine/rules/test_peak_limiting.py --no-cov -q` - failed with missing `PeakContext` import and missing peak-limiting rule.
- Green phase: `uv run python -m pytest tests/unit/engine/test_operating_mode.py tests/unit/engine/rules/test_peak_limiting.py --no-cov -q` - 39 passed.
- Final: `uv run python -m ruff check .` - passed.
- Final: `uv run python -m ruff format --check .` - passed.
- Final: `uv run python -m mypy src/` - passed.
- Final: `uv run python -m pytest tests/unit/engine/test_operating_mode.py tests/unit/engine/rules/test_peak_limiting.py --no-cov -q` - 39 passed.
- Final: `uv run python -m pytest tests/ --no-cov -q` - 637 passed, 43 warnings.

### Completion Notes List

- Added frozen `PeakContext` validation for explicit peak-limiting inputs, including UTC and fixed 15-minute interval boundary validation.
- Extended `EvaluationInput` and `from_snapshot()` so peak data enters the engine only through `peak_context`.
- Added pure `evaluate_peak_limiting()` logic with typed `PeakLimitDecision` and `LoadReductionAction` outputs.
- Implemented deterministic candidate action ordering: EV charge-rate reduction before battery discharge support.
- Reused `derive_recommended_operating_mode()` to suppress peak-rule candidates under `conservative` and `fail_safe` mode recommendations.
- Preserved architecture boundaries: no storage, web, adapter, service, `StateStore`, runtime command, migration, or audit-log implementation.
- Added focused unit tests for `PeakContext`, overshoot/no-action behavior, degraded or absent optional device slots, grid-meter suppression, deterministic output, and import-boundary safety.

### File List

- `_bmad-output/implementation-artifacts/7-2-implement-peak-limiting-logic-consuming-peakcontext.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`
- `src/open_ems/engine/__init__.py`
- `src/open_ems/engine/models.py`
- `src/open_ems/engine/rules/__init__.py`
- `src/open_ems/engine/rules/peak_limiting.py`
- `tests/unit/engine/test_operating_mode.py`
- `tests/unit/engine/rules/__init__.py`
- `tests/unit/engine/rules/test_peak_limiting.py`

### Change Log

- 2026-05-04: Implemented pure peak-limiting rule with `PeakContext`, focused tests, and full validation; story marked ready for review.

### Review Findings

<!-- Code review conducted 2026-05-04. Layers: Blind Hunter, Edge Case Hunter, Acceptance Auditor. -->

- [x] [Review][Decision] `current_partial_window_projection_kw` accepts NaN and ±inf — NaN propagates into `PeakLimitDecision.required_reduction_kw` with `action_required=True` because `NaN <= 0` is `False` in Python; `+inf` produces an unsatisfiable `required_reduction_kw=+inf`. Spec says "do not clamp negative values to zero" but does not address IEEE 754 special values. Decision needed: add a `math.isfinite` guard in the validator, or document that callers are responsible for supplying finite projections. [`src/open_ems/engine/models.py` — `PeakContext.current_partial_window_projection_kw`]

- [x] [Review][Patch] `PeakLimitDecision.required_reduction_kw` has no model-level non-negative constraint — model permits negative values; correctness depends on call-site discipline rather than the model invariant. Add `Field(ge=0.0)` or use `NonNegativeKw`. [`src/open_ems/engine/rules/peak_limiting.py:23`]

- [x] [Review][Patch] Suppression set `{conservative, fail_safe}` is a bare set literal — extract to a named module-level constant (e.g., `_SUPPRESSED_MODES`) with a one-line comment referencing AC6 so future maintainers know why exactly these two modes are covered. [`src/open_ems/engine/rules/peak_limiting.py:35`]

- [x] [Review][Patch] `EvaluationInput.from_snapshot()` signature change may break production callers — the old single-argument form now raises `TypeError`; only tests in this diff were updated. Verify no production call sites (e.g., in services or background tasks) still call the old signature. [`src/open_ems/engine/models.py:63`]

- [x] [Review][Patch] `peak_context or _peak_context()` falsy idiom in test helper `_input_with()` — Pydantic `BaseModel` instances are always truthy; the fallback never fires on a valid `PeakContext`, only on `None`. Replace with `peak_context if peak_context is not None else _peak_context()`. [`tests/unit/engine/rules/test_peak_limiting.py` — `_input_with()`]

- [x] [Review][Defer] `current_monthly_recorded_peak_kw`, `current_interval_start`, `current_interval_elapsed_seconds` are stored in `PeakContext` but unused in rule logic — intentional per spec ("monthly recorded peak is informational; do not use it as the trigger"). Fields are validated and available for later stories (7.3–7.5). [`src/open_ems/engine/rules/peak_limiting.py`] — deferred, pre-existing by spec design

- [x] [Review][Defer] `current_interval_elapsed_seconds=900` (fully elapsed interval) is accepted and treated identically to mid-window — no branch differentiates a fully elapsed interval from a partial one; projection arithmetic is applied uniformly. Epic 8 / Story 7.5 may need to handle this boundary. [`src/open_ems/engine/models.py:IntervalElapsedSeconds`] — deferred, pre-existing by spec design

- [x] [Review][Defer] `_require_utc` is a private helper used exactly once — adds indirection without reuse. Consider inlining into its single validator or promoting to a shared utility when a second UTC-validated datetime field is added. [`src/open_ems/engine/models.py`] — deferred, pre-existing

- [x] [Review][Defer] `LoadReductionAction` StrEnum members carry redundant explicit string values — `StrEnum` defaults member value to lowercase name; `reduce_ev_charge_rate = "reduce_ev_charge_rate"` is a self-assignment. Cosmetic; remove explicit values or add a comment if the serialized string matters for an external contract. [`src/open_ems/engine/rules/peak_limiting.py:14-15`] — deferred, pre-existing
