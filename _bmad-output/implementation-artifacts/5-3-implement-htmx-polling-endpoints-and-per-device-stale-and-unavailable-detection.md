# Story 5.3: Implement HTMX polling endpoints and per-device stale and unavailable detection

Status: done

<!-- Completion note: Ultimate context engine analysis completed - comprehensive developer guide created. -->

## Story

As a developer,
I want HTMX polling endpoints that serve the same state as SSE using server-rendered fragments, and configurable per-device stale detection that overlays freshness metadata without discarding retained successful device state,
so that UI components degrade gracefully when SSE is unavailable and users always see honest data-freshness indicators rather than stale values silently presented as current.

## Acceptance Criteria

**AC1 - HTMX polling fragments render current role-safe homeowner state**
**Given** a UI component polls a homeowner state fragment endpoint:
- `/fragments/homeowner/battery-card`
- `/fragments/homeowner/solar-card`
- `/fragments/homeowner/grid-card`
- `/fragments/homeowner/ev-card`
**When** the request arrives with a valid homeowner session
**Then** the server renders an HTMX-compatible HTML fragment from the current `SystemSnapshot`
**And** the fragment uses the same role-safe serialization/shared transformation path as `/api/stream/state` for the same role and snapshot
**And** Jinja templates may format display strings but must not infer component state, freshness, role filtering, or business meaning from raw `DeviceState`
**And** the rendered result has no loading-state difference from SSE-driven updates.

**AC2 - Stale detection is a non-destructive overlay**
**Given** a retained successful `DeviceState` has a UTC timestamp older than `STALE_THRESHOLD_SECONDS` at snapshot construction time
**When** `StateStore` constructs the next snapshot
**Then** `StateStore` compares the snapshot construction time to that device state's own timestamp field
**And** the per-component state for that device slot is `STALE`
**And** the retained successful `DeviceState` remains present in the snapshot
**And** `data_age_seconds` for that role is integer seconds, rounded down, since the retained successful device timestamp
**And** the implementation does not replace stale device state with `None`, zero values, or `DegradedDeviceState`.
**And** omission from a publish cycle is not itself a stale signal.

**AC3 - Configurable stale threshold**
**Given** the app starts with default settings
**When** `StateStore` is constructed
**Then** the stale threshold defaults to 30 seconds
**And** the setting is defined as `stale_threshold_seconds: int = Field(default=30, gt=0)`
**And** the threshold is configurable via pydantic-settings using environment variable `STALE_THRESHOLD_SECONDS`
**And** invalid values fail settings validation at startup rather than silently falling back.

**AC4 - Unavailable and stale remain distinct**
**Given** the system has just started and no state has ever been received for a device
**When** any state fragment is requested for that device
**Then** the device slot has component state `UNAVAILABLE`
**And** the fragment renders an explicit unavailable state, not zero values and not a stale caption
**And** `UNAVAILABLE` and `STALE` are distinct in `SystemSnapshot.component_states`, serialized payloads, and rendered fragments.

**AC5 - StateStore is the only primary stale mechanism**
**Given** an adapter returns an explicit degraded state with a reason such as `*_stale`
**When** `StateStore` constructs a snapshot
**Then** that adapter state is treated as degraded adapter input, not as the primary stale mechanism
**And** `ComponentState.STALE` is produced by StateStore's age-based overlay over retained successful `DeviceState` values
**And** stale is not represented by `DegradedDeviceState`
**And** no adapter-specific freshness rules are invented in StateStore, serializers, routes, or templates.

**AC6 - Stale captions are stable and calm**
**Given** a device's component state is `STALE`
**When** a fragment for that device is rendered
**Then** it shows a neutral caption such as `Grid data last updated 3 min ago`
**And** the caption is in a stable layout position and does not change card shell, border, background, heading, or trigger alert styling
**And** when fresh data arrives and the component returns to a non-stale state, the caption disappears without layout shift.

**AC7 - Stale does not degrade global state**
**Given** a required or optional component has `ComponentState.STALE`
**When** `StateStore` derives `GlobalState`
**Then** `ComponentState.STALE` alone does not change or degrade `GlobalState`
**And** only true degraded device states and required-role `UNAVAILABLE` conditions affect `GlobalState`, preserving Story 5.1 semantics.

