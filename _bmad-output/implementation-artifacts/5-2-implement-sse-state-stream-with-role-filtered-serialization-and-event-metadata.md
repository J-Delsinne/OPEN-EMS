# Story 5.2: Implement SSE state stream with role-filtered serialization and event metadata

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a web UI consumer,
I want an SSE endpoint that streams role-filtered, versioned system state snapshots as server-sent events with standard metadata,
so that the homeowner and installer UIs receive live state in real time, each seeing only the data appropriate for their role, with SSE event IDs for ordering and reconnection.

## Acceptance Criteria

**AC1 - Homeowner stream payload is role-filtered**
**Given** an authenticated homeowner opens `/api/stream/state`
**When** `StateStore` publishes a new snapshot
**Then** an SSE event is delivered to the homeowner connection with:
- `event: state_update`
- `id: <sequence_id>`
- JSON `data` containing only homeowner-safe state: global UI state, operating mode summary suitable for homeowner display, battery SOC, PV production, grid draw, EV charger public state, data age/freshness fields needed by the dashboard, and `system_clock_status`
**And** homeowner data excludes installer diagnostics, raw `DegradedDeviceState.reason` values, per-device diagnostic details, capability/debug details, internal exception messages, and any storage/auth/session fields.
**And** homeowner serialization is allowlist-only; do not serialize the full `SystemSnapshot` and delete installer fields afterward.
**And** the homeowner payload must not contain a `reason` key anywhere, including nested degraded or public-unavailable device structures.

**AC2 - Installer stream payload includes diagnostics**
**Given** an authenticated installer opens `/api/stream/state`
**When** `StateStore` publishes a new snapshot
**Then** the SSE event contains the full installer-safe snapshot view including per-component state flags, `DegradedDeviceState` details, data age fields, system clock status, and degraded-mode context
**And** the `id` field in the SSE event carries the same `sequence_id` value regardless of role.

**AC3 - Last-Event-ID reconnection semantics**
**Given** an SSE client reconnects after a disconnect and sends a `Last-Event-ID` header
**When** the server's latest `StateStore` snapshot has a `sequence_id` greater than that value
**Then** the server immediately sends the latest snapshot as a `state_update` event
**And** if the latest snapshot is not newer, the connection remains open and waits for the next published snapshot or keepalive.
**And** `Last-Event-ID` is parsed as a non-negative integer after trimming whitespace.
**And** if parsing fails or the parsed value is negative, the header is ignored and the stream behaves as if no `Last-Event-ID` was supplied; do not return HTTP 400 for malformed `Last-Event-ID`.
**And** the implementation does not promise replay of historical events because Story 5.2 has no persisted event buffer.

**AC4 - Concurrent clients receive ordered events**
**Given** multiple concurrent clients connect to `/api/stream/state` using installer and homeowner sessions across multiple browser tabs
**When** `StateStore` publishes snapshots
**Then** each active connection receives events in monotonically increasing `sequence_id` order
**And** each connection tracks `last_sent_sequence_id`.
**And** a `state_update` event is emitted only when `snapshot.sequence_id > last_sent_sequence_id`.
**And** the same snapshot must never be emitted twice to the same connection.
**And** each SSE connection runs its own independent async generator loop.
**And** the loop must not hold shared locks or await shared resources that could block other connections.
**And** `StateStore.get_snapshot()` remains a fast, synchronous, non-blocking call within the loop.
**And** one slow or disconnected client does not block other clients or the `StateStore.publish()` writer path.

**AC5 - Authentication and role enforcement happen before streaming**
**Given** an unauthenticated request accesses `/api/stream/state`
**When** the route resolves authentication
**Then** it is rejected with HTTP 401 before any SSE frames are sent.
**And** a homeowner receives only homeowner serialization and an installer receives installer serialization.
**And** role filtering is performed server-side at serialization time, not by client-side JavaScript.

**AC6 - Expired sessions close the stream**
**Given** an authenticated user's session expires while an SSE stream is open
**When** the stream loop performs its next session validation check
**Then** the SSE connection is closed with no further events sent after the expiry point
**And** no partial diagnostic payload is emitted while closing.
**And** the stream dependency authenticates before `StreamingResponse` is constructed.
**And** during the stream loop, the authenticated session expiry timestamp is kept in memory.
**And** on each loop iteration, the cached expiry timestamp is checked before reading or emitting state.
**And** the session repository is re-queried only when the cached expiry is reached or passed, or at a small bounded validation interval if the existing dependency design requires it.
**And** if the session is expired, deleted, or no longer valid, the stream closes silently and emits no further `state_update` event.
**And** the SSE stream does not renew or extend session lifetime.

