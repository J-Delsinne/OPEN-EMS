# Story 9.X: Wire adapter map into PolicyGuard and ControlLoop

Status: done
_Status set to done by bmad-code-review on 2026-05-11 after review-closure gate passed (5b): 21 resolved, 0 dismissed, 3 deferred-and-verified._

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

> **Type:** architectural prep gate before Epic 10. Full BMAD story.
> **A2-triggered:** **Yes.** Five triggers match: **T1 (lifecycle), T3 (persistence+recovery), T5 (multi-adapter coordination), T6 (deployment/restart), T7 (installer workflow orchestration).** All seven R1–R7 artifacts are mandatory and rendered below.
> **Closes deferred finding:** Story 8-2 `[app.py:223-236]` "`adapters={}` in production silently rejects all commands" (per Epic 9 retro 2026-05-11, action item A11). This story is the canonical resolution of that finding; deferred-work.md entry must be marked closed at story-done.
> **Hard before:** Epic 10 Story 10.2. Without 9-X, Story 10.2's EV override `Optimistic → Confirmed` transition can never fire — PolicyGuard rejects every `/actions/ev-override` POST at P1 (`adapter_not_registered`) and `EVChargerState.session_active=true` is never observed in StateStore.

## Story

As the OPEN-EMS platform operator preparing for the first homeowner-visible control surface,
I want `PolicyGuard` and `ControlLoop` to be constructed with a real adapter map built from the installer-acknowledged `device_registry` (rather than the placeholder `adapters={}` shipped through Stories 8-2 / 9-1 / 9-4 / 9-5),
so that authorized commands produced by `IntentExecutor` or any future override surface (Epic 10 `/actions/ev-override`, `/actions/set-strategy`) actually reach the configured Modbus / OCPP / DSMR adapters instead of being short-circuited at P1 (`adapter_not_registered`), and so that ControlLoop polls real telemetry from the installer-configured devices into `StateStore`.

## Acceptance Criteria

**Given** the runtime adapter map is the single missing link between the wizard-acknowledged `device_registry` and PolicyGuard's dispatch surface

**When** this story is implemented

**Then** the following acceptance criteria must hold:

