# Story 5.1: Define system state model and implement StateStore with immutable versioned snapshots

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want a StateStore that holds an immutable, versioned SystemSnapshot and defines all global and per-component state types,
so that every higher layer reads a consistent, point-in-time system view from a single source without mutating shared state.

## Acceptance Criteria

**AC1 - State model enums and snapshot shape**
**Given** the state package is initialized
**When** state types are imported
**Then** global UI states are defined: `NORMAL` / `DEGRADED` / `STALE` / `FAILED`
**And** per-component states are defined: `IDLE` / `PENDING` / `ACTIVE` / `ERROR` / `UNAVAILABLE` / `STALE`
**And** `SystemOperatingMode` enum is defined: `normal` / `degraded` / `conservative` / `fail_safe`, for Epic 7 and Epic 8 consumption
**And** the authoritative `SystemOperatingMode` to UI state mapping is:

| `SystemOperatingMode` | UI global state | Notes |
|---|---|---|
| `normal` | `NORMAL` | All devices healthy |
| `degraded` | `DEGRADED` | One or more devices in error/unavailable/reduced state |
| `conservative` | `DEGRADED` | Reduced-capability mode; visually identical to degraded |
| `fail_safe` | `FAILED` | System halted; no control actions permitted |

**And** `STALE` is a per-device UI state derived from stale data, not a `SystemOperatingMode` value
**And** `SystemSnapshot` is an immutable Pydantic model containing:
- `sequence_id: int`, monotonically increasing by 1 for every publish
- `captured_at: datetime`, timezone-aware UTC, generated at StateStore aggregation time
- `global_state: GlobalState`
- `operating_mode: SystemOperatingMode`
- device slots: `inverter`, `battery`, `ev_charger`, `grid_meter`, each `DeviceState | DegradedDeviceState | None`
- `component_states: Mapping[DeviceRole, ComponentState]`
- `data_age_seconds: Mapping[DeviceRole, float | None]`
- `system_clock_status: ClockStatus`, where `ClockStatus` is a local core type alias compatible with `Literal["valid", "suspect", "unknown"]`
**And** `SystemSnapshot` does not expose mutable shared dictionaries; StateStore copies mapping inputs when constructing snapshots

**AC2 - Global state derivation**
**Given** StateStore constructs a snapshot
**When** it evaluates device and component states
**Then** global state is derived in strict priority order: `FAILED` > `STALE` > `DEGRADED` > `NORMAL`
**And** the required device roles for Story 5.1 are:

| `DeviceRole` | Required for global state? |
|---|---|
| `grid_meter` | REQUIRED |
| `inverter` | REQUIRED |
| `battery` | OPTIONAL |
| `ev_charger` | OPTIONAL |

**And** only REQUIRED roles participate in global `STALE` escalation, `UNAVAILABLE` to `DEGRADED` derivation, and `NORMAL` eligibility
**And** OPTIONAL roles may be `None` without degrading global state
**And** `FAILED` is produced if any device slot contains `DegradedDeviceState` with one of these FAILED-class reasons:
- exact `device_id_mismatch`
- any reason starting with `validation_error:`
- exact `unexpected_raw_state_type`
**And** unknown degraded reasons are treated as `DEGRADED`, not `FAILED`
**And** all other known degraded reasons remain `DEGRADED` unless explicitly mapped to `STALE`
**And** `dsmr_stale` maps that component to `STALE`
**And** `STALE` global state is produced only if a REQUIRED role has component state `STALE`
**And** `DEGRADED` is produced if any device slot contains `DegradedDeviceState` and no higher-priority rule applies
**And** `DEGRADED` is produced if any required device slot is `UNAVAILABLE` and no higher-priority rule applies
**And** `NORMAL` is produced only when all required device slots are healthy and no higher-priority rule applies

