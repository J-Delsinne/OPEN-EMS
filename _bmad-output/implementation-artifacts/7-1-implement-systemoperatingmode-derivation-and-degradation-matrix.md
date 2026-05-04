# Story 7.1: Implement SystemOperatingMode Derivation and Degradation Matrix

Status: done

<!-- Completion note: Ultimate context engine analysis completed - comprehensive developer guide created. -->

## Story

As a developer,
I want the decision engine to derive a recommended `SystemOperatingMode` from the evaluation input before each cycle,
so that all energy decisions are conditioned on an explicit, deterministic mode rather than ad hoc per-device checks scattered through the decision logic.

## Acceptance Criteria

**AC1 - Recommended mode derivation**
Given the decision engine begins an evaluation cycle
When `SystemOperatingMode` is derived from the evaluation input
Then the degradation matrix maps the set of `DegradedDeviceState` entries in the input to exactly one of `normal`, `degraded`, `conservative`, or `fail_safe`
And the implementation reuses the existing `open_ems.core.SystemOperatingMode` enum; it does not create a duplicate enum or string-only contract.

**AC2 - Degradation matrix**
Given the evaluation input contains current device slots for `inverter`, `battery`, `ev_charger`, and `grid_meter`
When the recommended mode is derived
Then the rules are applied deterministically as follows:

| Degraded roles present | Recommended mode | Notes |
|---|---|---|
| none | `normal` | Optional roles with `None` do not count as degraded before they have ever been configured or observed. |
| `inverter` only | `degraded` | PV optimization is reduced; peak safety can continue from grid data. |
| `battery` only | `degraded` | Battery commands must later be suppressed; EV scheduling and peak monitoring can continue. |
| `ev_charger` only | `degraded` | EV commands must later be suppressed; battery and grid-meter based peak monitoring can continue. |
| any combination of `inverter`, `battery`, and `ev_charger` without `grid_meter` | `degraded` | Core grid safety data remains available. |
| `grid_meter` only | `conservative` | Loss or staleness of grid draw data removes the peak-safety guarantee. |
| `grid_meter` plus any other degraded device role | `fail_safe` | A critical safety input plus another degraded adapter prevents safe control decisions. |
| all configured device roles degraded | `fail_safe` | Covered by the grid-meter plus second degraded role rule when grid meter is degraded. |

And a stale DSMR meter represented as `DegradedDeviceState(role=DeviceRole.grid_meter, reason="dsmr_stale")` is treated as grid-meter degradation and produces at least `conservative`
And external forecast or tariff unavailability is not modeled as `DegradedDeviceState` in this story and must not affect core `SystemOperatingMode` derivation.

**AC3 - Determinism**
Given the same logical set of degraded roles is supplied in any input ordering
When derivation is called repeatedly
Then the same `SystemOperatingMode` is returned every time
And the implementation does not depend on unordered dictionary, set, random, wall-clock, database, adapter, or web-session behavior.

**AC4 - First decision input**
Given future Epic 7 rules evaluate peak limiting, strategy selection, battery control, and EV scheduling
When they consume evaluation input
Then the recommended operating mode is the first derived value those rules can inspect
And this story exposes the derivation in a module and function name that later rule modules can call directly.

**AC5 - Ownership boundary**
Given `StateStore` already owns the UI-readable current `SystemSnapshot.operating_mode`
When the decision engine derives a mode
Then it derives a recommended mode only for engine decision purposes
And it does not call `StateStore.publish()`
And it does not mutate the current `SystemSnapshot`
And it does not apply runtime fail-safe transitions
And Epic 8 remains responsible for applying fail-safe transitions from the later `EvaluationResult.recommended_operating_mode`.