**AC7 - Keepalive comments are sent**
**Given** no SSE frame has been sent for 15 seconds
**When** an SSE stream remains connected
**Then** an SSE comment line `: keepalive` is sent to all active connections to prevent proxy and browser connection timeouts.
**And** keepalive timing is based on time since the last emitted SSE frame, whether that frame was a `state_update` or a keepalive.
**And** frequent `state_update` events reset the keepalive timer, so keepalive is suppressed until 15 seconds after the last emitted frame.

**AC8 - Tests**
**And** unit tests verify homeowner serialization excludes installer diagnostics and raw degraded reasons.
**And** unit tests verify installer serialization includes degraded details and component state flags.
**And** unit tests verify SSE events include `event: state_update`, `id: <sequence_id>`, and JSON `data` fields.
**And** integration tests verify unauthenticated requests return 401 before streaming.
**And** integration tests verify role-specific sessions receive role-specific payloads.
**And** integration tests verify `Last-Event-ID` causes the latest newer snapshot to be sent immediately and does not replay old history.
**And** tests verify negative `Last-Event-ID` is treated as malformed and ignored.
**And** tests verify the same snapshot is not emitted twice to one SSE connection.
**And** integration tests verify a new `StateStore` snapshot triggers an SSE event on all active connections within 30 seconds.
**And** tests verify frequent `state_update` events suppress keepalive until 15 seconds after the last emitted frame.
**And** concurrency tests verify slow or disconnected clients do not block other active stream clients.
**And** tests verify stream closure on session expiry or deleted session.

## Tasks / Subtasks