**AC3 - StateStore publish contract**
**Given** the StateStore is running and a publisher calls `StateStore.publish(device_states: dict[DeviceRole, DeviceState | DegradedDeviceState], operating_mode: SystemOperatingMode | None = None)`
**When** publish executes
**Then** StateStore constructs a complete new `SystemSnapshot` from the provided states and never reads adapters directly
**And** the intended publishers are the startup sequence and the Epic 8 control loop; web routes, decision logic, and adapters do not call `publish()` directly
**And** `sequence_id` increments by exactly 1 from the previous snapshot on every publish call
**And** replacement is atomic: readers never observe a partially constructed snapshot
**And** the single-writer asyncio lock is held only while building and replacing the snapshot

**AC4 - Lock-free read contract**
**Given** any component calls `StateStore.get_snapshot()`
**When** it executes
**Then** it returns the latest immutable `SystemSnapshot` without acquiring a lock
**And** callers treat the returned snapshot as read-only and must not store it across evaluation cycles
**And** no shared mutable state exists between concurrent readers

**AC5 - Snapshot immutability and device absence semantics**
**Given** a component holds an older snapshot reference
**When** StateStore publishes a newer snapshot
**Then** the older snapshot remains unchanged
**And** configured or known devices remain represented when degraded, unavailable, stale, or reconnecting
**And** temporary failure is represented as `DegradedDeviceState` or a component state overlay, not by silently removing the device slot
**And** `None` means the device has never been configured/observed in this StateStore instance, not a transient communication failure
**And** StateStore does not persist configured-device knowledge across process restarts in Story 5.1
**And** "known" means known within the current StateStore instance/runtime
**And** startup or later orchestration layers are responsible for publishing initial known/configured devices
**And** StateStore preserves known runtime devices once observed, but does not invent persisted device configuration

**AC6 - Data age derivation**
**Given** StateStore constructs a snapshot
**When** a device slot contains a state with a usable measurement timestamp
**Then** `data_age_seconds` for that role is derived from `snapshot.captured_at - measurement_timestamp`
**And** `read_at` is used for `InverterState`, `BatteryState`, and `EVChargerState`
**And** `received_at` is used for `GridMeterState`
**And** `occurred_at` is used for `DegradedDeviceState` where appropriate
**And** if no usable measurement timestamp exists, `data_age_seconds` is `None`
**And** configurable stale threshold settings are not added in Story 5.1; that belongs to Story 5.3

**AC7 - System clock status propagation**
**Given** startup clock status has been read by the startup layer from `open_ems.services.time_sync.get_clock_status()`
**When** the startup layer constructs StateStore and StateStore creates any snapshot
**Then** `system_clock_status` is preserved in the snapshot as `valid`, `suspect`, or `unknown`
**And** the value is available to later SSE and HTMX serializers without recomputing clock status
**And** `core/` does not import `services/time_sync.py`; the status is passed into StateStore from the startup layer

**AC8 - Tests**
**And** unit tests verify enum values and `SystemOperatingMode` to global state mapping
**And** unit tests verify frozen model behavior and that publishing does not mutate previous snapshots
**And** unit tests verify callers cannot mutate `component_states` or `data_age_seconds` through the returned snapshot
**And** unit tests verify sequence IDs increment by exactly 1 under rapid successive publishes
**And** unit tests verify global state derivation for `FAILED`, `STALE`, `DEGRADED`, and `NORMAL`
**And** unit tests verify required vs optional roles, including missing optional battery/EV charger not degrading global state and missing required inverter/grid meter degrading global state
**And** unit tests verify exact FAILED-class reasons, `validation_error:*` prefix behavior, and unknown degraded reasons remaining `DEGRADED`
**And** unit tests verify `dsmr_stale` maps component state to `STALE` and global `STALE` only for required roles
**And** unit tests verify runtime-known device preservation across publishes
**And** unit tests verify `system_clock_status` is preserved in every snapshot
**And** concurrency tests verify concurrent readers see consistent, non-torn snapshots during concurrent publish activity
**And** tests verify `core/` still does not export or import raw protocol types