**AC6 - Tests**
- Unit tests cover `normal`, `degraded`, `conservative`, and `fail_safe` outcomes with concrete `DegradedDeviceState` combinations.
- Unit tests verify determinism across repeated calls and differently ordered input mappings.
- Unit tests verify that optional `None` battery and EV slots do not degrade a healthy inverter plus grid meter input.
- Unit tests verify that a healthy input derived from a `SystemSnapshot` whose current `operating_mode` is `fail_safe` can still recommend `normal`; the function must not simply echo `snapshot.operating_mode`.
- Unit tests verify that the derivation function does not require adapters, a database, a web app, or async runtime.

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` and record the baseline.
  - [x] Run `uv run python -m ruff check .`.
  - [x] Run `uv run python -m mypy src/`.

- [x] Task 1: Add the minimal engine package surface (AC: AC1, AC4, AC5)
  - [x] Create `src/open_ems/engine/__init__.py`.
  - [x] Create `src/open_ems/engine/models.py` for a minimal frozen `EvaluationInput` model.
  - [x] Create `src/open_ems/engine/operating_mode.py` for the pure derivation function.
  - [x] Export the public engine symbols from `src/open_ems/engine/__init__.py`.
  - [x] Do not create a runtime control loop, `PolicyGuard`, command dispatch, event logging, or database integration in this story.

- [x] Task 2: Define `EvaluationInput` without widening scope (AC: AC1, AC4, AC5)
  - [x] Include device slots for `inverter`, `battery`, `ev_charger`, and `grid_meter` using the existing core device state types.
  - [x] Prefer a frozen Pydantic v2 model with `ConfigDict(frozen=True, extra="forbid")`, matching existing core model style.
  - [x] Add a `from_snapshot(snapshot: SystemSnapshot) -> EvaluationInput` constructor or equivalent helper.
  - [x] Copy the device slots from the snapshot; do not retain or mutate `StateStore`.
  - [x] Do not add `PeakContext`, strategy, capability summaries, homeowner override state, or `EvaluationResult` in this story; those are Stories 7.2 through 7.5.

- [x] Task 3: Implement the degradation matrix (AC: AC1, AC2, AC3)
  - [x] Implement `derive_recommended_operating_mode(input: EvaluationInput) -> SystemOperatingMode` or an equivalently explicit name.
  - [x] Build the degraded role set only from slots that are instances of `DegradedDeviceState`.
  - [x] Treat `None` as absent or not-yet-configured, not degraded.
  - [x] Use `frozenset[DeviceRole]` or another canonical role set for matrix evaluation.
  - [x] Apply the rule order: no degraded roles -> `normal`; grid meter plus any other degraded role -> `fail_safe`; grid meter alone -> `conservative`; all other degraded role sets -> `degraded`.
  - [x] Do not parse `DegradedDeviceState.reason` to reuse UI failure rules from `core.state`; the matrix is role-driven for Story 7.1.

- [x] Task 4: Preserve existing StateStore and UI behavior (AC: AC5)
  - [x] Do not change `src/open_ems/core/state.py` enum values or `OPERATING_MODE_GLOBAL_STATE`.
  - [x] Do not change `src/open_ems/core/state_store.py` publish semantics or default `operating_mode`.
  - [x] Do not change homeowner or installer serializers in `src/open_ems/web/state_serialization.py`.
  - [x] If a small helper is needed in `core`, keep it I/O-free and avoid imports from `engine`, `adapters`, `storage`, `services`, or `web`.

- [x] Task 5: Add focused unit tests (AC: AC1-AC6)
  - [x] Create `tests/unit/engine/__init__.py`.
  - [x] Create `tests/unit/engine/test_operating_mode.py`.
  - [x] Test no degraded roles with healthy inverter and grid meter returns `SystemOperatingMode.normal`.
  - [x] Test optional `battery=None` and `ev_charger=None` with healthy required roles remains `normal`.
  - [x] Parametrize single-role degraded cases for inverter, battery, and EV charger as `degraded`.
  - [x] Test combinations of inverter, battery, and EV charger without grid meter as `degraded`.
  - [x] Test grid meter degraded with reason `dsmr_stale` returns `conservative`.
  - [x] Parametrize grid meter plus inverter, battery, and EV charger degraded cases as `fail_safe`.
  - [x] Test all four roles degraded returns `fail_safe`.
  - [x] Test determinism by passing logically identical role sets through differently ordered mappings and repeated calls.
  - [x] Test `EvaluationInput.from_snapshot()` does not echo `snapshot.operating_mode`.

- [x] Task 6: Final validation (AC: all)
  - [x] Run `uv run python -m ruff check .`.
  - [x] Run `uv run python -m ruff format --check .`.
  - [x] Run `uv run python -m mypy src/`.
  - [x] Run `uv run python -m pytest tests/unit/engine/test_operating_mode.py --no-cov -q`.
  - [x] Run `uv run python -m pytest tests/ --no-cov -q`.
  - [x] Leave this story in `review` only after implementation is complete and all checked items are true.

## Dev Notes

### Scope and Non-Scope

This story creates the first pure decision-engine surface. The output is a recommended operating mode, not a runtime transition.

In scope:
- A minimal typed `EvaluationInput`.
- A pure deterministic function that maps degraded device roles to `SystemOperatingMode`.
- Unit tests for all matrix outcomes and the StateStore ownership boundary.

Out of scope:
- `EvaluationResult` and decision reasons. Story 7.5 owns that contract.
- Peak limiting and `PeakContext`. Story 7.2 owns that contract.
- Battery and EV intents. Story 7.4 owns those contracts.
- Runtime control loop, `PolicyGuard`, command dispatch, command acknowledgments, recovery events, or watchdog behavior. Epic 8 owns runtime application.
- External forecast or tariff degradation. Epic 13 enriches optimization inputs; external source unavailability must not affect core `SystemOperatingMode` in Story 7.1.

### Existing Code to Preserve

`src/open_ems/core/state.py`
- Current state: defines `SystemOperatingMode` with exact values `normal`, `degraded`, `conservative`, and `fail_safe`; defines `GlobalState`, `ComponentState`, `SystemSnapshot`, and UI/global derivation helpers.
- Change expected: none.
- Preserve: `derive_global_state()` and `_is_failed_reason()` are UI/global-state helpers, not the decision-engine degradation matrix.

`src/open_ems/core/devices.py`
- Current state: defines `DeviceRole`, concrete normalized device states, and `DegradedDeviceState`.
- Change expected: none.
- Preserve: `DegradedDeviceState` is the domain degraded state. Do not import protocol-level degraded states into the engine.

`src/open_ems/core/state_store.py`
- Current state: holds immutable `SystemSnapshot` objects; `publish()` can accept an optional runtime `operating_mode` but otherwise preserves the previous mode.
- Change expected: none.
- Preserve: the decision engine must not mutate the store or publish snapshots in this story.

`src/open_ems/web/state_serialization.py`
- Current state: serializes homeowner and installer snapshots from `SystemSnapshot.operating_mode` and role-filtered device data.
- Change expected: none.
- Preserve: homeowner degraded reasons remain hidden; installer diagnostics remain allowlisted.

### Implementation Guardrails

- Put new decision-engine code under `src/open_ems/engine/`, which does not exist yet.
- Keep the engine import boundary clean: `engine` may import `open_ems.core`, but must not import `open_ems.web`, `open_ems.storage`, or concrete adapter modules.
- Keep derivation synchronous and side-effect free. It should be callable from a unit test without `pytest.mark.asyncio`.
- Do not use `StateStore.get_snapshot()` inside the derivation function. Accept an already-built `EvaluationInput`.
- Do not reuse `GlobalState` or `ComponentState` to derive engine mode. UI state and engine operating mode are related but not equivalent.
- Do not treat stale component overlays as degraded unless the slot itself is a `DegradedDeviceState`. Current StateStore can mark a successful old device value as `ComponentState.stale`; Story 7.1 should only consume device slots.
- A DSMR stale grid meter should enter the engine as `DegradedDeviceState(role=DeviceRole.grid_meter, reason="dsmr_stale")`; that produces `conservative`.
- Use canonical role sets, not iteration order, for matrix selection.
- Avoid new dependencies. The current stack already has Python 3.12, Pydantic v2, pytest, ruff, and mypy.

### Suggested Module Shape

```python
# src/open_ems/engine/models.py
class EvaluationInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    inverter: DeviceSlot
    battery: DeviceSlot
    ev_charger: DeviceSlot
    grid_meter: DeviceSlot

    @classmethod
    def from_snapshot(cls, snapshot: SystemSnapshot) -> EvaluationInput:
        ...
