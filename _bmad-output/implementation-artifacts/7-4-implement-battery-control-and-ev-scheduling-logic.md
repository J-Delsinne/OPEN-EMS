# Story 7.4: Implement Battery Control and EV Scheduling Logic

Status: done

<!-- Completion note: Ultimate context engine analysis completed - comprehensive developer guide created. -->

## Story

As a developer,
I want the decision engine to produce battery charge/discharge intents and EV session intents using capability summaries and a homeowner override flag supplied in the evaluation input,
so that battery and EV decisions respect configured constraints and homeowner preferences without the decision engine accessing adapters or web session state.

## Acceptance Criteria

**AC1 - Battery and EV control inputs are explicit, frozen, and supplied through `EvaluationInput`**
Given the decision engine evaluates battery control and EV scheduling
When the input contract is extended
Then `src/open_ems/engine/models.py` defines frozen Pydantic v2 context models using `ConfigDict(frozen=True, extra="forbid")`:
- `BatteryControlContext`
- `EVChargingWindow`
- `EVSchedulingContext`

And `EvaluationInput` gains required fields:
- `battery_control: BatteryControlContext`
- `ev_scheduling: EVSchedulingContext`

And `EvaluationInput.from_snapshot()` accepts keyword-only `battery_control` and `ev_scheduling` arguments and copies them into the model
And existing required fields `peak_context` and `strategy` remain unchanged
And existing required-slot validation for `inverter` and `grid_meter` is preserved.

**AC2 - Context model fields and validation**
Given `BatteryControlContext` is constructed
When validation runs
Then it contains:
- `reserve_floor_percent: float` constrained to `0 <= value <= 100`
- `capability_profile: DeviceCapabilityProfile | None`

Given `EVChargingWindow` is constructed
When validation runs
Then it contains:
- `start_local_time: datetime.time`
- `end_local_time: datetime.time`
- `timezone_name: NonEmptyStr`

And `start_local_time` and `end_local_time` must not be equal
And `timezone_name` must be accepted by `zoneinfo.ZoneInfo`
And same-day windows and overnight windows are both supported.

Given `EVSchedulingContext` is constructed
When validation runs
Then it contains:
- `capability_profile: DeviceCapabilityProfile | None`
- `charging_window: EVChargingWindow | None`
- `homeowner_override_active: bool`
- `evaluated_at: datetime`
- `target_charge_rate_kw: float | None`

And `evaluated_at` must be timezone-aware UTC
And `target_charge_rate_kw`, when present, must be greater than zero
And no wall-clock reads are needed by EV scheduling because the current evaluation time is supplied explicitly.

**AC3 - Battery intent contract**
Given battery control runs
When it returns a result
Then it returns a typed `BatteryIntent` from `src/open_ems/engine/rules/battery_control.py`
And the intent is a frozen Pydantic model with:
- `action: BatteryIntentAction`
- `target_power_kw: float | None`
- `reserve_floor_percent: float`
- `reason_code: NonEmptyStr`
- `source_candidate: CandidateAction | None`

And `BatteryIntentAction` is a `StrEnum` with exactly:
- `hold = "hold"`
- `charge = "charge"`
- `discharge = "discharge"`

And battery rule code emits intents only; it does not emit `Command`, `DeviceCommand`, `CommandResult`, audit events, event-log entries, database writes, or adapter calls.

**AC4 - Battery control honors reserve floor and capability summaries**
Given `evaluate_battery_control(evaluation_input, resolved_candidate)` receives a resolved `CandidateAction | None`
When the battery slot is absent, degraded, or not a `BatteryState`
Then it returns `BatteryIntent(action=BatteryIntentAction.hold, target_power_kw=None, reason_code="battery_unavailable")`

Given the resolved candidate is absent or targets a role other than `DeviceRole.battery`
When battery control runs
Then it returns a hold intent with no target power.

Given a battery candidate requires charging
When `BatteryControlContext.capability_profile.write_capabilities` contains `WriteCapability.set_charge_rate`
Then a `charge` intent may be returned
And if that write capability is absent, the result is `hold` with no target power.

Given a battery candidate requires discharging
When `battery.soc_percent <= reserve_floor_percent`
Then the result is `hold` with `reason_code="battery_reserve_floor_reached"`
And no discharge target is included.

Given a battery candidate requires discharging
When `battery.soc_percent > reserve_floor_percent` and `WriteCapability.set_discharge_rate` is present
Then a `discharge` intent may be returned
And `reserve_floor_percent` is copied into the intent so Epic 8 PolicyGuard can enforce the same floor during command dispatch.

And capability profiles are trusted only when `capability_profile.device_id == battery.device_id`; a mismatch is treated as missing capability data and returns hold.

**AC5 - EV scheduling honors charging window and homeowner override**
Given `evaluate_ev_scheduling(evaluation_input, resolved_candidate)` receives a resolved `CandidateAction | None`
When the EV charger slot is absent, degraded, or not an `EVChargerState`
Then it returns `EVChargerIntent(action=EVChargerIntentAction.hold, target_charge_rate_kw=None, reason_code="ev_charger_unavailable")`

Given the resolved candidate is absent or targets a role other than `DeviceRole.ev_charger`
When EV scheduling runs
Then it returns a hold intent with no target rate.

Given a candidate requests EV charging and a charging window is configured
When `evaluated_at` converted to `charging_window.timezone_name` is inside the configured window
Then a charge intent may be returned.

Given a candidate requests EV charging and the current local time is outside the configured window
When `homeowner_override_active` is false
Then no charge intent is returned:
- if `EVChargerState.session_active` is false, return `hold`
- if `EVChargerState.session_active` is true, return `stop`