**AC8 - Clock status passes through to fragments**
**Given** `system_clock_status` in the snapshot is `suspect` or `unknown`
**When** any state fragment is rendered
**Then** the clock-status value is available in the fragment template context
**And** the fragment layer does not suppress, rename, or reinterpret it.

**AC9 - Existing SSE ordering remains intact**
**Given** concurrent SSE readers and HTMX polling requests observe rapid `StateStore.publish()` calls
**When** snapshots are read
**Then** `sequence_id` remains monotonic and is never observed out of order by SSE readers
**And** HTMX polling does not modify `StateStore` or participate in event ordering.

**AC10 - Tests**
**And** unit tests verify stale overlay behavior: stale component state, retained successful `DeviceState`, timestamp-only freshness calculation, and integer rounded-down `data_age_seconds`.
**And** unit tests verify never-received devices remain `UNAVAILABLE`, not `STALE`.
**And** unit tests verify omission from a publish cycle does not itself create `STALE`.
**And** unit tests verify adapter `*_stale` degraded reasons do not drive the primary stale mechanism and do not produce a stale `DegradedDeviceState`.
**And** unit tests verify `ComponentState.STALE` alone does not degrade `GlobalState`.
**And** unit tests verify `STALE_THRESHOLD_SECONDS` is read from configuration and applied correctly.
**And** integration or route tests verify HTMX polling endpoints use the same normalized role-safe transformation path as SSE serialization for the same snapshot and role.
**And** unit tests verify stale captions are present for stale fragments and absent for fresh fragments.
**And** tests verify unavailable fragments render explicit unavailable text instead of zeros.
**And** deterministic concurrency tests using `asyncio.Event`, a bounded queue, or controlled publish/read coordination verify rapid snapshot updates do not cause `sequence_id` to be observed out of order by concurrent SSE readers.

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` and record the baseline.
  - [x] Run `uv run python -m ruff check .`.
  - [x] Run `uv run python -m mypy src/`.

- [x] Task 1: Add stale-threshold configuration (AC: AC2, AC3)
  - [x] Add `stale_threshold_seconds: int = Field(default=30, gt=0)` to `src/open_ems/settings.py`.
  - [x] Pass `settings.stale_threshold_seconds` into `StateStore(...)` from `src/open_ems/web/app.py`.
  - [x] Preserve current startup behavior: invalid settings should log `startup_failed` via the existing `ValidationError` path.
  - [x] Do not hardcode the threshold inside `core/`, routes, serializers, or templates.
  - [x] Keep any HTMX polling interval as a named constant or setting only if needed; do not broaden scope around timing configuration.

- [x] Task 2: Implement timestamp-only stale overlay in StateStore (AC: AC2, AC3, AC4, AC5, AC7, AC9)
  - [x] Update `src/open_ems/core/state_store.py` so `StateStore.__init__` accepts a stale-threshold value and stores it.
  - [x] Preserve `_known_device_ids` behavior for never-received vs known-but-missing devices.
  - [x] Preserve last successful `DeviceState` per role separately from explicit degraded states.
  - [x] Base freshness only on the last successful `DeviceState` timestamp already present in the core model: `read_at` for inverter, battery, and EV charger; `received_at` for grid meter.
  - [x] Compare snapshot construction time against that UTC timestamp; do not infer staleness from a role being omitted from a publish cycle.
  - [x] Derive `data_age_seconds` as integer seconds rounded down from snapshot construction time minus the last successful device timestamp.
  - [x] Set `component_states[role] = ComponentState.stale` only from StateStore's age-based overlay on retained successful `DeviceState` values.
  - [x] Do not convert stale states into `DegradedDeviceState`; explicit degraded adapter states may remain degraded, but stale is not represented by `DegradedDeviceState`.
  - [x] Ensure `ComponentState.stale` alone does not degrade `GlobalState`; preserve Story 5.1 global-state semantics for true degraded and required-role unavailable conditions.
  - [x] Keep `publish()` single-writer and snapshot replacement atomic; `get_snapshot()` must remain synchronous and lock-free.

- [x] Task 3: Keep serializers and fragment transformations aligned with SSE (AC: AC1, AC4, AC5, AC6, AC8)
  - [x] Review `src/open_ems/web/state_serialization.py` and ensure stale slots serialize retained successful public device values plus component/data-age metadata.
  - [x] Add shared role-safe transformation helpers if fragment rendering needs display-ready data beyond the current SSE payload.
  - [x] Ensure HTMX fragment routes call the same shared transformation path as SSE; do not duplicate business logic in routes or Jinja templates.
  - [x] Keep homeowner serialization allowlist-only; do not expose `reason`, `device_id`, raw degraded diagnostics, auth/session values, or internal exception details.
  - [x] Ensure installer serialization still includes diagnostics for real degraded states and preserves `component_states` and `data_age_seconds`.
  - [x] Do not add adapter reads, database reads, or route-specific state inference to serialization.

- [x] Task 4: Add explicit homeowner HTMX fragment route module (AC: AC1, AC4, AC6, AC8)
  - [x] Add `src/open_ems/web/routes/fragments.py`.
  - [x] Register it in `src/open_ems/web/app.py` using the same router pattern as `stream`, `homeowner`, and `installer`.
  - [x] Add exactly these homeowner GET routes: `/fragments/homeowner/battery-card`, `/fragments/homeowner/solar-card`, `/fragments/homeowner/grid-card`, `/fragments/homeowner/ev-card`.
  - [x] Use `require_homeowner` for homeowner fragment routes.
  - [x] Add installer fragment routes only if needed for this story, and keep them under an explicit separate installer path with `require_installer`.
  - [x] Use `get_state_store` and `state_store.get_snapshot()` only; do not read adapters directly.
  - [x] Return HTML fragments, not JSON, for HTMX polling endpoints.

- [x] Task 5: Add role-safe fragment rendering helpers/templates (AC: AC1, AC4, AC6, AC8)
  - [x] Create focused homeowner Jinja2 partial templates under `src/open_ems/web/templates/fragments/homeowner/`.
  - [x] Keep fragments idempotent: each response fully replaces its target component.
  - [x] Use stable container dimensions/classes so stale captions can appear or disappear without layout shift.
  - [x] Render unavailable as explicit text such as `Unavailable`; never render `0 kW`, `0%`, or blank text when data is unavailable.
  - [x] Render stale captions using transformed `data_age_seconds` values; include the metric name when ambiguity is possible.
  - [x] Keep templates limited to display formatting. They must not infer stale/unavailable/degraded state from raw device models.
  - [x] Pass `system_clock_status` through template context unchanged.

- [x] Task 6: Wire polling in dashboard templates only as far as needed (AC: AC1, AC6)
  - [x] Update the existing dashboard templates or placeholder `dashboard.html` only enough to host fragment targets for this story.
  - [x] Use HTMX `hx-get` and `hx-trigger="every 10s"` for polling fallback behavior.
  - [x] Use `hx-swap="outerHTML"` or a project-consistent swap strategy that replaces the complete fragment.
  - [x] Preserve the existing CSRF behavior for state-changing HTMX requests; this story's polling GETs should not require CSRF.
  - [x] Do not implement full Epic 10/11 dashboard UX, EV override actions, strategy selector, or installer event log.

- [x] Task 7: Add tests (AC: AC1-AC10)
  - [x] Extend `tests/unit/core/test_state_store.py` for stale threshold, timestamp-only freshness calculation, last-successful-state retention, unavailable distinction, integer rounded-down `data_age_seconds`, stale-not-global-degraded behavior, and sequence monotonicity.
  - [x] Add coverage proving omission from a publish cycle is not itself a stale signal.
  - [x] Add coverage proving adapter `*_stale` degraded reasons do not drive the primary stale mechanism and stale is not represented by `DegradedDeviceState`.
  - [x] Extend or add `tests/unit/web/test_state_serialization.py` for stale retained values and role-safe payloads.
  - [x] Add route/template tests such as `tests/unit/web/test_fragment_routes.py`.
  - [x] Test explicit homeowner fragment routes and role isolation.
  - [x] Test stale caption present/absent and unavailable text behavior in rendered HTML.
  - [x] Add a deterministic concurrency test that combines rapid publishes with active SSE generator reads using `asyncio.Event`, a bounded queue, or controlled publish/read coordination; avoid sleep-based race tests.

- [x] Task 8: Final validation (AC: all)
  - [x] Run `uv run python -m ruff check .`.
  - [x] Run `uv run python -m ruff format --check .`.
  - [x] Run `uv run python -m mypy src/`.
  - [x] Run `uv run python -m pytest tests/ --no-cov -q`.
  - [x] Close or explicitly defer every review finding before marking this story done.

## Dev Notes

### Context and Scope

Epic 5 establishes the single source of truth for system state and live UI distribution. Story 5.1 created immutable `SystemSnapshot` and `StateStore`. Story 5.2 added `/api/stream/state`, role-filtered serialization, and SSE ordering/session handling. Story 5.3 completes the fallback path and freshness model.

This story does:
- Add configurable stale detection to snapshot construction.
- Keep stale as a StateStore-owned component-state overlay on retained successful device data.
- Add server-rendered HTMX fragment endpoints for state cards.
- Ensure fragment values use the same role-safe transformation path as SSE for the same snapshot.

This story does not:
- Implement adapter polling or the runtime control loop.
- Implement decision-engine stale-control behavior; Epic 7 consumes the stale model later.
- Implement full homeowner or installer dashboards from Epics 10/11.
- Add WebSocket transport for UI state.
- Add a client-side state store or npm build step.
- Invent adapter-specific freshness rules or use adapter `*_stale` reasons as the primary stale mechanism.

### Current Codebase State

Files to read before implementation:
- `src/open_ems/core/state.py`: defines `GlobalState`, `ComponentState.stale`, `SystemSnapshot`, role maps, and current derivation helpers.
- `src/open_ems/core/state_store.py`: owns snapshot construction, `_known_device_ids`, atomic publish, and lock-free reads. This is the primary stale-overlay change point.
- `src/open_ems/settings.py`: current pydantic-settings model. Add the threshold here.
- `src/open_ems/web/app.py`: constructs `StateStore(system_clock_status=clock_status)` during lifespan and registers routers.
- `src/open_ems/web/dependencies.py`: owns role/session dependencies and `get_state_store`.
- `src/open_ems/web/state_serialization.py`: current SSE-normalized role-safe serialization.
- `src/open_ems/web/routes/stream.py`: existing SSE generator and sequence behavior that must not regress.
- `src/open_ems/web/routes/homeowner.py` and `src/open_ems/web/routes/installer.py`: current route/template patterns.
- `src/open_ems/web/templates/dashboard.html`: placeholder shared dashboard template; avoid implementing future dashboard scope accidentally.

Expected new files:
- `src/open_ems/web/routes/fragments.py`
- `src/open_ems/web/templates/fragments/homeowner/*.html`
- `tests/unit/web/test_fragment_routes.py`

Expected existing files to modify:
- `src/open_ems/settings.py`
- `src/open_ems/web/app.py`
- `src/open_ems/core/state_store.py`
- `src/open_ems/web/state_serialization.py` if stale retained values need serializer adjustment
- `src/open_ems/web/templates/dashboard.html` or role-specific dashboard templates when fragment targets are added
- `tests/unit/core/test_state_store.py`
- `tests/unit/web/test_state_serialization.py`
- `tests/unit/web/test_stream_routes.py` only if concurrency coverage belongs there

### Critical Implementation Guidance

The risky part is the stale overlay. Story 5.3 must not use omission from a publish cycle as the stale signal. Staleness is based only on the timestamp of the last received successful `DeviceState`.

```text
successful DeviceState received -> retain it as last_successful_state[role]
snapshot construction time captured -> datetime.now(UTC)
device timestamp read -> state.read_at for inverter/battery/ev_charger, state.received_at for grid_meter
data_age_seconds[role] -> floor((captured_at - device_timestamp).total_seconds())
if age > stale_threshold_seconds -> component_states[role] = STALE
snapshot.<role> -> retained successful DeviceState, not DegradedDeviceState
```

If a role is omitted from a publish cycle, that omission may mean "no new successful state was received"; it does not itself mean stale. The retained successful `DeviceState` becomes stale only when its own timestamp is older than the configured threshold at snapshot construction time.

Explicit degraded adapter inputs may remain degraded. Adapter-level `*_stale` reasons are not the primary stale mechanism and must not be converted into `ComponentState.STALE` or stale `DegradedDeviceState`. `ComponentState.STALE` is produced by StateStore's age-based overlay over retained successful device states.

`ComponentState.STALE` must not degrade `GlobalState`. Preserve Story 5.1 semantics: true degraded states and required-role unavailable conditions affect `GlobalState`; stale is freshness metadata for UI and later decision-engine consumers.

Never show stale data as current. The retained values are allowed because the component state and age metadata tell every consumer that they are stale.

Never use zeros as fallback display values. Zero is a real energy value and would mislead homeowners and installers.

### Architecture Constraints

Follow these import boundaries:

```text
core/     -> no imports from adapters/, storage/, web/, or services/
adapters/ -> may import from core/
web/      -> may call services/repositories and read StateStore, but must not call adapters directly
engine/   -> may read StateStore later, but must not call web/
```

Architecture requires:
- StateStore is the only source of UI state.
- StateStore is the only primary stale detector; adapters may report degraded states, but freshness is centralized in StateStore.
- Snapshots are immutable; serializers and templates must not mutate them.
- Role filtering stays server-side.
- Homeowner data excludes installer diagnostics.
- HTMX and SSE should update the same user-visible state with no visible fallback difference.
- State data in interfaces reflects confirmed device state within 30 seconds.
- JSON/API field names remain snake_case and units stay in field names (`_kw`, `_kwh`, `_percent`, `_seconds`).
- Timestamps remain timezone-aware UTC.
- `data_age_seconds` is integer seconds rounded down from snapshot construction time minus the retained successful device timestamp.

### Fragment Contract

HTMX endpoints should return complete, self-contained HTML fragments for replacement. Required homeowner routes:

```text
GET /fragments/homeowner/battery-card
GET /fragments/homeowner/solar-card
GET /fragments/homeowner/grid-card
GET /fragments/homeowner/ev-card
```

If installer fragments are needed in this story, keep installer routes separate under `/fragments/installer/...` and protect them with `require_installer`. Do not render installer-only diagnostics into homeowner fragments.

Each fragment context should include:
- normalized role-safe payload from the same shared transformation path used by SSE
- `component_state`
- integer `data_age_seconds`
- `system_clock_status`
- enough display text to avoid templates re-deriving business logic from raw models

Fragments must be idempotent complete replacements, not patches. Templates may format already-transformed display strings, but they must not infer stale, unavailable, degraded, or role-safe behavior from raw `DeviceState`.

### Previous Story Intelligence

Story 5.2 established:
- `/api/stream/state` already exists and emits `state_update` events with `sequence_id` as SSE `id`.
- `serialize_homeowner_snapshot` is allowlist-only and must never contain a `reason` key.
- `serialize_installer_snapshot` includes installer-safe diagnostics.
- `get_state_store` retrieves `app.state.state_store`.
- `StateStore.get_snapshot()` is intentionally synchronous and lock-free.
- A future or malformed `Last-Event-ID` resets to current snapshot behavior.
- Open SSE streams revalidate sessions and roles before emitting state.

Carry-forward lessons:
- Do not bypass role enforcement in fragment routes.
- Do not duplicate serializer or business transformation logic in templates.
- Keep tests deterministic with injected clocks, explicit timestamps, `asyncio.Event`, or bounded queues.
- Do not let polling or fragment rendering block `StateStore.publish()`.

### Error-Path Table

| Failure or edge path | Expected behavior | Required test signal |
|---|---|---|
| No session on fragment request | HTTP 401 or existing browser/HTMX auth behavior | Unauthenticated fragment route test |
| Homeowner requests explicit homeowner fragment | HTML fragment with homeowner-safe fields only | Homeowner fragment test |
| Installer-only diagnostics | Never rendered in homeowner fragment | Role isolation assertion |
| Device never received state | Component `UNAVAILABLE`; explicit unavailable text; no stale caption | StateStore + fragment test |
| Role omitted from a publish cycle | Omission alone does not create `STALE`; freshness still depends only on retained successful `DeviceState` timestamp | StateStore test |
| Last successful timestamp before threshold | Retained successful value; component not stale | StateStore test |
| Last successful timestamp past threshold | Retained successful value; component `STALE`; integer age set | StateStore test |
| Explicit adapter degraded state with `*_stale` reason | Remains degraded adapter input; does not become primary stale overlay | StateStore test |
| Required role stale but otherwise healthy | `ComponentState.STALE`; `GlobalState` not degraded by stale alone | StateStore test |
| Fresh update after stale | Component returns non-stale; stale caption removed | StateStore + fragment test |
| Threshold env var invalid | Settings validation fails at startup | Settings test |
| HTMX and SSE for same snapshot | Same shared role-safe transformation path and normalized values | Route/serializer comparison test |
| Rapid publishes during SSE reads | Monotonic sequence IDs, no out-of-order reads, coordinated without sleep-based races | Concurrency test |

### Latest Technical Notes

- Current project dependencies are pinned by `uv.lock`; use the existing stack (`fastapi`, `jinja2`, `pydantic-settings`, `htmx` in templates) and do not add new dependencies for fragment polling.
- HTMX official docs define polling with `hx-trigger="every <timing>"` and expect AJAX responses to be HTML fragments. Use that directly for polling fallback. [Source: https://htmx.org/docs/#polling]
- FastAPI's `Jinja2Templates.TemplateResponse` is the existing project pattern for server-rendered HTML. Keep using it for fragments rather than introducing a separate rendering library. [Source: https://fastapi.tiangolo.com/reference/templating/]
- Pydantic Settings reads `BaseSettings` fields from environment variables and parses simple fields such as integers using the field type. Add a normal settings field instead of custom environment parsing. [Source: https://docs.pydantic.dev/latest/concepts/pydantic_settings/]

### Testing Standards

Use current project tooling:
- Python 3.12+
- FastAPI / Starlette through existing FastAPI imports
- Jinja2 through `fastapi.templating.Jinja2Templates`
- pytest and pytest-asyncio
- httpx/TestClient patterns already used in `tests/unit/web`
- strict mypy
- ruff line length 100

Hardware, protocol adapters, real network sockets, and real browser automation are not required for this story. Build deterministic snapshots directly from core device models.

For concurrency tests, use explicit coordination (`asyncio.Event`, bounded `asyncio.Queue`, or a controlled fake reader/publisher schedule). Avoid tests whose correctness depends on arbitrary sleep durations or timing races.

### References

- Story source: [Source: `_bmad-output/planning-artifacts/epics.md`#Story 5.3]
- Epic 5 scope and constraints: [Source: `_bmad-output/planning-artifacts/epics.md`#Epic 5]
- StateStore architecture: [Source: `_bmad-output/planning-artifacts/architecture.md`#Decision 1.1 - In-memory system state model]
- Real-time state transport: [Source: `_bmad-output/planning-artifacts/architecture.md`#Decision 4.1 - Real-time state updates]
- StateStore immutability pattern: [Source: `_bmad-output/planning-artifacts/architecture.md`#Pattern - StateStore snapshots are immutable]
- Import boundaries: [Source: `_bmad-output/planning-artifacts/architecture.md`#Import boundary]
- UX real-time behavior: [Source: `_bmad-output/planning-artifacts/ux-design-specification.md`#Real-Time Update Pattern]
- UX stale/unavailable states: [Source: `_bmad-output/planning-artifacts/ux-design-specification.md`#System State Coverage Rule]
- UX connectivity degradation behavior: [Source: `_bmad-output/planning-artifacts/ux-design-specification.md`#SSE Connection Drop]
- Current StateStore implementation: [Source: `src/open_ems/core/state_store.py`]
- Current snapshot model: [Source: `src/open_ems/core/state.py`]
- Current SSE route: [Source: `src/open_ems/web/routes/stream.py`]
- Current serializers: [Source: `src/open_ems/web/state_serialization.py`]
- Previous story: [Source: `_bmad-output/implementation-artifacts/5-2-implement-sse-state-stream-with-role-filtered-serialization-and-event-metadata.md`]

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Debug Log References

- 2026-05-04: Baseline `uv run python -m pytest tests/ --no-cov -q` passed: 522 passed, 39 warnings.
- 2026-05-04: Baseline `uv run python -m ruff check .` passed.
- 2026-05-04: Baseline `uv run python -m mypy src/` passed.
- 2026-05-04: Focused validation passed for StateStore, settings, serialization, fragment routes, and stream routes: 62 passed, 4 warnings.
- 2026-05-04: Final `uv run python -m ruff check .` passed.
- 2026-05-04: Final `uv run python -m ruff format --check .` passed.
- 2026-05-04: Final `uv run python -m mypy src/` passed.
- 2026-05-04: Final `uv run python -m pytest tests/ --no-cov -q` passed: 539 passed, 43 warnings.

### Completion Notes List

- Added `STALE_THRESHOLD_SECONDS` backed configuration and wired it into application StateStore construction.
- Implemented StateStore-owned stale overlay using retained successful device states and integer floored data age, without converting stale data to degraded states.
- Removed adapter `*_stale` reasons as the primary stale mechanism; explicit degraded adapter input remains degraded/error.
- Added shared homeowner card transformation built from the same homeowner-safe serialization payload used by SSE.
- Added authenticated homeowner HTMX fragment endpoints and templates for battery, solar, grid, and EV cards.
- Wired homeowner dashboard placeholders to poll the fragment endpoints every 10 seconds without adding state-changing CSRF requirements.
- Added and updated tests for stale overlay behavior, role-safe serialization, fragment rendering, route auth, settings validation, and deterministic sequence monotonicity.

### File List

- `_bmad-output/implementation-artifacts/5-3-implement-htmx-polling-endpoints-and-per-device-stale-and-unavailable-detection.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`
- `src/open_ems/core/state.py`
- `src/open_ems/core/state_store.py`
- `src/open_ems/settings.py`
- `src/open_ems/web/app.py`
- `src/open_ems/web/routes/fragments.py`
- `src/open_ems/web/routes/homeowner.py`
- `src/open_ems/web/routes/installer.py`
- `src/open_ems/web/state_serialization.py`
- `src/open_ems/web/templates/dashboard.html`
- `src/open_ems/web/templates/fragments/homeowner/_card.html`
- `src/open_ems/web/templates/fragments/homeowner/battery-card.html`
- `src/open_ems/web/templates/fragments/homeowner/ev-card.html`
- `src/open_ems/web/templates/fragments/homeowner/grid-card.html`
- `src/open_ems/web/templates/fragments/homeowner/solar-card.html`
- `tests/unit/core/test_state.py`
- `tests/unit/core/test_state_store.py`
- `tests/unit/test_settings.py`
- `tests/unit/web/test_fragment_routes.py`
- `tests/unit/web/test_state_serialization.py`

### Review Findings

- [x] [Review][Decision] `GlobalState.stale` is now a dead enum value — retained as reserved-for-future use (Epic 7 / decision engine may repurpose it). No code change. `src/open_ems/core/state.py`
- [x] [Review][Patch] `_card_component_key("grid")` returns `"grid"` but the serialized payload uses key `"grid_meter"` — every request to `/fragments/homeowner/grid-card` raises `KeyError` at runtime [src/open_ems/web/state_serialization.py:_card_component_key]
- [x] [Review][Patch] `is_unavailable` flag is only set when `component_state == "UNAVAILABLE"`, but a `DegradedDeviceState` slot (e.g. "reconnecting") also serializes to `{"state": "unavailable"}` for homeowners — template renders an empty `<dl>` with no "Unavailable" text [src/open_ems/web/state_serialization.py:build_homeowner_card_context]
- [x] [Review][Patch] Test fixtures in `test_state_serialization.py` pass float values (e.g. `0.0`, `1.0`) for `data_age_seconds` after the type was narrowed to `int | None` — Pydantic silently coerces, masking regressions [tests/unit/web/test_state_serialization.py]
- [x] [Review][Patch] No integration-level test verifies that `settings.stale_threshold_seconds` is passed through `app.py` lifespan into `StateStore.__init__` — wiring present but untested end-to-end [src/open_ems/web/app.py:lifespan]
- [x] [Review][Patch] `_card_title(card)` called twice in `build_homeowner_card_context` — once to assign `title` and again inside the `stale_caption` ternary; pass `title` to `_stale_caption` instead [src/open_ems/web/state_serialization.py:build_homeowner_card_context]
- [x] [Review][Defer] `_build_slots` mutates `_known_device_ids` for roles processed before a `_validate_state_role` failure — pre-existing pattern; `_last_successful_states` follows the same already-established convention [src/open_ems/core/state_store.py:_build_slots] — deferred, pre-existing
- [x] [Review][Defer] No dedicated concurrency test combining HTMX `get_snapshot()` calls with rapid `publish()` calls — `get_snapshot()` is lock-free by design; existing SSE concurrency test covers the harder write-path case [tests/unit/core/test_state_store.py] — deferred, pre-existing

### Change Log

- 2026-05-04: Implemented Story 5.3 stale overlay, homeowner HTMX fragments, dashboard polling hooks, and validation coverage.
- 2026-05-04: Code review completed — 1 decision needed, 5 patches, 2 deferred, 9 dismissed.