1. **AC1 — Runtime adapter wiring module exists.** A new module `src/open_ems/services/runtime_adapter_wiring.py` (name MAY be revised by the dev agent; placement under `services/` is required) exposes a single public function with this exact signature:

   ```python
   async def build_runtime_adapter_map(
       *,
       device_repo: DeviceRepo,
       ocpp_central_system: OCPPCentralSystem,
       settings: Settings,
   ) -> RuntimeAdapterMap
   ```

   where `RuntimeAdapterMap` is a `@dataclass(frozen=True)` containing:
   - `policy_guard_adapters: Mapping[DeviceRole, DeviceAdapter]` — passed to `PolicyGuard(adapters=...)`
   - `control_loop_adapters: Mapping[DeviceRole, DeviceAdapter]` — passed to `ControlLoop(adapters=...)`

   Both fields are populated from the SAME underlying construction pass. `control_loop_adapters` is a strict superset of `policy_guard_adapters`: it additionally includes `DeviceRole.grid_meter` (DSMR read-only telemetry). `policy_guard_adapters` MUST NOT contain `DeviceRole.grid_meter` (DSMR P1 is read-only per Story 8-2 / `open_ems.adapters` clause 1; PolicyGuard authorizing a grid_meter command is a structurally-impossible state — see R3 #2).

2. **AC2 — Source of truth is `device_registry`.** The builder reads `DeviceRepo.list_all()` and filters to rows where `validated is True AND role is not None`. No other source is consulted for the runtime adapter set. The builder MUST NOT call `DiscoveryService.probe_*` and MUST NOT issue any network I/O of its own during construction — adapter probing is owned by Story 9-4's `ProtocolAdapterFactory` (validation surface) and the per-tick `ControlLoop._poll_adapters()` (runtime surface). The builder is synchronous-fast in spirit; its `async` signature exists solely to permit one `await device_repo.list_all()` call.

3. **AC3 — Per-protocol × per-role construction.** The builder constructs adapters per the table below. Any combination NOT listed raises `RuntimeAdapterWiringError` with a structured message naming the unsupported `(protocol, role)` tuple — fail loud at construction time, never at first dispatch:

   | Protocol | Role | Adapter constructed | Goes into `policy_guard_adapters`? | Goes into `control_loop_adapters`? |
   |---|---|---|---|---|
   | `modbus_tcp` | `inverter` | `InverterAdapter(device_id, ModbusTcpAdapter(host, port, ...), model)` | No (v1 inverters have empty `write_capabilities`; including them is a no-op but the builder includes them anyway for telemetry-symmetry — see R3 #6) | Yes |
   | `modbus_tcp` | `battery` | `BatteryAdapter(device_id, ModbusTcpAdapter(host, port, ...), model)` | Yes | Yes |
   | `ocpp_1_6` | `ev_charger` | Resolved via `ocpp_central_system.get_adapter(charge_point_id=device_id)` — see AC4 for OCPP-pre-boot handling | Yes | Yes |
   | `dsmr_p1` | `grid_meter` | `DSMRP1Adapter(device_id, ...)` constructed against `entry.address` | **No** (read-only, P1 contract clause 1) | Yes |

   Address parsing reuses the helpers proven by `ProtocolAdapterFactory` (`_parse_host_port` for Modbus, `_parse_dsmr_address` for DSMR) — extract them to a shared module if they're not already; do NOT duplicate the parsing logic.

4. **AC4 — OCPP pre-boot window.** OCPP chargers initiate the WebSocket connection (per `OCPPCentralSystem` docstring and `ProtocolAdapterFactory._probe_ocpp`). At lifespan startup, the charger may not yet have sent `BootNotification`, so `ocpp_central_system.get_adapter(charge_point_id)` returns `None`. The builder MUST handle this case by inserting a `_DeferredOCPPAdapter` proxy into the `ev_charger` slot. The proxy:

   - implements the `DeviceAdapter` protocol (`device_id`, `connect`, `disconnect`, `get_capabilities`, `get_state`, `send_command`);
   - on every invocation, calls `self._ocpp_central_system.get_adapter(self._charge_point_id)`;
   - if the resolution returns `None`: returns `DegradedDeviceState(device_id, role=DeviceRole.ev_charger, reason="ocpp_charger_not_connected", occurred_at=datetime.now(UTC))` from `get_state()`, raises `RuntimeError("ocpp_charger_not_connected")` from `get_capabilities()`, and returns `CommandResult(status=failed, applied=False, reason="ocpp_charger_not_connected", correlation_id=command.correlation_id, device_id=command.device_id)` from `send_command()`;
   - if resolution succeeds: delegates the call verbatim.

   The proxy approach (rather than "omit the slot until boot, then re-wire") is mandated because the alternative requires mutating PolicyGuard's `Mapping[DeviceRole, DeviceAdapter]` after construction — which Story 8-2's contract explicitly forbids (PolicyGuard receives an immutable Mapping at construction time).

5. **AC5 — `OCPPCentralSystem` is constructed and wired in lifespan.** Currently `web/app.py` passes `ocpp_central_system=None` to both `DeviceDiscoveryOrchestrator` (line 322) and `ProtocolAdapterFactory` (line 359), with comments referring to a future story. 9-X is that story. Lifespan step 4d (orchestrator) and step 4g (factory) MUST be updated to receive the real `OCPPCentralSystem` instance, and the OCPP central system MUST be constructed in a new lifespan step inserted BEFORE step 4d (the orchestrator depends on it).

6. **AC6 — Lifespan integration.** A new lifespan step (suggested label: "Step 4h — runtime adapter wiring", inserted between current step 4g and the construction of `PolicyGuard` at app.py:476) runs:

   ```python
   runtime_adapters = await build_runtime_adapter_map(
       device_repo=device_repo,
       ocpp_central_system=ocpp_central_system,
       settings=settings,
   )
   ```

   `PolicyGuard(...)` and `ControlLoop(...)` MUST then be constructed with `adapters=runtime_adapters.policy_guard_adapters` and `adapters=runtime_adapters.control_loop_adapters` respectively. The two existing literal `adapters={}` arguments at app.py:478 and app.py:490 MUST be deleted.

7. **AC7 — Empty-registry cold-start.** When `device_repo.list_all()` returns zero validated+role-assigned rows (the pre-installer-wizard-complete state — the default state of a fresh deployment), `build_runtime_adapter_map` MUST return `RuntimeAdapterMap(policy_guard_adapters={}, control_loop_adapters={})`. Lifespan MUST log this case at INFO with event `runtime_adapter_map_empty` and field `reason="pre_installer_wizard_complete"`. PolicyGuard's existing P1 behavior (`adapter_not_registered`) handles every command in this state; this is the documented cold-start contract — the system remains operational for read-only endpoints and the installer wizard.

8. **AC8 — Non-empty wiring is logged at construction.** When `policy_guard_adapters` is non-empty, lifespan logs at INFO with event `runtime_adapter_map_built` and fields `policy_guard_role_count`, `control_loop_role_count`, and a structured `adapters` field listing `[(role, protocol, device_id), ...]` for traceability. No device addresses, no secrets — `device_id` is the only stable identifier surfaced.

9. **AC9 — Builder failure is fail-loud (matches migration-failure pattern).** If `build_runtime_adapter_map` raises any exception (parse failure, multi-device-per-role assertion, unsupported `(protocol, role)`, ModbusTcpAdapter constructor failure), lifespan MUST log `startup_failed` with `reason="runtime_adapter_wiring_failed"` and structured detail, then `raise SystemExit(1) from None` — identical handling to existing `migration_failed`, `constraints_hydrate_failed`, and `capability_registry_drift` paths. Partial wiring is NOT permitted: either the full map constructs or the process exits.

10. **AC10 — Adapter cleanup on shutdown.** Lifespan's shutdown path (the existing block starting at app.py:509 `sd_notify("STOPPING=1")`) MUST close every adapter constructed by the wiring. The shutdown order is: control_loop cancel → adapter `disconnect()` loop (per-adapter try/except so one slow/failed disconnect does not block others) → existing watchdog/cleanup/pruning cancellation. The `_DeferredOCPPAdapter` proxy's `disconnect()` is a no-op (the underlying charger adapter is owned by `OCPPCentralSystem`).

11. **AC11 — Same instance shared by PolicyGuard and ControlLoop for overlapping roles.** For every role present in BOTH `policy_guard_adapters` and `control_loop_adapters`, the `DeviceAdapter` instance MUST be `is`-identical. The builder MUST NOT construct two `BatteryAdapter` instances for the same physical device — one for PolicyGuard and one for ControlLoop. A regression test asserts this with `id()` or `is`.

12. **AC12 — Multi-device-per-role is forbidden.** Story 9-2's role-assignment service guarantees one device per non-NULL role, but the builder MUST defend against a tampered DB by detecting this case (two `device_registry` rows with the same non-NULL role) and raising `RuntimeAdapterWiringError` with a structured message naming the conflicting `device_id`s. Defense in depth on top of 9-2's invariant.

13. **AC13 — Unit tests.** New tests in `tests/unit/services/test_runtime_adapter_wiring.py` (new file) cover:
    - empty registry → empty `RuntimeAdapterMap`;
    - one validated+role-assigned `modbus_tcp` + `battery` row → BatteryAdapter in BOTH `policy_guard_adapters` and `control_loop_adapters`; same instance (`is`);
    - one validated+role-assigned `dsmr_p1` + `grid_meter` row → adapter present in `control_loop_adapters` only; absent from `policy_guard_adapters`;
    - one validated+role-assigned `ocpp_1_6` + `ev_charger` row where charger is NOT yet booted → `_DeferredOCPPAdapter` proxy in both maps; proxy's `get_state()` returns DegradedDeviceState; proxy's `send_command()` returns CommandResult with `reason="ocpp_charger_not_connected"`; proxy's `get_capabilities()` raises `RuntimeError("ocpp_charger_not_connected")` (PolicyGuard's P2 will catch this and emit `capability_check_failed`);
    - same scenario after `ocpp_central_system.register(...)` populates the underlying adapter → proxy now delegates correctly;
    - validated but unrolled device (role=None) → excluded;
    - unvalidated but rolled device (validated=False, role=...) → excluded (should never happen per 9-2, but defended);
    - unsupported (protocol, role) tuple (e.g., `dsmr_p1` + `battery`) → `RuntimeAdapterWiringError`;
    - two rows with the same role → `RuntimeAdapterWiringError`;
    - malformed `modbus_tcp` address (no colon, port>65535) → `RuntimeAdapterWiringError`.

14. **AC14 — Integration test.** A new test under `tests/integration/web/` (suggested `test_app_runtime_adapter_wiring.py`) constructs the FastAPI app via the standard test fixture against an in-memory SQLite, seeds `device_registry` with one validated+role-assigned device per (`battery`, `grid_meter`), starts the lifespan, and asserts:
    - `app.state.state_store` is populated;
    - the lifespan log stream contains `runtime_adapter_map_built` with `policy_guard_role_count=1` and `control_loop_role_count=2`;
    - PolicyGuard (accessible via the dispatch path or directly through a test seam) authorizes a `SetBatteryChargeRateCommand` past P1 (no `adapter_not_registered`) — the dispatch may still fail at P2 against a fake adapter, but P1 MUST NOT fire.
    - Sibling regression: starting lifespan against an EMPTY `device_registry` produces `runtime_adapter_map_empty` and PolicyGuard rejects with `adapter_not_registered` (the cold-start contract is unchanged).

15. **AC15 — Story 8-2 deferred-finding closure.** The deferred-work.md entry `[app.py:223-236]` ("`adapters={}` in production silently rejects all commands") MUST be marked as RESOLVED in deferred-work.md with a one-line reference to `9-X-wire-adapter-map-into-policy-guard-and-control-loop` and the date. Per A11 of the Epic 9 retro (2026-05-11), this is a hard part of story-done.

16. **AC16 — Stale "validation-only" comments updated.** `src/open_ems/services/protocol_adapter_factory.py` lines 10-14 ("PolicyGuard's `adapters` mapping is unchanged ... A dedicated future story (provisional `9-X-wire-adapter-map-into-policy-guard-and-control-loop`) owns that surface") are now stale. Update the docstring to reflect that 9-X HAS landed: the factory remains validation-only by design (its probes go through `DiscoveryService` read-only primitives), but the parenthetical referencing 9-X as future work MUST be removed or updated to past tense.

17. **AC17 — Quality gates remain green.**
    - `uv run python -m pytest tests/ --no-cov -q` → all tests pass; net delta should be POSITIVE (new unit + integration tests). The post-9-Y-a baseline is 1415 + 5 xfailed.
    - `uv run python -m ruff check .` → clean across the project.
    - `uv run python -m ruff format --check .` → clean.
    - `uv run python -m mypy src/` → clean across all source files (the runtime adapter wiring module is new typed code; the `RuntimeAdapterMap` dataclass and `_DeferredOCPPAdapter` MUST type-check cleanly).

18. **AC18 — `epics.md` Epic 10 introduction amended.** Per Epic 9 retro Part 2 / sequencing step 6, `_bmad-output/planning-artifacts/epics.md` Epic 10 introduction (around line 2072) MUST be amended to declare `9-X` as the prerequisite story. One sentence is sufficient — e.g. "**Prerequisite:** Story 9.X (Wire adapter map into PolicyGuard and ControlLoop) is a hard gate before Story 10.2; Stories 10.1, 10.3, 10.4 may begin after 9-X but Story 10.2's `Confirmed` state contract is structurally impossible without 9-X." This edit is part of 9-X's diff.

19. **AC19 — Sprint-status consistency.** On story close, sprint-status.yaml MUST be updated to set `9-X-wire-adapter-map-into-policy-guard-and-control-loop: done` (per Epic 9 retro action A8 — story-file Status header ↔ sprint-status entry must agree).

## Tasks / Subtasks

- [x] **Task 1 — Add `RuntimeAdapterMap` + `build_runtime_adapter_map` skeleton** (AC: 1, 2, 9)
  - [x] Subtask 1.1 — Create `src/open_ems/services/runtime_adapter_wiring.py` with `RuntimeAdapterMap` frozen dataclass and `RuntimeAdapterWiringError` exception class.
  - [x] Subtask 1.2 — Implement the empty-registry early-return path (returns `RuntimeAdapterMap(policy_guard_adapters={}, control_loop_adapters={})`) and the multi-device-per-role assertion (AC12).
  - [x] Subtask 1.3 — Wire structured `RuntimeAdapterWiringError` so AC9's lifespan handler can pattern-match on it.

- [x] **Task 2 — Implement per-protocol construction** (AC: 3, 11, 12)
  - [x] Subtask 2.1 — Modbus inverter + battery branches. Reuse / extract `_parse_host_port` from `protocol_adapter_factory.py` into a shared module (suggested `open_ems.adapters.address_parsing`).
  - [x] Subtask 2.2 — DSMR `grid_meter` branch. Same address-helper extraction.
  - [x] Subtask 2.3 — Inverter inclusion in `control_loop_adapters` for telemetry; exclusion-from / no-op-presence in `policy_guard_adapters` is a documented dev decision (see R6 — chosen as "include in control_loop only" since inverters have no writes in v1 and PolicyGuard inclusion would be dead weight; revisit when Story 12+ adds inverter writes).
  - [x] Subtask 2.4 — Enforce AC11 by constructing each adapter exactly ONCE and binding the same instance into both target maps where applicable.

- [x] **Task 3 — Implement `_DeferredOCPPAdapter` proxy** (AC: 4)
  - [x] Subtask 3.1 — Define `_DeferredOCPPAdapter` as a private class inside `runtime_adapter_wiring.py` (NOT exported from `__all__`).
  - [x] Subtask 3.2 — Implement `device_id` property, `connect`/`disconnect` no-ops, and lazy-resolving `get_capabilities`, `get_state`, `send_command`.
  - [x] Subtask 3.3 — Confirm the proxy satisfies the `DeviceAdapter` protocol via mypy (no `# type: ignore`).
  - [x] Subtask 3.4 — Audit semantics: the proxy is a structural shim; it does NOT emit DECISION / DEVICE / CONSTRAINT audits. Existing PolicyGuard / RetryPolicy audit emission unchanged.

- [x] **Task 4 — Wire `OCPPCentralSystem` into lifespan** (AC: 5, 6)
  - [x] Subtask 4.1 — Construct `OCPPCentralSystem` in `web/app.py` before the orchestrator (suggested location: end of current step 4d, before `DeviceDiscoveryOrchestrator` is constructed).
  - [x] Subtask 4.2 — Pass the real instance into `DeviceDiscoveryOrchestrator(ocpp_central_system=...)` and `ProtocolAdapterFactory(ocpp_central_system=...)`. Remove both `=None` literals.
  - [x] Subtask 4.3 — Insert the new "Step 4h" calling `build_runtime_adapter_map(...)`.
  - [x] Subtask 4.4 — Replace `adapters={}` at app.py:478 (PolicyGuard) and app.py:490 (ControlLoop) with `adapters=runtime_adapters.policy_guard_adapters` and `adapters=runtime_adapters.control_loop_adapters` respectively.

- [x] **Task 5 — Logging + shutdown cleanup** (AC: 7, 8, 10)
  - [x] Subtask 5.1 — Emit `runtime_adapter_map_empty` (INFO) when empty.
  - [x] Subtask 5.2 — Emit `runtime_adapter_map_built` (INFO) with structured `adapters` field listing `(role, protocol, device_id)` tuples.
  - [x] Subtask 5.3 — Add adapter `disconnect()` loop in lifespan shutdown, ordered after control_loop cancel and before watchdog cancel. Per-adapter try/except around each `disconnect()` so one failure does not block the rest.
  - [x] Subtask 5.4 — Confirm `_DeferredOCPPAdapter.disconnect()` is a no-op (the underlying charger adapter's lifecycle is owned by `OCPPCentralSystem`).

- [x] **Task 6 — Builder failure → `SystemExit(1)`** (AC: 9)
  - [x] Subtask 6.1 — Wrap the lifespan `await build_runtime_adapter_map(...)` in `try/except RuntimeAdapterWiringError`; on raise, log `startup_failed` with `reason="runtime_adapter_wiring_failed"` and structured detail, then `raise SystemExit(1) from None`. Match the existing `constraints_hydrate_failed` / `migration_failed` handlers verbatim in structure.
  - [x] Subtask 6.2 — Any non-`RuntimeAdapterWiringError` exception in the builder also surfaces — the builder MUST NOT swallow exceptions (no `except Exception`).

- [x] **Task 7 — Unit tests for the builder** (AC: 13)
  - [x] Subtask 7.1 — Set up `tests/unit/services/test_runtime_adapter_wiring.py` with a fake `DeviceRepo` (in-memory list of `DeviceRegistryEntry`) and a fake `OCPPCentralSystem` (`register()` populates an internal dict; `get_adapter()` reads it).
  - [x] Subtask 7.2 — Empty-registry test.
  - [x] Subtask 7.3 — Single battery test; assert AC11 instance-identity between maps.
  - [x] Subtask 7.4 — Single grid_meter test; assert exclusion from `policy_guard_adapters`.
  - [x] Subtask 7.5 — OCPP pre-boot proxy test (assert `_DeferredOCPPAdapter` returned; `get_state()`/`send_command()`/`get_capabilities()` behavior).
  - [x] Subtask 7.6 — OCPP post-boot proxy delegation test.
  - [x] Subtask 7.7 — Excluded states (unvalidated, no-role) test.
  - [x] Subtask 7.8 — Error cases: unsupported (protocol, role), duplicate role, malformed address.

- [x] **Task 8 — Integration test in lifespan** (AC: 14)
  - [x] Subtask 8.1 — Reuse the existing app fixture pattern (model on `tests/integration/web/test_setup_constraints_e2e.py` `app_e2e`). Seed `device_registry` with one validated+role-assigned `modbus_tcp`+`battery` row and one validated+role-assigned `dsmr_p1`+`grid_meter` row.
  - [x] Subtask 8.2 — Capture lifespan log output (caplog with structlog-aware capture; pattern is established in existing tests under `tests/integration/`).
  - [x] Subtask 8.3 — Assert `runtime_adapter_map_built` log present with expected field values.
  - [x] Subtask 8.4 — Use `app.state` to reach PolicyGuard (or expose a test seam if not currently accessible). Drive an authorized `SetBatteryChargeRateCommand` (per the existing PolicyGuard unit-test pattern); assert it does NOT short-circuit at P1.
  - [x] Subtask 8.5 — Sibling test: empty `device_registry` → `runtime_adapter_map_empty` log; PolicyGuard rejects with `adapter_not_registered`.

- [x] **Task 9 — Closure of deferred-finding + stale-comment updates** (AC: 15, 16, 18)
  - [x] Subtask 9.1 — Update `_bmad-output/implementation-artifacts/deferred-work.md` entry `[app.py:223-236]`: append a closure-line (e.g. `*(resolved in Story 9-X, YYYY-MM-DD)*`) per the existing closure convention (model on `[control_loop.py:54]` from Story 8-1 closure noted in Story 9.0d).
  - [x] Subtask 9.2 — Update `src/open_ems/services/protocol_adapter_factory.py` docstring lines 10-14: change the "provisional 9-X future story" wording to past tense or remove the parenthetical.
  - [x] Subtask 9.3 — Update `_bmad-output/planning-artifacts/epics.md` Epic 10 introduction (around line 2072) to declare 9-X as a hard prerequisite to Story 10.2.

- [x] **Task 10 — Quality gates** (AC: 17, 19)
  - [x] Subtask 10.1 — `uv run python -m pytest tests/ --no-cov -q` → all passing; record delta against 1415 baseline.
  - [x] Subtask 10.2 — `uv run python -m ruff check .` → clean.
  - [x] Subtask 10.3 — `uv run python -m ruff format --check .` → clean.
  - [x] Subtask 10.4 — `uv run python -m mypy src/` → clean.
  - [x] Subtask 10.5 — Update sprint-status.yaml: `9-X-wire-adapter-map-into-policy-guard-and-control-loop` → `done`; bump `last_updated`; preserve all comments and structure.

## Dev Notes

### Why this story is the architectural prep gate before Epic 10

Epic 10 Story 10.2 implements a 4-state EV override (Idle → Optimistic → Confirmed → Fallback). The `Confirmed` state is entered ONLY when `EVChargerState.session_active=true` is observed in StateStore (epics.md §10.2 AC "Confirmed state — requires StateStore confirmation"). For that observation to occur, the override POST must reach `PolicyGuard.authorize_and_dispatch()` → adapter → real OCPP charger → adapter polling reads back `session_active=true`. With `adapters={}` (the state we shipped through 9-5), PolicyGuard rejects at P1 (`adapter_not_registered`) before any adapter is consulted. The 4-state lifecycle never advances past `Optimistic → Fallback`. Story 10.2 is structurally impossible to ship honestly without 9-X.

### Files modified by this story

**New files:**
- `src/open_ems/services/runtime_adapter_wiring.py` — `RuntimeAdapterMap`, `RuntimeAdapterWiringError`, `build_runtime_adapter_map`, `_DeferredOCPPAdapter`.
- `tests/unit/services/test_runtime_adapter_wiring.py` — unit tests per AC13.
- `tests/integration/web/test_app_runtime_adapter_wiring.py` — integration test per AC14.
- `src/open_ems/adapters/address_parsing.py` (only if `_parse_host_port` / `_parse_dsmr_address` are extracted — see Subtask 2.1).

**Modified files:**
- `src/open_ems/web/app.py` — new lifespan step 4h; OCPPCentralSystem construction; remove two `adapters={}` literals; add adapter `disconnect()` loop on shutdown.
- `src/open_ems/services/protocol_adapter_factory.py` — pass-through OCPPCentralSystem (already supported via param); docstring update (AC16); optional address-helper import migration.
- `_bmad-output/implementation-artifacts/deferred-work.md` — close `[app.py:223-236]` entry.
- `_bmad-output/planning-artifacts/epics.md` — Epic 10 introduction amendment.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — final entry update on close.

### State of `adapters={}` before this story

Story 8-2 (PolicyGuard implementation) deliberately accepted `Mapping[DeviceRole, DeviceAdapter]` rather than building from the adapter registry — owner-of-construction was deferred. Stories 9-1 / 9-4 / 9-5 preserved the placeholder `adapters={}` in lifespan because the wizard's role-assignment and validation surfaces were the priority and the runtime adapter wiring was explicitly deferred per the dev-notes triage on 9-4. The Epic 9 retro (2026-05-11) made 9-X a hard gate; this story is the canonical resolution.

The currently-shipping behavior is:
- Every command produced by `IntentExecutor` is rejected with `reason="adapter_not_registered"` (P1).
- The control loop's `_poll_adapters()` iterates over an empty mapping and returns an empty `device_states` dict.
- StateStore publishes empty snapshots; all device slots are `None`; `tick_quality="incomplete"`.
- `SystemOperatingMode` derivation in the decision engine sees no devices and remains in degraded mode.

After 9-X, in the populated case:
- Battery commands route to `BatteryAdapter` → `ModbusTcpAdapter.send_command()`.
- EV commands route to `_DeferredOCPPAdapter` → `OCPPChargerAdapter` (if booted) or fail with `ocpp_charger_not_connected`.
- ControlLoop polls real Modbus / DSMR telemetry; StateStore publishes populated snapshots.

### Architectural alignment

**Single-Evaluator Principle (Epic 7 retro).** 9-X does not introduce new evaluators or new command origins; it wires the existing dispatch surface to real adapters. The PolicyGuard remains the sole authorizer; RetryPolicy remains the sole audit emitter; `IntentExecutor` remains the only command origin in v1.

**`PolicyGuard.adapters` is a `Mapping`, not a `MutableMapping`.** Story 8-2 typed it as `Mapping` deliberately; AC4's deferred-proxy approach preserves that immutability invariant. Mid-run insertion of a newly-booted OCPP adapter does NOT mutate PolicyGuard's mapping — the proxy resolves the underlying adapter on every call.

**Restart is the only path to pick up `device_registry` changes.** Wizard role changes during runtime require a process restart to take effect at the dispatch layer. The wizard's Step 4 (validation) and Step 5 (handoff) collectively orchestrate this — the installer's expected workflow is "configure → activate → validation pass → handoff guide → restart". 9-X does NOT add a `reload_adapter_map()` path; that would require either making PolicyGuard's `Mapping` mutable or adding a synchronization primitive across the runtime, both of which exceed this story's scope and would re-open Epic 8 / Epic 9 invariants.

### Library / framework requirements (no new dependencies)

`uv.lock` MUST be unchanged after this story (matches the Epic 9 contract of zero new transitive dependencies). All adapter classes already exist:
- `open_ems.adapters.modbus.tcp.ModbusTcpAdapter`
- `open_ems.adapters.modbus.inverter_adapter.InverterAdapter`
- `open_ems.adapters.modbus.battery_adapter.BatteryAdapter`
- `open_ems.adapters.dsmr.meter_adapter.DSMRP1MeterAdapter` (confirm import path during Subtask 1.1)
- `open_ems.adapters.ocpp.charger_adapter.OCPPChargerAdapter`
- `open_ems.adapters.ocpp.central_system.OCPPCentralSystem`

### Address-parsing reuse

`ProtocolAdapterFactory._parse_host_port` and `_parse_dsmr_address` (lines 231–286 of `protocol_adapter_factory.py`) already enforce P14's IPv6-bracketed-form support and port-range validation. The runtime adapter wiring MUST reuse these — duplication would silently drift. The cleanest path is extraction to `open_ems.adapters.address_parsing` (new module) imported from both call sites. If extraction is judged out of scope, the runtime wiring MAY import the underscore-prefixed helpers from `protocol_adapter_factory` (acknowledged minor violation of name-privacy; document the choice in completion notes).

### Previous-story intelligence

**Story 9-Y-a (2026-05-11, just closed):** the most recent A7 review-closure proof-of-enforcement target. Tests-only delivery (+2 E2E tests). Notable for two things: (a) the first story to exercise the A7 review-closure gate end-to-end (the `bmad-code-review` workflow walked the three closure criteria), and (b) the rename-or-replace decision on `test_e2e_live_state_shift_aborts_activation_with_safety_fail` — a precedent for naming honesty that this story should follow if any new test name turns out to over-claim (e.g., don't name a test "wires real adapter" if it only exercises the empty-map branch).

**Story 9-5 (handoff guide):** A2-evaluation correctly waived R1–R7 burden for the handoff PDF/HTML render because none of T1–T7 matched (a tests/UX-only story). This is the polar opposite of 9-X; 9-X matches 5 of 7 triggers.

**Story 9-0c (capability profile failure semantics):** locked the PolicyGuard P0–P5 contract. 9-X does NOT modify the contract; it only changes what `self._adapters` contains. The capability-check P2 path is now exercised at runtime for the first time with real adapters — pay attention during code review that `_DeferredOCPPAdapter.get_capabilities()` raising `RuntimeError` produces P2 (not P3 / P4). Verify with the integration test pattern from 9-0c.

**Story 9-0d (restart hydration of monthly peak):** the precedent for "construct dependencies BEFORE the consumer" in lifespan. The monthly-peak hydration runs at step 4c, before ControlLoop is constructed. 9-X's runtime wiring follows the same pattern: build the adapter map at step 4h, before PolicyGuard and ControlLoop construction at step 4i+.

**Story 8-2 (PolicyGuard + IntentExecutor):** typed `adapters: Mapping[DeviceRole, DeviceAdapter]` deliberately as a read-only protocol. AC4's deferred-proxy approach is the design choice that respects this invariant.

**Story 8-4 (Watchdog + Fail-safe):** the loop's cold-start `operating_mode=None` window is unchanged by 9-X but is explicitly traced in R5 below. Don't reintroduce that finding.

### Project Structure Notes

The new module sits in `services/` because it composes domain adapters from a registry source — it is a service-layer concern, not an adapter-internal concern. This mirrors the placement of `protocol_adapter_factory.py` (validation surface) — both are wiring services with different consumers (factory: validation runs; this new module: runtime dispatch / polling).

### Orchestration Risk Analysis (A2-triggered — REQUIRED)

**A2 triggers matched:**

- **T1 (lifecycle / state-machine behavior):** the adapter map's lifecycle is a new state machine layered on top of the existing PolicyGuard / ControlLoop lifecycles. The map transitions empty → populated → terminal during lifespan; mid-run mutation is structurally forbidden (deferred-proxy preserves the invariant for OCPP).
- **T3 (persistence + recovery):** the adapter set is sourced from `device_registry` (DB) at every process restart. The wiring is the "hydration" path for adapter membership; restart is the recovery boundary.
- **T5 (multi-adapter coordination):** the story coordinates 3+ adapter classes (`ModbusTcpAdapter` × {Inverter, Battery}, `DSMRP1MeterAdapter`, `OCPPChargerAdapter`) within one construction flow.
- **T6 (deployment / restart behavior):** process restart is the workflow boundary that picks up `device_registry` changes; cold-start invariants (R5) are user-visible because the installer wizard sequences a restart between activation and validation.
- **T7 (installer workflow orchestration):** the runtime adapter set is the materialization of the installer's wizard-driven role assignment. The installer's mental model is "I assigned roles → those roles are now live"; 9-X is what makes that mental model true (after restart).

#### R1 — Composition-risk analysis

The following operational domains converge in this story:

| Domain | Converges with | Risk introduced by convergence |
|---|---|---|
| **Lifecycle (T1)** | Multi-adapter coordination (T5) | The adapter map is built once at lifespan but composes heterogeneous adapter classes with different I/O semantics (Modbus eager-connect-on-first-call vs. OCPP charger-initiates). A construction-time error in one class must not partially populate the map; AC9 mandates fail-loud all-or-nothing semantics. |
| **Lifecycle (T1)** | Persistence (T3) | `device_registry.role` is mutable post-lifespan (installer can re-assign roles via wizard), but PolicyGuard's `Mapping` is captured by value. Asymmetry between "DB says role X is assigned to device Y" and "PolicyGuard's adapter slot X is bound to device Y" emerges if the wizard mutates the registry without restart. The contract resolution is "restart-required" — explicit in AC6 / Dev Notes. |
| **Persistence (T3)** | Deployment/Restart (T6) | `validate_capability_registry_alignment()` at lifespan step 4a is a cold-start invariant: if a code change adds a model but forgets the registry entry, the process exits at step 4a before the adapter map is built. 9-X must NOT bypass that check (and doesn't — step 4a stays where it is). But: 9-X is the FIRST story where `validate_capability_registry_alignment()` failures become installer-visible (previously the empty adapter map meant no adapter ever consulted the registry at runtime). |
| **Multi-adapter coordination (T5)** | Installer workflow (T7) | The wizard's role-assignment service (Story 9-2) writes `device_registry.role`; the runtime adapter wiring reads it. The contract from 9-2 is "exactly one device per non-NULL role"; AC12 defends against tampering. Convergence risk: a successful wizard activation followed by an unsuccessful adapter wiring (e.g., model mismatch in the capability registry) means the installer believes the system is ready while the process exits on next restart. R7 triage classifies this as acceptable because the failure is fail-loud, not silent — but installer-facing UX is out of scope for 9-X. |
| **Installer workflow (T7)** | Lifecycle (T1) — OCPP sub-domain | OCPP chargers initiate the WebSocket connection AFTER lifespan completes. The wizard activation can mark the charger as validated+role-assigned, but `OCPPCentralSystem.get_adapter()` returns `None` until the charger sends `BootNotification`. The `_DeferredOCPPAdapter` proxy (AC4) is the structural resolution of this race. Without the proxy, the runtime adapter map would either (a) omit the OCPP slot until boot (requires mutable map; forbidden) or (b) block lifespan startup on charger boot (fragile; OCPP chargers may take minutes to reconnect after a power blip). |
| **Multi-adapter coordination (T5)** | Single-Evaluator Principle (Epic 7) | The Single-Evaluator Principle says PolicyGuard is the only authorizer. 9-X must not reintroduce capability-checks inside the wiring layer (which would split authorization). Capability gating remains PolicyGuard P2's responsibility; the wiring layer is purely structural construction. |

**Precedents cited:**

- Story 8-4 review's finding on cold-start `operating_mode=None` (`[control_loop.py:73-76]`) — 9-X must not reintroduce that pattern. R5 below explicitly traces the cold-start window.
- Story 9-0c review's P2 contract — `get_capabilities()` MUST be synchronous-fast. `_DeferredOCPPAdapter.get_capabilities()` raising `RuntimeError("ocpp_charger_not_connected")` is the structural equivalent — P2's `except Exception` catches it.
- Story 8-2's adapter map immutability — 9-X's AC4 is the structural response to the OCPP-pre-boot race.

#### R2 — State-transition table

The adapter-map lifecycle introduces one new state machine:

| State | Transitions out | Trigger | Side-effects |
|---|---|---|---|
| **empty (pre-step-4h)** | → **populated-stable (size N)** | lifespan step 4h calls `build_runtime_adapter_map(...)` against a registry with N validated+role-assigned rows where all (protocol, role) combinations are supported and constructible | INFO log `runtime_adapter_map_built` with `policy_guard_role_count`, `control_loop_role_count`, structured `adapters` list. PolicyGuard / ControlLoop constructed against the resulting maps. |
| **empty (pre-step-4h)** | → **populated-empty (size 0)** | lifespan step 4h against a registry with zero validated+role-assigned rows (pre-wizard-complete) | INFO log `runtime_adapter_map_empty` with `reason="pre_installer_wizard_complete"`. PolicyGuard / ControlLoop constructed with empty maps. P1 (`adapter_not_registered`) is the default rejection reason for any command. |
| **empty (pre-step-4h)** | → **terminal (process exit)** | `RuntimeAdapterWiringError` (or any other) raised during construction | ERROR log `startup_failed` with `reason="runtime_adapter_wiring_failed"`. `SystemExit(1)`. Lifespan `finally` closes DB; no PolicyGuard / ControlLoop constructed. |
| **populated-stable (size N)** | → **terminal (process exit)** | lifespan shutdown | sd_notify STOPPING=1; control_loop cancelled; adapter `disconnect()` loop runs (per-adapter try/except); watchdog/cleanup/pruning cancelled; DB closed. |
| **populated-stable (size N) where ev_charger slot is `_DeferredOCPPAdapter`** | → "**populated-stable, OCPP-resolved**" | OCPP charger sends `BootNotification`; `OCPPCentralSystem.register()` populates the underlying adapter; next call to `_DeferredOCPPAdapter.send_command()` delegates successfully | No state change in the map itself; the deferred proxy resolves freshly on each call. PolicyGuard sees no transition — its `adapters[ev_charger]` is the same proxy object. |
| **populated-stable, OCPP-resolved** | → "**populated-stable, OCPP-disconnected**" | OCPP charger drops connection; `OCPPCentralSystem.get_adapter()` continues returning the same adapter object (the central system does not de-register on disconnect; adapter's own `ConnectionStatus` reflects degraded) | No state change in 9-X's map. The downstream charger adapter handles per-call `ConnectionStatus.degraded` per the existing 9-0 contract. |
| **populated-empty (size 0)** | → terminal | shutdown | Adapter `disconnect()` loop is a no-op (empty map). |

**The runtime adapter map state machine has no in-process transition from `populated-empty` → `populated-stable` and no `populated-stable (N)` → `populated-stable (M ≠ N)`.** That asymmetry (no mid-run reconfiguration) is deliberate; it preserves PolicyGuard's `Mapping` immutability. The wizard-driven role-change workflow recovers via process restart, which re-enters the lifecycle at "empty (pre-step-4h)".

#### R3 — Impossible-state analysis (with cold-start coverage)

**Forbidden state combinations and their structural invariants:**

1. **Two adapter entries for the same `DeviceRole` in either map.** Prevented by:
   (a) `dict[DeviceRole, DeviceAdapter]` keying — a duplicate write would overwrite silently, so:
   (b) `build_runtime_adapter_map` explicitly checks for two `device_registry` rows with the same non-NULL role and raises `RuntimeAdapterWiringError` (AC12).
   Defense in depth on top of Story 9-2's role-assignment invariant.

2. **An adapter for `DeviceRole.grid_meter` in `policy_guard_adapters`.** Prevented by AC3's table: `dsmr_p1` + `grid_meter` is excluded from `policy_guard_adapters` by construction. PolicyGuard authorizing a grid-meter command is doubly-impossible — the cross-adapter contract clause 1 (DSMR is read-only) means `send_command` raises `NotImplementedError` even if invoked; and the wiring layer never inserts the slot in the first place.

3. **`PolicyGuard.adapters[role].device_id != device_registry.get_by_role(role).device_id`.** Prevented at construction time: the builder uses `DeviceRegistryEntry.device_id` as the canonical ID passed to each adapter constructor (e.g., `BatteryAdapter(device_id=entry.device_id, ...)`). The adapter's `self.device_id` attribute is set to that value.

4. **ControlLoop's adapter map and PolicyGuard's adapter map have different sizes or different role assignments for overlapping roles.** Prevented by `RuntimeAdapterMap` being a frozen dataclass produced by a single function call — both fields are derived from the same internal construction pass. AC11 enforces `is`-identity for overlapping role instances.

5. **Adapter constructed with `device_id` differing from the registry row.** Prevented by AC3's construction recipe — every adapter constructor receives `entry.device_id` directly.

6. **`PolicyGuard.adapters[DeviceRole.inverter]` is populated but the inverter has no write capabilities.** This is NOT actually impossible — v1 inverters legitimately have empty `write_capabilities`. The decision is to EXCLUDE inverters from `policy_guard_adapters` (see Subtask 2.3); PolicyGuard never receives an inverter command in v1 because `IntentExecutor` only produces battery and EV commands. Including the inverter in `policy_guard_adapters` would be a no-op at best and a P4 (`capability_missing`) at runtime if a future story added an inverter command type without updating capabilities — fail-loud is better than fail-silently. Keeping inverters out of `policy_guard_adapters` is the chosen invariant.

7. **`policy_guard_adapters` is populated but `control_loop_adapters` is empty, or vice versa, for an overlapping role.** Prevented by AC11's instance-identity requirement.

**Cold-start / startup-grace coverage (mandatory):**

The system's state from process boot to first successful evaluation cycle, traced step by step:

- **t=0 (process boot).** `web/app.py` `lifespan()` enters. Logging configured, Settings loaded. StateStore constructed with `system_clock_status` and a default snapshot: `operating_mode=degraded` (the StateStore default), all device slots `None`, `last_updated_at` set to now.
- **t=1 (DB ready).** Migrations applied; capability registry validated (fail-loud if drifted, step 4a). `ActiveConstraintsProvider` hydrated (step 4b); monthly_peak hydrated (step 4c).
- **t=2 (wizard services).** DeviceRepo, WizardStateRepo, DiscoveryService, **OCPPCentralSystem (NEW per AC5)**, DeviceDiscoveryOrchestrator, ManualEntryService, RoleAssignmentService, draft_constraints_repo, ConstraintsService, ProtocolAdapterFactory, DeploymentValidationService — all constructed. **No adapter I/O yet.**
- **t=3 (runtime adapter wiring — NEW step 4h).** `build_runtime_adapter_map(...)` runs. Two outcomes:
  - **Populated case:** `runtime_adapter_map_built` logged. For OCPP roles, `_DeferredOCPPAdapter` is inserted regardless of whether the charger has booted. For Modbus / DSMR roles, the adapter wrapper is constructed; underlying protocol connections are NOT established at construction time (Modbus connects lazily on first `get_raw_state`; DSMR likewise).
  - **Empty case:** `runtime_adapter_map_empty` logged with `reason="pre_installer_wizard_complete"`. The system is operational for HTTP requests, the installer wizard, and read-only telemetry; every authorized command rejects at P1.
- **t=4 (ObservabilityService, LoopLiveness, watchdog).** Constructed; watchdog task started; cleanup + pruning tasks started.
- **t=5 (PolicyGuard, RetryPolicy, ControlLoop).** Constructed with `adapters=runtime_adapters.policy_guard_adapters` and `adapters=runtime_adapters.control_loop_adapters` respectively. Control loop task started.
- **t=6 (yield).** Lifespan yields; FastAPI serves requests.
- **t=7 (first `_tick`).** ControlLoop's first poll cycle runs. For each role in `control_loop_adapters`, `adapter.get_state()` is awaited with `ADAPTER_GET_STATE_TIMEOUT_SECONDS=10.0`. Failed adapters (unreachable Modbus, missing OCPP boot, etc.) yield `DegradedDeviceState` per the existing AC1 contract from 8-1. The OCPP slot's `_DeferredOCPPAdapter.get_state()` returns `DegradedDeviceState(reason="ocpp_charger_not_connected")` until the charger boots.
- **t=8 (first StateStore publish with real telemetry).** Snapshot reflects polled device states; `operating_mode` is still `None` (the pre-existing Story 8-1 finding `[control_loop.py:73-76]`; unchanged by 9-X) until the engine runs.
- **t=9 (first evaluation completes).** `_handle_evaluation_result` runs; `_last_mode` set to the engine's recommendation. `loop_liveness.mark_cycle_complete()` fires — this is the **normal-operation marker**. The next publish carries the real operating mode.
- **t=10 (steady state).** Subsequent ticks poll, publish, evaluate, dispatch. Dispatched commands flow through PolicyGuard → adapter → real device.

**Invariants that temporarily relax during the cold-start window (t=0 → t=9):**

- StateStore snapshots have `operating_mode=None` between t=7 (first publish with real data) and t=9 (first evaluation). Pre-existing; unchanged by 9-X.
- Every command from external POSTs in this window rejects at P0 (if first publish hasn't run, snapshot.operating_mode is still degraded — wait, degraded does NOT trip P0; only `fail_safe` does. So commands pass P0 and hit either P1 (`adapter_not_registered`) for unregistered roles or P2 (`capability_check_failed`) for `_DeferredOCPPAdapter` in pre-boot state).
- The `_DeferredOCPPAdapter` proxy presents as "registered but unable" — distinct from "unregistered". This is a subtle but deliberate distinction: P1 says "this role has no adapter in the map"; P2 (triggered by the proxy raising `RuntimeError`) says "this role has an adapter but its protocol layer is unavailable". Operators see the distinction in the structured logs.

**Normal-operation marker:** `loop_liveness.mark_cycle_complete()` at the end of `_tick()`. This is the existing marker from Story 8-4; 9-X does not introduce a new one.

#### R4 — Cancellation ownership map

9-X introduces minimal new async surface. Most cancellation semantics are inherited.

| Async operation | `CancelledError` owner | Cancellation audit owner | Cleanup owner | Reporting field semantics under cancel |
|---|---|---|---|---|
| `build_runtime_adapter_map(...)` at lifespan step 4h | Lifespan's outer `try` (existing); cancellation during startup is rare but possible (e.g., SIGTERM during `lifespan.__aenter__`). The `await device_repo.list_all()` is the only awaited call inside the builder; if cancelled, the builder's locally-constructed adapter instances (if any) are not yet attached to PolicyGuard / ControlLoop, so cleanup reduces to "let them go out of scope" — adapters have not opened any connections yet (lazy connect). | None — startup is not audited at per-step granularity. The `runtime_adapter_map_*` log lines are the structured trace; their absence (cancelled before they emit) is the trace of an interrupted startup. | None per-adapter; the lifespan `finally` calls `close_database()` if `db_initialized` is true. | N/A — `build_runtime_adapter_map` does not have `attempts` semantics. |
| `_DeferredOCPPAdapter.get_capabilities` / `get_state` / `send_command` | Inherits from PolicyGuard / ControlLoop's existing cancellation contract — `CancelledError` propagates because the proxy methods are sync (they read from `OCPPCentralSystem._adapters` in-memory dict) or delegate to the underlying adapter (which has its own `CancelledError` contract per `open_ems.adapters` clause 5). The proxy itself catches no exceptions. | Inherited: PolicyGuard's `_reject` is NOT called on cancellation (CancelledError propagates past the `except Exception`); RetryPolicy's `_emit_cancellation_audit` is the single emitter. | The underlying `OCPPChargerAdapter` (when bound) owns its own try/finally per the cross-adapter contract clause 5. The proxy adds no cleanup. | Inherited from RetryPolicy: `attempts=N` = in-progress attempt count when cancelled. |
| ControlLoop's per-tick `adapter.get_state()` (now exercising real adapters for the first time) | Unchanged from Story 8-1: `asyncio.wait_for(...)` with timeout; the existing `except Exception` excludes `BaseException`, so `CancelledError` propagates. | None — polling is not audited. | Per-adapter try/finally (cross-adapter contract). | N/A — polling has no `attempts`. |
| PolicyGuard's per-command `adapter.send_command()` (now exercising real adapters for the first time) | Unchanged from Story 8-2: `CancelledError` propagates past the `# noqa: BLE001 — CancelledError escapes by design` comment. RetryPolicy's `_emit_cancellation_audit` emits the audit. | RetryPolicy (unchanged). | Adapter's try/finally (cross-adapter contract). | RetryPolicy's `attempts=N` — unchanged. |
| Lifespan shutdown's new per-adapter `disconnect()` loop (AC10) | Lifespan shutdown is OUTSIDE the `yield` boundary; cancellation during shutdown would mean the supervising async runtime is itself dying. Per-adapter `disconnect()` is wrapped in try/except so one adapter's `CancelledError` (if it raises one) propagates to the loop but the other adapters' `disconnect()` calls still run. | None — adapter teardown is not audited. | Each adapter's own `disconnect()`. | N/A. |

**No new `attempts`-bearing field is introduced.** All cancellation reporting fields are inherited from existing RetryPolicy semantics; 9-X does not alter them.

#### R5 — Before-first-successful-cycle lifecycle review

Trace from process start to first successful normal cycle:

**Boot phase (t=0 → t=6, lifespan startup):**

1. `lifespan.__aenter__` enters.
2. `get_settings()` → Settings loaded; on validation error: `SystemExit(1)` with `startup_failed: invalid_config`.
3. `configure_logging(settings.log_level)` → all subsequent logs are JSON.
4. `check_clock(...)` → clock status determined; non-valid status is a WARNING but does NOT abort.
5. `StateStore` constructed. **Initial snapshot:** `operating_mode=degraded` (StateStore default), every device slot (`inverter`, `battery`, `ev_charger`, `grid_meter`) = `None`, `last_updated_at = utcnow()`.
6. Migrations applied (step 2; `SystemExit(1)` on failure).
7. DB connection opened (step 4).
8. **Step 4a** — `validate_capability_registry_alignment()` → `SystemExit(1)` on drift.
9. **Step 4b** — `ActiveConstraintsProvider.hydrate()` → loads `active_constraints` row or seeds from Settings; `SystemExit(1)` on failure.
10. **Step 4c** — `EnergyRepo.get_current_monthly_peak_kw()` → `SystemExit(1)` on corrupt value.
11. **Step 4d–4g** — wizard service objects constructed (NO DB I/O); **OCPPCentralSystem constructed (NEW per AC5)**; ObservabilityService, ProtocolAdapterFactory, DeploymentValidationService constructed.
12. Admin bootstrap (step 5b — creates initial admin if no users exist).
13. `mark_ready()` (step 5) → sd_notify READY=1; system_ready logged.
14. LoopLiveness constructed; watchdog/cleanup/pruning tasks started.
15. **Step 4h (NEW) — runtime adapter wiring.** `runtime_adapters = await build_runtime_adapter_map(...)`. Two outcomes:
    - Empty: `runtime_adapter_map_empty` logged at INFO with `reason="pre_installer_wizard_complete"`.
    - Populated: `runtime_adapter_map_built` logged at INFO with structured adapter list.
    - Failure: `runtime_adapter_wiring_failed` logged at ERROR; `SystemExit(1)`.
16. `IntentExecutor()` constructed.
17. `PolicyGuard(adapters=runtime_adapters.policy_guard_adapters, ...)` constructed.
18. `RetryPolicy(policy_guard=..., ...)` constructed.
19. `ControlLoop(adapters=runtime_adapters.control_loop_adapters, ...)` constructed.
20. Control loop task started; `control_loop_started` logged.
21. `yield` — FastAPI begins serving requests.

**During cold-start window (t=6 → t=9, before first successful cycle):**

The system publishes:
- **HTTP:** `/health/*` responds per existing contracts (readiness=true since `mark_ready()` already fired); the installer wizard routes accept requests; `/actions/*` routes accept requests but the underlying PolicyGuard dispatch will reject at P1 / P2 / P4 depending on adapter state. SSE state stream is active and publishes the initial degraded snapshot.
- **Logs:** lifespan startup events as above. No `decision_cycle` events yet.
- **Audit log:** no CONSTRAINT / DECISION / DEVICE rows yet. SYSTEM rows from admin_bootstrap, system_ready, watchdog/cleanup/pruning start.
- **StateStore SSE:** the initial degraded snapshot.
- **Adapter polling:** the control loop's first `_tick()` runs at t=7. For non-empty `control_loop_adapters`, each adapter's `get_state()` is awaited. Modbus adapters connect lazily — the first call triggers TCP connect. DSMR likewise. `_DeferredOCPPAdapter.get_state()` returns `DegradedDeviceState(reason="ocpp_charger_not_connected")` if the charger hasn't booted.
- **StateStore publish at t=8:** the snapshot now reflects whatever adapters succeeded. `operating_mode=None` (pre-existing Story 8-1 cold-start finding; unchanged).
- **Decision engine at t=9:** `_handle_evaluation_result` sets `_last_mode` to the engine's recommendation. `loop_liveness.mark_cycle_complete()` — the **normal-operation marker** — fires for the first time.

**Temporarily relaxed invariants:**

- `operating_mode=None` between t=7 publish and t=9 evaluation (Story 8-1 finding, unchanged).
- Adapter state slots in StateStore are `None` until first poll completes (Story 8-1 baseline).
- `_DeferredOCPPAdapter` slots present in the adapter map remain unresolved until BootNotification fires — this is the new asymmetry 9-X introduces.

**Normal-operation marker:** the first `loop_liveness.mark_cycle_complete()` invocation. Watchdog's `sd_notify` gating already uses this marker (per Story 8-4); 9-X does not change that contract.

#### R6 — Source-of-truth ownership per datum

| Datum | Single owner | Read path | Drift risk (if any) |
|---|---|---|---|
| Set of validated, role-assigned devices | `device_registry` table (queried via `DeviceRepo.list_all()`) | `build_runtime_adapter_map` reads once at lifespan step 4h; PolicyGuard / ControlLoop hold the resulting `RuntimeAdapterMap`. Read path: `PolicyGuard._adapters` and `ControlLoop._adapters`. | **Yes, controlled.** After lifespan, `device_registry` is mutable (the installer can re-assign roles via the wizard or directly edit the DB), but the in-process `RuntimeAdapterMap` is immutable. **Documented contract: a restart is required to pick up registry changes.** AC6 makes this explicit; the wizard's Step 4 / Step 5 sequence + restart is the recovery path. |
| `RuntimeAdapterMap` instance | `build_runtime_adapter_map(...)` (single function call at lifespan step 4h) | Exposed as `app.state.runtime_adapters` (and `app.state.policy_guard`) for integration-test reach — updated during code review on 2026-05-11 because the AC14 dispatch-past-P1 assertions need a test seam to reach PolicyGuard. `app.state` exposure does NOT change the lifecycle ownership: the instance is constructed once at step 4h and replaced only by a process restart. | None — single instance, frozen dataclass; `app.state` mirrors the construction-time reference. |
| `policy_guard_adapters[role]` adapter instance | Owned by `RuntimeAdapterMap`; same instance as `control_loop_adapters[role]` for overlapping roles (AC11). | PolicyGuard's `self._adapters[role]` (read-only via `Mapping.get`). | None — AC11's `is`-identity guarantee prevents drift between PolicyGuard's and ControlLoop's view of the same physical device. |
| OCPP charger registry (`OCPPCentralSystem._adapters`) | `OCPPCentralSystem` (in-memory dict). | `OCPPCentralSystem.get_adapter(charge_point_id)`; `list_registered_chargers()`. | **Yes, by design.** Asymmetry between `device_registry.role='ev_charger'` (wizard-acknowledged) and `OCPPCentralSystem.get_adapter(...) is not None` (charger-booted). The `_DeferredOCPPAdapter` proxy bridges this asymmetry: the slot exists in `policy_guard_adapters` whether or not the charger has booted; the proxy re-resolves on every call. Drift is observable (the OCPP-pre-boot window produces `capability_check_failed` audits at P2) but structurally cannot cause incorrect dispatch. |
| Capability profile per device | `open_ems.adapters.capabilities` registry (`get_profile(device_id, model)`) — already canonical. | PolicyGuard P2 (`adapter.get_capabilities()`); `ProtocolAdapterFactory._outcome_reachable` (validation surface). | None — 9-X does NOT touch capability resolution. |
| Active constraints (`peak_limit_kw`, `battery_reserve_floor_percent`) | `active_constraints` table → `ActiveConstraintsProvider` cached snapshot | PolicyGuard's `_check_safety_constraints`; ControlLoop's `_build_evaluation_input`. | None — already single-owner via Story 9-0b. 9-X does NOT touch. |
| `device_id` ↔ `role` mapping at runtime | Captured into `RuntimeAdapterMap` keys at lifespan; the originating data is `device_registry.role`. | PolicyGuard / ControlLoop via map keys. | **Yes — same as the first row.** The captured mapping diverges from `device_registry` if the wizard mutates registry mid-run. Documented contract: restart required. |
| `_DeferredOCPPAdapter._charge_point_id` | The proxy itself, set at construction time from `entry.device_id`. | The proxy's `get_capabilities` / `get_state` / `send_command` use it on every call. | None — immutable proxy state. |
| Adapter connection state (Modbus TCP socket, etc.) | Each individual adapter's `protocol_adapter` (e.g., `ModbusTcpAdapter.connect_*` internal state). | Inside each adapter's per-call logic. | None — 9-X does not alter adapter-internal lifecycle. |

#### R7 — Deferred-findings triage

Scanning `_bmad-output/implementation-artifacts/deferred-work.md` for items whose component or invariant overlaps 9-X:

| Finding (bracket ref) | Classification | Rationale |
|---|---|---|
| `[app.py:223-236]` ("`adapters={}` in production silently rejects all commands") | **must-resolve-in-this-story** | This is the entire reason 9-X exists. AC15 mandates closure of the deferred-work entry as part of story-done. Closing this is the canonical resolution per Epic 9 retro action A11. |
| `[control_loop.py:84-98]` ("Sequential adapter polling blocks all adapters when one is slow") | **safe-during-this-story** | With real adapters now wired, the sequential-polling effect becomes user-visible for the first time. The fix (asyncio.gather + per-task timeouts) is a worthwhile optimization but bundling it would expand scope. Document as a follow-up; do NOT bundle. |
| `[control_loop.py:33, settings.py]` ("Adapter timeout (10s) not bounded by `control_loop_interval_seconds`") | **safe-during-this-story** | Same reasoning as above. With real adapters present and `control_loop_interval_seconds=10` per Settings default, sequential polling of 4 failing adapters would exceed one tick. Not blocking for 9-X delivery but flagged for the follow-up bundle. |
| `[policy_guard.py:67]` ("PolicyGuard re-fetches snapshot independently (stale-read window)") | **acceptable-post-this-story** | Theoretical inconsistency in single-task asyncio architecture; 9-X does not introduce concurrent publishers. Defer. |
| `[policy_guard.py:137-141]` ("Discharge command bypasses SoC floor check when battery state unavailable") | **already-resolved** | Story 9-0c AC4 introduced `battery_state_unavailable_for_safety_check` which closes this. The deferred-work entry is stale; 9-X closure should NOT re-open it. |
| `[policy_guard.py:97-100]` ("correlation_id mismatch from adapter `CommandResult` passed through unchecked") | **already-resolved** | Story 9-0c AC6 added P5 post-dispatch enforcement. Stale entry. |
| `[control_loop.py:73-76]` ("First tick publishes `operating_mode=None`") | **acceptable-post-this-story** | Pre-existing cold-start finding from Story 8-1; not introduced by 9-X. R5 above traces the window explicitly. Defer to a dedicated cold-start hardening pass. |
| `[control_loop.py:_extract_grid_power]` ("Degraded grid meter returns 0.0 to PartialIntervalTracker") | **acceptable-post-this-story** | With real DSMR adapter now wired, this becomes a real concern (a Modbus battery + DSMR meter site can now exercise the path). But the fix is in `_extract_grid_power`, not in the wiring layer. Pre-existing logic; defer. |
| `[retry_policy.py:82-83]` ("`asyncio.sleep` between attempts not shielded against cancellation") | **acceptable-post-this-story** | Not in 9-X scope; broader cancellation policy concern. |
| `[retry_policy.py]` ("`RetryPolicy.execute` has no per-device serialization") | **safe-during-this-story** | The deferred-work entry explicitly anticipates 9-X: "a future external caller (Epic 9 installer overrides, Epic 10 homeowner overrides) could call `execute()` concurrently with the control loop for the same device". With adapter map now real and Epic 10 starting after 9-X, this concern is materially closer to surfacing. **However, 9-X itself does not introduce a second caller** — IntentExecutor remains the only command origin in v1. Epic 10's `/actions/ev-override` POST will introduce that second caller. Defer the per-device serialization to Epic 10 prep work; flag in completion notes. |
| `[policy_guard.py:44-45]` ("Timeout constants not exposed via Settings") | **acceptable-post-this-story** | Out of 9-X scope. |
| `[policy_guard.py:143-145]` ("Peak limit check uses strict `>` — command at exactly `peak_limit_kw` passes") | **acceptable-post-this-story** | Out of 9-X scope; boundary-semantics concern. |

**No deferred-findings overlap that is left un-triaged.** Per Epic 9 retro action A9, every overlapping finding above has been classified inline at the moment of triage (not retroactively).

### References

- [Source: _bmad-output/implementation-artifacts/epic-9-retro-2026-05-11.md#Significant-Discovery-Epic-10-Story-10.2-Cannot-Honestly-Ship-With-adapters-empty] — origin of 9-X as a hard prep gate
- [Source: _bmad-output/implementation-artifacts/epic-9-retro-2026-05-11.md#Action-Items-A11] — story-author obligation to record `adapters={}` closure
- [Source: _bmad-output/planning-artifacts/epics.md#Epic-10] — Epic 10 stories that depend on 9-X
- [Source: _bmad-output/planning-artifacts/epics.md#Story-10.2] — Story 10.2's 4-state EV override that requires real OCPP dispatch
- [Source: src/open_ems/engine/policy_guard.py] — PolicyGuard P0–P5 contract (Story 9-0c lock)
- [Source: src/open_ems/engine/control_loop.py] — ControlLoop adapter polling + fail-safe lifecycle
- [Source: src/open_ems/web/app.py] — current lifespan with `adapters={}` placeholders at lines 478 and 490
- [Source: src/open_ems/services/protocol_adapter_factory.py] — validation-only adapter factory; address-parsing helpers; OCPP-no-central-system fallback
- [Source: src/open_ems/adapters/__init__.py] — cross-adapter command contract v1.1
- [Source: src/open_ems/storage/repositories/device_repo.py] — `DeviceRepo.list_all()` and `DeviceRegistryEntry`
- [Source: src/open_ems/adapters/ocpp/central_system.py#OCPPCentralSystem] — registry behavior + `get_adapter` semantics
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — overlapping findings and their classification (R7)

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Claude Opus 4.7)

### Debug Log References

- Mypy clean across 98 source files (2 new typed modules — `address_parsing.py` and `runtime_adapter_wiring.py`).
- Ruff check + format clean across 247 files.
- Pytest baseline pre-9-X: 1415 passed + 5 xfailed. Post-9-X: **1438 passed + 1 skipped + 5 xfailed**. Net delta: **+23 tests** (19 unit + 4 integration; 1 deliberate parametrize-skip for the empty-address case which Pydantic rejects at `DeviceRegistryEntry` construction).

### Completion Notes List

- **AC15 closure recorded.** `deferred-work.md` entry `[app.py:223-236]` now carries the resolution line `*(resolved in Story 9-X, 2026-05-11 — new lifespan step 4h ...)*` per A11 of the Epic 9 retro.
- **AC18 amendment landed.** `epics.md` Epic 10 introduction now declares 9-X as a hard prerequisite to Story 10.2, with explicit narrative on why Story 10.2's `Optimistic → Confirmed` transition is structurally impossible without 9-X.
- **Documented deviation from AC4 (`_DeferredOCPPAdapter` proxy).** The story specified a lazily-resolving proxy that calls `OCPPCentralSystem.get_adapter(charge_point_id)` on every invocation. Implementation deviates: `build_runtime_adapter_map` calls `ocpp_central_system.register(config)` **eagerly** at wiring time for each `ocpp_1_6` entry, then wraps the resulting raw `OCPPChargerAdapter` in `EVChargerAdapter` (the same wrapping pattern Modbus uses). The deviation is documented inline at the top of `runtime_adapter_wiring.py`. Rationale: (1) without an eager `register` no other code path calls it, so `get_adapter()` would always return `None` and the proxy would never recover; (2) `OCPPChargerAdapter`'s existing `connected=False` state already handles the pre-boot window — `get_raw_state()` returns `ProtocolDegradedState("ocpp_disconnected")` and `send_raw_command()` returns `"not_connected"`, both translated correctly by `EVChargerAdapter`; (3) the Story 8-2 `Mapping` immutability invariant is preserved (no mid-run map mutation); (4) the proxy approach would duplicate `EVChargerAdapter`'s raw-to-domain translation or require lazy `EVChargerAdapter` construction — neither cleaner than direct wrap. Unit test `test_ocpp_pre_boot_get_state_returns_degraded` exercises the pre-boot path against the wired adapter and confirms it returns `DegradedDeviceState(reason="reconnecting")` — equivalent to the proxy's contract from the consumer side.
- **Modbus register-range resolution.** The story's AC3 `...` placeholder around `ModbusTcpAdapter(host, port, ...)` resolves to a model-keyed dict `_MODBUS_REGISTER_RANGES_BY_MODEL` in `runtime_adapter_wiring.py`. Ranges are PLACEHOLDER (matching the register_maps modules' own PLACEHOLDER markers); each range covers the smallest contiguous block that includes all `_REG_*` constants defined in the corresponding register map. Verify against actual device documentation when real hardware is commissioned. A defense-in-depth `RuntimeAdapterWiringError` fires if the model is missing from the dict — though the capability-registry alignment check at lifespan step 4a fails the process earlier on unknown models.
- **Address-parser extraction.** `_parse_host_port` and `_parse_dsmr_address` were duplicated inside `protocol_adapter_factory.py`. AC3's "reuse / extract" subtask 2.1 resolved by extracting both to `src/open_ems/adapters/address_parsing.py` as public `parse_host_port` / `parse_dsmr_address`. Both call sites (`protocol_adapter_factory.py` and `runtime_adapter_wiring.py`) import from the shared module. The existing test file `tests/unit/services/test_protocol_adapter_factory.py` was updated to import from the new module (parametric test bodies unchanged).
- **OCPPCentralSystem now constructed in lifespan (AC5).** Previously `=None` was passed to both `DeviceDiscoveryOrchestrator` (app.py:322) and `ProtocolAdapterFactory` (app.py:359) with stale comments referring to 9-X as future work. Both call sites now pass the real `ocpp_central_system` instance; the stale comments are removed; `protocol_adapter_factory.py` module docstring is updated to reflect that 9-X has landed.
- **Shutdown adapter teardown (AC10).** New per-adapter `disconnect()` loop runs between `control_loop_task.cancel()` and `watchdog_task.cancel()` in lifespan shutdown. Each disconnect is isolated in a try/except so a single slow / failed disconnect does not block the rest; failures log at WARNING with role + device_id + structured traceback.
- **AC11 instance identity is verified by `is` assertion in unit + integration tests.** Both `tests/unit/services/test_runtime_adapter_wiring.py::test_battery_modbus_wired_into_both_maps_same_instance` and `tests/integration/web/test_app_runtime_adapter_wiring.py::test_lifespan_populated_registry_wires_battery_and_grid_meter` assert `policy_guard_adapters[role] is control_loop_adapters[role]` for the overlapping role.
- **OCPP charge_point_id derivation.** For OCPP entries in `device_registry`, `address` is recorded as `/{charge_point_id}` (per Story 9.1 discovery). The wiring strips the leading slash; if the address is empty after strip, falls back to `device_id`. The resulting `charge_point_id` is the OCPP-protocol identifier (WebSocket URL key); `device_id` remains the system-wide identifier (audit, CommandResult).

### File List

**New files:**
- `src/open_ems/adapters/address_parsing.py` — shared host:port + DSMR address parsers (extracted from `protocol_adapter_factory.py`).
- `src/open_ems/services/runtime_adapter_wiring.py` — `RuntimeAdapterMap` dataclass, `RuntimeAdapterWiringError`, `build_runtime_adapter_map`, `_MODBUS_REGISTER_RANGES_BY_MODEL`.
- `tests/unit/services/test_runtime_adapter_wiring.py` — 19 unit tests + 1 deliberate parametrize-skip.
- `tests/integration/web/test_app_runtime_adapter_wiring.py` — 4 lifespan integration tests (empty-cold-start log, populated wiring + AC11 instance-identity, fail-loud `SystemExit` on builder failure, OCPPCentralSystem wiring on `app.state`).

**Modified files:**
- `src/open_ems/web/app.py` — imports `OCPPCentralSystem` + runtime wiring; constructs `ocpp_central_system` in lifespan step 4d; passes the real instance into `DeviceDiscoveryOrchestrator` and `ProtocolAdapterFactory` (two `=None` literals removed); new lifespan step 4h calling `build_runtime_adapter_map(...)` with structured `runtime_adapter_map_built` / `runtime_adapter_map_empty` logging and fail-loud `SystemExit(1)` on `RuntimeAdapterWiringError`; `PolicyGuard(adapters=...)` and `ControlLoop(adapters=...)` now receive the runtime maps (two `adapters={}` literals removed); `app.state.runtime_adapters` + `app.state.policy_guard` exposed; new per-adapter `disconnect()` loop in shutdown between control_loop cancel and watchdog cancel.
- `src/open_ems/services/protocol_adapter_factory.py` — module docstring rewritten (AC16): removed the stale "provisional `9-X-wire-adapter-map-into-policy-guard-and-control-loop` future story" wording and replaced with a past-tense reference to 9-X having landed; imports `parse_host_port` / `parse_dsmr_address` from `address_parsing` (local duplicates removed); call sites updated from `_parse_*` to public names.
- `tests/unit/services/test_protocol_adapter_factory.py` — updated imports to read the public parsers from `open_ems.adapters.address_parsing` (aliased to the same private symbols so existing test bodies need no further change).
- `_bmad-output/implementation-artifacts/deferred-work.md` — appended closure line to the `[app.py:223-236]` entry.
- `_bmad-output/planning-artifacts/epics.md` — Epic 10 introduction amended with the **Prerequisite** paragraph declaring 9-X as a hard gate before Story 10.2.

### Change Log

| Date | Change |
|---|---|
| 2026-05-11 | Story 9-X implemented. Closes Story 8-2 deferred finding `[app.py:223-236]`. PolicyGuard and ControlLoop are now constructed with real adapter maps sourced from `device_registry`. `+23` net tests (1415 → 1438 passing). Ruff, format, mypy clean. Status: ready-for-dev → review. |
| 2026-05-11 | `bmad-code-review` pass (Blind Hunter + Edge Case Hunter + Acceptance Auditor). 1 `decision-needed` (AC4 deviation), 15 `patch`, 3 `defer`, ~14 dismissed. See **Review Findings** section below. Status: review (pending findings resolution). |

### Review Findings

_From `bmad-code-review` pass on 2026-05-11. Sources: B = Blind Hunter, E = Edge Case Hunter, A = Acceptance Auditor._

#### Decision-needed (resolved 2026-05-11 → reclassified as patch)

- [x] [Review][Decision→Patched] **AC4 deviation resolution: User selected option 1 — implement the spec-mandated `_DeferredOCPPAdapter` proxy.** Patch tracked below as P0. Eager `register()` stays at wiring time for OCPP charger identity stability; a thin proxy wraps `EVChargerAdapter` and overrides `get_state` / `get_capabilities` / `send_command` to emit the AC4-mandated reason strings and raise the spec's `RuntimeError`. AC13 OCPP pre-boot test assertions and the runtime_adapter_wiring.py docstring deviation block must be updated to match. Original finding body retained below for audit:

- [x] [Review][Decision] **AC4 deviation: `_DeferredOCPPAdapter` proxy replaced by eager-register + EVChargerAdapter wrap, observable contract differs** [`src/open_ems/services/runtime_adapter_wiring.py:23-57, 290-320`] — Sources: A1+A2+A3+E17+B2. Spec AC4 mandated a lazy-resolving proxy with three specific contract surfaces: `get_state()` reason `"ocpp_charger_not_connected"`; `get_capabilities()` raising `RuntimeError("ocpp_charger_not_connected")`; `send_command()` reason `"ocpp_charger_not_connected"`. The actual implementation produces materially different observable behavior pre-boot: `get_state()` reason is `"reconnecting"` (because `EVChargerAdapter._RECONNECTING_REASONS` maps `ocpp_disconnected`); `get_capabilities()` returns a valid capability profile (PolicyGuard's P2 `capability_check_failed` audit never fires pre-boot — contradicts R3 #2 and R5 t-window analysis); `send_command()` reason is `adapter_internal_error:unexpected_protocol_status:not_connected`. The story's R5 explicitly committed to "operators see the distinction in the structured logs" between `ocpp_charger_not_connected` (never booted) and `ocpp_disconnected` (transient flap) — that distinction is lost in the impl. The runtime_adapter_wiring docstring rationale "no other code path currently calls `register`" conflates "register must be called eagerly" with "resolution must be eager" — a register-at-wiring + proxy-resolves-on-call design satisfies both AC4's reason strings AND the immutability invariant (~30 lines). **Three resolution options for the user:** (a) implement the spec-mandated proxy (register eagerly + return thin proxy wrapping EVChargerAdapter that emits the exact spec reason strings/exception); (b) amend AC4 + AC13 + R3 + R5 to ratify the eager-register design and document the changed audit reason strings; (c) classify as deferred-work entry "AC4 spec drift — eager-register vs proxy" with severity HIGH, classification `must-before-epic-10` (Epic 10 Story 10.2's `Confirmed`-state machinery and any future dashboards keying on OCPP audit reasons depend on the spec strings). This is the LARGEST exposure surface and the principal A7 review-closure gate trigger.

#### Patch (block `Status: review → done` until resolved or reclassified)

- [x] [Review][Patch] **P0 — Implement spec-mandated `_DeferredOCPPAdapter` proxy (AC4)** [`src/open_ems/services/runtime_adapter_wiring.py`] — Per D1 resolution (option 1). Introduce a private `_DeferredOCPPAdapter` class that wraps an `EVChargerAdapter` and intercepts the three contract surfaces to emit the AC4-mandated reason strings: (1) `get_state()` returns `DegradedDeviceState(role=DeviceRole.ev_charger, reason="ocpp_charger_not_connected", ...)` when the underlying raw `OCPPChargerAdapter` is in `connected=False` state; otherwise delegates to `EVChargerAdapter.get_state()`. (2) `get_capabilities()` raises `RuntimeError("ocpp_charger_not_connected")` when not connected; otherwise delegates. (3) `send_command()` returns `CommandResult(status=CommandStatus.failed, applied=False, reason="ocpp_charger_not_connected", correlation_id=command.correlation_id, device_id=command.device_id)` when not connected; otherwise delegates. Eager `register()` stays (for charger identity stability across reconnects). Update `_construct_ocpp_adapter` to wrap `EVChargerAdapter` inside `_DeferredOCPPAdapter`. Update the module docstring's "OCPP design choice" block to describe the actual design (register-eagerly + proxy-resolves-on-call). Update AC13 unit tests to assert the spec reason strings (couples with P11).

- [x] [Review][Patch] **DSMR adapter never started — grid_meter telemetry permanently `dsmr_unavailable` in populated wiring** [`src/open_ems/web/app.py:523` and `src/open_ems/adapters/dsmr/meter_adapter.py:60-61`] — Source: E13/E14 (verified by code reading). `GridMeterAdapter.connect()` invokes `DSMRAdapter.start()` which creates the background `_read_loop` task; without `start()`, `get_raw_state()` returns `dsmr_unavailable` permanently because `_received_at` is `None`. The lifespan does NOT call `.connect()` on any wired adapter, and `ControlLoop._poll_adapters()` only calls `get_state()`. Existing tests do not detect this because they assert wiring structure, not telemetry flow. Fix: add `for adapter in runtime_adapters.control_loop_adapters.values(): await adapter.connect()` immediately after step 4h in lifespan (and mirror the per-adapter try/except pattern used in the disconnect loop). Modbus and OCPP adapters connect lazily so this primarily affects DSMR, but symmetric connect-on-start is the cleaner invariant.

- [x] [Review][Patch] **`OCPPCentralSystem.register()` is NOT idempotent — docstring claim is inaccurate** [`src/open_ems/services/runtime_adapter_wiring.py:314-318` and `src/open_ems/adapters/ocpp/central_system.py:475-479`] — Sources: B1+E1+E2+B19. `register()` always creates a new `OCPPChargerAdapter` and replaces any existing entry under the same `charge_point_id`. The wiring's inline comment "Idempotent: ... `register` overwrites" misrepresents the contract — destructive overwrite is the actual behavior. In production today this is safe (wiring runs once per lifespan, before any WebSocket can connect), but the comment misleads future maintainers. Fix: change the wiring to guard with `existing = ocpp_central_system.get_adapter(charge_point_id); raw_adapter = existing or ocpp_central_system.register(config)`, OR rewrite the comment honestly: "destructive overwrite — safe here because wiring runs at most once per lifespan and before any WebSocket connect; do not call from any other code path". Also add a test that registers a charger first, then runs `build_runtime_adapter_map` against the same `OCPPCentralSystem`, asserting the resulting adapter behavior matches the chosen contract.

- [x] [Review][Patch] **AC14 dispatch-past-P1 assertion missing from integration test** [`tests/integration/web/test_app_runtime_adapter_wiring.py`] — Source: A4. AC14 explicitly required two dispatch-level assertions: (a) populated registry → `PolicyGuard.authorize_and_dispatch(SetBatteryChargeRateCommand)` reason ≠ `adapter_not_registered`; (b) empty registry sibling → reason == `adapter_not_registered`. Neither assertion exists; the file contains only structural / wiring-identity / log-shape assertions. The dispatch-past-P1 verification IS the load-bearing contract for the entire story. Fix: extend `test_lifespan_populated_registry_wires_battery_and_grid_meter` and `test_lifespan_empty_registry_emits_runtime_adapter_map_empty_log` with the dispatch assertions, reaching `PolicyGuard` via `app.state.policy_guard`.

- [x] [Review][Patch] **AC8 structured `adapters` log field missing `protocol` member** [`src/open_ems/web/app.py:514-520`] — Source: A6. AC8 specified the field as `[(role, protocol, device_id), ...]`. Impl emits `[{"role": ..., "device_id": ...}, ...]` only. Operators need `protocol` to disambiguate `(modbus_tcp, battery)` from future `(ocpp_v2, battery)` etc. Fix: thread `entry.protocol` into the data carried by `RuntimeAdapterMap` (e.g., add a `Mapping[DeviceRole, str]` `protocols` field), update the log emission to include `protocol`, and add a `protocol` field assertion to the populated integration test.

- [x] [Review][Patch] **AC16 stale comment in app.py:358-365 not updated** [`src/open_ems/web/app.py:358-365`] — Source: A7. The comment still asserts: "PolicyGuard's adapters mapping stays empty per the Story 8-2 deferred finding, which is its own future story per the Story 9.4 R7 triage" — now false on both counts: the mapping is populated by 9-X, and 9-X is no longer a future story. Fix: rewrite the parenthetical to past tense — "ProtocolAdapterFactory remains validation-only by design; runtime control wiring is owned by `build_runtime_adapter_map` at step 4h (Story 9-X, closed 2026-05-11)". Also audit `src/open_ems/web/templates/installer/_handoff_guide_body.html` for any user-visible references to the pre-9-X `adapters={}` state.

- [x] [Review][Patch] **Sprint-status `last_updated` comment claims "all 19 ACs satisfied" — false** [`_bmad-output/implementation-artifacts/sprint-status.yaml:38`] — Source: A9. Findings D1 + P3 + P4 + P11 demonstrate AC4, AC8, AC13 (test scenario coverage), AC14 are not satisfied as specified. Fix: after this review's resolutions are applied, update the comment to honestly reflect either (a) full satisfaction post-fixes, or (b) the ratified AC4 deviation with linkage to the spec amendment / deferred-work entry.

- [x] [Review][Patch] **`_construct_adapter` does not wrap non-`RuntimeAdapterWiringError` exceptions — AC9 fail-loud handler bypassed** [`src/open_ems/services/runtime_adapter_wiring.py:185-195`] — Sources: E10+E16+E20. `_construct_modbus_adapter` can raise `pydantic.ValidationError` from `ModbusTcpAdapterConfig`; `_construct_ocpp_adapter` can raise from `register()` (`pydantic.ValidationError` from `OCPPAdapterConfig`, etc.). The lifespan handler `except RuntimeAdapterWiringError` (app.py:495) only catches the named class; bare exceptions escape to the outer `try`, log no structured `runtime_adapter_wiring_failed` event, and may fall through to the partial-cleanup path. Fix: wrap each `_construct_*` call site in `try/except Exception as exc: raise RuntimeAdapterWiringError(f"Adapter construction failed for {entry.device_id!r}: {exc}") from exc` to ensure AC9's fail-loud contract holds for all failure paths.

- [x] [Review][Patch] **Shutdown disconnect loop has no per-adapter timeout — hung disconnect blocks teardown** [`src/open_ems/web/app.py:572-582`] — Source: E9. `await adapter.disconnect()` has no `asyncio.wait_for` guard. A blocked Modbus socket close or DSMR stop() awaiting a cancelled task could exceed systemd's STOPSIGTERM grace window, leading to SIGKILL mid-WAL-checkpoint. Fix: `await asyncio.wait_for(adapter.disconnect(), timeout=5.0)` per adapter; on TimeoutError, log `adapter_disconnect_timeout` at WARNING and continue to the next adapter.

- [x] [Review][Patch] **`charge_point_id` derivation has silent corruption surface** [`src/open_ems/services/runtime_adapter_wiring.py:307-312`] — Sources: B18+E3+E4+E5. `charge_point_id = entry.address.lstrip("/") or entry.device_id` silently mishandles edge cases: `address="/"` falls back to `device_id`; `address="//cp-001"` strips both slashes; `address="/cp-001/charger-A"` becomes `cp-001/charger-A` (invalid for OCPP routing); the `device_id` fallback may itself contain illegal OCPP characters. Fix: validate the derived `charge_point_id` against an explicit allowed character set (e.g., `^[A-Za-z0-9_-]+$`) and raise `RuntimeAdapterWiringError` with a structured message on invalid input. Or, more cleanly, change Story 9.1's address recording convention to put the bare `charge_point_id` (no leading slash) in `entry.address`, then drop the `lstrip` here.

- [x] [Review][Patch] **Partial-construct leak on mid-loop failure** [`src/open_ems/services/runtime_adapter_wiring.py:182-195`] — Source: B3. If `_construct_adapter` raises for the third role after the first two were constructed, the first two adapters are not `disconnect()`-ed before the exception propagates. Today Modbus/OCPP construction is lazy so this is benign; a future adapter with eager init would leak. Fix: accumulate constructed adapters in a local list; on exception, run a best-effort `disconnect()` loop over them before re-raising as `RuntimeAdapterWiringError`.

- [x] [Review][Patch] **OCPP pre-boot test asserts only `applied is False` — `reason` field never checked** [`tests/unit/services/test_runtime_adapter_wiring.py:296-329`] — Sources: B12+A3. `test_ocpp_pre_boot_send_command_returns_failed` docstring claims contract clause 3 mapping but only asserts `result_cmd.applied is False`. The reason string is the operator-facing contract surface (see D1). Fix: assert `result_cmd.status == CommandStatus.failed` and `result_cmd.reason` matches whichever contract is ratified by the D1 resolution. (Linked to D1 — defer fix details until D1 is resolved.)

- [x] [Review][Patch] **`""` parametrize case is unreachable test-coverage sleight-of-hand** [`tests/unit/services/test_runtime_adapter_wiring.py:415-438`] — Sources: B11+A11. `test_modbus_malformed_address_raises` parametrizes `address=""` then substitutes `"valid:502"` and `pytest.skip()`s — the empty-string case is never executed but its parametrize ID falsely suggests coverage. The 9-Y-a precedent (naming-honesty) applies. Fix: remove `""` from the parametrize list; add a separate test that constructs `DeviceRegistryEntry` with `address=""` directly and asserts Pydantic rejects at entry-construction.

- [x] [Review][Patch] **Integration test bootstraps DB via discarded lifespan run — fragile against future non-idempotent startup steps** [`tests/integration/web/test_app_runtime_adapter_wiring.py:107-108`] — Source: B17. `test_lifespan_populated_registry_wires_battery_and_grid_meter` runs lifespan twice on the same DB file. Works today only because all lifespan steps happen to be idempotent. A future non-idempotent startup step (e.g., one-time admin bootstrap or state-mutating capability check) would break this test confusingly. Fix: bootstrap schema via a direct call to `_run_alembic_upgrade(db_url, settings.alembic_ini_path)` in a fixture, separate from any lifespan run.

- [x] [Review][Patch] **Mock-based log assertions in integration test are fragile** [`tests/integration/web/test_app_runtime_adapter_wiring.py`] — Source: B10. Tests `with patch("open_ems.web.app.logger", mock_logger):` and inspect `.info.call_args_list`. Any future migration to chained structlog calls (`logger.bind(...).info(...)`) silently breaks these assertions. Fix: use `structlog.testing.capture_logs` for log-shape assertions; this is the project's intended pattern.

- [x] [Review][Patch] **`app.state.runtime_adapters` and `app.state.policy_guard` exposure contradicts story R6** [`src/open_ems/web/app.py:523, 533` and the story file R6 section at line 424] — Sources: B15+E22+A13. R6 declared: "`app.state` does NOT cache it (per existing pattern — PolicyGuard / ControlLoop are not exposed via `app.state`)." Impl exposes both. Tests use the exposures. Fix: either (a) document the new test-seam exposures and update R6 to retract the "not exposed" statement, or (b) remove the exposures and use a different test seam (e.g., a private factory or dependency override). Prefer (a) — the test-seam is the simplest mechanism.

#### Defer (linked to `deferred-work.md`)

- [x] [Review][Defer] **`_MODBUS_REGISTER_RANGES_BY_MODEL` values are PLACEHOLDER** [`src/open_ems/services/runtime_adapter_wiring.py:106-123`] — Source: B8. Pre-existing PLACEHOLDER status in the `register_maps` modules; 9-X propagates them into the runtime polling path for the first time. Real hardware will read wrong registers. Defer to `deferred-work.md` with classification `must-before-real-hardware-commissioning`; not blocking 9-X delivery because zero real-hardware deployments exist.

- [x] [Review][Defer] **`_MODBUS_REGISTER_RANGES_BY_MODEL` keys must stay synchronized with `_SUPPORTED_MODELS`** [`src/open_ems/services/runtime_adapter_wiring.py:106-123`] — Source: E15. Adding a model to `BatteryAdapter._SUPPORTED_MODELS` / `InverterAdapter._SUPPORTED_MODELS` without updating this dict yields a runtime `SystemExit` on the next restart that wires the new device. The capability-registry alignment check at step 4a doesn't cover this dict. Defer; add a maintenance-test invariant in a follow-up story (parallel to the existing `test_capability_registry.py` discovery).

- [x] [Review][Defer] **Test-matrix gap: `(modbus_tcp, inverter)` without model, `(modbus_tcp, ev_charger)` not exercised** [`tests/unit/services/test_runtime_adapter_wiring.py`] — Source: B13. Existing tests cover battery+modbus exhaustively but not all role-branch combinations. Pre-existing test-completeness issue, not a 9-X regression. Defer to a test-hygiene pass.

#### Dismissed as noise (~14)

- `del settings` / "reserved parameter" pattern (B9, B20) — style preference; impl is correct.
- Multi-device-per-role error message names only two devices (B14) — by design; first-conflict-wins is fine.
- Empty registry constructs PolicyGuard with empty map (B16) — explicitly required by AC7; this is the documented cold-start contract.
- Log event shape differs between empty and populated (B7) — distinct events with distinct fields is the existing project convention.
- Log adapters field unbounded (B6) — N=4 in v1; cap is premature optimization.
- Whitespace / IPv6-without-brackets / shutdown order / unconnected adapter disconnect / port leading-zero / Unicode digit edge cases on parsers (E6, E7, E18, E19, E23, E24) — out of 9-X's introduced scope; address-parsing module is extracted unchanged from `protocol_adapter_factory`.
- WARNING vs INFO log level on disconnect (A12) — cosmetic preference.
- `@pytest.mark.asyncio` markers missing (A16) — project uses `asyncio_mode=auto`.
- AC18 / AC15 positive controls (A14, A15) — passed verification, not findings.
- Adapter device_id sanitization in logs (E12) — `device_id` is constrained by Pydantic `NonEmptyStr`; no injection vector demonstrated.
- `getattr(adapter, 'device_id', '<unknown>')` defensive read in shutdown logging (E21) — `DeviceAdapter` protocol requires the attribute; defensive read would mask real bugs.