Given `homeowner_override_active` is true
When EV scheduling runs outside the configured window
Then the window preference is bypassed and a charge intent may be returned
And the override bypasses only the window preference; it does not bypass peak-limit safety, capability limits, or Epic 8 PolicyGuard.

Given no charging window is configured
When `homeowner_override_active` is false
Then no new EV charge intent is returned
And when `homeowner_override_active` is true, EV charging may proceed subject to the same safety and capability checks.

**AC6 - EV target charge rate is included only when capability supports dynamic rate adjustment**
Given an EV charge intent is otherwise allowed
When `EVSchedulingContext.target_charge_rate_kw` is present
And `EVSchedulingContext.capability_profile.write_capabilities` contains `WriteCapability.set_ev_charge_current`
And `capability_profile.device_id == ev_charger.device_id`
Then the intent may include `target_charge_rate_kw`.

Given the capability profile is absent, mismatched, or does not include `WriteCapability.set_ev_charge_current`
When EV scheduling returns a charge intent
Then `target_charge_rate_kw` must be `None`; the intent remains binary.

Given the current peak projection is already at or above the configured peak limit
When EV scheduling evaluates any load-increasing charge candidate, including homeowner override
Then it returns no charge intent and uses `reason_code="peak_limit_blocks_ev_charge"`.

Given a target charge rate is present and dynamic-rate capability is available
When `target_charge_rate_kw` is greater than available peak headroom (`configured_peak_limit_kw - current_partial_window_projection_kw`)
Then the target rate is not included and the result is `hold` or `stop` with `reason_code="peak_headroom_insufficient"`.

Binary charge intents without a target rate are still abstract intents. They must pass through Epic 8 PolicyGuard before any command is dispatched.

**AC7 - EV intent contract**
Given EV scheduling returns a result
When it returns a typed `EVChargerIntent`
Then the intent is a frozen Pydantic model with:
- `action: EVChargerIntentAction`
- `target_charge_rate_kw: float | None`
- `homeowner_override_active: bool`
- `reason_code: NonEmptyStr`
- `source_candidate: CandidateAction | None`

And `EVChargerIntentAction` is a `StrEnum` with exactly:
- `hold = "hold"`
- `charge = "charge"`
- `stop = "stop"`

And EV scheduling emits intents only; it does not read HTTP requests, session state, templates, routes, repositories, `StateStore`, adapters, or wall-clock time.

**AC8 - Architecture boundaries and purity**
Given this story adds battery and EV rule modules
When imports are inspected
Then `battery_control.py` and `ev_scheduling.py` may import only:
- Python standard library modules
- Pydantic
- `open_ems.core` or `open_ems.core.devices`
- `open_ems.engine.models`
- `open_ems.engine.rules.energy_balancing`

And they must not import from:
- `open_ems.web`
- `open_ems.storage`
- `open_ems.adapters`
- `open_ems.services`

And they must not import `StateStore`, concrete adapter classes, repository classes, FastAPI request/session objects, or runtime command dispatch code.