- [x] **Task 0: Pre-story quality gate** (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` and record the baseline
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m mypy src/`

- [x] **Task 1: Add role-specific StateStore serializers** (AC: AC1, AC2, AC8)
  - [x] Create `src/open_ems/web/state_serialization.py` or equivalent web-layer module
  - [x] Implement `serialize_homeowner_snapshot(snapshot: SystemSnapshot) -> dict[str, object]`
  - [x] Implement `serialize_installer_snapshot(snapshot: SystemSnapshot) -> dict[str, object]`
  - [x] Use `SystemSnapshot` and device state models from `open_ems.core`; do not read adapters, storage, or repositories during serialization
  - [x] Serialize datetimes as ISO 8601 UTC strings and enums as their `.value`
  - [x] Preserve exact units and field names from domain models, including `_kw`, `_kwh`, `_percent`, and `_seconds`
  - [x] Ensure homeowner serialization never includes raw `DegradedDeviceState.reason`, `device_id` for diagnostic-only degraded state, stack traces, session IDs, CSRF tokens, user IDs, or installer-only limitation/debug text
  - [x] Build homeowner serialization from an allowlist only; do not serialize a full `SystemSnapshot` and remove fields afterward
  - [x] Ensure the homeowner payload contains no `reason` key anywhere, including nested degraded or public-unavailable device structures
  - [x] Ensure installer serialization includes diagnostics needed by Epic 11 without leaking auth/session internals

- [x] **Task 2: Expose the app-wide StateStore to the web layer** (AC: AC1, AC2, AC4)
  - [x] Update `src/open_ems/web/app.py` to instantiate `StateStore(system_clock_status=clock_status)` during lifespan after the clock check
  - [x] Store it on `app.state.state_store`
  - [x] Keep startup sequence aligned with Story 5.1: startup layer passes clock status into StateStore; `core/` does not import `services/time_sync.py`
  - [x] Provide a small dependency/helper for retrieving the app StateStore in tests and routes; fail clearly if missing
  - [x] Do not add adapter polling, control-loop publishing, HTMX fragments, or stale-threshold configuration in this story

- [x] **Task 3: Implement SSE route** (AC: AC1-AC7)
  - [x] Create `src/open_ems/web/routes/stream.py`
  - [x] Add `GET /api/stream/state`
  - [x] Authenticate with a combined route dependency that accepts installer or homeowner sessions and returns the authenticated role and session expiry before constructing `StreamingResponse`
  - [x] Return `StreamingResponse` with media type `text/event-stream`
  - [x] Set stream headers appropriate for SSE: `Cache-Control: no-cache`, `X-Accel-Buffering: no`, and no response compression
  - [x] Format events exactly as SSE text blocks: optional comment lines, `event: state_update`, `id: <sequence_id>`, one or more `data: <json>` lines, blank line terminator
  - [x] Track `last_sent_sequence_id` per connection
  - [x] Send the current snapshot immediately when the request has no `Last-Event-ID`
  - [x] On `Last-Event-ID`, send the latest snapshot immediately only if `snapshot.sequence_id > parsed_last_event_id`
  - [x] Parse `Last-Event-ID` as a trimmed non-negative integer; if it is malformed or negative, ignore it and send the current snapshot rather than failing the stream
  - [x] Emit a `state_update` only when `snapshot.sequence_id > last_sent_sequence_id`; never emit the same snapshot twice to one connection
  - [x] Use simple bounded polling of `StateStore.get_snapshot()`
  - [x] Use a bounded async sleep interval between polling loop iterations
  - [x] Keep the polling interval between 100ms and 1 second; recommended default is 250ms
  - [x] Ensure the polling interval prevents busy-loop CPU usage while still meeting the 30-second UI update SLA
  - [x] Allow tests to override the polling interval via dependency injection or configuration for deterministic timing
  - [x] Do not modify `StateStore.publish()` to know about web clients
  - [x] Do not add a pub/sub notifier in this story
  - [x] Send keepalive comments only when no SSE frame has been emitted for 15 seconds; frequent `state_update` events reset this timer
  - [x] Check `await request.is_disconnected()` in the loop and exit promptly
  - [x] Check cached session expiry on each loop iteration before reading or emitting state
  - [x] Re-query the session repository only when cached expiry is reached/passed, or at a small bounded validation interval if needed by the dependency design
  - [x] Close silently when the session is expired, deleted, or invalid
  - [x] Do not renew or extend session lifetime from the SSE stream

- [x] **Task 4: Register the stream route** (AC: AC1-AC7)
  - [x] Update `src/open_ems/web/app.py` to include `stream_router`
  - [x] Keep route imports consistent with existing `auth`, `health`, `installer`, and `homeowner` routers
  - [x] Ensure CSRF middleware does not block this GET route

- [x] **Task 5: Add serialization tests** (AC: AC1, AC2, AC8)
  - [x] Add `tests/unit/web/test_state_serialization.py`
  - [x] Build representative `SystemSnapshot` fixtures using `InverterState`, `BatteryState`, `EVChargerState`, `GridMeterState`, and `DegradedDeviceState`
  - [x] Assert homeowner payload includes only public dashboard-safe fields
  - [x] Assert homeowner payload excludes raw degraded reasons and per-device diagnostics
  - [x] Assert homeowner payload contains no `reason` key anywhere, including nested degraded or public-unavailable device structures
  - [x] Assert installer payload includes component states, data ages, degraded reasons, and degraded device metadata
  - [x] Assert enum values and datetime strings are JSON serializable without custom client code

- [x] **Task 6: Add stream route integration and concurrency tests** (AC: AC3-AC8)
  - [x] Add `tests/unit/web/test_stream_routes.py` or `tests/integration/web/test_stream_routes.py`
  - [x] Use the existing session/user repository fixtures and `TestClient`/httpx streaming support
  - [x] Verify unauthenticated request returns 401 and sends no SSE frame
  - [x] Verify installer and homeowner cookies produce different payload shapes for the same snapshot
  - [x] Verify the SSE frame contains `event: state_update`, `id: <sequence_id>`, and parseable JSON `data`
  - [x] Verify `Last-Event-ID` behavior for older, equal, and malformed header values
  - [x] Verify negative `Last-Event-ID` is treated as malformed and ignored
  - [x] Verify the same snapshot is not emitted twice to one SSE connection
  - [x] Verify frequent `state_update` events suppress keepalive until 15 seconds after the last emitted frame
  - [x] Verify deleted or expired sessions close the stream on the next validation check
  - [x] Verify multiple connected clients can receive the same later snapshot sequence without blocking each other

- [x] **Task 7: Final validation** (AC: all)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/ --no-cov -q`
  - [x] Close or explicitly defer every review finding before marking this story done

### Review Findings

- [x] [Review][Patch] Treat future/stale Last-Event-ID values as reset and emit the current snapshot [src/open_ems/web/routes/stream.py:72]
- [x] [Review][Patch] Deleted sessions can keep receiving state until cached expiry [src/open_ems/web/routes/stream.py:79]
- [x] [Review][Patch] Role or user revocation is not revalidated for open streams [src/open_ems/web/dependencies.py:195]
- [x] [Review][Patch] Exact expiry boundary is treated as still valid [src/open_ems/web/dependencies.py:171]
- [x] [Review][Patch] Missing authenticated role-specific stream integration test [tests/unit/web/test_stream_routes.py:109]
- [x] [Review][Patch] Missing Last-Event-ID stream behavior coverage for older and malformed values [tests/unit/web/test_stream_routes.py:120]
- [x] [Review][Patch] Missing slow/disconnected client concurrency coverage [tests/unit/web/test_stream_routes.py:263]

## Dev Notes

### Context and Scope

Epic 5 establishes the single source of truth for system state and real-time distribution. Story 5.2 owns the SSE transport and role-aware serialization of existing `SystemSnapshot` values.

This story does:
- Add `GET /api/stream/state`.
- Stream `StateStore` snapshots as SSE `state_update` events.
- Apply installer/homeowner filtering on the server side at serialization.
- Use `sequence_id` as the SSE event `id`.
- Implement reconnect, keepalive, authentication, session-expiry closure, and test coverage.

This story does not:
- Implement HTMX polling fragments or configurable stale thresholds; that is Story 5.3.
- Implement adapter polling, runtime control loop publishing, decision engine behavior, or degraded-mode policy; those are later epics.
- Persist stream events or replay historical snapshots.
- Add WebSocket transport for UI state; WebSocket remains reserved for OCPP.
- Add a new SSE dependency unless there is a clear reason. The current dependency set already includes FastAPI/Starlette streaming primitives.

### Current Codebase State

Existing files to read before implementation:
- `src/open_ems/core/state.py` defines `GlobalState`, `ComponentState`, `SystemOperatingMode`, immutable `SystemSnapshot`, device role maps, and derivation helpers.
- `src/open_ems/core/state_store.py` defines `StateStore.publish(...)` and synchronous lock-free `StateStore.get_snapshot()`.
- `src/open_ems/core/devices.py` defines the device domain models and `DegradedDeviceState` used inside snapshots.
- `src/open_ems/web/app.py` owns lifespan startup and router registration. It already computes `clock_status` with `check_clock(...)`; this is the correct place to construct `StateStore`.
- `src/open_ems/web/dependencies.py` owns session lookup, expiry handling, role enforcement, and session cookie renewal. It currently exposes role-specific `require_installer` and `require_homeowner`; Story 5.2 needs either a shared internal auth resolver or a stream-specific dependency that accepts both roles without duplicating unsafe session logic.
- `src/open_ems/web/routes/installer.py` and `src/open_ems/web/routes/homeowner.py` show existing route style and dependency usage.
- `tests/conftest.py` provides users/sessions table DDL and repository fixtures.
- `tests/unit/web/test_dependencies.py` and `tests/unit/web/test_auth_routes.py` show the expected auth/session test style.

Expected new files:
- `src/open_ems/web/routes/stream.py`
- `src/open_ems/web/state_serialization.py`
- `tests/unit/web/test_state_serialization.py`
- `tests/unit/web/test_stream_routes.py` or an equivalent integration test file

Expected existing files to modify:
- `src/open_ems/web/app.py`
- `src/open_ems/web/dependencies.py` if a shared `require_authenticated_user` helper is needed
- `src/open_ems/web/routes/__init__.py` only if the project starts using router exports there

### Architecture Constraints

Follow these import boundaries:

```text
core/     -> no imports from adapters/, storage/, web/, or services/
adapters/ -> may import from core/
web/      -> may call services/repositories and read StateStore, but must not call adapters directly
engine/   -> may read StateStore later, but must not call web/
```

Architecture requires:
- `StateStore` is the only source of UI state; the stream route must not read device adapters directly.
- State snapshots are immutable; serializers must not mutate snapshots or nested mappings.
- Role filtering is applied at the serialization point.
- The homeowner payload excludes installer diagnostics; the installer payload may include degraded-mode details.
- SSE interval defaults to the UX contract of 10-second real-time updates, with keepalive comments at 15 seconds when no state changes occur.
- Web UI data must reflect actual device state within 30 seconds.

### Serialization Contract

Recommended event shape:

```text
event: state_update
id: 12
data: {"sequence_id":12,"captured_at":"2026-05-03T21:30:00+00:00",...}

```

Recommended homeowner payload fields:
- `sequence_id`
- `captured_at`
- `global_state`
- `operating_mode`
- `system_clock_status`
- `data_age_seconds`
- `battery`: public SOC/power fields if present, public unavailable/stale state if degraded
- `inverter`: public PV/ac power fields if present, public unavailable/stale state if degraded
- `grid_meter`: public grid power and energy counters if present, public unavailable/stale state if degraded
- `ev_charger`: public status, session_active, and current power fields if present
- `components`: public component state values only, not raw diagnostic reasons

Recommended installer payload fields:
- All homeowner fields
- `component_states`
- full `data_age_seconds`
- per-device `device_id`
- per-device degraded details: `role`, `reason`, `occurred_at`
- explicit `operating_mode` and `global_state`

Do not include:
- session ID, user ID, CSRF token, token hashes, password state, or cookies
- raw protocol payloads
- stack traces or exception text
- repository/database rows

Use Pydantic `model_dump(mode="json")` only inside explicit per-device allowlist helpers where it preserves the required field shape. Homeowner serialization must be allowlist-only. Do not serialize a whole `SystemSnapshot` and delete fields afterward for homeowner data. The homeowner payload must not contain a `reason` key anywhere.

### Previous Story Intelligence

Story 5.1 established:
- `SystemSnapshot.sequence_id` is monotonic and increments by exactly 1 for every publish.
- `SystemSnapshot.captured_at` is timezone-aware UTC and generated by `StateStore` at aggregation time.
- `SystemSnapshot` is immutable and nested mappings are mapping proxies.
- `get_snapshot()` is synchronous and lock-free; SSE can call it without awaiting writer locks.
- `StateStore.publish()` is the only writer path and must not depend on web clients.
- Runtime-known devices remain represented as `DegradedDeviceState(reason="unavailable")` when omitted from later publishes.
- Required roles are `grid_meter` and `inverter`; optional roles are `battery` and `ev_charger`.
- `system_clock_status` is already part of every snapshot and must be exposed by Story 5.2 serializers.

Carry-forward review lessons from Story 5.1:
- Preserve role/state matching; never reinterpret a device state under a different role.
- Treat unknown degraded reasons as degraded diagnostics for installer, but do not leak them to homeowner.
- Keep tests deterministic and explicit for concurrency behavior; avoid arbitrary long sleeps.

### Error-Path Table

| Failure or edge path | Expected behavior | Required test signal |
|---|---|---|
| No session cookie | Return HTTP 401 before streaming | Unauthenticated stream test |
| Invalid session token | Return HTTP 401 before streaming | Invalid-cookie stream test |
| Homeowner session | Stream homeowner allowlist payload only | Role isolation test |
| Installer session | Stream installer diagnostic payload | Installer serializer test |
| Session expires or is deleted mid-stream | Exit stream loop without another data event | Expiry/deleted session test |
| Client disconnects | `request.is_disconnected()` causes loop exit | Disconnect/unit loop test where feasible |
| `Last-Event-ID` older than latest snapshot | Send latest snapshot immediately | Reconnect test |
| `Last-Event-ID` equal to latest snapshot | Wait for newer snapshot or keepalive | Reconnect test |
| `Last-Event-ID` malformed | Ignore header and send current snapshot | Malformed header test |
| `Last-Event-ID` negative | Treat as malformed; ignore header and send current snapshot | Negative header test |
| Same snapshot observed on repeated polling | Do not emit duplicate `state_update` to the same connection | Duplicate-suppression test |
| No SSE frame for 15 seconds | Send `: keepalive` comment | Keepalive test |
| Frequent `state_update` events | Reset keepalive timer; no keepalive until 15 seconds after the last emitted frame | Keepalive suppression test |
| Multiple clients connected | Events delivered independently in sequence order | Concurrency test |
| Slow client | Does not block another client or `StateStore.publish()` | Concurrency/nonblocking test |
| Snapshot contains `DegradedDeviceState(reason="validation_error:x")` | Installer sees reason; homeowner sees generic degraded/unavailable state only | Role serialization test |

### Latest Technical Notes

- FastAPI re-exports Starlette response classes; `StreamingResponse` accepts an async generator and streams the response body. Use it with `media_type="text/event-stream"` rather than adding a new dependency unless the implementation needs a full EventSource helper. [Source: https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse]
- Starlette documents `request.is_disconnected()` for long-polling or streaming responses; use it to exit the SSE loop promptly when the browser disconnects. [Source: https://www.starlette.io/requests/#request]
- MDN and the HTML standard define SSE streams as UTF-8 text with `text/event-stream`; `event`, `id`, and `data` fields are line-oriented and an empty line dispatches the event. Comments start with `:` and are valid keepalives. [Source: https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events] [Source: https://html.spec.whatwg.org/dev/server-sent-events.html]
- Starlette's GZip middleware intentionally avoids compressing `text/event-stream`; keep that media type exact so proxies/middleware do not buffer or compress SSE frames. [Source: https://www.starlette.io/middleware/#gzipmiddleware]

### Testing Standards

Use current project tooling:
- Python 3.12+
- FastAPI and Starlette through existing FastAPI imports
- pytest and pytest-asyncio
- httpx/TestClient patterns already used in `tests/unit/web`
- strict mypy
- ruff line length 100

Test data should build snapshots directly from core models. Do not use real hardware, protocol adapters, or network sockets. Keep concurrency tests deterministic with `asyncio.Event`, bounded queues, and short explicit test hooks where needed.

### References

- Story source: [Source: `_bmad-output/planning-artifacts/epics.md`#Story 5.2]
- Epic 5 constraints: [Source: `_bmad-output/planning-artifacts/epics.md`#Cross-story constraints applying to all Epic 5 stories]
- SSE architecture: [Source: `_bmad-output/planning-artifacts/architecture.md`#Frontend Architecture]
- Role-aware SSE decision: [Source: `_bmad-output/planning-artifacts/architecture.md`#Decision 4.1 - State update transport]
- StateStore architecture: [Source: `_bmad-output/planning-artifacts/architecture.md`#Decision 1.1 - In-memory system state model]
- Import boundaries: [Source: `_bmad-output/planning-artifacts/architecture.md`#Import Boundaries]
- UX real-time behavior: [Source: `_bmad-output/planning-artifacts/ux-design-specification.md`#Feedback - real-time updates]
- UX SSE contract and fallback: [Source: `_bmad-output/planning-artifacts/ux-design-specification.md`#SSE / HTMX Polling Contract]
- Current StateStore implementation: [Source: `src/open_ems/core/state_store.py`]
- Current snapshot models: [Source: `src/open_ems/core/state.py`]
- Current auth dependencies: [Source: `src/open_ems/web/dependencies.py`]
- Current app lifespan/router registration: [Source: `src/open_ems/web/app.py`]
- Previous story: [Source: `_bmad-output/implementation-artifacts/5-1-define-system-state-model-and-implement-statestore-with-immutable-versioned-snapshots.md`]

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Debug Log References

- 2026-05-03T23:46:24+02:00 baseline: `uv run python -m pytest tests/ --no-cov -q` passed, 496 passed, 39 warnings.
- 2026-05-03T23:46:24+02:00 baseline: `uv run python -m ruff check .` passed.
- 2026-05-03T23:46:24+02:00 baseline: `uv run python -m mypy src/` passed.
- 2026-05-03T23:54:xx+02:00 final: `uv run python -m ruff check .` passed.
- 2026-05-03T23:54:xx+02:00 final: `uv run python -m ruff format --check .` passed.
- 2026-05-03T23:54:xx+02:00 final: `uv run python -m mypy src/` passed.
- 2026-05-03T23:54:xx+02:00 final: `uv run python -m pytest tests/ --no-cov -q` passed, 515 passed, 39 warnings.

### Completion Notes List

- Implemented allowlist-only homeowner snapshot serialization and installer-safe diagnostic serialization.
- Added app-wide `StateStore` creation during lifespan after the clock check and a dependency that fails clearly when the store is unavailable.
- Added `/api/stream/state` SSE route with role-aware server-side serialization, Last-Event-ID parsing, per-connection duplicate suppression, bounded polling, keepalive comments, disconnect checks, and silent session-expiry/deletion closure.
- Added focused tests for serialization, StateStore dependency retrieval, SSE event formatting, unauthenticated rejection, Last-Event-ID behavior, duplicate suppression, keepalive timing, stream closure, and concurrent independent stream generators.
- Addressed code review findings: stale/future Last-Event-ID reset behavior, session deletion validation before emits, role/user revocation validation, exact expiry boundary handling, and missing AC8 test coverage.

### File List

- `src/open_ems/web/app.py`
- `src/open_ems/web/dependencies.py`
- `src/open_ems/web/routes/stream.py`
- `src/open_ems/web/state_serialization.py`
- `tests/unit/web/test_state_serialization.py`
- `tests/unit/web/test_state_store_dependency.py`
- `tests/unit/web/test_stream_routes.py`

### Change Log

- 2026-05-03: Implemented Story 5.2 SSE state stream, role-filtered serializers, app StateStore wiring, stream auth/session validation, and test coverage.
- 2026-05-04: Addressed code review findings and marked story done.