## Tasks / Subtasks

- [x] **Task 0: Pre-story quality gate** (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` and record the baseline
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m mypy src/`

- [x] **Task 1: Add state domain models in `core/state.py`** (AC: AC1, AC2, AC5, AC6, AC7)
  - [x] Create `src/open_ems/core/state.py`
  - [x] Define `GlobalState`, `ComponentState`, and `SystemOperatingMode` as `enum.StrEnum`
  - [x] Define a local `ClockStatus` type alias compatible with `Literal["valid", "suspect", "unknown"]`; do not import it from `services/time_sync.py`
  - [x] Define immutable `SystemSnapshot` using Pydantic v2 with `ConfigDict(frozen=True, extra="forbid")`
  - [x] Type `component_states` as `Mapping[DeviceRole, ComponentState]` and `data_age_seconds` as `Mapping[DeviceRole, float | None]`
  - [x] Ensure snapshot construction copies mapping inputs so no caller-owned mutable dictionaries are exposed
  - [x] Reuse `DeviceRole`, `DeviceState`, and `DegradedDeviceState` from `core/devices.py`
  - [x] Validate `captured_at` with the existing UTC-only behavior from `core/devices.py`; do not duplicate validation inconsistently
  - [x] Keep all model fields JSON-serializable for Epic 5.2 SSE and Epic 5.3 HTMX use

- [x] **Task 2: Implement StateStore in `core/state_store.py`** (AC: AC2, AC3, AC4, AC5, AC6, AC7)
  - [x] Create `src/open_ems/core/state_store.py`
  - [x] Implement `StateStore.__init__(...)` with an initial immutable snapshot, `sequence_id=0`, UTC `captured_at`, and `system_clock_status`
  - [x] Implement `async publish(...) -> SystemSnapshot`
  - [x] Implement `get_snapshot() -> SystemSnapshot` as a lock-free read returning the current snapshot reference
  - [x] Use `asyncio.Lock` only for the single writer path
  - [x] Build a complete new snapshot before assigning `self._snapshot`
  - [x] Preserve known runtime device roles across publishes within the same StateStore instance
  - [x] Do not persist or invent configured-device knowledge across process restarts
  - [x] Do not import adapters, storage, web, or services from `core/state.py` or `core/state_store.py`

- [x] **Task 3: Implement deterministic component and global state derivation** (AC: AC1, AC2, AC5)
  - [x] Add a pure helper for component state derivation from `DeviceState | DegradedDeviceState | None`
  - [x] Define required roles as `grid_meter` and `inverter`; define optional roles as `battery` and `ev_charger`
  - [x] Ensure optional missing roles do not degrade global state
  - [x] Ensure missing required roles degrade global state
  - [x] Add a pure helper for global state derivation with strict priority: `FAILED` > `STALE` > `DEGRADED` > `NORMAL`
  - [x] Map exact `device_id_mismatch`, prefix `validation_error:`, and exact `unexpected_raw_state_type` to `FAILED`
  - [x] Default unknown degraded reasons to `DEGRADED`, not `FAILED`
  - [x] Map `dsmr_stale` to component `STALE`; escalate to global `STALE` only for required roles
  - [x] Preserve `STALE` and `UNAVAILABLE` as distinct component states for Epic 5.3
  - [x] Do not implement decision-engine degradation matrix or control-loop policy in Epic 5.1

- [x] **Task 4: Implement data-age derivation** (AC: AC6)
  - [x] Derive `data_age_seconds` from `captured_at` minus each state's measurement timestamp
  - [x] Use `read_at` for `InverterState`, `BatteryState`, and `EVChargerState`
  - [x] Use `received_at` for `GridMeterState`
  - [x] Use `occurred_at` for `DegradedDeviceState` where appropriate
  - [x] Return `None` when no usable measurement timestamp exists
  - [x] Do not add stale-threshold configuration in this story

- [x] **Task 5: Export core state APIs** (AC: AC1)
  - [x] Update `src/open_ems/core/__init__.py` to export `GlobalState`, `ComponentState`, `SystemOperatingMode`, `SystemSnapshot`, and `StateStore`
  - [x] Ensure raw protocol types remain absent from `open_ems.core`

- [x] **Task 6: Write unit tests for models and derivation** (AC: AC1, AC2, AC5, AC6, AC7, AC8)
  - [x] Add `tests/unit/core/test_state.py`
  - [x] Test exact enum values
  - [x] Test snapshot immutability and extra-field rejection
  - [x] Test nested mapping immutability for `component_states` and `data_age_seconds`
  - [x] Test UTC validation for `captured_at`
  - [x] Test all global state priority cases
  - [x] Test required vs optional roles
  - [x] Test optional missing battery/EV charger does not degrade global state
  - [x] Test missing required inverter/grid_meter degrades global state
  - [x] Test exact FAILED-class reasons and `validation_error:*` prefix behavior
  - [x] Test unknown degraded reason remains `DEGRADED`
  - [x] Test `dsmr_stale` maps component state to `STALE` and global `STALE` only for required roles
  - [x] Test `None` device slots map to `UNAVAILABLE`, not `STALE`
  - [x] Test data age derivation from each state timestamp type

- [x] **Task 7: Write unit and concurrency tests for StateStore** (AC: AC3, AC4, AC5, AC6, AC7, AC8)
  - [x] Add `tests/unit/core/test_state_store.py`
  - [x] Test initial snapshot shape and `sequence_id=0`
  - [x] Test publish returns and stores a new snapshot with `sequence_id += 1`
  - [x] Test older snapshot references remain unchanged after publish
  - [x] Test runtime-known device preservation across publishes within one StateStore instance
  - [x] Test rapid successive publishes produce contiguous sequence IDs
  - [x] Test concurrent readers during publish never see torn or partially updated snapshots
  - [x] Test `get_snapshot()` does not await or acquire the writer lock

- [x] **Task 8: Final validation** (AC: all)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/ --no-cov -q`
  - [x] Close or explicitly defer every review finding before marking this story done

### Review Findings

- [x] [Review][Patch] Optional known-device omission must not degrade global state — Decision: known optional device unavailability should remain represented but not degrade global state.
- [x] [Review][Patch] Reject or safely handle role/state mismatches during publish [src/open_ems/core/state_store.py:96]
- [x] [Review][Patch] Validate complete role maps on public `SystemSnapshot` construction [src/open_ems/core/state.py:154]
- [x] [Review][Patch] Add publish-time clock-status preservation coverage [tests/unit/core/test_state_store.py:83]

## Dev Notes

### Context and Scope

Epic 5 establishes the single source of truth for state aggregation and real-time distribution. Story 5.1 owns the in-memory state model and StateStore only.

This story does:
- Define immutable, versioned system snapshots.
- Provide a single StateStore publish/read boundary for later control-loop, SSE, HTMX, dashboard, and decision-engine work.
- Preserve the Epic 4 `DeviceState | DegradedDeviceState` domain interface.
- Define deterministic state derivation rules that later stories can serialize and render.

This story does not:
- Implement SSE (`GET /api/stream/state`) - Story 5.2.
- Implement HTMX polling fragments or configurable stale threshold - Story 5.3.
- Implement the control loop, decision engine, degradation matrix, command dispatch, or adapter polling - Epics 7 and 8.
- Persist state to SQLite or write event logs.
- Read protocol adapters directly.

### Current Codebase State

Existing files to understand before implementation:

- `src/open_ems/core/devices.py` contains all current domain device models. `DeviceState` is a union of `InverterState`, `BatteryState`, `EVChargerState`, and `GridMeterState`. `DegradedDeviceState` carries `device_id`, `role`, `reason`, and UTC `occurred_at`. All existing domain models are frozen Pydantic models with `extra="forbid"`.
- `src/open_ems/core/__init__.py` exports the core domain API. New state models and StateStore should be exported here after implementation.
- `src/open_ems/services/time_sync.py` defines `ClockStatus = Literal["valid", "suspect", "unknown"]` and `get_clock_status()`. Story 5.1 snapshots must carry this value forward, but `core/` must not import `services/`; pass the value into StateStore from startup or tests.
- `src/open_ems/settings.py` does not yet define `stale_threshold_seconds`. Do not add it in this story unless implementation needs a placeholder; configurable stale detection belongs to Story 5.3.
- There is no existing `src/open_ems/core/state.py` or `src/open_ems/core/state_store.py`. These should be new files.

Expected new files:
- `src/open_ems/core/state.py`
- `src/open_ems/core/state_store.py`
- `tests/unit/core/test_state.py`
- `tests/unit/core/test_state_store.py`

Expected existing files to modify:
- `src/open_ems/core/__init__.py`

Do not modify adapter implementations for this story unless a test reveals a direct contract bug. Epic 4 declared the adapter/domain interface complete and stable.

### Architecture Constraints

Strict import boundary:

```text
core/     -> no imports from adapters/, storage/, web/
adapters/ -> may import from core/
web/      -> may read StateStore later, but must not call adapters directly
engine/   -> may read StateStore later, but must not call web/
```

StateStore belongs under `core/` because it is the domain state boundary used by engine and web layers. Keep it free of FastAPI, repository, adapter, storage, and service dependencies. The startup layer can call `get_clock_status()` and pass the literal value into StateStore.

Architecture requires:
- Python 3.12+, asyncio-first service architecture.
- Pydantic v2 models for typed domain contracts.
- Snapshots immutable or copy-on-read; this story chooses immutable full-snapshot replacement.
- Readers call `state_store.get_snapshot()` and never mutate returned snapshots.
- Components must not infer system state from raw adapter data.
- StateStore is a dependency of decision engine, web layer, and watchdog.

### Previous Story Intelligence

From Story 4.5 and the Epic 4 retrospective:

- `DeviceState | DegradedDeviceState` and `DeviceRole` are the stable interface for StateStore aggregation.
- Epic 4 adapter reason strings are now cross-epic stable. Consumers must not rename or remove existing values, and must treat unknown reason strings as generic degraded state.
- `DegradedDeviceState.reason` current inventory:

| Reason | Meaning | Story 5.1 handling |
|---|---|---|
| `reconnecting` | Transport/protocol temporarily unavailable | `DEGRADED` unless another higher-priority rule applies |
| `dsmr_stale` | DSMR reading exceeded 60s staleness threshold | Component `STALE`; global `STALE` if required grid meter |
| `ocpp_invalid_power` | Invalid negative MeterValues power | `DEGRADED` |
| `missing_register:<addr>` | Required Modbus register absent | `DEGRADED` |
| `invalid_register_value` | Register value unsupported at domain level | `DEGRADED` |
| `device_id_mismatch` | Raw state belongs to wrong device | `FAILED` |
| `validation_error:<detail>` | Decoded values failed validation | `FAILED` for any `validation_error:` prefix |
| `missing_dsmr_field:<key>` | Required DSMR OBIS field absent | `DEGRADED` |
| `ocpp_no_status` | Charger has no StatusNotification yet | `DEGRADED` |
| `ocpp_unknown_status:<status>` | Unknown OCPP status string | `DEGRADED` |
| `unexpected_raw_state_type` | Adapter received wrong raw state type | `FAILED` |

SystemSnapshot semantics from Epic 4 retro:
- Immutable once created.
- Strictly monotonic integer version, never reused.
- Full snapshot replacement; no partial mutation.
- Known runtime devices remain represented when degraded, unavailable, stale, or reconnecting.
- StateStore does not persist configured-device knowledge across process restarts in Story 5.1.
- "Known" means known within the current StateStore instance/runtime.
- Startup or later orchestration layers publish initial known/configured devices; StateStore preserves what it has observed but does not invent persisted configuration.
- Adapter timestamps represent measurement time; snapshot `captured_at` represents StateStore aggregation time.
- No reader locks are required because the current snapshot reference is replaced atomically with a complete immutable model.

Required device roles for Story 5.1:

| Role | Requirement |
|---|---|
| `DeviceRole.grid_meter` | REQUIRED |
| `DeviceRole.inverter` | REQUIRED |
| `DeviceRole.battery` | OPTIONAL |
| `DeviceRole.ev_charger` | OPTIONAL |

Only required roles participate in global `STALE` escalation, required-role `UNAVAILABLE` to global `DEGRADED` derivation, and `NORMAL` eligibility. Optional roles may be `None` without degrading global state.

FAILED-class degraded reason mapping is deterministic:

| Reason rule | Global result |
|---|---|
| exact `device_id_mismatch` | `FAILED` |
| prefix `validation_error:` | `FAILED` |
| exact `unexpected_raw_state_type` | `FAILED` |
| exact `dsmr_stale` | component `STALE`; global `STALE` only if role is required |
| unknown reason | `DEGRADED` |
| all other known reasons | `DEGRADED` unless explicitly mapped above |

Process lesson from Epic 4:
- For stories with async I/O, transport errors, multi-layer exception propagation, new subsystems, or cross-cutting adapter changes, dev notes must include explicit error-path analysis. Story 5.1 is a new subsystem with concurrency risk, so tests must cover lock behavior, old-reference immutability, and sequence consistency before review.

### Error-Path Table

| Failure or edge path | Expected behavior | Required test signal |
|---|---|---|
| Publish receives no state for optional battery or EV charger | Slot remains `None`; component state is `UNAVAILABLE`; global state does not degrade because of that optional absence | Required-vs-optional unit test |
| Publish receives no state for required inverter or grid meter | Slot remains `None`; component state is `UNAVAILABLE`; global state is at least `DEGRADED` | Required-vs-optional unit test |
| Publish receives `DegradedDeviceState(reason="reconnecting")` | Slot contains degraded state; component/global state degrade without removing last-known device identity | Unit test for degraded derivation |
| Publish receives `DegradedDeviceState(reason="dsmr_stale")` for required grid meter | Component state becomes `STALE`; global state becomes `STALE` | Unit test for stale priority |
| Publish receives `DegradedDeviceState(reason="dsmr_stale")` for optional role | Component state becomes `STALE`; global state does not become `STALE` from that optional role alone | Unit test for optional stale behavior |
| Publish receives `device_id_mismatch` | Global state becomes `FAILED` | Exact reason unit test |
| Publish receives `validation_error:<detail>` | Global state becomes `FAILED` | Prefix unit test |
| Publish receives `unexpected_raw_state_type` | Global state becomes `FAILED` | Exact reason unit test |
| Publish receives unknown reason | Treat as `DEGRADED`; do not raise | Forward-compatibility unit test |
| Snapshot exposes `component_states` or `data_age_seconds` | Callers cannot mutate StateStore-owned dictionaries through returned snapshot | Nested mapping immutability test |
| Publish omits a previously known runtime device | Previously known role remains represented as known/unavailable according to the story rules; StateStore does not forget it within the same runtime | Runtime-known preservation test |
| Multiple publishes happen rapidly | Sequence IDs are contiguous and never repeated | Async unit test |
| Reader holds old snapshot during publish | Old snapshot remains unchanged | Immutability/reference test |
| Reader calls `get_snapshot()` while writer lock is held | Read returns current snapshot without waiting for writer lock | Concurrency test that intentionally holds writer path |
| Clock status is suspect/unknown | Snapshot stores the exact status | Parametrized unit test |

### Implementation Guidance

Recommended shape:

```python
class GlobalState(enum.StrEnum):
    normal = "NORMAL"
    degraded = "DEGRADED"
    stale = "STALE"
    failed = "FAILED"
```

Use enum member names that fit project style, but preserve the exact serialized values required by ACs. `SystemOperatingMode` values should be lowercase strings: `normal`, `degraded`, `conservative`, `fail_safe`.

Prefer Pydantic models over dataclasses because the existing domain layer uses Pydantic v2 frozen models and validation errors are already tested. Pydantic v2 `ConfigDict(frozen=True)` is the current official immutable-model mechanism; note that it prevents normal attribute assignment but does not make nested mutable objects magically immutable.

For this story, `SystemSnapshot` must not expose mutable shared dictionaries. Use:
- `Mapping[DeviceRole, ComponentState]` for `component_states`
- `Mapping[DeviceRole, float | None]` for `data_age_seconds`

Copy input dictionaries when constructing snapshots, and add tests proving callers cannot mutate those mappings through a returned snapshot.

`data_age_seconds` derivation is StateStore-owned:
- Use `snapshot.captured_at - state.read_at` for `InverterState`, `BatteryState`, and `EVChargerState`.
- Use `snapshot.captured_at - state.received_at` for `GridMeterState`.
- Use `snapshot.captured_at - state.occurred_at` for `DegradedDeviceState` where appropriate.
- Use `None` when no usable measurement timestamp exists.
- Do not add configurable stale threshold settings in Story 5.1; stale threshold behavior belongs to Story 5.3.

For StateStore, construct the entire `SystemSnapshot` in a local variable before assigning `self._snapshot`. `get_snapshot()` should be a normal synchronous method, not `async`, so callers cannot accidentally await on a read path.

The initial snapshot should be useful and safe:
- `sequence_id=0`
- all device slots `None`
- all component states `UNAVAILABLE`
- `global_state=DEGRADED` for "not yet enough data" or any required device `UNAVAILABLE`
- `operating_mode=SystemOperatingMode.degraded` unless caller supplies a different startup mode
- `system_clock_status` from startup/time sync

### Testing Standards

Use current project tooling:
- Python 3.12+
- pytest and pytest-asyncio
- strict mypy
- ruff line length 100

Follow existing core test style in `tests/unit/core/test_devices.py`:
- deterministic `_NOW_UTC` constants
- Pydantic `ValidationError` assertions for frozen and invalid data
- no real hardware, no adapter imports

Concurrency tests should use `asyncio.gather()`, `asyncio.Event`, and enough iterations to catch torn reads deterministically without making the suite flaky. Do not use arbitrary long sleeps. If an internal test-only hook is needed to hold a writer at a known point, keep it private and avoid production complexity.

### Latest Technical Notes

- Python 3.12 `asyncio.Lock` is appropriate for the single-writer critical section. The official docs specify asyncio synchronization primitives are not thread-safe and are for asyncio task coordination, which matches this single-event-loop StateStore use case.
- Pydantic v2 uses `ConfigDict(frozen=True)` for immutable model instances; this replaces the old v1 `allow_mutation=False` pattern.
- The project currently pins `pydantic>=2.13.3`, `pytest>=9.0.3`, `pytest-asyncio>=1.3.0`, and Python `>=3.12` in `pyproject.toml`.

### References

- Story source: [Source: `_bmad-output/planning-artifacts/epics.md`#Story 5.1]
- Epic 5 constraints: [Source: `_bmad-output/planning-artifacts/epics.md`#Cross-story constraints applying to all Epic 5 stories]
- StateStore architecture: [Source: `_bmad-output/planning-artifacts/architecture.md`#Decision 1.1 - In-memory system state model]
- StateStore immutable pattern: [Source: `_bmad-output/planning-artifacts/architecture.md`#Pattern: StateStore snapshots are immutable]
- Source tree target: [Source: `_bmad-output/planning-artifacts/architecture.md`#Unified Project Structure]
- Import boundaries: [Source: `_bmad-output/planning-artifacts/architecture.md`#Import Boundaries]
- UX state contract: [Source: `_bmad-output/planning-artifacts/ux-design-specification.md`#State Model]
- Current device domain models: [Source: `src/open_ems/core/devices.py`]
- Current core exports: [Source: `src/open_ems/core/__init__.py`]
- Clock status source: [Source: `src/open_ems/services/time_sync.py`]
- Previous story: [Source: `_bmad-output/implementation-artifacts/4-5-implement-device-discovery-and-connection-management.md`]
- Epic 4 retrospective carry-forward: [Source: `_bmad-output/implementation-artifacts/epic-4-retro-2026-05-03.md`]
- Python asyncio lock docs: [Source: `https://docs.python.org/3.12/library/asyncio-sync.html`]
- Pydantic frozen model docs: [Source: `https://docs.pydantic.dev/2.8/concepts/models/#faux-immutability`]

## Dev Agent Record

### Agent Model Used

GPT-5

### Debug Log References

- Baseline: `uv run python -m pytest tests/ --no-cov -q` -> 468 passed, 39 warnings
- Baseline: `uv run python -m ruff check .` -> All checks passed
- Baseline: `uv run python -m mypy src/` -> Success, no issues in 53 source files
- Focused tests after implementation: `uv run python -m pytest tests/unit/core/test_state.py tests/unit/core/test_state_store.py --no-cov -q` -> 24 passed
- Final: `uv run python -m ruff check .` -> All checks passed
- Final: `uv run python -m ruff format --check .` -> 111 files already formatted
- Final: `uv run python -m mypy src/` -> Success, no issues in 55 source files
- Final: `uv run python -m pytest tests/ --no-cov -q` -> 492 passed, 39 warnings
- Review fixes: `uv run python -m pytest tests/unit/core/test_state.py tests/unit/core/test_state_store.py --no-cov -q` -> 28 passed
- Review fixes final: `uv run python -m ruff check .` -> All checks passed
- Review fixes final: `uv run python -m ruff format --check .` -> 111 files already formatted
- Review fixes final: `uv run python -m mypy src/` -> Success, no issues in 55 source files
- Review fixes final: `uv run python -m pytest tests/ --no-cov -q` -> 496 passed, 39 warnings

### Completion Notes List

- Implemented immutable `SystemSnapshot`, state enums, operating-mode mapping, clock-status propagation type, nested mapping copy/freeze behavior, and JSON serialization support.
- Implemented `StateStore` with single-writer async publish, lock-free reads, atomic full-snapshot replacement, contiguous sequence IDs, runtime-known device preservation, deterministic component/global derivation, and data-age derivation.
- Exported the new core state API while keeping raw protocol types out of `open_ems.core`.
- Added unit and concurrency coverage for model immutability, enum/mapping contracts, required vs optional roles, degraded reason priority, stale behavior, data ages, StateStore publishing, old-reference immutability, sequence continuity, runtime-known preservation, and lock-free reads.
- Addressed code review findings: optional known-device unavailability remains represented without degrading global state; publish now rejects role/state mismatches; public snapshots require complete role maps; publish-time clock status preservation is tested.

### File List

- `src/open_ems/core/__init__.py`
- `src/open_ems/core/state.py`
- `src/open_ems/core/state_store.py`
- `tests/unit/core/test_state.py`
- `tests/unit/core/test_state_store.py`

### Change Log

- 2026-05-03: Implemented Story 5.1 StateStore and immutable SystemSnapshot state model; added unit/concurrency tests; moved story to review.
- 2026-05-03: Addressed all code review patch findings and moved story to done.