**AC9 - Tests**
- Unit tests verify `BatteryControlContext`, `EVChargingWindow`, and `EVSchedulingContext` validation rules.
- Unit tests verify existing `EvaluationInput` construction rejects omitted `battery_control` and omitted `ev_scheduling`.
- Unit tests verify `EvaluationInput.from_snapshot()` requires `battery_control` and `ev_scheduling` as keyword-only arguments.
- Unit tests verify battery discharge is blocked at and below the reserve floor and replaced by hold.
- Unit tests verify battery charge/discharge intents are produced only when the matching write capability exists.
- Unit tests verify mismatched battery capability profile `device_id` is treated as missing capability data.
- Unit tests verify EV charge outside the window is suppressed unless homeowner override is active.
- Unit tests verify overnight windows such as 22:00-06:00 work correctly, with start inclusive and end exclusive.
- Unit tests verify no configured window plus no override produces no charge intent.
- Unit tests verify homeowner override does not bypass current peak overshoot.
- Unit tests verify dynamic EV rate is included only when `WriteCapability.set_ev_charge_current` is present.
- Unit tests verify target EV rate above available peak headroom produces `peak_headroom_insufficient`.
- Unit tests verify repeated calls with the same input return equal intents.
- Unit tests verify both new rule modules avoid runtime infrastructure imports using the AST import-boundary pattern from `test_peak_limiting.py` and `test_energy_balancing.py`.
- Unit tests verify the existing Story 7.1, 7.2, and 7.3 engine tests still pass with the extended `EvaluationInput`.

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` and record the baseline pass count.
  - [x] Run `uv run python -m ruff check .`.
  - [x] Run `uv run python -m ruff format --check .`.
  - [x] Run `uv run python -m mypy src/`.
  - [x] Treat unrelated pre-existing failures as baseline notes; do not hide them by changing unrelated code.

- [x] Task 1: Extend pure engine input models (AC: AC1, AC2, AC9)
  - [x] Update `src/open_ems/engine/models.py`.
  - [x] Add `BatteryControlContext`, `EVChargingWindow`, and `EVSchedulingContext`.
  - [x] Use frozen Pydantic models with `extra="forbid"`.
  - [x] Validate reserve-floor percentage and target EV charge rate with unit-bearing field names.
  - [x] Validate `EVChargingWindow.timezone_name` using `zoneinfo.ZoneInfo`.
  - [x] Validate `EVSchedulingContext.evaluated_at` using the existing UTC validation pattern in `models.py`.
  - [x] Add `battery_control` and `ev_scheduling` as required fields on `EvaluationInput`.
  - [x] Update `EvaluationInput.from_snapshot()` with keyword-only context parameters.
  - [x] Preserve `peak_context`, `strategy`, and `_required_slots_present`.

- [x] Task 2: Implement pure battery control (AC: AC3, AC4, AC8)
  - [x] Create `src/open_ems/engine/rules/battery_control.py`.
  - [x] Define `BatteryIntentAction` and `BatteryIntent`.
  - [x] Define `evaluate_battery_control(evaluation_input: EvaluationInput, resolved_candidate: CandidateAction | None) -> BatteryIntent`.
  - [x] Map 7.3 candidate action types to battery intent actions:
    - `battery_charge_from_pv` -> `charge`
    - `battery_discharge_to_avoid_import` -> `discharge`
    - `battery_support_ev` -> `discharge`
  - [x] Unknown or non-battery candidates return hold.
  - [x] Require a healthy `BatteryState`.
  - [x] Require matching `capability_profile.device_id` before reading capability data.
  - [x] Require `WriteCapability.set_charge_rate` for charge.
  - [x] Require `WriteCapability.set_discharge_rate` and `soc_percent > reserve_floor_percent` for discharge.
  - [x] Copy `reserve_floor_percent` into all returned battery intents.
  - [x] Do not create commands, dispatch, logging, database access, or adapter access.

- [x] Task 3: Implement pure EV scheduling (AC: AC5, AC6, AC7, AC8)
  - [x] Create `src/open_ems/engine/rules/ev_scheduling.py`.
  - [x] Define `EVChargerIntentAction` and `EVChargerIntent`.
  - [x] Define `evaluate_ev_scheduling(evaluation_input: EvaluationInput, resolved_candidate: CandidateAction | None) -> EVChargerIntent`.
  - [x] Map 7.3 candidate action types `permit_ev_charge` and `ev_charge` to EV charge evaluation.
  - [x] Unknown or non-EV candidates return hold.
  - [x] Require a healthy `EVChargerState`.
  - [x] Implement active-window calculation from `EVSchedulingContext.evaluated_at`, `EVChargingWindow.start_local_time`, `end_local_time`, and `timezone_name`.
  - [x] Support overnight windows where `start_local_time > end_local_time`.
  - [x] Treat start as inclusive and end as exclusive.
  - [x] Allow homeowner override to bypass only the window preference.
  - [x] Suppress charge when current peak projection is already at or above the configured peak limit.
  - [x] Include `target_charge_rate_kw` only with matching profile `device_id` and `WriteCapability.set_ev_charge_current`.
  - [x] Suppress target rates that exceed current peak headroom.
  - [x] Do not create commands, dispatch, logging, database access, web/session reads, or adapter access.

- [x] Task 4: Export stable public symbols and update existing engine tests (AC: AC1, AC7, AC9)
  - [x] Update `src/open_ems/engine/__init__.py` to export the new context models, intent enums/models, and evaluate functions.
  - [x] Update `src/open_ems/engine/rules/__init__.py` only if the project wants rule-level imports there; keep the public surface boring and explicit.
  - [x] Update existing engine test helpers in:
    - `tests/unit/engine/test_operating_mode.py`
    - `tests/unit/engine/rules/test_peak_limiting.py`
    - `tests/unit/engine/rules/test_energy_balancing.py`
  - [x] Provide deterministic default contexts in helpers so earlier Epic 7 tests still test their original behavior.
  - [x] Do not weaken any prior validation assertions.

- [x] Task 5: Add focused unit tests (AC: AC1-AC9)
  - [x] Create `tests/unit/engine/rules/test_battery_control.py`.
  - [x] Create `tests/unit/engine/rules/test_ev_scheduling.py`.
  - [x] Mirror helper-constructor style from `test_peak_limiting.py` and `test_energy_balancing.py`.
  - [x] Use fixed timezone-aware UTC datetimes from `datetime.UTC`.
  - [x] Use `zoneinfo.ZoneInfo` or equivalent stdlib logic only; no new timezone dependency.
  - [x] Add tests for capability-missing, capability-present, capability-device-id mismatch, floor boundary, EV window boundary, overnight window, override, peak suppression, and deterministic repeated calls.
  - [x] Add AST import-boundary tests for both new modules.

- [x] Task 6: Final validation (AC: all)
  - [x] Run `uv run python -m pytest tests/unit/engine/ --no-cov -q`.
  - [x] Run `uv run python -m ruff check .`.
  - [x] Run `uv run python -m ruff format --check .`.
  - [x] Run `uv run python -m mypy src/`.
  - [x] Run `uv run python -m pytest tests/ --no-cov -q`.
  - [x] Leave this story in `review` only after implementation is complete and all checked items are true.

## Dev Notes

### Assumptions and Clarifications

- "Capability summary" in the Epic 7.4 source story maps to the existing `DeviceCapabilityProfile` from `src/open_ems/core/devices.py`; do not create a second summary model unless implementation proves the existing profile is insufficient.
- `BatteryIntent.target_power_kw` is optional because Story 7.4 is still rule-level intent generation. Numeric sizing can be added only when the input supplies a specific target and capability permits it; Story 7.5 and Epic 8 still own final result composition and command translation.
- Binary EV `charge` / `stop` intents are abstract intents, not protocol commands. They must still pass through Epic 8 intent execution and PolicyGuard before reaching OCPP or any adapter.
- Homeowner override bypasses the charging-window preference only. It never bypasses peak-limit checks, battery reserve-floor checks, capability gates, PolicyGuard, or command acknowledgment handling.
- No open user questions were identified during story creation.

### Review Findings

- [x] [Review][Patch] `battery_support_ev` candidate emitted without `ev_charger.session_active` guard [`src/open_ems/engine/rules/energy_balancing.py:_evaluate_prioritize_ev`] — Battery discharge intent is approved even when no EV session is active, creating a discharge with no load sink. Fix: add `isinstance(ev_charger, EVChargerState) and ev_charger.session_active` guard before appending the `battery_support_ev` candidate (mirroring the `ev_charge` guard on the EV candidate).
- [x] [Review][Patch] Unhandled `KeyError` in `evaluate_energy_strategy` dispatch dict for unknown `EnergyStrategy` [`src/open_ems/engine/rules/energy_balancing.py:evaluate_energy_strategy`] — A future `EnergyStrategy` addition or unexpected value causes an uncaught `KeyError` that crashes the evaluation loop. Fix: use `dispatch.get(evaluation_input.strategy)` with a graceful fallback (e.g., return empty candidates or raise a descriptive error).
- [x] [Review][Defer] `resolve_conflicts` priority weight sort direction undocumented [`src/open_ems/engine/rules/energy_balancing.py:resolve_conflicts`] — deferred, pre-existing story 7.3 design; document in a future pass.
- [x] [Review][Defer] `tiebreaker_key` lexicographic sort produces alphabetically-dependent resolution order [`src/open_ems/engine/rules/energy_balancing.py:resolve_conflicts`] — deferred, pre-existing story 7.3 design.
- [x] [Review][Defer] Simultaneous charge+discharge candidates possible in `maximize_self_consumption` [`src/open_ems/engine/rules/energy_balancing.py:_evaluate_maximize_self_consumption`] — deferred, pre-existing story 7.3 design; conflict resolution handles it silently.
- [x] [Review][Defer] Action type strings are untyped literals with no central registry [`src/open_ems/engine/rules/energy_balancing.py`, `battery_control.py`, `ev_scheduling.py`] — deferred, story 7.3 design; a registry/enum can be added in a later refactor.
- [x] [Review][Defer] DST fold attribute stripped by `_is_inside_window` during clock-back transitions [`src/open_ems/engine/rules/ev_scheduling.py:_is_inside_window`] — deferred, requires a design decision on DST policy; practical EV impact is minimal.
- [x] [Review][Defer] No SOC upper-bound check before approving battery charge intent [`src/open_ems/engine/rules/battery_control.py:evaluate_battery_control`] — deferred, spec defines only reserve floor; charge ceiling is BMS responsibility.

### Current Codebase State

Story 7.4 starts from a pure decision-engine package already established by Stories 7.1 through 7.3.

Existing engine contracts:
- `src/open_ems/engine/models.py` defines `PeakContext` and `EvaluationInput` with fields for device slots, `peak_context`, and `strategy`. `from_snapshot()` already requires keyword-only `peak_context` and `strategy`. [Source: `src/open_ems/engine/models.py:26`, `src/open_ems/engine/models.py:54`, `src/open_ems/engine/models.py:75`]
- `src/open_ems/engine/operating_mode.py` defines `derive_recommended_operating_mode()`. Reuse it only if this story needs operating-mode suppression; do not reimplement the matrix. [Source: `src/open_ems/engine/operating_mode.py`]
- `src/open_ems/engine/rules/peak_limiting.py` defines `PeakLimitDecision`, `LoadReductionAction`, and `evaluate_peak_limiting()`. This story should not modify peak-limiting logic. [Source: `src/open_ems/engine/rules/peak_limiting.py:14`, `src/open_ems/engine/rules/peak_limiting.py:21`, `src/open_ems/engine/rules/peak_limiting.py:36`]
- `src/open_ems/engine/rules/energy_balancing.py` defines `CandidateAction`, `PriorityBand`, `StrategyEvaluation`, `evaluate_energy_strategy()`, and `resolve_conflicts()`. Story 7.4 should reuse `CandidateAction` instead of inventing a parallel candidate type. [Source: `src/open_ems/engine/rules/energy_balancing.py:42`, `src/open_ems/engine/rules/energy_balancing.py:73`, `src/open_ems/engine/rules/energy_balancing.py:99`]

Existing domain and capability contracts:
- `src/open_ems/core/devices.py` defines `DeviceRole`, `BatteryState`, `EVChargerState`, `DegradedDeviceState`, `WriteCapability`, and `DeviceCapabilityProfile`. [Source: `src/open_ems/core/devices.py:73`, `src/open_ems/core/devices.py:100`, `src/open_ems/core/devices.py:118`, `src/open_ems/core/devices.py:164`, `src/open_ems/core/devices.py:193`, `src/open_ems/core/devices.py:210`]
- `DeviceCapabilityProfile.write_capabilities` is the existing capability summary. Use it; do not call `DeviceAdapter.get_capabilities()` from engine rules.
- `WriteCapability.set_charge_rate` and `set_discharge_rate` apply to battery charge/discharge. `WriteCapability.set_ev_charge_current` gates EV dynamic rate adjustment.
- `DeviceCapabilityProfile` is exported from `open_ems.core`; `WriteCapability` is not currently exported there. Import `WriteCapability` from `open_ems.core.devices` unless you intentionally expand the core public surface.

Current worktree note:
- `git status --short` shows Story 7.3 implementation files and sprint status are already uncommitted. Treat those as existing user/workflow state. Do not revert or overwrite them.

### Story Foundation

Epic 7 owns pure decision logic only. The engine evaluates typed input objects built from StateStore snapshots and produces typed outputs without adapter calls, database writes, or web/session access. [Source: `_bmad-output/planning-artifacts/epics.md:1528`]

Story 7.4 specifically adds battery control and EV scheduling. Its source acceptance criteria require:
- battery intents using a capability summary in evaluation input
- reserve-floor blocking for discharge
- EV charge only inside the configured charging window unless homeowner override is supplied
- dynamic charge rate only when the charger capability summary supports it
- no adapter, web, or session access during evaluation [Source: `_bmad-output/planning-artifacts/epics.md:1622`]

Cross-story constraints for all Epic 7 stories are still active:
- no adapter calls, no database writes, no web/session access
- `PeakContext` is the only peak input
- capability summaries enter through evaluation input
- homeowner override state enters as a flag in evaluation input
- StateStore remains UI authority; Epic 8 applies runtime fail-safe transitions [Source: `_bmad-output/planning-artifacts/epics.md:1686`]

### Previous Story Intelligence

Story 7.3 established the strategy candidate and conflict-resolution layer:
- `EnergyStrategy` lives in `core/state.py`, next to `SystemOperatingMode`.
- `EvaluationInput` was extended by adding a required field and updating prior tests with deterministic defaults.
- `CandidateAction` is the rule-level action record. It has `role`, `action_type`, `priority_band`, `priority_weight`, `tiebreaker_key`, and `source_rule`.
- `resolve_conflicts()` already produces at most one `CandidateAction` per `DeviceRole`.
- Do not create a generic `Intent` base class that conflicts with Story 7.5.
- Do not modify `PeakLimitDecision` or `LoadReductionAction`; Story 7.5 will integrate peak-limiting output with the broader candidate stream.

Review patches already applied in Story 7.3:
- conflict resolution now includes `action_type` as the final deterministic tie-breaker
- self-consumption requires positive PV before emitting `battery_charge_from_pv`
- tests cover omitted `from_snapshot(strategy=...)`, fully tied resolver inputs, and zero-PV/negative-AC inverter state

Deferred items still relevant:
- `current_interval_elapsed_seconds=900` boundary differentiation is deferred to Story 7.5 / Epic 8.
- `_require_utc` in `engine/models.py` is a private helper used once; do not promote it unless this story's `EVSchedulingContext.evaluated_at` gives it a second legitimate use.
- `DegradedDeviceState.role` is still not cross-validated against the positional `EvaluationInput` slot. Do not broaden this story to fix it.

### Recent Git Intelligence

- `1d651bb 7.2` added `PeakContext`, extended `EvaluationInput`, created `engine/rules/peak_limiting.py`, and established the AST import-boundary test pattern.
- `6f96944 7.1` created `src/open_ems/engine/`, `EvaluationInput`, `derive_recommended_operating_mode()`, and the first engine tests.
- `2ddc23f 6.3` and `98cbc7a 6.2` added config audit and event-log foundations. This story must not add audit/event-log writes; runtime logging belongs to Epic 8 and later result handling.

### Architecture Compliance

Safety and command boundary:
- All actual device commands must eventually pass through `PolicyGuard.authorize_and_dispatch()`. Story 7.4 produces abstract intents only. [Source: `_bmad-output/planning-artifacts/architecture.md:381`]
- Do not call concrete adapters from engine rules. Do not create `Command`, `DeviceCommand`, or `CommandResult`.
- PolicyGuard remains the final enforcement point for command-level safety. The pure engine should still suppress obviously unsafe intents visible from `PeakContext` and reserve-floor inputs.

State and import boundary:
- Decision engine and web layer read immutable snapshots; no component mutates snapshots directly. [Source: `_bmad-output/planning-artifacts/architecture.md:401`]
- Architecture names `engine/rules/battery_control.py` for FR9 and `engine/rules/ev_scheduling.py` for FR10/FR12. Create those exact files. [Source: `_bmad-output/planning-artifacts/architecture.md:816`]
- Import boundaries prohibit engine rules from importing web, storage, adapters, or services.

Peak-safety boundary:
- BAD-1 separates the in-memory partial-window projection from the persisted billing record. Story 7.4 reads only `EvaluationInput.peak_context`; it must not read or write `peak_intervals`. [Source: `_bmad-output/planning-artifacts/architecture.md:1045`]
- EV override does not bypass peak safety. If the current projection is already at or above limit, no EV charge intent should be emitted.

Degradation boundary:
- BAD-2 says grid-meter degradation removes the peak safety guarantee and conservative/fail-safe operation suppresses load-increasing commands. Story 7.4 should preserve the existing operating-mode derivation behavior and avoid pretending stale grid data is safe. [Source: `_bmad-output/planning-artifacts/architecture.md:1080`]

UX and user-action context:
- Homeowner EV override is intended to be immediate and single-tap, but the UI explicitly communicates that the peak limit remains active. [Source: `_bmad-output/planning-artifacts/ux-design-specification.md:96`]
- Backend POST actions return within 2 seconds and device confirmation arrives asynchronously via SSE in later web/runtime stories. Story 7.4 must not block on device confirmation. [Source: `_bmad-output/planning-artifacts/ux-design-specification.md:1814`]
- Every user action is later logged server-side, but this story does not implement logging. [Source: `_bmad-output/planning-artifacts/ux-design-specification.md:1916`]

### Implementation Guardrails

- Keep rule functions synchronous and pure.
- Do not use `datetime.now()`, `time.time()`, `random`, UUIDs, database reads, environment variables, files, sockets, adapters, `StateStore`, or FastAPI request/session state.
- Use `zoneinfo.ZoneInfo` from the Python standard library for EV window conversion. The project already depends on `tzdata`.
- Keep all datetimes timezone-aware UTC at the input boundary.
- Use `ConfigDict(frozen=True, extra="forbid")` on all new Pydantic models.
- Use explicit reason codes. They are not final Story 7.5 decision reasons, but they prevent placeholder behavior and make tests unambiguous.
- Do not invent new core capability enums unless unavoidable. For this story, `WriteCapability.set_ev_charge_current` is enough to distinguish dynamic rate from binary EV intent.
- Do not add migrations, repositories, web routes, templates, services, command queues, or PolicyGuard.
- Do not change homeowner/installer state serializers.
- Do not modify `energy_balancing.py` unless an import/export correction is truly required.
- Do not modify `peak_limiting.py`; use `PeakContext` through `EvaluationInput`.

### Suggested Module Shape

This shape is a guide, not a requirement. The final code must provide equivalent type safety, purity, and tests.

```python
# src/open_ems/engine/models.py
class BatteryControlContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reserve_floor_percent: float = Field(ge=0.0, le=100.0)
    capability_profile: DeviceCapabilityProfile | None = None


class EVChargingWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_local_time: time
    end_local_time: time
    timezone_name: NonEmptyStr


class EVSchedulingContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capability_profile: DeviceCapabilityProfile | None = None
    charging_window: EVChargingWindow | None = None
    homeowner_override_active: bool = False
    evaluated_at: datetime
    target_charge_rate_kw: float | None = Field(default=None, gt=0.0)
```

```python
# src/open_ems/engine/rules/battery_control.py
def evaluate_battery_control(
    evaluation_input: EvaluationInput,
    resolved_candidate: CandidateAction | None,
) -> BatteryIntent:
    ...
```

```python
# src/open_ems/engine/rules/ev_scheduling.py
def evaluate_ev_scheduling(
    evaluation_input: EvaluationInput,
    resolved_candidate: CandidateAction | None,
) -> EVChargerIntent:
    ...
```

### Testing Standards

- Mirror source structure:
  - `src/open_ems/engine/rules/battery_control.py` maps to `tests/unit/engine/rules/test_battery_control.py`
  - `src/open_ems/engine/rules/ev_scheduling.py` maps to `tests/unit/engine/rules/test_ev_scheduling.py`
- Reuse helper style from `tests/unit/engine/rules/test_peak_limiting.py` and `tests/unit/engine/rules/test_energy_balancing.py`.
- Use `datetime.UTC` and fixed datetimes in all tests.
- Use stdlib `zoneinfo.ZoneInfo` only.
- Keep tests synchronous; no async fixtures are needed.
- Include AST import-boundary tests for both new rule modules.
- Keep line length at 100 characters and follow `pyproject.toml` Ruff/mypy settings.

### Latest Technical Information

Checked on 2026-05-05:
- The project requires Python `>=3.12`, so `enum.StrEnum`, `zoneinfo`, and modern `typing` syntax are available. Python docs confirm `StrEnum` was added in Python 3.11 and Python 3.12 changed enum containment checks to allow raw values without raising `TypeError`. Source: https://docs.python.org/3.12/library/enum.html
- The project uses Pydantic v2 style throughout. Pydantic config docs confirm `extra="forbid"` rejects extra inputs and `frozen=True` disables normal attribute assignment while generating hash support when fields are hashable. Source: https://pydantic.dev/docs/validation/latest/api/pydantic/config/
- Pydantic standard-library type docs cover enum validation, so new `StrEnum` intent actions should follow the same style as `EnergyStrategy` and `PriorityBand`. Source: https://pydantic.dev/docs/validation/latest/api/pydantic/standard_library_types/
- `pytest` 9.0.3 is the current locked test runner in `pyproject.toml`; pytest docs list 9.0.3 as released on 2026-04-07 with bug fixes including CVE-2025-71176. Source: https://docs.pytest.org/en/latest/changelog.html
- Ruff formatter docs confirm `ruff format --check` checks formatting without writing files, matching this repository's validation flow. Source: https://docs.astral.sh/ruff/formatter/
- No new third-party dependency or external API is required.

### Project Structure Notes

Expected new files:
- `src/open_ems/engine/rules/battery_control.py`
- `src/open_ems/engine/rules/ev_scheduling.py`
- `tests/unit/engine/rules/test_battery_control.py`
- `tests/unit/engine/rules/test_ev_scheduling.py`

Expected updated files:
- `src/open_ems/engine/models.py`
- `src/open_ems/engine/__init__.py`
- `src/open_ems/engine/rules/__init__.py` if rule-level exports are desired
- `tests/unit/engine/test_operating_mode.py`
- `tests/unit/engine/rules/test_peak_limiting.py`
- `tests/unit/engine/rules/test_energy_balancing.py`

Expected files not to modify:
- `src/open_ems/web/**`
- `src/open_ems/storage/**`
- `src/open_ems/adapters/**`
- `src/open_ems/services/**`
- `migrations/**`
- `src/open_ems/engine/rules/peak_limiting.py`

### Anti-Patterns to Avoid

- Do not call `DeviceAdapter.get_capabilities()` from decision-engine code.
- Do not call `StateStore.get_snapshot()` or `StateStore.publish()` from rule modules.
- Do not read web requests, cookies, sessions, forms, or HTTP state for homeowner override.
- Do not use wall-clock time. Use `EVSchedulingContext.evaluated_at`.
- Do not create or dispatch commands.
- Do not log from pure rule code.
- Do not silently treat missing capability summaries as full support.
- Do not add dynamic-rate targets when `set_ev_charge_current` is absent.
- Do not let homeowner override bypass peak-limit checks.
- Do not represent binary EV start/stop as a dynamic rate command.
- Do not create generic `Intent`, `DeviceIntent`, or `EvaluationResult` contracts in this story; Story 7.5 owns final result composition.

### References

- `_bmad-output/planning-artifacts/epics.md:1528` - Epic 7 overview and purity boundary.
- `_bmad-output/planning-artifacts/epics.md:1622` - Story 7.4 source acceptance criteria.
- `_bmad-output/planning-artifacts/epics.md:1686` - Epic 7 cross-story constraints.
- `_bmad-output/planning-artifacts/prd.md` - FR7, FR9, FR10, FR11b, FR12, FR13, FR15, NFR-P1, NFR-R2.
- `_bmad-output/planning-artifacts/architecture.md:381` - PolicyGuard command boundary.
- `_bmad-output/planning-artifacts/architecture.md:816` - Expected engine rule file locations.
- `_bmad-output/planning-artifacts/architecture.md:1045` - BAD-1 peak window split.
- `_bmad-output/planning-artifacts/architecture.md:1080` - BAD-2 degradation matrix.
- `_bmad-output/planning-artifacts/ux-design-specification.md:96` - EV override UX and peak-limit notice.
- `_bmad-output/planning-artifacts/ux-design-specification.md:1814` - Action response contract.
- `_bmad-output/implementation-artifacts/4-4-implement-device-capability-profile-system-and-capability-gate.md` - capability registry and `DeviceCapabilityProfile` behavior.
- `_bmad-output/implementation-artifacts/7-1-implement-systemoperatingmode-derivation-and-degradation-matrix.md` - operating mode contract.
- `_bmad-output/implementation-artifacts/7-2-implement-peak-limiting-logic-consuming-peakcontext.md` - `PeakContext`, peak-limiting rule, and import-boundary test pattern.
- `_bmad-output/implementation-artifacts/7-3-implement-energy-strategy-evaluation-and-deterministic-conflict-resolution.md` - `EnergyStrategy`, `CandidateAction`, and conflict-resolution contract.
- `src/open_ems/core/devices.py` - device states, capability models, write capabilities, and sign conventions.
- `src/open_ems/engine/models.py` - current `EvaluationInput` and `PeakContext`.
- `src/open_ems/engine/rules/energy_balancing.py` - `CandidateAction` and `resolve_conflicts()`.
- `src/open_ems/engine/rules/peak_limiting.py` - existing peak-safety rule.
- `tests/unit/engine/rules/test_peak_limiting.py` - helper and AST import-boundary patterns.
- `tests/unit/engine/rules/test_energy_balancing.py` - candidate-action helper and deterministic tests.
- `pyproject.toml` - Python, dependency, Ruff, mypy, and pytest configuration.
- Python enum docs: https://docs.python.org/3.12/library/enum.html
- Pydantic config docs: https://pydantic.dev/docs/validation/latest/api/pydantic/config/
- Pydantic standard-library type docs: https://pydantic.dev/docs/validation/latest/api/pydantic/standard_library_types/
- pytest changelog: https://docs.pytest.org/en/latest/changelog.html
- Ruff formatter docs: https://docs.astral.sh/ruff/formatter/

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Implementation Plan

- Extend frozen engine input context models first, with tests proving validation and required `EvaluationInput` fields.
- Add pure battery and EV rule modules that consume `EvaluationInput` and resolved `CandidateAction` only.
- Export the stable symbols and update existing Epic 7 test helpers with deterministic default contexts.
- Add focused rule tests, then run the full story validation suite before moving the story to review.

### Debug Log References

- 2026-05-05: Pre-story baseline `uv run python -m pytest tests/ --no-cov -q` -> 670 passed, 43 warnings.
- 2026-05-05: Pre-story baseline `uv run python -m ruff check .` -> passed.
- 2026-05-05: Pre-story baseline `uv run python -m ruff format --check .` -> 139 files already formatted.
- 2026-05-05: Pre-story baseline `uv run python -m mypy src/` -> success, 67 source files checked.
- 2026-05-05: Task 1 red test `uv run python -m pytest tests/unit/engine/test_models.py --no-cov -q` -> failed on missing context classes.
- 2026-05-05: Task 1 model tests `uv run python -m pytest tests/unit/engine/test_models.py --no-cov -q` -> 9 passed.
- 2026-05-05: Task 1 engine regression `uv run python -m pytest tests/unit/engine/ --no-cov -q` -> 81 passed.
- 2026-05-05: Task 1 full regression `uv run python -m pytest tests/ --no-cov -q` -> 679 passed, 43 warnings.
- 2026-05-05: Task 1 quality checks `ruff check`, `ruff format --check`, and `mypy src/` -> passed.
- 2026-05-05: Task 2 red test `uv run python -m pytest tests/unit/engine/rules/test_battery_control.py --no-cov -q` -> failed on missing module.
- 2026-05-05: Task 2 battery tests `uv run python -m pytest tests/unit/engine/rules/test_battery_control.py --no-cov -q` -> 14 passed.
- 2026-05-05: Task 2 engine regression `uv run python -m pytest tests/unit/engine/ --no-cov -q` -> 95 passed.
- 2026-05-05: Task 2 full regression `uv run python -m pytest tests/ --no-cov -q` -> 693 passed, 43 warnings.
- 2026-05-05: Task 2 quality checks `ruff check`, `ruff format --check`, and `mypy src/` -> passed.
- 2026-05-05: Task 3 red test `uv run python -m pytest tests/unit/engine/rules/test_ev_scheduling.py --no-cov -q` -> failed on missing module.
- 2026-05-05: Task 3 EV tests `uv run python -m pytest tests/unit/engine/rules/test_ev_scheduling.py --no-cov -q` -> 21 passed.
- 2026-05-05: Task 3 engine regression `uv run python -m pytest tests/unit/engine/ --no-cov -q` -> 116 passed.
- 2026-05-05: Task 3 full regression `uv run python -m pytest tests/ --no-cov -q` -> 714 passed, 43 warnings.
- 2026-05-05: Task 3 quality checks `ruff check`, `ruff format --check`, and `mypy src/` -> passed.
- 2026-05-05: Task 4 red export test `uv run python -m pytest tests/unit/engine/test_models.py::test_engine_package_exports_new_battery_and_ev_contracts --no-cov -q` -> failed on missing package exports.
- 2026-05-05: Task 4 export test `uv run python -m pytest tests/unit/engine/test_models.py::test_engine_package_exports_new_battery_and_ev_contracts --no-cov -q` -> passed.
- 2026-05-05: Task 4 engine regression `uv run python -m pytest tests/unit/engine/ --no-cov -q` -> 117 passed.
- 2026-05-05: Task 4 full regression `uv run python -m pytest tests/ --no-cov -q` -> 715 passed, 43 warnings.
- 2026-05-05: Task 4 quality checks `ruff check`, `ruff format --check`, and `mypy src/` -> passed.
- 2026-05-05: Task 5 focused tests `uv run python -m pytest tests/unit/engine/rules/test_battery_control.py tests/unit/engine/rules/test_ev_scheduling.py --no-cov -q` -> 37 passed.
- 2026-05-05: Task 5 engine regression `uv run python -m pytest tests/unit/engine/ --no-cov -q` -> 119 passed.
- 2026-05-05: Task 5 full regression `uv run python -m pytest tests/ --no-cov -q` -> 717 passed, 43 warnings.
- 2026-05-05: Task 5 quality checks `ruff check`, `ruff format --check`, and `mypy src/` -> passed.
- 2026-05-05: Task 6 final `uv run python -m pytest tests/unit/engine/ --no-cov -q` -> 119 passed.
- 2026-05-05: Task 6 final `uv run python -m ruff check .` -> passed.
- 2026-05-05: Task 6 final `uv run python -m ruff format --check .` -> 144 files already formatted.
- 2026-05-05: Task 6 final `uv run python -m mypy src/` -> success, 69 source files checked.
- 2026-05-05: Task 6 final `uv run python -m pytest tests/ --no-cov -q` -> 717 passed, 43 warnings.
- 2026-05-05: Step 9 completion scan found no unchecked task boxes.
- 2026-05-05: Step 9 full regression `uv run python -m pytest tests/ --no-cov -q` -> 717 passed, 43 warnings.

### Completion Notes List

- Completed Task 0 pre-story quality gate with all baseline validations passing; existing warnings are Starlette TestClient cookie deprecation warnings.
- Completed Task 1 by adding frozen battery/EV context models, UTC/timezone validation, required `EvaluationInput` context fields, and keyword-only `from_snapshot()` context arguments. Existing Epic 7 tests now use deterministic default contexts.
- Completed Task 2 by adding pure battery intents, candidate-action mapping, reserve-floor blocking, capability-profile device matching, and AST-covered import-boundary tests.
- Completed Task 3 by adding pure EV charger intents, window and overnight-window enforcement, homeowner override handling, peak-limit blocking, dynamic-rate capability gating, and AST-covered import-boundary tests.
- Completed Task 4 by exporting the new context and intent contracts from `open_ems.engine` and preserving prior Epic 7 helper behavior with deterministic default contexts. `engine.rules.__init__` was left unchanged because the existing rule-level surface is intentionally narrow and does not export Story 7.3 rule symbols either.
- Completed Task 5 by broadening focused battery and EV rule coverage for capability presence/mismatch, reserve-floor boundaries, same-day and overnight window boundaries, override, peak suppression, dynamic target rates, repeated-call determinism, and import boundaries.
- Completed Task 6 final validation with engine regression, full regression, Ruff, format, and mypy all passing.
- Definition-of-done validation passed: all tasks/subtasks are complete, acceptance criteria are covered by implementation and tests, full regression passes, quality checks pass, and the File List reflects Story 7.4 implementation files.

### File List

- `_bmad-output/implementation-artifacts/7-4-implement-battery-control-and-ev-scheduling-logic.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`
- `src/open_ems/engine/__init__.py`
- `src/open_ems/engine/models.py`
- `src/open_ems/engine/rules/battery_control.py`
- `src/open_ems/engine/rules/ev_scheduling.py`
- `tests/unit/engine/test_models.py`
- `tests/unit/engine/test_operating_mode.py`
- `tests/unit/engine/rules/test_battery_control.py`
- `tests/unit/engine/rules/test_ev_scheduling.py`
- `tests/unit/engine/rules/test_peak_limiting.py`
- `tests/unit/engine/rules/test_energy_balancing.py`

## Change Log

- 2026-05-05: Story created and marked ready for dev.
- 2026-05-05: Started implementation and completed pre-story quality gate.
- 2026-05-05: Extended decision-engine input models with battery and EV scheduling contexts.
- 2026-05-05: Added pure battery-control intent evaluation with reserve-floor and capability enforcement.
- 2026-05-05: Added pure EV scheduling intent evaluation with window, override, peak, and dynamic-rate gates.
- 2026-05-05: Exported new battery and EV engine contracts from the public engine package.
- 2026-05-05: Added focused battery-control and EV-scheduling unit test coverage.
- 2026-05-05: Completed final validation for Story 7.4.
- 2026-05-05: Marked Story 7.4 ready for review after completion validation.