```

```python
# src/open_ems/engine/operating_mode.py
def derive_recommended_operating_mode(input: EvaluationInput) -> SystemOperatingMode:
    ...
```

Keep public names direct and boring. Future Story 7.5 can compose this into `EvaluationResult.recommended_operating_mode`.

### Previous Story Intelligence

There is no previous Epic 7 story. Relevant recent work:
- Story 5.1 established immutable `SystemSnapshot`, `SystemOperatingMode`, `StateStore`, and StateStore tests. Reuse those core types instead of inventing engine-specific duplicates.
- Story 5.2 and 5.3 established role-filtered SSE and HTMX serialization. Do not change web serialization while adding engine internals.
- Story 6.1 through 6.3 established `ObservabilityService`, append-only event logging, installer notes, and config audit storage. Do not add audit logging to this pure derivation story; runtime logging belongs with later control-loop and result stories.

Recent git history:
- `03ff345 5.3` modified `core/state.py`, `core/state_store.py`, and web serialization for stale and unavailable detection.
- `98cbc7a 6.2` and `2ddc23f 6.3` added event-log and config-audit foundations.
- No `src/open_ems/engine/` package exists yet, so this story should create the package without pretending runtime engine infrastructure already exists.

### Architecture Compliance

- The architecture says decision-engine logic is pure and testable in isolation: no adapter calls, no database writes, no web/session access.
- The architecture BAD-2 originally describes the matrix as part of `control_loop.py`, but this story refines the ownership boundary: Story 7.1 derives the recommended mode in pure engine code; Epic 8 runtime control-loop code applies transitions later.
- `PolicyGuard.authorize_and_dispatch()` remains the required command path, but this story must not produce commands.
- Readiness remains `ready` during degraded operation; this story does not touch readiness.
- Fail-safe means "stop issuing new commands" at runtime. This story only recommends `fail_safe`; it does not stop commands directly.

### Testing Standards

- Tests mirror source structure: `src/open_ems/engine/operating_mode.py` maps to `tests/unit/engine/test_operating_mode.py`.
- Use existing pytest style: small helper constructors for `InverterState`, `BatteryState`, `EVChargerState`, `GridMeterState`, and `DegradedDeviceState`.
- Use timezone-aware UTC datetimes from `datetime.UTC`.
- Keep tests isolated from database and FastAPI app creation.
- The repository enforces strict mypy and ruff with 100-character line length.

### Latest Technical Information

No new third-party library or external API is required. Use the currently resolved project toolchain:
- Python `>=3.12`
- Pydantic `2.13.3`
- pytest `9.0.3`
- ruff `0.15.12`
- mypy `1.20.2`

Checked on 2026-05-04: PyPI listings for Pydantic, pytest, and ruff show the project is already using current major-version APIs for this work. Do not upgrade or add dependencies for this story.

### Project Structure Notes

Expected new files:
- `src/open_ems/engine/__init__.py`
- `src/open_ems/engine/models.py`
- `src/open_ems/engine/operating_mode.py`
- `tests/unit/engine/__init__.py`
- `tests/unit/engine/test_operating_mode.py`

Files to read and preserve before editing:
- `src/open_ems/core/state.py`
- `src/open_ems/core/devices.py`
- `src/open_ems/core/state_store.py`
- `tests/unit/core/test_state.py`
- `tests/unit/core/test_state_store.py`

Expected files not to modify:
- `src/open_ems/web/state_serialization.py`
- `src/open_ems/services/audit_log.py`
- `src/open_ems/storage/**`
- `migrations/**`

### References

- `_bmad-output/planning-artifacts/epics.md` - Epic 7 overview and Story 7.1 acceptance criteria.
- `_bmad-output/planning-artifacts/epics.md` - Epic 7 cross-story constraints: pure decision engine, explicit input objects, no adapter/database/web access.
- `_bmad-output/planning-artifacts/architecture.md` - Project Structure Patterns and Project Structure & Boundaries.
- `_bmad-output/planning-artifacts/architecture.md` - BAD-2 Conservative Fallback Behavior Matrix.
- `_bmad-output/planning-artifacts/prd.md` - FR7, FR13, FR15, FR18, FR20, FR34, NFR-P1, NFR-R2, NFR-I3.
- `_bmad-output/planning-artifacts/ux-design-specification.md` - System State Model and role-specific degraded-mode messaging.
- `src/open_ems/core/state.py` - existing `SystemOperatingMode`, `SystemSnapshot`, global/component state derivation.
- `src/open_ems/core/devices.py` - existing `DeviceRole` and `DegradedDeviceState`.
- `src/open_ems/core/state_store.py` - existing StateStore ownership of current snapshot mode.
- `pyproject.toml` - Python, dependency, ruff, mypy, and pytest configuration.

### Review Findings

- [x] [Review][Decision] `None` for required device slots (inverter, grid_meter) silently treated as healthy — resolved: added `@model_validator(mode="after")` to `EvaluationInput` that raises `ValidationError` when `inverter` or `grid_meter` is `None`; two new parametrized tests added and all 615 tests pass.
- [x] [Review][Defer] `DegradedDeviceState.role` not validated against positional slot [`src/open_ems/engine/models.py`] — deferred, pre-existing design: spec explicitly uses `state.role` as the role source of truth; `from_snapshot` is the canonical construction path and produces correct objects; defensive cross-validation is a future hardening concern.

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Debug Log References

- Baseline: `uv run python -m pytest tests/ --no-cov -q` - 598 passed, 43 warnings.
- Baseline: `uv run python -m mypy src/` - passed.
- Baseline: `uv run python -m ruff check .` - found one pre-existing line-length issue in `tests/unit/services/test_audit_log.py`; wrapped the line mechanically before final validation.
- Red phase: `uv run python -m pytest tests/unit/engine/test_operating_mode.py --no-cov -q` - failed with `ModuleNotFoundError: No module named 'open_ems.engine'`.
- Green phase: `uv run python -m pytest tests/unit/engine/test_operating_mode.py --no-cov -q` - 15 passed.
- Final: `uv run python -m ruff check .` - passed.
- Final: `uv run python -m ruff format --check .` - passed after formatting `tests/unit/engine/test_operating_mode.py`.
- Final: `uv run python -m mypy src/` - passed.
- Final: `uv run python -m pytest tests/unit/engine/test_operating_mode.py --no-cov -q` - 15 passed.
- Final: `uv run python -m pytest tests/ --no-cov -q` - 613 passed, 43 warnings.

### Completion Notes List

- Added the initial pure `open_ems.engine` package surface with `EvaluationInput` and `derive_recommended_operating_mode`.
- Implemented a frozen Pydantic v2 `EvaluationInput` that copies device slots from `SystemSnapshot` without retaining or mutating `StateStore`.
- Implemented deterministic role-set based operating-mode recommendation: no degraded roles -> `normal`, grid meter only -> `conservative`, grid meter plus another degraded role -> `fail_safe`, and all other degraded role sets -> `degraded`.
- Preserved existing core StateStore, core state enums, web serialization, storage, services, and migrations.
- Added focused unit coverage for all Story 7.1 matrix outcomes, optional absent-role handling, determinism, and the "do not echo snapshot operating_mode" ownership boundary.
- Mechanically wrapped one existing overlong assertion in `tests/unit/services/test_audit_log.py` so required full-repo Ruff validation passes.

### File List

- `_bmad-output/implementation-artifacts/7-1-implement-systemoperatingmode-derivation-and-degradation-matrix.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`
- `src/open_ems/engine/__init__.py`
- `src/open_ems/engine/models.py`
- `src/open_ems/engine/operating_mode.py`
- `tests/unit/engine/__init__.py`
- `tests/unit/engine/test_operating_mode.py`
- `tests/unit/services/test_audit_log.py`

### Change Log

- 2026-05-04: Story created and marked ready for dev.
- 2026-05-04: Implemented pure engine operating-mode recommendation with focused tests; story marked ready for review.
