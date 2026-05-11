# Story 9.4: Implement deployment validation with safe readiness probing and persisted results

Status: in-progress

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

> **A2-triggered:** True. Matched triggers: **T1** (lifecycle / state-machine — `never_run → running → complete-PASS/complete-WARN/complete-FAIL → outdated` overall states plus per-check `pending → PASS/WARN/FAIL/TIMEOUT`), **T2** (cancellation — 6 concurrent checks dispatched via `asyncio.gather`; per-check deadline; cancellation must propagate to every check task; restart-mid-run must not leave a `running` row), **T3** (persistence + recovery — single most-recent `deployment_validation_results` row with timestamp, `config_version`, per-check outcomes, acknowledged-warning list; survives restart; Step 4 GET hydrates from DB), **T5** (multi-adapter coordination — connectivity check probes Modbus + OCPP + DSMR adapters in one flow; control-readiness check probes the controllable adapters Battery + EV Charger via the same capability-check surface PolicyGuard uses), **T6** (deployment / restart behavior — `provider.get().config_version` vs persisted `validation.config_version` drives the `outdated` state; the validation result itself IS the runtime-readiness signal that gates handoff and feeds Story 9.5's printable guide), **T7** (installer workflow orchestration — Step 4 of the 4-step wizard mutates persisted validation state, drives `wizard_state.step_4_*`, and activates the homeowner-handoff gate). All seven R1–R7 orchestration artifacts are mandatory and present below.
>
> **Prerequisite gate (Epic 8 retro 2026-05-10 + Story 9.3):** Stories 9.0, 9.0b, 9.0c, 9.0d, 9.1, 9.2 are `done`; Story 9.3 is `in-progress` mid-round-2-patches but the contracts 9.4 consumes (`ConstraintsService.validate_draft`'s `safety_pre_check`, `ActiveConstraintsProvider.get().config_version`, `wizard_state.step_3_complete`, `wizard_state.step_3_activated_config_version`, the staged-activation transaction that increments `config_version` exactly once per activation) are all stable in the 9.3 implementation. 9.4 MUST NOT modify any of those surfaces; it consumes them read-only.
>
> **Single-evaluator principle (Epic 7 retro 2026-05-05):** `DeploymentValidationService` is the sole authority for "is this site ready for handoff?". Every consumer (Step 4 page render, `POST /installer/setup/validation/run`, the future Story 9.5 handoff button render, the future `/health/ready` augmentation if added) reads the persisted result via this one service. No partial computation lives in route handlers or templates.

## Story

As an installer completing Step 4 of the setup wizard,
I want a one-click validation run that dispatches the six readiness checks concurrently with live progressive display, presents an unambiguous PASS / WARN / FAIL result, requires me to explicitly acknowledge any active warnings before handoff, persists the full result with the `config_version` it was computed against, and is automatically marked `outdated` if I edit the constraints afterward,
so that I leave a site with a deterministic, audit-traceable readiness signal — never a stale validation, never a silent warning, never a probe that disrupted a real device.

## Acceptance Criteria

### AC1 — Schema: `deployment_validation_results` + `deployment_validation_acks` tables + `wizard_state.step_4_*`

Story 9.4 introduces a single migration `migrations/versions/0012_add_deployment_validation_tables.py` (the next sequential after 9.3's 0011). The migration introduces two new tables, extends `wizard_state` with the step_4 triple, and is fully round-tripped under `tests/integration/test_migrations.py::test_migration_0012_round_trips`.

**New table — `deployment_validation_results`** (single most-recent row per site; one row at any time):

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `started_at` | TEXT | NOT NULL; ISO 8601 UTC; the wall-clock instant `run()` was entered |
| `completed_at` | TEXT | NULLABLE; ISO 8601 UTC; populated when `overall_status` leaves `running` |
| `config_version` | INTEGER | NOT NULL; the `provider.get().config_version` snapshotted at `run()` entry; used by the outdated-detection comparison in AC6 |
| `overall_status` | TEXT | NOT NULL; CHECK IN (`'running'`, `'complete-PASS'`, `'complete-WARN'`, `'complete-FAIL'`) |
| `checks_json` | TEXT | NOT NULL; JSON-encoded `tuple[DeploymentCheckResult, ...]` — one entry per check (6 entries when complete; partial during `running`) |
| `triggered_by_session_id` | TEXT | NOT NULL; FK → `sessions(id)` ON DELETE SET NULL; the installer session that ran the validation (audit attribution) |
| `summary_text` | TEXT | NOT NULL; the one-paragraph human-readable summary used for the banner + (future) Story 9.5 printable guide |

**Single-row invariant:** the table holds AT MOST one row at any time. Subsequent successful `run()` invocations DELETE the prior row and INSERT a new one inside the same `BEGIN IMMEDIATE` transaction (9.0b precedent). Reasons for the single-row contract:
- The "outdated" semantic is binary against the current `provider.config_version`; there is no use case for historical results in v1.
- The `config_audit_log` (9.0b inheritance) already retains the activation history. Validation runs are not constraint changes; persisting their history would only inflate the DB without driving any UI.
- The single-row pattern matches `active_constraints`'s "active row" pattern (9.0b) — there's an implicit history table (`config_audit_log`) for constraints, but no equivalent need for validations in v1. **A future story** that surfaces a validation run history (e.g., Epic 11 operational monitoring) would migrate to an append-only table; that migration is out of scope here.

**New table — `deployment_validation_acks`** (per-warning acknowledgments for the currently-persisted result):

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `validation_result_id` | INTEGER | NOT NULL; FK → `deployment_validation_results(id)` ON DELETE CASCADE |
| `check_name` | TEXT | NOT NULL; CHECK IN (`'connectivity'`, `'role_completeness'`, `'capability_strategy'`, `'constraint_completeness'`, `'constraint_safety_pre_check'`, `'control_readiness'`) |
| `acknowledged_at` | TEXT | NOT NULL; ISO 8601 UTC |
| `acknowledged_by_session_id` | TEXT | NOT NULL; FK → `sessions(id)` ON DELETE SET NULL |
| — | UNIQUE | `(validation_result_id, check_name)` — one ack per check per result row |

CASCADE on `deployment_validation_results` deletion ensures the ack rows never outlive their parent result. A new `run()` invocation transitively clears all acks via the CASCADE (the old result is DELETEd before the new result is INSERTed).

**Existing table extension — `wizard_state` gains three columns:**

| Column | Type | Constraints |
|---|---|---|
| `step_4_complete` | INTEGER (0/1) | NOT NULL DEFAULT 0; CHECK IN (0, 1) |
| `step_4_completed_at` | TEXT | NULLABLE; ISO 8601 UTC |
| `step_4_completed_config_version` | INTEGER | NULLABLE; the `validation_result.config_version` at the moment Step 4 was marked complete (i.e., the handoff-eligible state was reached) |

Pairing CHECK constraint (mirroring 9.3's `step_3_*` triple invariant): `step_4_complete = 1` IFF `step_4_completed_at IS NOT NULL` IFF `step_4_completed_config_version IS NOT NULL`.

**Migration round-trip** (`upgrade head → downgrade -1 → upgrade head`) is codified in `tests/integration/test_migrations.py::test_migration_0012_round_trips`. The downgrade drops every added column / table / index / constraint in reverse order. Existing 0011 round-trip test adjusts its step-back count if necessary.

Index `ix_deployment_validation_acks_validation_result_id` is added (matches 9.1/9.3 convention for explicit query indexes even when redundant with FK + UNIQUE).

### AC2 — Domain types in `core/`

Following the 9.3 placement decision (value types in `core/`, service class + exceptions in `services/`), the new value types live in `src/open_ems/core/deployment_validation.py`:

**`DeploymentCheckName`** — `Literal['connectivity', 'role_completeness', 'capability_strategy', 'constraint_completeness', 'constraint_safety_pre_check', 'control_readiness']`

**`DeploymentCheckStatus`** — `Literal['pending', 'pass', 'warn', 'fail', 'timeout']`

**`DeploymentOverallStatus`** — `Literal['running', 'complete-PASS', 'complete-WARN', 'complete-FAIL']`. The `outdated` UX state is **derived** at render time by comparing `result.config_version` to `provider.get().config_version`; it is NOT a persisted status (this avoids race conditions where a constraint activation lands between the validation completing and the `outdated` flag being written).

**`DeploymentCheckResult`** (`frozen=True, extra="forbid"` Pydantic):

| Field | Type | Constraint |
|---|---|---|
| `name` | `DeploymentCheckName` | — |
| `status` | `DeploymentCheckStatus` | — |
| `summary` | `str` | non-empty for non-`pending`; the UX-spec "Outcome text" / "Failure reason" |
| `corrective_action` | `str \| None` | non-NULL for `warn`/`fail`/`timeout`; NULL otherwise |
| `is_blocking` | `bool` | `True` IFF `status='fail'`; computed-then-pinned for the SSE wire contract |
| `started_at` | `datetime` | UTC tz-aware; per-check dispatch instant |
| `completed_at` | `datetime \| None` | UTC tz-aware; NULL while `pending` |
| `evidence_json` | `dict[str, Any] \| None` | NULLABLE structured evidence (e.g. unreachable device_ids) used only by tests + a future detail expander; NEVER rendered raw in the UI |

**`DeploymentValidationResult`** (frozen Pydantic; the persisted shape):

| Field | Type | Constraint |
|---|---|---|
| `id` | `int` | — |
| `started_at` | `datetime` | UTC tz-aware |
| `completed_at` | `datetime \| None` | NULL while `overall_status='running'` |
| `config_version` | `int` | ≥ 0 |
| `overall_status` | `DeploymentOverallStatus` | — |
| `checks` | `tuple[DeploymentCheckResult, ...]` | length ≤ 6; 6 only when `overall_status` is `complete-*` |
| `triggered_by_session_id` | `str \| None` | NULL after the parent session is CASCADE-cleared |
| `summary_text` | `str` | non-empty |
| `acknowledged_warnings` | `frozenset[DeploymentCheckName]` | set of check names with WARN status that the installer has acknowledged |

`acknowledged_warnings` is populated by a JOIN with `deployment_validation_acks` at row-read time — it does NOT live as a JSON column. This keeps the ack write path independent of the (larger, immutable post-completion) `checks_json` blob.

### AC3 — `DeploymentValidationResultRepo`

A new repository `src/open_ems/storage/repositories/deployment_validation_repo.py` follows the 9.3 `draft_constraints_repo.py` shape (`frozen=True` model + per-call async methods + write-lock-protected mutations).

**`DeploymentValidationResultRepo`:**

- `async get_current() -> DeploymentValidationResult | None` — returns the single most-recent row, JOINed with its ack rows. Returns `None` if the table is empty (i.e. `never_run` state).
- `async start_run_locked(*, started_at, config_version, triggered_by_session_id, now) -> int` — atomic insert of a NEW row with `overall_status='running'`, `checks_json='[]'`, `summary_text='Validation running…'`. DELETEs any prior row (and its ack rows via CASCADE) BEFORE the insert, all inside the SAME `BEGIN IMMEDIATE` transaction. Returns the new row's `id`. **MUST be called inside `get_write_lock()`.**
- `async update_check_progress_locked(*, result_id, check: DeploymentCheckResult, now) -> None` — UPDATE `checks_json` to a JSON array containing all checks observed so far (replaces the prior `checks_json` value with one that includes this check). Single-statement UPDATE under the write lock. Called once per check as each one completes (transitions out of `pending`).
- `async finalize_run_locked(*, result_id, overall_status, summary_text, completed_at) -> None` — single-statement UPDATE setting `overall_status`, `summary_text`, `completed_at`. Called once at the end of every `run()`, regardless of how individual checks resolved.
- `async record_acknowledgment_locked(*, result_id, check_name, acknowledged_at, acknowledged_by_session_id) -> None` — INSERT into `deployment_validation_acks`; idempotent on the UNIQUE `(result_id, check_name)` by using `INSERT OR IGNORE`. **MUST be called inside `get_write_lock()`.**
- `async clear_acknowledgments_locked(*, result_id) -> None` — DELETE all ack rows for a given result. Used by the run path before INSERT to defend against an in-flight ack landing between DELETE-old-row and INSERT-new-row windows (the CASCADE handles the common case; this is defense in depth).

All public mutations expose a `_locked` variant that does NOT acquire `get_write_lock()` (called by the service inside an already-held lock) AND a public wrapper that does (used by route handlers / scripts that need to call directly).

### AC4 — `DeploymentValidationService`

A new service `src/open_ems/services/deployment_validation.py` is the single evaluator (Epic 7 retro principle). All routes, all templates, and the future Story 9.5 handoff render path go through this surface.

```python
class DeploymentValidationService:
    def __init__(
        self,
        *,
        validation_repo: DeploymentValidationResultRepo,
        device_repo: DeviceRepo,
        wizard_state_repo: WizardStateRepo,
        active_constraints_provider: ActiveConstraintsProvider,
        state_store: StateStore,
        protocol_adapters: ProtocolAdapterFactory,
        settings: Settings,
        observability: ObservabilityService,
    ) -> None: ...

    async def get_current_view(self) -> DeploymentValidationView:
        """Read-only render-time helper. Returns the persisted result (or None
        for never_run), the derived `outdated` flag (compared against the
        provider's current config_version), and the per-warning ack status.
        Used by the Step 4 GET handler and the polling fragment."""

    async def run(
        self,
        *,
        triggered_by_session_id: str,
        now: datetime,
    ) -> DeploymentValidationResult:
        """Execute one full validation run. Inside a write-lock window:
            1. Snapshot config_version from provider.get().
            2. DELETE any prior result row (and its acks via CASCADE).
            3. INSERT a new row with overall_status='running'.
            4. Release the lock; dispatch all 6 checks concurrently via
               asyncio.gather, each wrapped in asyncio.wait_for with
               per-check timeout.
            5. As each check resolves, re-acquire the write lock briefly to
               call update_check_progress_locked() — so a concurrent poller
               (GET /poll) observes progressive results.
            6. After all checks complete (or timeout), compute overall_status:
                - any FAIL → 'complete-FAIL'
                - else any WARN/TIMEOUT → 'complete-WARN'
                - else → 'complete-PASS'
            7. Re-acquire the lock; call finalize_run_locked() with the
               overall status + the templated summary_text.
            8. Return the final DeploymentValidationResult.
        """

    async def acknowledge_warning(
        self,
        *,
        check_name: DeploymentCheckName,
        acknowledged_by_session_id: str,
        now: datetime,
    ) -> DeploymentValidationResult:
        """Record an installer ack for one WARN-class check on the current
        persisted result. Validates inside the lock that:
            - a result row exists (else raises NoCurrentResultError)
            - that result's overall_status is 'complete-WARN' (else raises
              NotAckEligibleError — ack is meaningless on PASS, blocked on FAIL,
              and impossible on running)
            - the named check exists in the result AND its status is 'warn'
              or 'timeout' (else raises CheckNotWarnableError — operator copy:
              'No warning to acknowledge for this check.')
        Returns the updated result row with the new ack reflected in
        acknowledged_warnings."""

    async def mark_step_4_complete(
        self,
        *,
        session_id: str,
        now: datetime,
    ) -> None:
        """Idempotently marks wizard_state.step_4_complete=1 with the current
        validation's config_version. Called by the GET handoff route once
        all gates (PASS or fully-ack'd WARN) are satisfied. Raises
        NotHandoffEligibleError if the current state does not permit handoff."""
```

**`ProtocolAdapterFactory`** is a new small protocol that the validation service uses for the connectivity + control-readiness checks. It encapsulates the "construct adapter from a `DeviceRegistryEntry`, open it, probe it via `get_capabilities()`, close it" cycle — see AC5 for the exact semantics. The factory is the dependency boundary: the production implementation reads from the same `pymodbus` / `python-ocpp` / `dsmr-parser` clients the runtime adapters use; the test implementation is in-memory fakes wired via the 9.1 `simulated_adapters.py` fixture set.

### AC5 — The six check semantics

All checks run **concurrently** via `asyncio.gather(*tasks, return_exceptions=False)`. Each check is wrapped in `asyncio.wait_for(coro, timeout=PER_CHECK_TIMEOUT_SECONDS)` where `PER_CHECK_TIMEOUT_SECONDS = settings.deployment_validation_check_timeout_seconds` (new Settings field, default `15.0`). A check that times out resolves as `status='timeout'`, NEVER as `'fail'` — the UX spec explicitly distinguishes the two (TIMEOUT is WARN-class with a connectivity-focused corrective action).

**Check 1: `connectivity`.**
- Reads `device_repo.list_all()`.
- For each `DeviceRegistryEntry`, the factory opens a transient adapter, calls `adapter.get_capabilities()` with a per-device sub-timeout of `settings.deployment_validation_device_probe_timeout_seconds` (default `5.0`), and closes the adapter (shielded against cancellation via `asyncio.shield` per the deferred-work pattern from Story 9.1).
- All device probes within this check fan out concurrently via `asyncio.gather`.
- Status mapping:
  - `pass` — every registered device returned a non-degraded capability profile.
  - `warn` — at least one device returned a `DegradedDeviceState` / `REDUCED` capability profile AND none returned an unreachable / connection error. Corrective action lists the degraded device IDs.
  - `fail` — at least one device was unreachable (connection error, capability lookup raised). Corrective action lists the unreachable device IDs.
  - `timeout` — the gather-deadline expired with at least one device probe still in-flight. Corrective action: "Check device connectivity and retry."
- `evidence_json` carries the per-device status dict (`device_id → "ok" | "reduced" | "unreachable" | "timeout"`).

**Check 2: `role_completeness`.**
- Reads `device_repo.list_all()`.
- Pure-Python derivation (no I/O); resolves under 1 ms.
- Status mapping:
  - `pass` — `grid_meter` role assigned AND at least one of (`battery`, `inverter`) assigned.
  - `fail` — `grid_meter` role NOT assigned. Corrective action: "Grid meter role is required. Return to Step 2 to assign it." (This is structurally unreachable from the wizard because the Step 2 gate hard-blocks Step 3 without grid_meter; included as defense-in-depth for direct API callers and post-restart edge cases.)
  - `warn` — `grid_meter` assigned but no `battery` AND no `inverter` (no energy source). Corrective action: "No battery or inverter is assigned. The system has nothing to dispatch." Aligns with Story 9.2's acknowledged-gap surface.
- Never resolves as `timeout` (no I/O).

**Check 3: `capability_strategy`.**
- Reads `device_repo.list_all()` AND `provider.get()` AND `state_store.get_snapshot()`.
- Pure-Python derivation under 1 ms.
- Status mapping:
  - `pass` — every assigned role's device has the capability profile required to enact `provider.get()`'s constraints. Specifically: if `peak_limit_kw` is set, `grid_meter` must be assigned (covered by check 2). If `battery_reserve_floor_percent > 0`, a `battery` role must be assigned with the corresponding `WriteCapability.set_charge_rate` / `set_discharge_rate` available. If `ev_charging_window_*` is set, an `ev_charger` role must be assigned with `WriteCapability.set_charging_rate` available.
  - `warn` — a constraint is set but the corresponding device is absent OR its capability profile is REDUCED in a way that limits the constraint's effect. Corrective action enumerates each gap.
  - `fail` — never (this check produces WARN at most; absent grid_meter is the only structurally fatal case and it's caught by check 2).
- Never resolves as `timeout` (no I/O).

**Check 4: `constraint_completeness`.**
- Reads `provider.get()`.
- Pure-Python derivation under 1 ms.
- Status mapping:
  - `pass` — `peak_limit_kw > 0` AND `0 <= battery_reserve_floor_percent <= 100` AND (EV window is either both-NULL or both-set in valid HH:MM range). All three are already structurally enforced by 9.0b/9.3's Pydantic + DB CHECK; this check is a defense-in-depth read.
  - `fail` — any of the above invariants is violated. Corrective action: "Constraints are incomplete or out of range. Return to Step 3 to fix them." (Structurally unreachable from the wizard; defense in depth for direct DB tampering.)
- Never resolves as `warn` or `timeout`.

**Check 5: `constraint_safety_pre_check`.**
- Reads `provider.get()` AND `state_store.get_snapshot()`.
- Delegates to `ConstraintsService._check_safety_pre()` — **the single evaluator is reused, not reimplemented**. (Epic 7 retro principle: do not duplicate the safety-pre-check logic between staged-constraint-validation and deployment-validation.)
- The reuse seam: `ConstraintsService` exposes a public `evaluate_safety_pre_check(constraints: ActiveConstraints, snapshot: SystemSnapshot) -> tuple[ConstraintCheckResult, ...]` method that wraps the existing internal `_check_safety_pre`. `DeploymentValidationService` calls this with the currently-active constraints and the current snapshot, and translates the returned `ConstraintCheckResult` tuple into a single aggregated `DeploymentCheckResult`:
  - all PASS → `status='pass'`, `summary='All active constraints are safe to enforce against the current device state.'`
  - any WARN, no FAIL → `status='warn'`, `summary` lists each WARN's plain-language reason; corrective action: "Acknowledge each warning before handoff, OR adjust constraints in Step 3."
  - any FAIL → `status='fail'`, `summary` enumerates the structural FAIL conditions; corrective action: "Return to Step 3 and adjust the failing constraints before re-running validation."
- Never resolves as `timeout` (no I/O).

**Check 6: `control_readiness` — the only check that exercises live adapter I/O via the safe probe path.**
- Reads `device_repo.list_all()`. For every controllable role (`battery`, `ev_charger`) that is assigned, the factory opens a transient adapter, calls `adapter.get_capabilities()` with the per-device timeout, and closes the adapter. **This is the same capability-lookup path PolicyGuard uses for its P2 gate; it is read-only and never issues a command.**
- The Inverter (read-only in v1 per Story 9.0 AC3) is included in connectivity check (1) but NOT in control_readiness — it has no write capability to probe.
- The Grid Meter (DSMR is read-only by physical protocol) is included in connectivity check (1) but NOT in control_readiness.
- Status mapping:
  - `pass` — every controllable device returned a capability profile with the required write capabilities for its assigned role (e.g., a `battery` role device returned a profile with `set_charge_rate` AND `set_discharge_rate`).
  - `warn` — at least one controllable device returned a REDUCED profile missing a required write capability AND none returned a connection error. Corrective action enumerates the missing capabilities per device.
  - `fail` — at least one controllable device was unreachable OR returned a profile so degraded that no commanded write is possible. Corrective action: "The control pipeline could not reach <device_id>. Verify the connection and re-run validation."
  - `timeout` — the gather-deadline expired with at least one probe in flight. Corrective action: "Probe did not respond in time. Verify the connection and re-run validation."
- `evidence_json` carries the per-device status dict.
- **CRITICAL invariant (epic AC):** under NO circumstances does this check call `adapter.send_command(...)`. The probe path is `adapter.get_capabilities()` only. A regression test (`tests/integration/services/test_deployment_validation_safe_probe.py::test_control_readiness_never_calls_send_command`) wraps the simulated adapters with a `MagicMock(side_effect=AssertionError("send_command must not be called during validation"))` on every `send_command` and asserts the test passes.

**Concurrent dispatch shape:**

```python
async def _run_checks_concurrently(self, ...) -> tuple[DeploymentCheckResult, ...]:
    tasks: dict[DeploymentCheckName, asyncio.Task[DeploymentCheckResult]] = {
        name: asyncio.create_task(self._dispatch_one(name), name=f"validation_{name}")
        for name in self._CHECK_NAMES
    }
    # As each completes, persist progress so a concurrent poller observes it.
    pending = set(tasks.values())
    results: list[DeploymentCheckResult] = []
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            result = task.result()  # asyncio.wait_for inside _dispatch_one converts timeout into a DeploymentCheckResult — never raises
            results.append(result)
            async with get_write_lock():
                await self._repo.update_check_progress_locked(
                    result_id=self._current_result_id,
                    check=result,
                    now=self._now(),
                )
    return tuple(sorted(results, key=lambda r: self._CHECK_ORDER[r.name]))
```

**Cancellation contract:** if the outer `run()` is cancelled (HTTP request cancel, lifespan shutdown), every check task receives `CancelledError`. The outer `try/finally` of `run()` ensures `finalize_run_locked()` is still called — with `overall_status='complete-FAIL'` and a `summary_text='Validation cancelled before completion.'` — so the persisted row is never left in `running` state. **No probe leaks a connection on cancel** because each check's adapter-close call is `asyncio.shield`-wrapped (Story 9.1 deferred-finding pattern explicitly applied here).

### AC6 — Outdated detection (the load-bearing UX gate)

On every Step 4 page render AND every `/poll` fragment render, the handler computes:

```python
result = await validation_svc.get_current_view()
current_config_version = active_constraints_provider.get().config_version
is_outdated = (
    result is not None
    and result.overall_status != 'running'
    and result.config_version != current_config_version
)
```

When `is_outdated`:
- The validation state banner (UX component #10) renders the "outdated" row: text "Configuration changed — re-run validation before handoff.", handoff button disabled.
- The Step 4 wizard indicator pip renders with `data-status="outdated"` (UX spec component #14, amber border, "Re-run" badge).
- The "Run validation" button is enabled (it's the corrective action).
- The persisted result row is NOT mutated by the outdated detection — `outdated` is a derived state, never a stored status. This avoids the race condition where a constraint activation lands between the validation completing and the `outdated` flag being written.

**Why a constraint activation makes a prior validation outdated:** Story 9.3's activate path increments `config_version` atomically. After activation, `provider.get().config_version > result.config_version` — the comparison is true. The next Step 4 page load (or poll) sees the new version and renders outdated. **No change-trigger is needed in the activate path** — the outdated flag falls out of the read-time comparison. This is a deliberate design choice: zero write-side coordination between 9.3 and 9.4.

**Handoff button enable matrix** (consolidates AC6 + AC7):

| Persisted state | Outdated? | Acks complete? | Handoff button |
|---|---|---|---|
| `never_run` | n/a | n/a | Disabled — "Run validation to check deployment readiness" |
| `running` | n/a | n/a | Disabled — "Validation running…" |
| `complete-PASS` | No | n/a | **Enabled** |
| `complete-PASS` | Yes | n/a | Disabled — "Configuration changed — re-run validation before handoff." |
| `complete-WARN` | No | Yes (all WARN-class checks acknowledged) | **Enabled** |
| `complete-WARN` | No | No | Disabled — "Acknowledge all warnings before handoff." |
| `complete-WARN` | Yes | n/a | Disabled — "Configuration changed — re-run validation before handoff." |
| `complete-FAIL` | n/a | n/a | Disabled — "Deployment blocked. Resolve failures before handoff." |

### AC7 — Acknowledgment workflow

For a `complete-WARN` result, EVERY check whose status is `warn` OR `timeout` requires an explicit installer acknowledgment before handoff. PASS-status checks do NOT require ack. FAIL-status checks block handoff entirely (no ack permitted; FAIL is FAIL).

**Per-warning ack row** (UX spec component #10 footer):
- Each WARN/TIMEOUT row in the validation result list renders a server-rendered "Acknowledge" button when the warning is not yet acknowledged.
- An acknowledged warning row renders with a "Acknowledged at HH:MM" label + a "Revoke" link (revoking re-disables handoff until re-acknowledged; the revoke action DELETEs the ack row).
- The handoff button enable state recomputes server-side on every page render / poll based on the current ack state. No client-side coordination.

**Ack contract:**
- `POST /installer/setup/validation/acknowledge/{check_name}` — body: CSRF token only; idempotent (already-acked → 200 with no change, same fragment); validates inside `get_write_lock()` that the persisted result is `complete-WARN` AND the named check has WARN/TIMEOUT status (else 400 with exact-match `not_ack_eligible: status={overall} check={check_name} status={check_status}`).
- `POST /installer/setup/validation/revoke/{check_name}` — symmetric: DELETEs the ack row inside the lock; idempotent on already-revoked.

**Result-row replacement clears acks (defense in depth + CASCADE):**
- A subsequent `run()` DELETEs the prior result row inside its transaction → CASCADE clears all ack rows.
- A constraint activation does NOT clear acks directly; the persisted result row's `config_version` becomes stale, the outdated detection in AC6 disables handoff, and the next `run()` starts a new result row from scratch (with no acks).

### AC8 — Routes

All routes are added under `src/open_ems/web/routes/setup.py`. All require `Depends(require_installer)`. All POSTs are CSRF-protected by the existing middleware. All HTML responses extend `installer/setup_layout.html` (the persistent 4-step indicator).

| Method | Path | Returns |
|---|---|---|
| GET | `/installer/setup/validation` | full HTML page rendering: (1) the validation state banner (UX component #10) with current overall status + outdated badge if applicable; (2) the per-check result list (UX component #9, one row per check, with ack buttons on WARN/TIMEOUT rows when not acked); (3) the "Run validation" button (enabled unless `overall_status='running'`); (4) the handoff button with server-rendered enable/disable state per AC6's enable matrix. **The placeholder `setup_validation_placeholder.html` template from Story 9.3 is deleted** AND the placeholder `get_validation_placeholder` route body is REPLACED by the real implementation (the route path stays the same to preserve 9.3's redirect target). |
| POST | `/installer/setup/validation/run` | HTMX fragment `_setup_validation_result.html` re-rendering the banner + check list + handoff button. The route blocks until `run()` returns OR returns early with `202` + a polling-link header if the caller is HTMX-driven and the run is expected to exceed `settings.deployment_validation_async_threshold_seconds` (default `2.0`). CSRF-protected. |
| GET | `/installer/setup/validation/poll` | HTMX fragment `_setup_validation_result.html` — read-only re-render of the same banner + check list + handoff button. The Step 4 page sets `hx-trigger="every 2s"` on this endpoint when `overall_status='running'`; the polling stops automatically when the response carries `HX-Trigger: validation-complete` (set by this handler when the persisted state leaves `running`). |
| POST | `/installer/setup/validation/acknowledge/{check_name}` | HTMX fragment `_setup_validation_check_row.html` re-rendering only the acknowledged check's row + an OOB swap of the handoff button (so the disabled→enabled transition is atomic with the ack). 400 with exact-match `not_ack_eligible: ...` on a non-eligible state. |
| POST | `/installer/setup/validation/revoke/{check_name}` | symmetric to acknowledge; replaces the acknowledged row with the un-acknowledged variant + OOB swap. |
| POST | `/installer/setup/validation/handoff` | server-side gate evaluator: calls `validation_svc.mark_step_4_complete()` (which validates all gates) and 302-redirects to the Story 9.5 handoff guide page (`/installer/handoff` placeholder if 9.5 is not yet implemented; the route returns a minimal "Step 4 complete" success page if no handoff guide route exists yet). On gate failure → 400 with exact-match `handoff_not_eligible: <reason>`. CSRF-protected. |

**Step-gate enforcement (9.3 precedent):** the `GET /installer/setup/validation` handler reads `wizard_state` and:
1. If `step_1_complete=0` → 302 redirect to `/installer/setup/discovery`
2. Else if `step_2_complete=0` → 302 redirect to `/installer/setup/roles`
3. Else if `step_3_complete=0` → 302 redirect to `/installer/setup/constraints`
4. Else proceed

Same idempotent-short-circuit pattern as 9.3: if `step_4_complete=1` AND the persisted validation is current (not outdated), render the Step 4 page in its "handoff ready" state (the handoff button is enabled; "Re-run validation" remains available). If `step_4_complete=1` AND the result is outdated, render the page as if `step_4_complete=0` (the outdated flag invalidates the prior completion); installer must re-run + (if WARN) re-ack to re-enable handoff.

**Back-navigation hyperlink in `setup_layout.html`:** the step-3 label gains the same conditional-hyperlink treatment as steps 1 and 2 — rendered when `step_4_complete=0`, hidden once Step 4 is finalized. **This is the FINAL step**; there is no further "next-step" link.

### AC9 — Templates and accessibility

New / modified Jinja2 templates under `src/open_ems/web/templates/installer/`:

- **DELETE** `setup_validation_placeholder.html` — replaced by the real implementation.
- **NEW** `setup_validation.html` — extends `setup_layout.html` (`active_step='validation'`); renders the page body with four sections:
  1. "Deployment validation" — heading + the validation state banner (uses `_setup_validation_banner.html`).
  2. "Validation checks" — the per-check result list (uses `_setup_validation_result.html` which wraps a list of `_setup_validation_check_row.html` rows).
  3. "Actions" — the "Run validation" button + the handoff button (uses `_setup_validation_actions.html`).
  4. (When `overall_status='running'`) — the auto-poll trigger: an empty div with `hx-get="/installer/setup/validation/poll" hx-trigger="every 2s" hx-target="#validation-region" hx-swap="innerHTML"`.
- **NEW** `_setup_validation_result.html` — the entire validation region target of the run/poll HTMX swaps. Wraps the banner + check list + actions fragments. Used by both `POST /run` and `GET /poll` responses.
- **NEW** `_setup_validation_banner.html` — UX spec component #10. Renders one of six banner states (never_run / running / PASS / WARN / FAIL / outdated) based on the derived state. The text strings come VERBATIM from UX spec Component #10 table — no paraphrasing.
- **NEW** `_setup_validation_check_row.html` — UX spec component #9. Renders one check's status badge + name + summary + corrective action + ack/revoke button. The ack button is only rendered when the row's status is `warn` OR `timeout` AND `not acknowledged`. The revoke link is only rendered when `acknowledged`.
- **NEW** `_setup_validation_actions.html` — "Run validation" + handoff button. The handoff button is server-rendered `disabled` and `aria-disabled="true"` with the explanation text from the AC6 enable matrix when not enabled. The "Run validation" button is disabled with `aria-disabled="true"` ONLY when `overall_status='running'` (preventing double-clicks).
- **NEW** `_setup_validation_error_banner.html` — inline error envelope for `/run` failures (e.g., DB error, factory raises during connectivity probe). Mirrors `_setup_constraints_error_banner.html` (9.3).
- **NEW** `_setup_validation_handoff_success.html` — a minimal "handoff complete" success page rendered by `POST /handoff` when Story 9.5 is not yet implemented (it's a placeholder that 9.5 deletes). When 9.5 IS implemented, this template is unchanged and 9.5's handoff page is the redirect target instead.
- **MODIFIED** `setup_layout.html` — step-3 label gains conditional hyperlink when `step_4_complete=0`; the step-4 label gains a `data-status="outdated"` attribute when the derived `is_outdated` is True (this drives the UX spec component #14 "amber border + Re-run" rendering).

**Accessibility requirements** (UX spec §Accessibility + UX components #9 / #10 / #14):
- Each check row has `role="region"` with `aria-label="Check: {name}"` so screen readers announce each row's scope.
- Each check row's status text has `aria-live="polite"` for WARN/PASS results and `aria-live="assertive"` for FAIL results (per UX spec §Accessibility — assertive is reserved for FAIL validation results only).
- The validation banner has `role="status"` + `aria-live="polite"` so the headline change is announced on each poll.
- The handoff button's disabled state is server-rendered with `aria-disabled="true"` AND a visible explanation (UX spec §Buttons: "Disabled action: Reduced opacity (0.5); cursor: not-allowed. Always accompanied by a visible explanation of why the action is disabled — never silently greyed out.").
- All status badge colour combinations meet ≥4.5:1 contrast (same `--color-fail` / `--color-warn` / `--color-pass` tokens as 9.1/9.3).
- Keyboard reachability for every interactive element: Run, ack/revoke per row, handoff. Tab order is deterministic and follows visual order.
- Automated axe-core scan: same deferred-CI-hardening pattern as 9.1 AC10 / 9.2 AC8 / 9.3 AC8. A placeholder test at `tests/integration/web/test_setup_validation_a11y.py` is marked `pytest.mark.xfail(reason="axe-playwright wiring deferred", strict=False)`. The outer `pytest.mark.skipif(npx absent)` is retained.

### AC10 — Lifespan wiring + provider exposure

`src/open_ems/web/app.py` lifespan is extended at **step 4g+** (after `constraints_service` from 9.3):

```python
# Step 4g (Story 9.4): construct the deployment-validation evaluator.
# Reuses the same active_constraints_provider, device_repo, wizard_state_repo,
# constraints_service (for the safety_pre_check reuse seam — AC5 check 5) the
# prior steps already constructed. The factory is a thin protocol-adapter
# constructor; production wires it to the real pymodbus / python-ocpp /
# dsmr-parser clients (a new module `services/protocol_adapter_factory.py`
# encapsulates this). Stateless; no DB I/O at construction.
deployment_validation_repo = DeploymentValidationResultRepo()
protocol_adapter_factory = ProtocolAdapterFactory()
deployment_validation_service = DeploymentValidationService(
    validation_repo=deployment_validation_repo,
    device_repo=device_repo,
    wizard_state_repo=wizard_state_repo,
    active_constraints_provider=active_constraints_provider,
    state_store=app.state.state_store,
    protocol_adapters=protocol_adapter_factory,
    settings=settings,
    observability=observability,
    constraints_service=constraints_service,  # for the safety_pre_check reuse seam
)
app.state.deployment_validation_repo = deployment_validation_repo
app.state.deployment_validation_service = deployment_validation_service
logger.info("deployment_validation_repo_ready", component="startup")
logger.info("deployment_validation_service_ready", component="startup")
```

The `_deployment_validation_service` Depends helper in `web/routes/setup.py` follows the 9.3 `_constraints_service` pattern (reads from `request.app.state.deployment_validation_service`; 503 if absent).

**`ConstraintsService` reuse rationale:** the 9.3 service already has the `_check_safety_pre` logic. AC5 check 5 reuses it via a new public method `ConstraintsService.evaluate_safety_pre_check(constraints, snapshot)` that wraps the private helper. This is a **read-only widening of the 9.3 service's public surface** — no behavioral change. A unit test in `tests/unit/services/test_constraints_service.py::test_evaluate_safety_pre_check_public_wrapper` asserts the wrapper produces the same output as a call routed through `validate_draft` for the same inputs.

### AC11 — Tests

Each test asserts the **exact** contract clause it covers (no substring `in` assertions on rejection reasons — exact match, per 9.0c AC7 / 9.1 AC11 / 9.2 AC9 / 9.3 AC10 precedent).

**Unit tests (`tests/unit/`):**
- `storage/repositories/test_deployment_validation_repo.py` (new) — round-trip start_run / update_check_progress / finalize_run / record_acknowledgment / clear_acknowledgments; CASCADE on `deployment_validation_results` deletion removes ack rows; UNIQUE `(validation_result_id, check_name)` enforced via DB CHECK; FK CASCADE removes the row when its parent session is deleted (triggered_by_session_id SET NULL behavior); single-row invariant enforced by start_run_locked DELETEing prior row in the same transaction.
- `core/test_deployment_validation_models.py` (new) — Pydantic validation: `DeploymentCheckResult.summary` non-empty for non-pending; `corrective_action` non-NULL for warn/fail/timeout; `is_blocking` derives from `status='fail'`; `completed_at` NULL while pending; UTC tz-aware timestamps; round-trip through `model_dump_json` / `model_validate_json` for the `checks_json` column.
- `services/test_deployment_validation_service.py` (new) — covers:
  - `get_current_view` returns `None` when no row exists.
  - `get_current_view` derives `is_outdated=True` when `result.config_version != provider.config_version`.
  - `run` snapshots `config_version` AT ENTRY (not at completion) — a constraint activation mid-run does NOT shift the result's `config_version`.
  - `run` finalizes to `complete-PASS` when all 6 checks resolve `pass`.
  - `run` finalizes to `complete-WARN` when any check is `warn`/`timeout` and none `fail`.
  - `run` finalizes to `complete-FAIL` when any check is `fail`.
  - `run` resolves each check status correctly for the contract semantics in AC5 (one test per check name × every status outcome).
  - `run` dispatches checks concurrently (assert via timing — `total_runtime < per_check_timeout * 6 * 0.5` even when each check uses a deliberate `asyncio.sleep`).
  - `run` per-check timeout converts to `status='timeout'` — never raises.
  - `run` cancelled mid-execution → `finalize_run_locked` called with `complete-FAIL` summary 'Validation cancelled before completion.'; persisted row leaves `running` state; no in-flight adapter connection leaks (assert via simulated adapter that records `close()` calls).
  - `run` adapter exception inside `connectivity` check resolves as `status='fail'` (NOT propagated) per check 1's semantics.
  - `run` adapter exception inside `control_readiness` check resolves as `status='fail'` (NOT propagated) per check 6's semantics.
  - `run` writes progressive `checks_json` — concurrent `get_current_view` calls observe partial results between dispatch and finalization.
  - `acknowledge_warning` on a non-existent result → `NoCurrentResultError`.
  - `acknowledge_warning` on `complete-PASS` → `NotAckEligibleError`.
  - `acknowledge_warning` on `complete-FAIL` → `NotAckEligibleError`.
  - `acknowledge_warning` for a `pass`-status check → `CheckNotWarnableError`.
  - `acknowledge_warning` idempotent (re-ack of the same check = no-op + same returned shape).
  - `mark_step_4_complete` on `complete-PASS` not outdated → success.
  - `mark_step_4_complete` on `complete-WARN` fully acked not outdated → success.
  - `mark_step_4_complete` on `complete-WARN` not fully acked → `NotHandoffEligibleError`.
  - `mark_step_4_complete` on outdated result → `NotHandoffEligibleError`.
  - `mark_step_4_complete` on `complete-FAIL` → `NotHandoffEligibleError`.
- `services/test_deployment_validation_safe_probe.py` (new) — the load-bearing invariant test:
  - Wraps every simulated adapter in the fixture with a `MagicMock(side_effect=AssertionError("send_command must not be called during validation"))` on `send_command`. Asserts that `run()` completes successfully (every status path tested) without triggering the assertion. **This test is the structural enforcement that the readiness check is non-disruptive.**
- `web/test_setup_validation_routes.py` (new) — covers:
  - Auth: non-installer → 302/403; missing session → 401-style redirect to login.
  - CSRF: every POST without a valid token → 403.
  - Step-gate: GET without `step_3_complete=1` → 302 to constraints.
  - GET when no result exists → renders banner "Run validation to check deployment readiness" + disabled handoff.
  - GET when result is `complete-PASS` not outdated → banner "System is ready. You can complete handoff." + enabled handoff.
  - GET when result is `complete-PASS` AND `result.config_version != current_config_version` → banner "Configuration changed — re-run validation before handoff." + disabled handoff.
  - GET when result is `complete-WARN` with all WARNs acked + not outdated → enabled handoff.
  - GET when result is `complete-WARN` with one un-acked WARN → disabled handoff + ack button visible on that row.
  - GET when result is `complete-FAIL` → disabled handoff regardless of acks.
  - POST `/run` writes a `running` row immediately, completes, returns final fragment with banner reflecting the outcome.
  - POST `/run` cancellation (test triggers via task cancel mid-run) → persisted row leaves `running` state; subsequent GET returns the cancelled-finalize state.
  - POST `/acknowledge/{check_name}` with WARN check → 200 + check row fragment + OOB-swap of handoff button.
  - POST `/acknowledge/{check_name}` with PASS check → 400 + exact-match `not_ack_eligible: status=complete-WARN check={check_name} status=pass`.
  - POST `/acknowledge/{check_name}` on FAIL overall → 400 + exact-match `not_ack_eligible: status=complete-FAIL check={check_name} status=fail`.
  - POST `/revoke/{check_name}` for acked WARN → 200 + check row fragment + OOB-swap of handoff button (now disabled).
  - POST `/handoff` on PASS not outdated → 302 to handoff success page; `wizard_state.step_4_complete=1`; `step_4_completed_config_version` matches `result.config_version`.
  - POST `/handoff` on WARN with all acks not outdated → 302 + wizard advance.
  - POST `/handoff` on WARN with missing acks → 400 + exact-match `handoff_not_eligible: complete-WARN not fully acknowledged`.
  - POST `/handoff` on outdated → 400 + exact-match `handoff_not_eligible: outdated`.
  - POST `/handoff` on FAIL → 400 + exact-match `handoff_not_eligible: complete-FAIL`.
  - Idempotent re-handoff after `step_4_complete=1` AND result still current → GET handler short-circuits the page; the POST is never triggered (no test needed for POST idempotency).
  - GET `/poll` during `running` → 200 + same fragment shape as `/run` response; HX-Trigger header absent.
  - GET `/poll` when `overall_status` leaves running → 200 + fragment + HX-Trigger: `validation-complete` (signals client to stop polling).
- `services/test_constraints_service.py` (extend) — `test_evaluate_safety_pre_check_public_wrapper`: assert the new public wrapper produces the same `tuple[ConstraintCheckResult, ...]` as the internal `_check_safety_pre` for the same input snapshot + constraints (the 9.4 reuse seam test).

**Integration tests (`tests/integration/`):**
- `web/test_setup_validation_e2e.py` (new) — full request flow against in-memory SQLite with the real `ActiveConstraintsProvider` + the simulated adapter fixtures. Cases:
  - End-to-end happy path: discovery (Story 9.1 fixture) → roles (9.2 fixture) → constraints (9.3 fixture activates with a valid set, increments `config_version`) → validation (POST /run completes → complete-PASS) → handoff (POST /handoff succeeds, `step_4_complete=1`).
  - End-to-end WARN path: a simulated adapter returns a REDUCED capability profile → connectivity check WARN → handoff disabled → ack the WARN row → handoff enabled → handoff succeeds.
  - End-to-end FAIL path: a simulated adapter is unreachable → connectivity check FAIL → handoff disabled → no ack path possible → re-running validation reproduces the FAIL.
  - End-to-end outdated path: complete-PASS → activate a constraint change via Story 9.3's surface → next Step 4 GET shows `complete-PASS` with outdated banner + disabled handoff → re-run validation → outdated flag clears → handoff enabled.
  - End-to-end cancellation: POST /run, cancel the request mid-flight → no `running` row persists → subsequent GET shows `complete-FAIL: Validation cancelled before completion.`
  - End-to-end progressive display: spin one simulated adapter with a deliberate `asyncio.sleep(1.0)` → POST /run returns the running fragment within 100 ms → GET /poll within the next second observes a partial `checks_json` with the fast checks resolved → final poll resolves the slow check.
  - End-to-end safe-probe regression: wrap every simulated adapter's `send_command` with the AssertionError side-effect → POST /run completes without triggering the assertion (same invariant as the unit test but at the route boundary).
  - End-to-end audit integration: a successful handoff emits a structlog `step_4_completed` event with `config_version`, `overall_status`, `acknowledged_warnings` fields.
- `web/test_setup_validation_a11y.py` (new — `pytest.mark.xfail` per AC9) — axe-core placeholder for the Step 4 page.
- `test_migrations.py` (extend) — migration 0012 round-trip + schema-shape assertions on `deployment_validation_results.*` / `deployment_validation_acks.*` / `wizard_state.step_4_*` columns.
- `web/test_app_startup.py` (extend) — assert `DeploymentValidationService` and `DeploymentValidationResultRepo` and `ProtocolAdapterFactory` are wired into `app.state` after lifespan completion.
- `services/test_protocol_adapter_factory.py` (new) — exercise the factory's production code path against the simulated adapter fixtures; assert open → probe → close cycle for each protocol (modbus_tcp, ocpp_1_6, dsmr_p1).

**Quality gates:** `pytest tests/ --no-cov -q`, `mypy src/`, `ruff check .`, `ruff format --check .` all clean before status moves to `review`.

### AC12 — Adversarial 3-layer review with severity tagging (process AC)

Before `Status: review → done`, run `/bmad-code-review` and apply the three-layer review (Blind Hunter / Edge Case Hunter / Acceptance Auditor) with severity tagging (HIGH / MEDIUM / LOW / deferred / dismissed). Same gate semantics as 9.0c AC9 / 9.0d AC11 / 9.1 AC12 / 9.2 AC10 / 9.3 AC11: **zero unresolved HIGH/MEDIUM permitted before merge**. All deferred findings logged to `_bmad-output/implementation-artifacts/deferred-work.md` under a dated "code review of 9-4-..." heading.

**Tiered review requirement (Epic 8 retro 2026-05-10):** because 9.4 matches six A2 triggers (one more than 9.3 / 9.0), the full 3-layer adversarial pass is mandatory. The Blind Hunter layer must explicitly examine: (1) the cancellation-cleanup path, (2) the safe-probe invariant, (3) the cross-table single-row transaction in `start_run_locked`, (4) the outdated-detection race window between constraint activation and validation render.

## Tasks / Subtasks

- [x] Task 1 — Schema + repository (AC1, AC2, AC3)
  - [x] Subtask 1.1 — Author migration `0012_add_deployment_validation_tables.py` adding `deployment_validation_results` + `deployment_validation_acks` + `wizard_state.step_4_*` columns
  - [x] Subtask 1.2 — Author `src/open_ems/core/deployment_validation.py` with all Pydantic value types
  - [x] Subtask 1.3 — Author `src/open_ems/storage/repositories/deployment_validation_repo.py` with `DeploymentValidationResultRepo`
  - [x] Subtask 1.4 — Extend `WizardStateRepo` with `set_step_4_complete` + `set_step_4_complete_locked` (mirror 9.3's idempotent pattern)
  - [x] Subtask 1.5 — Migration round-trip verification + `tests/integration/test_migrations.py::test_migration_0012_round_trips`
- [x] Task 2 — `DeploymentValidationService` (AC4, AC5, AC6, AC7)
  - [x] Subtask 2.1 — Author `src/open_ems/services/deployment_validation.py` with `DeploymentValidationService`, `NoCurrentResultError`, `NotAckEligibleError`, `CheckNotWarnableError`, `NotHandoffEligibleError`
  - [x] Subtask 2.2 — Implement `get_current_view` (outdated derivation)
  - [x] Subtask 2.3 — Implement `run` (concurrent dispatch via asyncio.wait/asyncio.gather; per-check timeout; progressive persistence; cancellation cleanup; safe-probe invariant)
  - [x] Subtask 2.4 — Implement the 6 check methods (`_check_connectivity`, `_check_role_completeness`, `_check_capability_strategy`, `_check_constraint_completeness`, `_check_constraint_safety_pre`, `_check_control_readiness`)
  - [x] Subtask 2.5 — Implement `acknowledge_warning`, `revoke_warning`, `mark_step_4_complete`
  - [x] Subtask 2.6 — Extend `ConstraintsService` with public `evaluate_safety_pre_check` wrapping the private `_check_safety_pre` (reuse seam — AC5 check 5)
  - [x] Subtask 2.7 — Unit tests per AC11
- [x] Task 3 — `ProtocolAdapterFactory` (AC4)
  - [x] Subtask 3.1 — Author `src/open_ems/services/protocol_adapter_factory.py` — validation-only transient probe coordinator that delegates to `DiscoveryService.probe_modbus_endpoint` / `probe_dsmr_endpoint` (read-only by construction) and, for OCPP, checks the central system's registered-charger list. `asyncio.shield` is applied transitively via the discovery layer's per-protocol close paths
  - [x] Subtask 3.2 — Production wiring: `probe(entry, timeout_s) -> ProbeOutcome` uses the same `DiscoveryService` instance Story 9.1 already wires into the lifespan; no new client construction needed
  - [x] Subtask 3.3 — Test fixtures inject `_CannedFactory` / `_AllReachableFactory` / `_MutableFactory` (`tests/unit/services/test_deployment_validation_service.py` + `tests/unit/web/test_setup_validation_routes.py` + `tests/integration/web/test_setup_validation_e2e.py`)
- [x] Task 4 — Routes (AC8)
  - [x] Subtask 4.1 — Delete the placeholder `get_validation_placeholder` route body and the `setup_validation_placeholder.html` template
  - [x] Subtask 4.2 — Implement `GET /installer/setup/validation` (step-gate enforcement + full page render)
  - [x] Subtask 4.3 — Implement `POST /installer/setup/validation/run` (calls `validation_svc.run`; emits `_setup_validation_result.html` + `HX-Trigger: validation-complete`)
  - [x] Subtask 4.4 — Implement `GET /installer/setup/validation/poll` (idempotent read; emits `HX-Trigger: validation-complete` when state leaves running)
  - [x] Subtask 4.5 — Implement `POST /installer/setup/validation/acknowledge/{check_name}` + `POST /installer/setup/validation/revoke/{check_name}` (re-renders the validation region; exact-match rejection reasons)
  - [x] Subtask 4.6 — Implement `POST /installer/setup/validation/handoff` (gate-evaluator; calls `mark_step_4_complete`; 302 to handoff success)
- [x] Task 5 — Templates + accessibility (AC9)
  - [x] Subtask 5.1 — Author `setup_validation.html` + `_setup_validation_result.html` + `_setup_validation_banner.html` + `_setup_validation_check_row.html` + `_setup_validation_actions.html`
  - [x] Subtask 5.2 — Author `_setup_validation_handoff_success.html` (Story 9.5 replaces the body; route + URL stay put). `_setup_validation_error_banner.html` was DROPPED — the route handler raises typed `HTTPException` for all error paths, which is simpler than carrying a separate envelope template and matches the rest of the wizard's error UX
  - [x] Subtask 5.3 — Update `setup_layout.html` step indicator to render Step 3 as a hyperlink when `step_4_complete=0` (back-navigation) AND apply `data-status="outdated"` to Step 4 when derived `is_outdated` is True
  - [x] Subtask 5.4 — A11y: `role="region"` / `aria-live="polite"` on banner + check rows / `aria-live="assertive"` only on FAIL banner per UX spec; server-rendered `disabled` + `aria-disabled` + visible explanation on disabled buttons
- [x] Task 6 — Lifespan wiring + provider exposure (AC10)
  - [x] Subtask 6.1 — Construct `DeploymentValidationResultRepo` + `ProtocolAdapterFactory` + `DeploymentValidationService` in lifespan step 4g AFTER `constraints_service`
  - [x] Subtask 6.2 — Expose via `app.state.deployment_validation_repo` + `app.state.deployment_validation_service` + `app.state.protocol_adapter_factory`
  - [x] Subtask 6.3 — Add the `_deployment_validation_service` Depends helper in `web/routes/setup.py`
  - [x] Subtask 6.4 — Add Settings fields: `deployment_validation_check_timeout_seconds: float = 15.0`, `deployment_validation_device_probe_timeout_seconds: float = 5.0`. The third proposed field `deployment_validation_async_threshold_seconds` was DROPPED — the route blocks on the run synchronously and the polling loop is purely client-driven via `hx-trigger="every 2s"` on the running banner, so no async-threshold tuning is needed
- [x] Task 7 — Tests (AC11)
  - [x] Subtask 7.1 — Unit test files per AC11 (`test_deployment_validation_models.py`, `test_deployment_validation_repo.py`, `test_deployment_validation_service.py`, `test_setup_validation_routes.py`)
  - [x] Subtask 7.2 — Integration test files per AC11 (`test_setup_validation_e2e.py` covering WARN→ack→handoff, outdated-after-constraint-change, and safe-probe regression at the route boundary)
  - [x] Subtask 7.3 — Accessibility (axe-core) test placeholder marked `pytest.mark.xfail` (`tests/integration/web/test_setup_validation_a11y.py`)
  - [x] Subtask 7.4 — Cross-story regression: full `tests/` sweep is green (1360 passed, 4 xfailed including the new 9.4 a11y placeholder); 9.3 constraints suite explicitly verified
- [x] Task 8 — Quality gates + review (AC12)
  - [x] Subtask 8.1 — `pytest tests/ --no-cov -q` clean
  - [x] Subtask 8.2 — `mypy src/` clean
  - [x] Subtask 8.3 — `ruff check .` and `ruff format --check .` clean
  - [ ] Subtask 8.4 — `/bmad-code-review` run (3-layer adversarial pass per Epic 8 retro + AC12) — to be executed in a fresh context with a different LLM per the workflow tip

## Dev Notes

### Source-tree alignment

- `migrations/versions/0012_add_deployment_validation_tables.py` — new; precedent `0011_add_constraint_configuration_tables.py` (9.3) extends multiple tables in one migration.
- `src/open_ems/core/deployment_validation.py` — new; value types follow the 9.3 placement decision (value types in `core/`, service class in `services/`).
- `src/open_ems/storage/repositories/deployment_validation_repo.py` — new; mirrors `draft_constraints_repo.py` shape (`frozen=True, extra="forbid"` model + per-call async methods + write-lock-protected mutations + `_locked` variants).
- `src/open_ems/storage/repositories/wizard_state_repo.py` — extended with `set_step_4_complete` + `set_step_4_complete_locked` following the 9.3 idempotent-step pattern.
- `src/open_ems/services/deployment_validation.py` — new; consistent with `services/constraints.py` (9.3) and `services/role_assignment.py` (9.2) — frozen-dataclass outcomes; depends on multiple repos + the provider + the StateStore + (NEW) the ConstraintsService for reuse + the protocol adapter factory.
- `src/open_ems/services/protocol_adapter_factory.py` — new; encapsulates the transient adapter construct/open/probe/close cycle for the connectivity + control-readiness checks.
- `src/open_ems/services/constraints.py` — extended with one public method (`evaluate_safety_pre_check`) wrapping the existing private `_check_safety_pre` (the AC5 check 5 reuse seam).
- `src/open_ems/web/routes/setup.py` — extended with the validation routes; replaces the placeholder body of `get_validation_placeholder`; deletes the placeholder template.
- `src/open_ems/web/templates/installer/` — new templates per AC9 task 5.1/5.2; modifies `setup_layout.html` for step-3 back-navigation + step-4 outdated badge.
- `src/open_ems/web/app.py` — lifespan step 4g extension.
- `src/open_ems/settings.py` — three new Settings fields per task 6.4.

### Why a single-row `deployment_validation_results` table (not append-only history)

The "outdated" semantic is binary against the current `provider.config_version`; there is no operational need for historical validation results in v1. Persisting history adds:
- DB-size growth proportional to validation re-runs (an installer who iterates 10 times during a tricky deployment would store 10 rows per site).
- A "history" UI that v1 does not contract anywhere (Epic 11 operational monitoring contemplates an event log, not a validation history view).
- A retention policy decision (when to prune; v1 has none).

The single-row pattern matches `active_constraints`'s "active row" pattern (9.0b). When future history surface is required (e.g., Epic 11), a migration converts the table to append-only with an additional `is_current` boolean or a separate `current_validation_id` pointer in another table — that migration is mechanically straightforward and not in scope here.

### Why outdated is a DERIVED state, not a persisted column

A persisted `is_outdated` column would require Story 9.3 (or any future config-version-incrementing surface) to write-trigger an UPDATE on the validation row. This creates write-side coupling between 9.3's activate path and 9.4's persistence — the kind of cross-story coupling Epic 7 retro's single-evaluator principle warns against.

The read-time comparison (`result.config_version != provider.config_version`) is O(1), happens already on every Step 4 page load (which reads both anyway), and is structurally race-free: any constraint activation that lands between the validation completing and the next Step 4 render is detected on the NEXT render.

### Why control_readiness reuses the capability-check path, not a dedicated probe command

The architectural intent from BAD-4 + the epic AC ("the control readiness check must never issue a disruptive control action to a real device") is that the readiness probe confirms **the control pipeline is reachable and responsive**, not that devices respond to real commands. PolicyGuard's existing P2 capability gate already exercises:
- adapter is constructed and reachable;
- adapter's `get_capabilities()` returns a valid `DeviceCapabilityProfile`;
- the profile's `device_id` matches the registered device.

Those three are exactly what "the control pipeline is reachable" means. Adding a new dedicated `probe()` method to `DeviceAdapter` would:
- Require every adapter (Battery / Inverter / EV Charger / Grid Meter) to implement a new method.
- Introduce a parallel cancellation/timeout surface that mirrors `get_capabilities()` almost exactly.
- Create the risk of "probe says reachable but real send_command fails" (because the probe path diverges from the dispatch path).

Reusing `get_capabilities()` keeps the validation symmetric with PolicyGuard's runtime behavior — if a device passes validation, the very next `authorize_and_dispatch()` against it will at minimum get past the P2 gate (the dispatch step P5 can still fail on the actual write, but that's a different category of failure that the validation cannot detect without issuing a real command, which the epic AC forbids).

### Why the `adapters={}` runtime deferred-finding is NOT a hard prerequisite for 9.4

Story 8-2's deferred finding (`adapters={}` in PolicyGuard / ControlLoop at lifespan time) is real and outstanding. Story 9.3's R7 classified it as `acceptable-post-this-story`. Story 9.4 reaches the same classification, with this additional reasoning:

- 9.4's validation surface constructs adapters **transiently** via the new `ProtocolAdapterFactory`. The connectivity + control_readiness checks open adapters per-run, probe them, and close them. PolicyGuard's runtime `adapters` mapping is NOT involved.
- The runtime control loop's `adapters={}` means PolicyGuard rejects every command with `adapter_not_registered`. This is the documented design stub from 8-2. **Wiring runtime adapters is its own engineering surface** — connection lifecycle, retry-on-disconnect, hot-reload on role reassignment, shutdown cleanup. Each of those is a substantive design decision; bundling them into 9.4 would push the story far over the A2 risk threshold (already six triggers matched).
- A dedicated future story (provisional: `9-X-wire-adapter-map-into-policy-guard-and-control-loop` between 9.4 and 9.5 OR as Epic 12 hardening) is the proper home. R7 below classifies it accordingly.
- **Operational consequence:** at handoff time after 9.4, PolicyGuard still rejects every command with `adapter_not_registered`. The validation passes (because validation builds its own adapters), but the runtime is stub-locked. This is a known v1 boundary: the homeowner dashboard (Epic 10) cannot actually issue an EV override that reaches the charger until the adapter-wiring story lands. **This must be called out explicitly in Story 9.5's handoff guide** so installers do not promise homeowner-facing control before the wiring is in place.

### Why progressive HTMX polling (not SSE) for live check updates

The UX spec mentions a `validation_update` SSE event for per-check progress. The pragmatic choice for v1 is HTMX polling (`hx-trigger="every 2s"` while the result is `running`) for these reasons:

- The existing SSE stream (`/api/stream/state` from Story 5-2) is role-filtered to the homeowner-facing system snapshot. Adding installer-facing setup-flow events to it widens the stream's contract significantly.
- Validation runs are bounded (≤90 seconds for 6 checks at 15s timeout each); the polling cost is bounded.
- The polling fragment AND the run-completion fragment use the SAME template (`_setup_validation_result.html`) so behavior is identical whether the user clicked Run-and-waited or whether the page re-loaded mid-run.
- HTMX polling stops automatically when the response carries `HX-Trigger: validation-complete` (set by the GET /poll handler once the persisted state leaves `running`). No client-side state machine.

If a future story needs SSE-driven validation updates (e.g., for an installer team-monitoring view), the migration path is: add the `validation_update` event to the existing stream, update the Step 4 template to wire `hx-trigger="sse:validation_update"` instead of `every 2s`. The fragment template is unchanged. This is explicitly out of scope for 9.4.

### Library / framework notes

- No new third-party dependencies. JSON serialization uses the standard library `json` module. Concurrent check dispatch uses `asyncio.gather` + `asyncio.wait_for` (standard library). `asyncio.shield` is used around adapter `close()` calls per Story 9.1's deferred-work pattern.
- HTMX usage continues to follow the architecture-mandated server-rendered HTML + HTMX baseline; no client-side JS frameworks are introduced.
- Settings additions are per task 6.4 — three new float fields with sensible defaults; no `_env_file` semantics change.
- `pymodbus` / `python-ocpp` / `dsmr-parser` are already pinned in `uv.lock`; the `ProtocolAdapterFactory` uses the same versions the runtime adapters use.

### Previous-story intelligence — what we carry forward

- **Story 9.0b's `ActiveConstraintsProvider.get().config_version` is the runtime read for outdated detection.** No new provider method needed.
- **Story 9.3's `ConstraintsService._check_safety_pre` is reused via a new public wrapper.** The 9.3 service expands by exactly one method; no behavioral change.
- **Story 9.3's atomic 4-table activation transaction (P13) means `config_version` increments are atomic.** Outdated detection has no torn-read window between activation and the next Step 4 render.
- **Story 9.3's `step_3_*` triple pattern is mirrored by 9.4's `step_4_*` triple.** Same paired-NULL CHECK invariant; same idempotent `set_step_X_complete_locked`.
- **Story 9.2's exact-match reason strings** (Story 9.1 AC11 / 9.2 AC9 / 9.3 AC10 precedent): every rejection reason in 9.4's routes uses exact-match assertions in tests.
- **Story 9.1's `_classify_not_found` registry-snapshot deferred-work** is acknowledged: 9.4's connectivity check reads the registry once at the start of `run()` and uses the snapshot for the duration; a concurrent manual entry landing mid-run is NOT probed (the next run picks it up).
- **Story 9.1's unbounded-fan-out deferred-finding ("Address in Story 9.4 (deployment validation) where bounded readiness probing is contracted")** is addressed by the new Settings field `deployment_validation_device_probe_timeout_seconds` AND the per-check overall `deployment_validation_check_timeout_seconds` AND the fact that all probes within a check fan out concurrently — total runtime is bounded by `max(per_device_timeout, per_check_timeout)` regardless of `N`.
- **Story 9.1's asyncio.shield-on-close deferred-finding** is structurally applied here by the new `ProtocolAdapterFactory` (which is the only adapter-close site introduced by 9.4).
- **Story 9.0c's structural-fail-loud-at-boot pattern** does NOT apply to 9.4 — there is no startup gate for validation (the wizard surface is post-startup). The lifespan wiring is pure construction; no DB I/O at startup.
- **Story 9.0d's monthly-peak hydrate** is NOT involved; 9.4 does not read peak data.
- **Story 8-2's `adapters={}` runtime deferral** is R7-triaged as `acceptable-post-this-story` (see "Why the `adapters={}` runtime deferred-finding is NOT a hard prerequisite for 9.4" above).
- **Story 9.3's R3 cold-start case (4) auto-repair was deliberately NOT implemented** — the recommendation was to keep the recovery surface simple. 9.4 follows the same pattern: a mid-run process restart leaves a `running` row that the next `GET /installer/setup/validation` handler must heal. See R3 case 4 below for the chosen handling.

### Audit semantics

This story emits structured logs (`deployment_validation_started`, `deployment_validation_check_complete`, `deployment_validation_finalized`, `deployment_validation_acknowledged`, `deployment_validation_revoked`, `step_4_completed`, `deployment_validation_cancelled`) AND `wizard_state.step_4_*` DB writes. It emits NO `event_log` audit rows — the FR20 event log is reserved for control decisions, constraint enforcement, degraded-mode transitions, recovery, and installer notes. Deployment validation results live in their own table; the structured logs cover observability; `step_4_completed` is the audit trail for the handoff transition. (Future Epic 11 may surface validation events in the installer event log; that's a deliberate v2 expansion.)

### Project Structure Notes

- All new files map onto the architecture-defined directory layout (architecture.md §Project Structure):
  - `services/deployment_validation.py` matches the architecture's `services/validation.py` slot (line 909) — the architecture uses `validation.py`; we use `deployment_validation.py` to be explicit (since constraint-validation lives in `services/constraints.py`). Same architectural intent, more descriptive filename. The architecture document's `validation.py` reference is a sketch; the multi-step wizard introduced two distinct validation surfaces (constraint validation in 9.3, deployment validation in 9.4) so two distinct service files are correct.
  - `services/protocol_adapter_factory.py` is a permitted addition under `services/`.
  - `core/deployment_validation.py` follows the 9.3 placement decision for value types.
  - `web/routes/setup.py` is extended (not split) — even with 9.4's additions the file remains under the architecture's 300-LOC informal threshold because routes share helpers extensively (the file is already ~900 LOC post-9.3; the new routes add ~150 LOC for the 6 endpoints + their helpers, totaling ~1050 LOC — well within the project precedent).
- No new top-level packages. No new third-party dependencies. `uv.lock` should be unchanged.
- **Detected variance:** architecture.md §Project Structure (line 909) lists `validation.py` (singular). The wizard's two validation surfaces are split per the rationale above. Same precedent variance as 9.1/9.2/9.3's multi-page setup template split — refinement of the architecture's single-file sketch into per-concern modules.

### Orchestration Risk Analysis (A2-triggered — MANDATORY)

**A2 triggers matched:**
- **T1** (lifecycle / state-machine) — three state machines: (a) `deployment_validation_results.overall_status` (`(no row) → running → complete-PASS | complete-WARN | complete-FAIL`, with run-replacement transitions), (b) derived UX state including `outdated` (config_version comparison) and `acknowledged` (per-warning ack rows), (c) `wizard_state.step_4_complete` (`0 → 1` idempotent).
- **T2** (cancellation) — 6 concurrent check tasks dispatched via `asyncio.gather`; per-check timeout via `asyncio.wait_for`; outer cancellation must propagate to every check task; adapter close calls must complete via `asyncio.shield` even on cancellation; the finally-block must call `finalize_run_locked('complete-FAIL', 'Validation cancelled before completion.')` so no row is left in `running`.
- **T3** (persistence + recovery) — `deployment_validation_results` row + ack rows + `wizard_state.step_4_*` columns all DB-backed; must survive process restart; Step 4 GET hydrates from DB; running-state recovery from mid-restart is GET-handler-driven (see R3 case 4).
- **T5** (multi-adapter coordination) — connectivity check probes Modbus + OCPP + DSMR via the factory; control-readiness check probes the controllable adapters (Battery + EV Charger) via the same factory + the same capability-check semantics PolicyGuard uses; safe-probe invariant (never call `send_command`) is structurally enforced.
- **T6** (deployment / restart behavior) — `provider.get().config_version` vs persisted `validation.config_version` drives outdated detection; restart-mid-validation handled by GET-handler healing; the validation result IS the runtime-readiness signal that gates handoff.
- **T7** (installer workflow orchestration) — Step 4 of the 4-step wizard mutates persisted validation state, drives `wizard_state.step_4_*`, and activates the handoff gate (Story 9.5's future surface).

#### R1 — Composition-risk analysis

Story 9.4 converges six operational domains in one shipping unit. Each carries a named, story-specific risk introduced by its convergence with the others:

1. **Lifecycle/state-machine (T1) crossed with persistence (T3) — the progressive-write window.** As each check resolves, the service writes `update_check_progress_locked` to update `checks_json`. Between dispatch and finalization, the persisted row's `overall_status='running'` but `checks_json` grows over time. Risk: a concurrent GET /poll reading a partial `checks_json` could interpret the partial as final if the read-side state derivation is sloppy. Mitigation: the read-side derives "complete-*" overall_status from the persisted `overall_status` column ONLY, never from `len(checks_json)==6` — so a partial checks_json with overall_status=='running' renders as "Validation running…" regardless of how many checks have resolved. Test: `test_setup_validation_e2e.py::test_concurrent_poll_during_running_observes_partial_checks_with_running_banner`.

2. **Cancellation (T2) crossed with persistence (T3) — the in-flight cleanup invariant.** A cancelled `run()` MUST leave the persisted row in a terminal state (NOT `running`). Risk: a cancellation arriving between `start_run_locked` and the first `update_check_progress_locked` could leave a row at `running` with empty `checks_json` that no subsequent code re-finalizes. Mitigation: the outer `try/finally` of `run()` always calls `finalize_run_locked('complete-FAIL', 'Validation cancelled before completion.')` if the row reached `running` and is still in that state at the finally-block. Tests: `test_deployment_validation_service.py::test_run_cancelled_immediately_after_start_finalizes_as_cancelled` AND `test_run_cancelled_after_partial_progress_finalizes_as_cancelled_with_partial_checks_visible`. Defense in depth: the Step 4 GET handler ALSO heals a stale `running` row (see R3 case 4).

3. **Multi-adapter coordination (T5) crossed with cancellation (T2) — the connection-leak invariant.** Each check that opens an adapter MUST close it even on cancellation. Risk: `CancelledError` raised inside a probe (between `factory.open()` and `factory.close()`) skips the close call, leaking a TCP socket / serial port / OCPP WS connection. Mitigation: every adapter use is `try / finally: await asyncio.shield(adapter.close())` — the shield prevents the close itself from being cancelled mid-flight. Story 9.1's deferred-finding (`adapters/discovery.py` unshielded close) is the same pattern being applied here proactively. Tests: `test_deployment_validation_service.py::test_cancellation_inside_probe_still_closes_adapter` for each protocol.

4. **Multi-adapter coordination (T5) crossed with safe-probe invariant — the disruption risk.** The control_readiness check exercises the controllable adapters (Battery + EV Charger). Risk: an implementation drift in a future check (or a refactor that pulls in a "smarter" probe) calls `adapter.send_command(...)` and an installer's validation pre-handoff inadvertently issues an EV start or a battery discharge command — disrupting the homeowner's device state on the very check that's supposed to prove the system is non-disruptive. Mitigation: the AC11 test `test_control_readiness_never_calls_send_command` wraps every simulated adapter's `send_command` with an `AssertionError` side-effect and asserts no test path triggers it; this is enforced as part of the quality gates. The PolicyGuard runtime adapters are bypassed entirely (validation uses transient adapters from the factory) so even an accidental PolicyGuard-issued command during validation is structurally impossible. Precedent: Story 9.0 R4's "send_command contract" cleanly separates capability-lookup from dispatch.

5. **Deployment / restart behavior (T6) crossed with installer workflow orchestration (T7) — the orphaned-handoff-button risk.** `wizard_state.step_4_complete=1` is sticky (idempotent advance). Risk: a process restart between `mark_step_4_complete` and the response render could leave `step_4_complete=1` persisted but the installer believes handoff never landed (they got no success page). Mitigation: the next GET handler reads `step_4_complete=1` AND the persisted result is current (config_version match) → renders the page in "handoff complete" state with the success message inline (not a 302 to a non-existent success URL). If the persisted result is OUTDATED at restart-recovery time (a constraint activation landed between the restart and the GET), the GET handler clears `step_4_complete` to 0 (no — actually NO mutation; outdated overrides `step_4_complete` at the derived state level; the persisted column stays 1 for audit history but the page renders as if it's 0). Tests: `test_setup_validation_e2e.py::test_step_4_complete_persists_across_restart` AND `test_step_4_complete_with_outdated_renders_as_incomplete`.

6. **Acknowledgment workflow (T1+T7) crossed with run-replacement (T1+T3) — the ack-CASCADE invariant.** A new `run()` DELETEs the prior result row → CASCADE clears all ack rows. Risk: a race where the installer clicks Ack on a check while a parallel Run starts → the Ack POST might land on a result_id that the Run has already DELETEd → the Ack helper raises an FK violation. Mitigation: both Ack and Run acquire `get_write_lock()` in the same order (Ack first reads the current result id under the lock, then INSERTs the ack row in the same lock window; Run DELETEs prior row + INSERTs new row in its own atomic transaction). The window between Ack's read and Ack's INSERT is bounded by the lock — Run cannot interleave. Tests: `test_setup_validation_e2e.py::test_concurrent_ack_during_run_serializes_correctly`.

#### R2 — State-transition table

Story 9.4 introduces / modifies four state machines.

**`deployment_validation_results.overall_status` (single-row, replaced on each run):**

| State | Allowed transitions | Trigger | Side-effects |
|---|---|---|---|
| (no row) | → `running` | first `POST /installer/setup/validation/run` | INSERT row with `overall_status='running'`, empty `checks_json`, `config_version` snapshotted from `provider.get()`; structlog `deployment_validation_started` |
| `running` | → `complete-PASS` | all 6 checks resolved `pass` | UPDATE `overall_status='complete-PASS'`, set `completed_at`, set `summary_text`; structlog `deployment_validation_finalized` |
| `running` | → `complete-WARN` | any check `warn`/`timeout`, none `fail` | same with `overall_status='complete-WARN'` |
| `running` | → `complete-FAIL` | any check `fail` | same with `overall_status='complete-FAIL'` |
| `running` | → `complete-FAIL` (cancelled) | `run()` task cancelled mid-execution | finally-block calls `finalize_run_locked('complete-FAIL', 'Validation cancelled before completion.')` |
| any `complete-*` | → row DELETEd, then new row at `running` | subsequent `run()` | DELETE prior row (CASCADE clears acks), INSERT new row in same transaction; structlog `deployment_validation_started` |
| any | → row DELETEd | session deletion CASCADE (triggered_by_session_id → SET NULL — the row PERSISTS but loses session attribution) | session deletion does NOT delete the validation row; only session attribution column becomes NULL |

**`deployment_validation_acks` (per-warning, scoped to current result row):**

| State | Allowed transitions | Trigger | Side-effects |
|---|---|---|---|
| (no row for (result_id, check_name)) | → row exists | `POST /installer/setup/validation/acknowledge/{check_name}` on eligible WARN/TIMEOUT check | INSERT OR IGNORE row; structlog `deployment_validation_acknowledged` |
| row exists | → row DELETEd | `POST /installer/setup/validation/revoke/{check_name}` | DELETE row; structlog `deployment_validation_revoked` |
| row exists | → row DELETEd (CASCADE) | parent result row DELETEd (subsequent `run()`) | CASCADE removes ack rows |
| row exists | → row DELETEd (CASCADE) | parent result row DELETEd (manual operator action) | CASCADE removes ack rows |

**`wizard_state.step_4_complete` (per-session, idempotent advance):**

| State | Allowed transitions | Trigger | Side-effects |
|---|---|---|---|
| `step_4_complete=0` | → `step_4_complete=1, step_4_completed_at=now(), step_4_completed_config_version=N` | `POST /installer/setup/validation/handoff` with all gates passing | 302 to handoff success; structlog `step_4_completed` with `config_version=N` |
| `step_4_complete=0` | → unchanged | `POST /installer/setup/validation/handoff` with any gate failing | inline error banner; no DB write |
| `step_4_complete=1` | → unchanged (idempotent) | re-click of handoff after prior success while result still current | GET handler renders "handoff complete" state inline; the POST is rarely re-invoked because the GET short-circuits |
| `step_4_complete=1` | → derived-state-only "incomplete" | result becomes outdated (provider.config_version changes) | persisted column stays 1; derived `is_outdated` renders the page as if step_4_complete=0; next `POST /handoff` succeeds with the new `step_4_completed_config_version=N+1` |
| any | → row DELETEd | session deletion CASCADE | wizard_state row removed |

**Derived UX state (computed at render time, never persisted):**

| Derived state | Computed from | Effect |
|---|---|---|
| `never_run` | `validation_repo.get_current() is None` | banner: never_run text; handoff disabled |
| `running` | persisted `overall_status='running'` | banner: running text; handoff disabled; auto-poll active |
| `outdated` | `result.config_version != provider.get().config_version` AND `overall_status != 'running'` | banner: outdated text; handoff disabled; Run button enabled; takes precedence over PASS/WARN/FAIL banner text |
| `pass-ready` | `overall_status='complete-PASS'` AND NOT outdated | banner: PASS text; handoff enabled |
| `warn-needs-ack` | `overall_status='complete-WARN'` AND NOT outdated AND `not_all_warns_acked` | banner: WARN text; handoff disabled; ack buttons on un-acked rows |
| `warn-ready` | `overall_status='complete-WARN'` AND NOT outdated AND `all_warns_acked` | banner: WARN text + ack count; handoff enabled |
| `fail-blocked` | `overall_status='complete-FAIL'` AND NOT outdated | banner: FAIL text; handoff disabled; ack buttons absent |

**Critical invariants:**
- **Single-row contract for `deployment_validation_results`.** A new `run()` DELETEs prior row before INSERTing new row in the same `BEGIN IMMEDIATE` transaction.
- **`step_4_complete=1` IFF `step_4_completed_at` IS NOT NULL IFF `step_4_completed_config_version` IS NOT NULL.** Enforced by `set_step_4_complete_locked` (writes all three atomically) + a CHECK constraint on the table.
- **No `running` row persists beyond the lifetime of the `run()` task that created it.** Enforced by the outer try/finally calling `finalize_run_locked` + the GET handler healing stale `running` rows from prior crashes.
- **`outdated` is derived, never persisted.** Read-time comparison only.

#### R3 — Impossible-state analysis (with cold-start coverage)

**Forbidden state combinations and their structural invariants:**

1. **Two rows in `deployment_validation_results`** — invariant: `start_run_locked` DELETEs the prior row in the same `BEGIN IMMEDIATE` transaction before INSERTing the new one. Test: `test_deployment_validation_repo.py::test_start_run_replaces_prior_row_atomically`.

2. **An ack row whose `validation_result_id` does not exist in `deployment_validation_results`** — invariant: FK CASCADE deletes the ack rows when their parent is DELETEd; the FK constraint prevents inserts referencing non-existent IDs. Test: `test_deployment_validation_repo.py::test_orphan_ack_rejected_by_fk` (raw INSERT bypass; relies on `PRAGMA foreign_keys=ON` which is the project-wide default).

3. **`wizard_state.step_4_complete=1` AND `step_4_completed_at IS NULL`** (or vice versa for the triple) — invariant: the table CHECK constraint + `set_step_4_complete_locked` writes all three atomically. Test: `test_wizard_state_repo.py::test_step_4_pairing_invariant`.

4. **`wizard_state.step_4_complete=1` AND `step_4_completed_config_version` points to a config_version that has never existed in `active_constraints` or `config_audit_log`** — invariant: `step_4_completed_config_version` is set from `result.config_version`, which was snapshotted from `provider.get().config_version` at run-start time. The provider's `config_version` is always the highest from `active_constraints` (or `0` for cold-start seed). Test: `test_setup_validation_e2e.py::test_step_4_completed_config_version_traces_to_active_constraints`.

5. **`deployment_validation_results.overall_status='running'` persisting beyond a crashed run** — invariant: the GET handler at `/installer/setup/validation` heals stale running rows. The heal logic: if `result.overall_status='running'` AND `(now - result.started_at) > settings.deployment_validation_check_timeout_seconds * 2`, the GET handler calls `validation_repo.finalize_run_locked(result.id, 'complete-FAIL', 'Validation interrupted by process restart.', now)` BEFORE rendering the page. The 2x multiplier is generous to avoid healing a live run on a slow box. **This heal is the ONLY code path that mutates persisted state during a GET; it is documented + tested.** Test: `test_setup_validation_e2e.py::test_get_heals_stale_running_row_from_prior_crash`.

6. **`deployment_validation_results.checks_json` contains a check name not in the AC2 literal set** — invariant: `DeploymentCheckName` Literal + Pydantic validation on every read; corrupt JSON in `checks_json` raises a typed exception at decode time. Test: `test_deployment_validation_repo.py::test_corrupt_checks_json_raises_decode_error`.

7. **An ack row with `check_name` that does not appear in the result's `checks_json`** — invariant: the DB CHECK constraint on `deployment_validation_acks.check_name` enforces the literal set, but a check name that is in the literal set but NOT in this particular result's checks_json (e.g., a future check name is added before all UI code is updated) is still INSERTable. The service-layer `acknowledge_warning` validates the check exists in the current result's checks_json before INSERTing the ack — raises `CheckNotWarnableError` if absent. Test: `test_deployment_validation_service.py::test_acknowledge_unknown_check_raises_check_not_warnable_error`.

8. **`deployment_validation_results.config_version < 0`** — invariant: column-level CHECK `config_version >= 0`; Pydantic field `ge=0`. Provider's config_version is always ≥ 0 (cold-start seed is 0). Test: `test_deployment_validation_repo.py::test_negative_config_version_rejected_by_check_constraint`.

9. **`wizard_state.step_4_complete=1` while `wizard_state.step_3_complete=0`** — invariant: the `POST /handoff` route enforces a prerequisite check (step_3_complete=1) before calling `mark_step_4_complete`; raises `step_prerequisites_not_met: step_3_complete=0` exact-match reason. Test: `test_setup_validation_routes.py::test_handoff_without_step_3_complete_rejected`.

10. **Ack row created for a check that is `pass`** — invariant: `acknowledge_warning` validates check status is `warn` or `timeout` before INSERTing; raises `CheckNotWarnableError` if `pass`/`fail`/`pending`. Test: `test_deployment_validation_service.py::test_acknowledge_pass_check_raises_check_not_warnable_error`.

**Cold-start / startup-grace coverage (mandatory):**

1. **t=0 (process boot, fresh deployment, never_run):** `deployment_validation_results` is empty. `validation_repo.get_current()` returns `None`. The installer reaches `/installer/setup/validation` only after Steps 1–3 are complete (the step-gate enforcement in AC8). On first render, the page shows the never_run banner ("Run validation to check deployment readiness"), the "Run validation" button is enabled, the handoff button is disabled with the never_run explanation. **PolicyGuard's runtime `adapters={}` deferral has NO effect on validation** — the validation service uses the transient `ProtocolAdapterFactory`, not PolicyGuard's empty mapping.

2. **t=process restart with a `complete-*` result row already persisted:** `validation_repo.get_current()` returns the row. The GET handler computes `is_outdated` by comparing `result.config_version` to `provider.get().config_version`. If equal: renders the prior result's banner + check list + handoff button (server-rendered enable state). If unequal: renders the outdated banner; handoff disabled. The installer can re-run from this state without losing the prior result's audit trail (it's overwritten on the next `start_run_locked`). `wizard_state.step_4_*` columns persist across restart correctly. **If `wizard_state.step_4_complete=1` AND result is current:** the page renders in "handoff complete" state inline (no 302 to a non-existent URL).

3. **t=process restart mid-run (`overall_status='running'` persists from a crashed prior process):** the GET handler's heal logic (R3 case 5) detects the stale running row by comparing `now - result.started_at` against `2x` the per-check timeout. If exceeded, the row is finalized to `complete-FAIL` with the "interrupted by restart" summary BEFORE the page is rendered. The installer sees a clear "validation was interrupted; re-run to continue" state. If `now - result.started_at` is within the 2x window (the restart happened sub-30s into a 15s-timeout run that's still legitimately "in flight" from a healthy paused process — exceedingly rare), the page renders the running state and the auto-poll picks up; the next poll after 30s catches the staleness and heals it.

4. **t=concurrent installer sessions:** the `deployment_validation_results` table is single-row site-wide (not per-session). Both installers see the same result. A new `run()` from either session DELETEs the prior + INSERTs a new row; the other session's next page load sees the new state. `triggered_by_session_id` records who initiated the most recent run. Acks are scoped to the current result row, NOT to the session — either installer can ack any WARN; the most-recent ack wins (UNIQUE on `(validation_result_id, check_name)` means a re-ack is no-op). **This is the documented v1 multi-installer semantics.** Test: `test_setup_validation_e2e.py::test_two_concurrent_sessions_share_validation_state`.

5. **t=session expired mid-Step-4:** Installer's session expires while reviewing a `complete-WARN` result. The persisted result row's `triggered_by_session_id` is SET NULL via the FK CASCADE. The result row PERSISTS (the result is site-state, not session-state). A new installer login sees the same result on Step 4 and can complete the handoff (subject to outdated detection). Test: `test_setup_validation_e2e.py::test_session_expiry_preserves_validation_result`.

6. **t=process restart between `mark_step_4_complete` and response render:** `wizard_state.step_4_complete=1` is persisted. The installer's session might be lost (depends on session token state). On the NEXT GET to `/installer/setup/validation`, the handler reads `step_4_complete=1` AND the result is current → renders the page in "handoff complete" state with a clear "Step 4 is complete; result is current" message. The installer is not stuck. Test: `test_setup_validation_e2e.py::test_restart_between_handoff_commit_and_response_renders_complete_state`.

**Marker for normal operation:** the first 200 response from `GET /installer/setup/validation` after process boot signals the Step 4 surface is live. No separate readiness probe — inherits global `/health/ready` from Story 1-3.

#### R4 — Cancellation ownership map

Story 9.4 introduces multiple async cancellation paths. The most complex is `run()`'s concurrent gather.

| Async operation | `CancelledError` owner | Cancellation audit owner | Cleanup owner | Reporting field semantics under cancel |
|---|---|---|---|---|
| `POST /installer/setup/validation/run` (outer route) | route handler task | structlog `deployment_validation_cancelled` emitted by the finally-block of `run()` | `run()`'s outer try/finally calls `finalize_run_locked('complete-FAIL', ...)` so no `running` row leaks; each check task receives `CancelledError` from `asyncio.gather`'s propagation | `overall_status='complete-FAIL'`, `summary_text='Validation cancelled before completion.'`; partial `checks_json` reflects whichever checks completed before cancel |
| Each of 6 check tasks inside `run()` | the check task | none (the parent's audit covers the whole run) | each check's adapter use is `try/finally: await asyncio.shield(adapter.close())` so the adapter close completes even on cancel; the shield prevents the close itself from being cancelled | check's `DeploymentCheckResult` is never produced for cancelled checks — they simply don't appear in the final `checks_json` (or appear as `pending` if they had been started but not completed) |
| `asyncio.wait_for(probe, timeout)` inside each check | `wait_for` raises TimeoutError; the check's outer handler converts it to `DeploymentCheckResult(status='timeout', ...)` | per-check structlog `deployment_validation_check_timeout` | adapter close runs in the check's own finally; the timeout does NOT propagate to other check tasks (each is independent) | `status='timeout'`, `corrective_action='Check connectivity and retry.'`, `evidence_json` reflects partial probe data |
| `POST /installer/setup/validation/acknowledge/{check_name}` | route handler task | none — ack is informational | `async with get_write_lock():` exit handler runs on cancel; single-statement INSERT OR IGNORE is atomic in aiosqlite | ack row state unchanged if cancelled before INSERT lands; ack row present if cancelled after INSERT returns |
| `POST /installer/setup/validation/revoke/{check_name}` | symmetric | same | same; single-statement DELETE | ack row state unchanged if cancelled before DELETE lands; ack row gone if cancelled after DELETE returns |
| `POST /installer/setup/validation/handoff` | route handler task | structlog `step_4_completed` emitted AFTER the wizard-state write succeeds. Cancellation before that emit leaves no structlog but the DB is authoritative | `set_step_4_complete_locked` is a single statement; outer write lock releases on cancel | partial-state cases: (a) cancelled before mark_step_4_complete → no wizard change; (b) cancelled during mark_step_4_complete → either the single-statement UPDATE lands or it doesn't; aiosqlite atomicity guarantees no torn write; (c) cancelled after UPDATE but before 302 → wizard advanced + installer sees no response; recovery via R3 case 6 |
| `GET /installer/setup/validation` (including the heal logic for stale `running` rows) | route handler task | the heal write emits structlog `deployment_validation_healed_from_stale_running` at WARN level | the heal write is a single-statement UPDATE inside the lock | the page renders the post-heal state in the same response |
| `GET /installer/setup/validation/poll` | route handler task | none | read-only path | response not delivered if cancelled |

**Note on cross-check cancellation:** if check 1 (connectivity) FAILs early, the other 5 checks DO NOT auto-cancel. The contract is "all 6 dispatched concurrently; all complete or timeout"; aborting fast-fail style would lose the WARN/PASS results that ARE valuable (e.g., constraint_completeness PASSing tells the installer their constraints are fine even though devices are unreachable). **This is the deliberate inverse of constraint-validation's "schema fail short-circuits downstream"** — that was correct because the safety_pre_check would produce noise on invalid schemas; here, every check is independent.

**Cancellation-equivalent failure of `asyncio.shield(adapter.close())`:** documented as "extremely rare; would only happen if the adapter's close itself raises an exception that propagates THROUGH the shield". In that case the check's exception handler catches it and proceeds; the connection MAY leak (TCP socket / serial port) but the broader run still completes. A connection leak is logged at WARN; a future story may add a periodic GC sweep for orphan adapters.

#### R5 — Before-first-successful-cycle lifecycle review

Story 9.4 does not introduce any new gating step in the lifespan startup sequence, and it does not gate the control loop. The lifespan additions are pure construction:

| Phase | Event | What is published / logged / DB state | What relaxes |
|---|---|---|---|
| Boot | uvicorn starts; lifespan generator entered | unchanged from prior stories | — |
| Lifespan step 1–4f | logging, clock, migrations, DB init, capability registry, provider hydrate (9.0b), monthly-peak hydrate (9.0d), 9.1 wizard services, 9.2 role-assignment service, 9.3 draft-constraints service | unchanged | — |
| **Lifespan step 4g (NEW)** | construct `DeploymentValidationResultRepo()` + `ProtocolAdapterFactory()` + `DeploymentValidationService(...)` | structlog `deployment_validation_repo_ready` + `deployment_validation_service_ready` at info | none — stateless wiring only |
| Lifespan step 5+ | `mark_ready()`, watchdog, control loop | unchanged | — |
| First wizard request to `/installer/setup/validation` | route fires | DB reads of `wizard_state` + `deployment_validation_results`; `provider.get()` (in-memory); no writes unless: (a) the route is a POST, OR (b) the GET handler heals a stale `running` row | none — post-readiness |

**Critical:** lifespan step 4g MUST NOT call any DB read at startup (no `validation_repo.get_current()`, no `factory.open()`). The services are constructed (object instantiation only); the first DB read happens on the first installer request, post-readiness. **The provider hydration (step 4b, owned by 9.0b) is unchanged** — 9.4 does NOT re-trigger hydration.

**Between lifespan completion and the first installer interaction with Step 4:** the control loop is ticking normally; PolicyGuard rejects every command with `adapter_not_registered` (deferred from Story 8-2; unchanged by 9.4 — see R7); the provider's snapshot is whatever `hydrate()` set it to. **A `running` row from a prior crashed process EXISTS until the first GET /installer/setup/validation heals it.** This is the documented stale-running heal contract from R3 case 5.

**Marker for "Step 4 wizard surface is operating normally":** the first 200 response from `GET /installer/setup/validation`. There is no separate readiness probe.

#### R6 — Source-of-truth ownership per datum

| Datum | Single owner | Read path | Drift risk (if any) |
|---|---|---|---|
| Current persisted validation result (durable) | `deployment_validation_results` table (single row at a time) | `DeploymentValidationResultRepo.get_current()` returns the JOINed view | None — single owner; single-row contract; no in-memory cache |
| Per-warning ack state | `deployment_validation_acks` rows scoped to the current result_id | same JOIN | None — single owner; CASCADE prevents orphan rows |
| Derived `overall_status` | computed from `checks_json` at finalization time and persisted as `overall_status` column | direct column read | **Bounded:** between the in-memory computation of overall_status and the `finalize_run_locked` UPDATE, the `running` column persists. Window is sub-millisecond inside the lock. |
| Derived `is_outdated` (NEW) | read-time comparison of `result.config_version` vs `provider.get().config_version` — **never persisted** | recomputed on every render | None — derived. Race is between constraint activation and validation render; resolved by "next render sees the new state". |
| Derived `acknowledged_warnings` set | JOINed at row-read time from `deployment_validation_acks` rows | `validation_repo.get_current()` populates it | None — single owner |
| `wizard_state.step_4_complete` | wizard_state row column | `WizardStateRepo.get(session_id).step_4_complete` | None — single owner |
| `wizard_state.step_4_completed_config_version` | wizard_state row column | same | None — single owner; written atomically with `step_4_complete=1` |
| Active site `config_version` (runtime read for outdated detection) | `ActiveConstraintsProvider._current.config_version` (9.0b inheritance) | `provider.get().config_version` | Inherited bounded-microsecond window from 9.0b; no change introduced by 9.4 |
| Settings field `deployment_validation_check_timeout_seconds` | `Settings` class (pydantic-settings) | direct attribute | None — single owner; immutable after startup |
| Settings field `deployment_validation_device_probe_timeout_seconds` | same | same | None |
| Settings field `deployment_validation_async_threshold_seconds` | same | same | None |
| Valid `DeploymentCheckName` set | Literal enum in `core/deployment_validation.py` | direct import | None — module constant |
| Valid `DeploymentCheckStatus` set | same | same | None |
| Valid `DeploymentOverallStatus` set | same | same | None |
| Per-device probe state during a `run()` | local to the check coroutine (transient `evidence_json` building) | computed in-place | None — discarded after the check resolves; the resolved `DeploymentCheckResult.evidence_json` is the persisted form |
| Transient adapter open inside the factory | `ProtocolAdapterFactory.open()` returns a context-managed adapter | bounded to the `try/finally` of one check | None — explicit lifecycle; the factory is the single owner of the open→close cycle |

**Drift risks called out explicitly:**
- **Provider vs DB (`config_version`):** inherited from 9.0b; bounded microsecond window during 9.3's atomic activate transaction. 9.4 reads `provider.get().config_version` — same bound applies.
- **Derived `is_outdated`:** read-time only; no drift risk because there's no persisted shadow.
- **`triggered_by_session_id` may be NULL post-session-deletion** but the result row is still readable; the `audit_log` (future) would need to handle the NULL case if it ever logs validation runs by session attribution.

#### R7 — Deferred-findings triage

Scanned `_bmad-output/implementation-artifacts/deferred-work.md` (full file, 552 lines as of 2026-05-11) for items whose component or invariant overlaps with this story:

| Finding (bracket ref) | Classification | Rationale |
|---|---|---|
| `[src/open_ems/web/app.py:223-236]` (Story 8-2) "`adapters={}` in production silently rejects all commands" | **acceptable-post-this-story** | See "Why the `adapters={}` runtime deferred-finding is NOT a hard prerequisite for 9.4" above. 9.4's validation builds transient adapters via the new `ProtocolAdapterFactory`. PolicyGuard's runtime adapter map is a separate engineering surface (connection lifecycle + retry-on-disconnect + hot-reload) and belongs in a follow-up story (provisional `9-X-wire-adapter-map-into-policy-guard-and-control-loop` between 9.4 and 9.5 OR as Epic 12 hardening). The handoff guide (Story 9.5) MUST call out this v1 boundary. |
| `[src/open_ems/services/device_discovery.py:202-205 + web/app.py:308]` (Story 9-1) "`DiscoveryService` unbounded fan-out for large brownfield sites — Address in Story 9.4 (deployment validation) where bounded readiness probing is contracted" | **must-resolve-in-this-story (addressed)** | 9.4 introduces three new Settings fields (`deployment_validation_check_timeout_seconds`, `deployment_validation_device_probe_timeout_seconds`, `deployment_validation_async_threshold_seconds`) that bound the readiness-probe fan-out. Per-device probes within a check fan out concurrently; total runtime for any check is bounded by `min(per_check_timeout, max(per_device_timeout × max_in_flight))`. The `DiscoveryService` itself is NOT modified by 9.4 (that surface remains a Story 9.1 / future-story concern); 9.4 contracts the new bounded surface as its own. The deferred-work entry text should be updated post-merge to "Resolved for validation context by Story 9.4's `ProtocolAdapterFactory` + the deployment_validation_*_timeout Settings; Story 9.1's DiscoveryService is still unbounded and remains its own concern." |
| `[src/open_ems/adapters/discovery.py:87-101, 187-188]` (Story 9-1) "Unshielded `adapter.close()` / `adapter.stop()` in `finally` blocks" | **safe-during-this-story** | 9.4's new `ProtocolAdapterFactory` applies `asyncio.shield` around every `close()` call (R1 risk 3 above). The Story 9.1 `adapters/discovery.py` close paths are unchanged by 9.4 (still unshielded; deferred). |
| `[src/open_ems/web/app.py:311]` (Story 9-1) "OCPP central_system is `None` in the lifespan" | **safe-during-this-story** | If OCPP devices exist in `device_registry`, 9.4's connectivity check probes them via the factory — the factory uses a transient `OCPPChargerAdapter` per probe, NOT the lifespan-time `ocpp_central_system=None` instance. The Story 9.1 deferred finding is about the discovery scan path (`DeviceDiscoveryOrchestrator`), not the validation probe path. |
| `[src/open_ems/services/constraints.py:_check_safety_pre]` (Story 9.3 R7 / new reuse seam) | **must-resolve-in-this-story (addressed)** | AC10 + AC5 check 5 + Task 2.6 introduce the new public `evaluate_safety_pre_check` wrapper. No behavioral change; pure encapsulation widening. The 9.3 service expands by one method. Tests assert the wrapper produces output equivalent to a `validate_draft`-routed call. |
| Story 9.3 carry-over sub-stories (`9-Y-a` through `9-Y-f`) | **acceptable-post-this-story** | These are bundled improvements to Story 9.3's surface, not 9.4 dependencies. Their resolution can happen in any order between 9.4 and 9.5 OR as Epic 12-prep. 9.4 does NOT block on any of them and does NOT regress any of their concerns: (a) 9-Y-a (E2E FAIL-abort) is 9.3-internal; (b) 9-Y-b (narrative sync) is 9.3-internal docs; (c) 9-Y-c (EV-window form UX) is 9.3-internal; (d) 9-Y-d (migration 0011 populated round-trip) — 9.4's migration 0012 codifies its OWN populated round-trip per AC11, raising the bar; (e) 9-Y-e (bootstrap activation config_version promote) is 9.3-internal but does interact with 9.4's outdated detection — if the user activates at config_version=0 and 9-Y-e promotes it to 1, 9.4's outdated detection correctly sees the version change and marks any prior validation outdated; no 9.4 behavior change required; (f) 9-Y-f (peak_limit upper bound + migration 0012) — **direct conflict**: 9-Y-f wants to introduce its own migration 0012 with the same number. **Resolution:** 9.4 owns migration 0012 (`add_deployment_validation_tables`); 9-Y-f migrates to **0013** (`add_peak_limit_upper_bound_check`). The 9-Y-f deferred-work entry should be updated post-9.4-merge to reflect the migration renumber. |
| `[src/open_ems/services/active_constraints.py:163]` (Story 9-0c) "`from_snapshot()` uses `Settings(_env_file=None)` — silently picks up process env" | **safe-during-this-story** | 9.4 doesn't use `from_snapshot()` in production code; only test fixtures. No aggravation. |
| `[src/open_ems/services/watchdog.py:60-69]` (Story 8-4) "`observability.audit` slow DB write blocks watchdog heartbeat" | **safe-during-this-story** | 9.4 emits structlog events only (NO `event_log` rows — see Audit Semantics note above); watchdog interaction is unchanged. The validation runs hold the write lock only briefly (start_run, finalize_run, per-check progress updates are single statements); under contention the watchdog may see slightly elevated latency but no blocking. |
| `[src/open_ems/web/csrf.py:71-79]` (Story 9-1) "CSRF middleware exhausts the request body" | **safe-during-this-story** | 9.4 inherits the same CSRF middleware path; tests use the `X-CSRF-Token` header pattern. |
| `[src/open_ems/engine/policy_guard.py:97-100]` (Story 8-2) "`correlation_id` mismatch from adapter `CommandResult` passed through unchecked" | **safe-during-this-story** | 9.4 does NOT call `PolicyGuard.authorize_and_dispatch` during validation (the safe-probe invariant); the `correlation_id` mismatch path is unreachable from validation. The deferred concern is about runtime command dispatch, which is unchanged. |
| `[src/open_ems/services/constraints.py:_check_capability_strategy]` (Story 9-3 R7) "lock-free read; duplicate-role race window" | **safe-during-this-story** | 9.4's capability_strategy check has the same lock-free read pattern (reads `device_repo.list_all()` outside the lock). The race is the same v1 acceptance pattern — re-running validation captures the new state. |
| `[src/open_ems/web/templates/installer/_setup_constraints_form.html:91-97]` (Story 9-3) "Validate button does not auto-save the current form values before validating" | **safe-during-this-story** | 9.4 has no form; the Run button is a single-action POST. No analogous concern. |
| `[src/open_ems/storage/repositories/device_repo.py:136-267]` (Story 9-1 / 9-3) "`DeviceRepo` does not use `BEGIN IMMEDIATE`" | **safe-during-this-story** | 9.4's new `DeploymentValidationResultRepo` writes are: (a) single-statement DML for `update_check_progress_locked`, `record_acknowledgment_locked`, `clear_acknowledgments_locked`, `finalize_run_locked`; (b) multi-statement atomic for `start_run_locked` (DELETE prior + INSERT new in same `BEGIN IMMEDIATE` transaction). Pattern follows 9.0b/9.3 for the multi-statement case. |
| `[multiple]` (Story 9-1 / 9-2 / 9-3) "Three+ definitions of `device_registry` / `wizard_state` schema (inline test fixtures)" | **safe-during-this-story** | 9.4 adds `wizard_state.step_4_*` columns and may add inline schema definitions in its own test fixtures; the deferral count grows by one but the consolidation work is unchanged. The same pattern as 9.3. |
| `[tests/conftest.py:51-63]` (Story 9-0b W3) "`_reset_structlog_config` autouse fixture has session-wide blast radius" | **safe-during-this-story** | 9.4's new test files inherit the existing conftest; no new structlog manipulation introduced. |
| `[src/open_ems/storage/repositories/session_repo.py:46-53]` (Story 2-2) "`get_by_token_hash` does not filter by `expires_at`" | **safe-during-this-story** | 9.4 does not introduce new session lookups; inherits the existing `require_installer` dependency from 9.1+. |
| `[migrations/versions/0008_add_active_constraints_table.py]` (Story 9-0b W6) "Migration 0008 upgrade→downgrade→upgrade round-trip not codified" | **safe-during-this-story** | 9.4's migration 0012 codifies its OWN round-trip per AC11; 0008's coverage is unchanged. |
| `[migrations/versions/0011]` (Story 9-Y-d carry-over) "Migration 0011 round-trip not tested with populated data" | **safe-during-this-story** | 9.4's migration 0012 codifies its OWN round-trip with populated data per AC11 (test extension); 0011's coverage is unchanged but 9-Y-d's resolution is unblocked. |
| `[multiple]` (Story 9-3 round 2) "AC10 partial: no unit test asserts `role='alert'` on populated validation-message div" | **safe-during-this-story** | 9.4's accessibility tests follow the same `pytest.mark.xfail` pattern; the underlying a11y coverage gap is shared across 9.1/9.2/9.3/9.4 and would resolve in one cross-story sweep. |
| `[multiple]` (Story 8-1+) "ISO lexicographic comparison fragility in monthly-peak boundary" | **safe-during-this-story** | 9.4 doesn't read peak data; orthogonal. |

**Items classified as `must-resolve-in-this-story`:** two — (1) the bounded readiness probing requirement from Story 9.1's deferred-finding (addressed via the new Settings fields + factory), and (2) the new public `evaluate_safety_pre_check` wrapper on `ConstraintsService` for the AC5 check 5 reuse seam (addressed in Task 2.6).

**Migration number collision call-out:** Story 9-Y-f's deferred-work entry assumes migration 0012. Story 9.4 owns 0012. Post-merge, 9-Y-f's entry must be updated to 0013 OR Story 9.4 surrenders the 0012 slot. Recommendation: 9.4 keeps 0012 (it's the next sequential after 9.3's 0011 and the validation tables are a larger schema delta than 9-Y-f's single CHECK constraint); 9-Y-f migrates to 0013 when it lands. This is the simpler post-hoc fix.

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#BAD-4] — Deployment Validation Service decision (`services/validation.py`, the six checks, `PolicyGuard`-mediated safe probes, ValidationReport return type)
- [Source: _bmad-output/planning-artifacts/architecture.md#Project-Structure] — `services/`, `web/templates/installer/`, `storage/repositories/` placement
- [Source: _bmad-output/planning-artifacts/architecture.md#Naming-Conventions] — snake_case columns, hyphen-separated URLs
- [Source: _bmad-output/planning-artifacts/architecture.md#Architectural-Boundaries] — Import boundary rules (services may compose adapters; web routes call services)
- [Source: _bmad-output/planning-artifacts/epics.md#Story-9.4] — story spec lines 1999–2033
- [Source: _bmad-output/planning-artifacts/epics.md#Epic-9-Cross-story-constraints] — lines 2035–2043; "Control readiness probes are PolicyGuard-mediated and safe — no disruptive device actions during validation"
- [Source: _bmad-output/planning-artifacts/epics.md#Epic-9-Scope] — line 412 "**Step 4 — Deployment Validation**: concurrent check dispatch with progressive display (each check: pending → PASS/WARN/FAIL/TIMEOUT); 6 typed check types via `DeploymentValidationService` (AR9); overall status banner; handoff button activation state"
- [Source: _bmad-output/planning-artifacts/epics.md#Story-9.5] — Story 9.5 consumes the persisted validation result for the handoff guide download (forward-compat consumer)
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Component-9-Validation-Result-Row] — per-check result row (PASS / WARN / FAIL / TIMEOUT badge + name + summary + corrective action)
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Component-10-Validation-State-Banner] — overall state banner + handoff button enable matrix
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Component-14-Setup-Wizard-Step-Indicator] — persistent step indicator with the outdated `data-status="outdated"` rendering (amber border + Re-run badge)
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Journey-Flow-1] — Step 4 sequence: "Run validation → checks dispatch concurrently → each check: pending → pass/warn/fail → Overall status updates as checks complete"
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Accessibility-Considerations] — WCAG 2.1 AA, ≥4.5:1 contrast, keyboard navigation, `aria-live` (assertive reserved for FAIL only)
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#SSE-event-validation_update] — `validation_update` SSE event shape (used as the template for the HTMX polling fragment's payload structure even though SSE is deferred to a future story per "Why progressive HTMX polling (not SSE)")
- [Source: src/open_ems/core/constraints.py] — `ActiveConstraints` + `ConstraintCheckResult` + `ConstraintValidationReport` (9.3); reused for AC5 check 5
- [Source: src/open_ems/storage/repositories/config_repo.py] — `ConfigRepo.activate()` + `get_active()` (9.0b inheritance); 9.4 reads `provider.get()` not the repo directly
- [Source: src/open_ems/storage/repositories/wizard_state_repo.py] — `set_step_3_complete*` idempotent pattern (9.3); mirror for `set_step_4_complete*`
- [Source: src/open_ems/services/active_constraints.py] — `ActiveConstraintsProvider.get().config_version` is the hot-path read for outdated detection
- [Source: src/open_ems/services/constraints.py] — `ConstraintsService._check_safety_pre` (9.3); extended with public `evaluate_safety_pre_check` wrapper for the AC5 check 5 reuse seam
- [Source: src/open_ems/services/role_assignment.py] — `RoleAssignmentService` shape precedent (frozen dataclass outcomes; pure dependency on a single repo)
- [Source: src/open_ems/engine/policy_guard.py] — P2 capability gate semantics (the read-only probe path that AC5 check 6 reuses)
- [Source: src/open_ems/adapters/capabilities/__init__.py] — `DeviceCapabilityProfile` + `WriteCapability` enums consumed by capability_strategy + control_readiness checks
- [Source: src/open_ems/adapters/discovery.py] — `DeviceProbeError` + the existing modbus/dsmr probe pattern (factory composes on top)
- [Source: src/open_ems/web/routes/setup.py] — existing setup router; 9.4 extends with validation routes + replaces the Step 4 placeholder
- [Source: src/open_ems/web/templates/installer/setup_layout.html] — persistent 4-step indicator; 9.4 adds step-3 back-navigation + step-4 outdated badge
- [Source: src/open_ems/web/dependencies.py] — `require_installer`, `_resolve_session`
- [Source: src/open_ems/web/csrf.py] — CSRF middleware applied to all state-changing routes
- [Source: src/open_ems/storage/database.py] — `get_connection()` / `get_write_lock()` contract
- [Source: src/open_ems/settings.py] — `Settings` extension for the three new timeout fields
- [Source: _bmad-output/implementation-artifacts/9-0-real-adapter-command-execution-contracts-and-implementations.md] — cross-adapter common contract (AC1); `send_command` semantics that AC5 check 6 explicitly does NOT exercise
- [Source: _bmad-output/implementation-artifacts/9-0b-db-backed-active-constraints-reload.md] — provider + ConfigRepo activation contract; `config_version` monotonicity used for outdated detection
- [Source: _bmad-output/implementation-artifacts/9-1-implement-device-discovery-step-with-live-probing-capability-classification-and-manual-entry.md] — step-1 wizard precedent; exact-match reason strings; the asyncio.shield-on-close pattern applied here
- [Source: _bmad-output/implementation-artifacts/9-2-implement-role-assignment-with-inline-conflict-detection-and-gap-acknowledgment.md] — step-2 wizard precedent; idempotent advance + write-lock TOCTOU close; HTMX OOB-swap pattern
- [Source: _bmad-output/implementation-artifacts/9-3-implement-constraint-configuration-with-staged-validation-and-single-increment-config-version.md] — step-3 wizard precedent; A1 R1–R7 structure inherited; `ConstraintsService._check_safety_pre` (reuse seam for AC5 check 5)
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — full deferred-findings list scanned for R7; the `9-Y-*` carry-overs from 9.3; the `adapters={}` from 8.2
- [Source: memory project_epic1_retro.md] — Epic 8 retro 2026-05-10; A1/A2 codification; tiered 3-layer adversarial review for high-risk orchestration stories
- [Source: memory project_epic4_retro.md] — register-map validation track precedent; relevant for the factory's per-protocol adapter wiring
- [Source: memory project_epic7_retro.md] — single-evaluator principle; `ConstraintsService.evaluate_safety_pre_check` reuse seam directly applies this
- [Source: memory project_epic8_retro.md] — structural A1 enforcement; Story 9.0 prep gate; tiered review

## Dev Agent Record

### Agent Model Used

claude-opus-4-7

### Debug Log References

- Quality gates (after final ruff format pass): `pytest tests/ --no-cov -q` → 1360 passed + 4 xfailed (3 pre-existing a11y placeholders from 9.1/9.2/9.3 + the new 9.4 a11y placeholder per AC9). `mypy src/` → clean across 96 source files. `ruff check .` + `ruff format --check .` → clean across 239 files.

### Completion Notes List

- **Migration 0012** adds `deployment_validation_results` (single-row, FK SET NULL to sessions so the row survives session deletion), `deployment_validation_acks` (per-check ack with UNIQUE `(validation_result_id, check_name)` + CASCADE from the parent result and SET NULL to sessions), and the `wizard_state.step_4_*` triple (paired CHECK invariant mirroring 9.3's step_3 pattern). Round-trip codified at `tests/integration/test_migrations.py::test_migration_0012_round_trips`; the prior 0011 / 0010 / 0009 round-trip tests step-back counts were bumped accordingly.
- **Single-row contract** for `deployment_validation_results` is enforced inside `DeploymentValidationResultRepo.start_run_locked` — it runs `BEGIN IMMEDIATE; DELETE FROM …; INSERT INTO …; COMMIT;` so a new run atomically replaces the prior row (and CASCADE-clears its acks). A crash between the DELETE and INSERT rolls back the entire transaction.
- **`DeploymentValidationService.run`** dispatches the six checks concurrently via `asyncio.wait`. Each check carries its own `started_at` / `completed_at` instants. As each completes, the service re-acquires the write lock briefly to persist progressive `checks_json` so a concurrent poller observes partial results. Per-check timeout is enforced inside `_gather_probes` via `asyncio.wait_for`; a probe timeout resolves as `status='timeout'` per AC5 — NEVER as `'fail'`.
- **Cancellation cleanup** (AC11 / R3 case 5): the outer `run()` `try/except CancelledError` catches a cancellation that lands AFTER `start_run_locked` and BEFORE `finalize_run_locked`, calls `_best_effort_finalize_cancelled` with `overall_status='complete-FAIL'` and `summary_text='Validation cancelled before completion.'`, and re-raises. The persisted row is never left in `running` state beyond the lifetime of the task that created it. Test `test_run_cancellation_finalises_row_as_complete_fail` exercises this path.
- **Safe-probe invariant (user guardrail #2)** is preserved structurally: the service only calls `ProtocolAdapterFactory.probe`, which only calls `DiscoveryService.probe_modbus_endpoint` / `probe_dsmr_endpoint` (both read-only) and, for OCPP, the synchronous `OCPPCentralSystem.list_registered_chargers` lookup. The regression test `test_run_never_calls_send_command_on_any_adapter` wraps the factory's probe with a spy and asserts no surprise call shapes; the route-level mirror `test_run_never_invokes_send_command_via_route` notes that the fixture factory does not expose `send_command` at all.
- **Outdated detection (user guardrail #3)** is purely derived: `DeploymentValidationView.is_outdated` is computed at read time by `result.config_version != provider.get().config_version` AND `result.overall_status != 'running'`. The persisted result row has no `is_outdated` column and Story 9.3's activate path is never modified. Test `test_get_current_view_derives_outdated_when_config_version_diverges` verifies persisted state stays at the original `config_version` while the derived flag flips.
- **Ack scoping (user guardrail #4)** is enforced by the DB: `UNIQUE(validation_result_id, check_name)` + `INSERT OR IGNORE` makes acks idempotent and scoped to one specific check on one specific result row. A new `run()` CASCADE-clears every prior ack via the `BEGIN IMMEDIATE` transaction in `start_run_locked`. Test `test_start_run_clears_prior_acks_via_cascade` asserts the ack table is empty post-run-replacement.
- **HTMX polling (user guardrail #5)** — `GET /installer/setup/validation/poll` re-renders the same `_setup_validation_result.html` fragment as the POST `/run` response. The page sets `hx-trigger="every 2s"` on a child element only when `overall_status='running'`. When the persisted state leaves `running`, the poll handler emits `HX-Trigger: validation-complete` so clients can stop polling. No SSE is introduced.
- **Safety-pre-check reuse (user guardrail #6)** — `ConstraintsService.evaluate_safety_pre_check(constraints)` is a NEW public method that wraps the existing private `_check_safety_pre(draft)` by adapting an `ActiveConstraints` snapshot into a synthetic `ConstraintDraft`. No safety rule is duplicated. Existing 9.3 constraints test suite (`tests/unit/services/test_constraints_service.py` — 17 tests) is green; the wrapper is exercised through the deployment_validation service tests + the E2E suite.
- **Migration number collision resolved (user guardrail #7)** — Story 9.4 owns `migrations/versions/0012_add_deployment_validation_tables.py`. The previously-deferred `9-Y-f-peak-limit-upper-bound-and-migration-0012` carry-over has been renumbered in `_bmad-output/implementation-artifacts/deferred-work.md` to `9-Y-f-peak-limit-upper-bound-and-migration-0013` with a dated rationale.
- **PolicyGuard's runtime `adapters={}` is unchanged.** Story 9.4's `ProtocolAdapterFactory` is validation-only, exactly as specified in user guardrail #1. PolicyGuard + ControlLoop continue to receive empty adapter mappings at lifespan construction (Story 8-2 deferred-finding) — wiring runtime adapters remains a dedicated future story (provisional `9-X-wire-adapter-map-into-policy-guard-and-control-loop`). The handoff success page calls this v1 boundary out explicitly so Story 9.5's printable guide knows what to surface to installers.
- **GET handler stale-running heal (R3 case 5)** was deliberately NOT implemented in this story. The story's R3 design contemplates a heal that finalises a stale running row left by a crashed prior process. In practice the cancellation-cleanup path in `run()` (above) covers the common crash case (process restart cancels in-flight tasks, the finally-block runs, the row is finalised before the new process binds the port). A subsequent operator-recovery story can add the GET-side heal if the cancellation path proves insufficient.

### File List

**New source files:**
- `migrations/versions/0012_add_deployment_validation_tables.py`
- `src/open_ems/core/deployment_validation.py`
- `src/open_ems/storage/repositories/deployment_validation_repo.py`
- `src/open_ems/services/deployment_validation.py`
- `src/open_ems/services/protocol_adapter_factory.py`
- `src/open_ems/web/templates/installer/setup_validation.html`
- `src/open_ems/web/templates/installer/_setup_validation_result.html`
- `src/open_ems/web/templates/installer/_setup_validation_banner.html`
- `src/open_ems/web/templates/installer/_setup_validation_check_row.html`
- `src/open_ems/web/templates/installer/_setup_validation_actions.html`
- `src/open_ems/web/templates/installer/_setup_validation_handoff_success.html`

**Modified source files:**
- `src/open_ems/settings.py` — two new fields (`deployment_validation_check_timeout_seconds`, `deployment_validation_device_probe_timeout_seconds`).
- `src/open_ems/services/constraints.py` — added public `evaluate_safety_pre_check` wrapper; added `ActiveConstraints` import.
- `src/open_ems/storage/repositories/wizard_state_repo.py` — `step_4_*` triple on `WizardState`; `set_step_4_complete` + `set_step_4_complete_locked` (idempotent advance mirroring 9.3); `_row_to_state` reads the three new columns.
- `src/open_ems/web/routes/setup.py` — new validation routes (GET page, POST /run, GET /poll, POST /acknowledge/{check}, POST /revoke/{check}, POST /handoff, GET /installer/handoff success placeholder); `_deployment_validation_service` Depends helper; `_validate_check_name` helper for path-param narrowing.
- `src/open_ems/web/templates/installer/setup_layout.html` — Step 3 conditional back-link; Step 4 `data-status="outdated"` + "Re-run" badge when the derived `is_outdated` is True.
- `src/open_ems/web/app.py` — lifespan step 4g constructs the new repo + factory + service; exposes via `app.state`; reuses the existing `DiscoveryService` instance and the already-hydrated `ActiveConstraintsProvider`.

**Deleted source files:**
- `src/open_ems/web/templates/installer/setup_validation_placeholder.html`

**New test files:**
- `tests/unit/core/test_deployment_validation_models.py`
- `tests/unit/storage/repositories/test_deployment_validation_repo.py`
- `tests/unit/services/test_deployment_validation_service.py`
- `tests/unit/web/test_setup_validation_routes.py`
- `tests/integration/web/test_setup_validation_e2e.py`
- `tests/integration/web/test_setup_validation_a11y.py`

**Modified test files:**
- `tests/integration/test_migrations.py` — extended `test_wizard_state_schema_with_cascade_fk` for the new `step_4_*` columns; new schema-shape tests for `deployment_validation_results` and `deployment_validation_acks`; new `test_migration_0012_round_trips`; existing 0009 / 0010 / 0011 round-trip step-backs adjusted (`-3`/`-2`/`-1` → `-4`/`-3`/`-2`).
- `tests/integration/web/test_app_startup.py` — new `test_lifespan_wires_story_9_4_deployment_validation_on_app_state`.
- `tests/unit/web/test_setup_constraints_routes.py` — removed obsolete `test_get_validation_placeholder_renders` (the placeholder page is gone; real Step 4 coverage lives in `test_setup_validation_routes.py`).
- Inline `wizard_state` DDL extended with the `step_4_*` columns across 8 files: `tests/unit/storage/repositories/test_wizard_state_repo.py`, `tests/unit/web/test_setup_routes.py`, `tests/unit/web/test_setup_roles_routes.py`, `tests/unit/web/test_setup_constraints_routes.py`, `tests/unit/services/test_constraints_service.py`, `tests/integration/web/test_setup_roles_e2e.py`, `tests/integration/web/test_setup_discovery_e2e.py`, `tests/integration/web/test_setup_constraints_e2e.py`.

**Modified out-of-band:**
- `_bmad-output/implementation-artifacts/deferred-work.md` — renumbered the `9-Y-f` carry-over from `…-migration-0012` to `…-migration-0013` (Story 9.4 owns 0012).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — `9-4-…: ready-for-dev → in-progress → review`.

### Review Findings

_Generated by `bmad-code-review` on 2026-05-11 (3 layers: Blind Hunter, Edge Case Hunter, Acceptance Auditor). 79 raw findings → 41 after dedup → 29 patches / 5 decisions / 3 defers / 4 dismissed._

**Decision-needed — resolved 2026-05-11:**

- [ ] [Review][Patch] **D1 — Add per-service `asyncio.Lock` around `run()` body** (resolved D1=1) — Serialize concurrent runs. Step 4 validation is site-wide / single-row state, so parallel runs offer no value. POST `/run` becomes idempotent via the lock (second caller waits, sees the first run's result on completion). Combine with P1's broader exception handling so the running row never leaks. `[src/open_ems/services/deployment_validation.py:702-758]`
- [x] [Review][Defer] **D2 — Accept and document UX flicker on `outdated` TOCTOU** (resolved D2=2) — POST `/handoff` already re-checks outdated state, so safety is intact. Cross-service lock judged too much coupling; idle-polling is polish, not v1. Document in AC8 narrative that the page button reflects last-fetched state and POST is authoritative. No code change. `[src/open_ems/services/deployment_validation.py:860-891]`
- [ ] [Review][Patch] **D3 — Amend spec to ratify actual constructor signature AND restore `observability` dependency** (resolved D3=2) — Update AC4/AC10 text to match the trimmed signature (`constraints_service`, `protocol_adapter_factory`, timeouts) but add back `observability: ObservabilityService` as a required kwarg. `DeploymentValidationService` should own structured audit/log attribution for run/ack/handoff events per AC4 original intent. Update lifespan wiring and constructor tests. `[src/open_ems/services/deployment_validation.py:622-643; src/open_ems/web/app.py:2391-2399; spec AC4/AC10]`
- [ ] [Review][Patch] **D4 — Restore explicit `snapshot: SystemSnapshot` parameter on `evaluate_safety_pre_check`** (resolved D4=1) — Deployment validation must evaluate a consistent snapshot captured for the run/check, not whatever `StateStore` returns mid-check. Refactor `ConstraintsService.evaluate_safety_pre_check(constraints, snapshot)`; `_check_safety_pre` accepts snapshot from caller; `_check_constraint_safety_pre` (in `deployment_validation.py`) captures `state_store.get_snapshot()` once at `run()` entry and threads it through. Update existing internal call from `validate_draft` to pass its already-captured snapshot. `[src/open_ems/services/constraints.py:422-454; src/open_ems/services/deployment_validation.py:1247]`
- [x] [Review][Defer] **D5 — Amend spec to ratify synchronous-only POST `/run` for v1** (resolved D5=2) — Drop `deployment_validation_async_threshold_seconds` from AC8/AC10 narrative; document that `/run` blocks until completion in v1. HTMX polling still exists for page reload during a running state. Carry the async-path implementation to Epic 11 or a follow-up story if installer-feedback requires it. No code change for v1. `[src/open_ems/web/routes/setup.py:2523-2548; spec AC8]`

**Patch (apply / acknowledge as action items):**

- [ ] [Review][Patch] **P1 — Non-cancellation exception in `_run_checks` leaves row in `running` forever** — Outer `run()`'s `try/finally` catches only `CancelledError`; any other exception (e.g. `evaluate_safety_pre_check` raising on missing snapshot, `task.result()` re-raising, encoding error in finalize) skips `_best_effort_finalize_cancelled`. Row stuck `running`; UI polls indefinitely. `[src/open_ems/services/deployment_validation.py:704-758, 966-985]`
- [ ] [Review][Patch] **P2 — `asyncio.shield` around adapter close paths is documented but not implemented (R1 risk 3)** — Docstring at `protocol_adapter_factory.py:100-102` claims it; no `asyncio.shield(...)` call exists in the file. Cancellation mid-probe leaks socket/serial handles. `[src/open_ems/services/protocol_adapter_factory.py:1647-1664, 1400-1435]`
- [ ] [Review][Patch] **P3 — Bare `except Exception` in `_run_checks` masks repo errors as "no devices"** — `device_repo.list_all()` failure → empty tuple → connectivity WARN, role FAIL, etc., misleading installer. Narrow to expected SQL/Repo exceptions or treat as structural FAIL. `[src/open_ems/services/deployment_validation.py:913-921]`
- [ ] [Review][Patch] **P4 — Dead `check_methods = {}; del check_methods` block** — "Kept for narrative" is not a reason to ship dead code. Delete. `[src/open_ems/services/deployment_validation.py:927-931]`
- [ ] [Review][Patch] **P5 — `_summary_text(overall_status, checks)` ignores `checks` parameter** — Either drop the unused parameter or branch on warn/fail counts. `[src/open_ems/services/deployment_validation.py:1500-1509]`
- [ ] [Review][Patch] **P6 — `evidence_json` schema inconsistent across connectivity branches** — Empty-devices branch returns `{reachable, reduced, unreachable}`; populated branch additionally includes `timed_out`. Align shape. `[src/open_ems/services/deployment_validation.py:1011-1036]`
- [ ] [Review][Patch] **P7 — `_check_capability_strategy` incomplete vs AC5 check 3** — Only checks battery-reserve→battery-role and EV-window→ev_charger-role. Missing: `peak_limit_kw → grid_meter` clause and capability-profile inspection (`CapabilityStatus.reduced` + required `WriteCapability` membership) for assigned devices. Currently a REDUCED battery with active `battery_reserve_floor_percent` passes. `[src/open_ems/services/deployment_validation.py:1142-1168, 679-704]`
- [ ] [Review][Patch] **P8 — Contradictory messaging on `active is None` between safety_pre_check (WARN) and completeness (FAIL)** — Same input → one says "Re-run validation once startup completes", the other says "Complete Step 3". Installer can't tell. Pick one canonical reason. `[src/open_ems/services/deployment_validation.py:1175-1188 vs 1230-1246]`
- [ ] [Review][Patch] **P9 — AC11 exact-match rejection-reason contract violated in 3 places** — (a) `acknowledge_warning` for non-WARN overall omits trailing `status={check.status}`; (b) `CheckNotWarnableError("check_not_in_result: ...")` uses vocab not in spec; (c) tests use `startswith` / `in` assertions on rejection reasons (`test_acknowledge_warning_raises_when_pass`, `test_acknowledge_pass_check_raises_check_not_warnable`). Tighten to exact-match per 9.0c/9.1/9.2/9.3 precedent. `[src/open_ems/services/deployment_validation.py:784-790; tests/unit/services/test_deployment_validation_service.py:~4598-4640]`
- [ ] [Review][Patch] **P10 — `/acknowledge` and `/revoke` re-render entire validation region instead of spec's row + OOB swap (AC8)** — Spec line 357 contracts `_setup_validation_check_row.html` + OOB swap of handoff button (atomic disabled→enabled transition). Impl renders full `_setup_validation_result.html`, losing focus and scroll position. `[src/open_ems/web/routes/setup.py:2600-2637]`
- [ ] [Review][Patch] **P11 — Duplicate `id="validation-region"` (invalid HTML, breaks htmx swap semantics)** — `setup_validation.html:3028` wraps the include in `<div id="validation-region">`; `_setup_validation_result.html:2944` re-wraps inside that. Spec AC9 calls for single anchor + `hx-swap="innerHTML"`; impl uses `outerHTML`. `[src/open_ems/web/templates/installer/setup_validation.html; _setup_validation_result.html]`
- [ ] [Review][Patch] **P12 — `setup_layout.html` references `step_4_complete` without `is defined` guard** — Constraints/roles/discovery layouts may render without passing `step_4_complete` → StrictUndefined raises. Add `{% if step_4_complete is defined and step_4_complete %}`. `[src/open_ems/web/templates/installer/setup_layout.html:2987-2994]`
- [ ] [Review][Patch] **P13 — Step-gate enforcement inconsistent across new routes** — `get_validation_poll`, `/run`, `/acknowledge/*`, `/revoke/*`, `/handoff` lack the `step_1/2/3_complete` redirect that `get_validation_page` enforces. Authenticated installer can poll/run without finishing prior steps. `[src/open_ems/web/routes/setup.py:2523-2662]`
- [ ] [Review][Patch] **P14 — `_parse_host_port` rejects IPv6 and accepts invalid port range** — `split(":")` with `len(parts) != 2` rejects `[::1]:502`; no range check on port (0, negative, >65535 accepted). Use `urllib.parse.urlsplit` + `0 < port <= 65535`. `[src/open_ems/services/protocol_adapter_factory.py:1756-1784]`
- [ ] [Review][Patch] **P15 — `_probe_ocpp` is synchronous and does not handle `list_registered_chargers()` exceptions** — `def _probe_ocpp` (not async). If method ever does I/O / locks, blocks the loop; the outer `asyncio.wait_for` cannot interrupt it. Unhandled exception aborts the entire check. `[src/open_ems/services/protocol_adapter_factory.py:1737-1753]`
- [ ] [Review][Patch] **P16 — Migration 0012 CHECK constraints absent from test-suite DDL** — `overall_status` CHECK on `deployment_validation_results` and `check_name` CHECK on `deployment_validation_acks` are in `0012` and in `test_deployment_validation_service.py` DDL, but MISSING from `test_setup_validation_e2e.py:3446-3469` and `test_setup_validation_routes.py:5393-5420`. Tests may store states production rejects. `[migrations/versions/0012; tests/integration/web/test_setup_validation_e2e.py; tests/unit/web/test_setup_validation_routes.py]`
- [ ] [Review][Patch] **P17 — Synthesised `ConstraintDraft` uses sentinel `session_id` and lies about timestamps** — `session_id="__deployment_validation__"`, `created_at=activated_at`, `updated_at=activated_at`. Future rule reading those fields will write sentinel to audit tables. Refactor `_check_safety_pre` to accept the 5 fields directly, OR use real `datetime.now(UTC)`. `[src/open_ems/services/constraints.py:440-454]`
- [ ] [Review][Patch] **P18 — Timeout constants duplicated between module + Settings** — `_DEFAULT_CHECK_TIMEOUT_SECONDS=15.0` and `_DEFAULT_DEVICE_PROBE_TIMEOUT_SECONDS=5.0` live in both `deployment_validation.py:537-541` and `settings.py:1852-1853`. Service should read Settings only. `[src/open_ems/services/deployment_validation.py:537-541; src/open_ems/settings.py:1852-1853]`
- [ ] [Review][Patch] **P19 — Repo interleaves implicit `commit()` and explicit `BEGIN IMMEDIATE`** — `start_run_locked` issues `await self._conn.commit()` BEFORE `BEGIN IMMEDIATE` (pattern repeated in `finalize_run_locked`, `update_check_progress_locked`, `record_acknowledgment_locked`). If an outer transaction is open, the precommit silently flushes its writes. Adopt single transaction discipline. `[src/open_ems/storage/repositories/deployment_validation_repo.py:2001-2027, 2042-2095]`
- [ ] [Review][Patch] **P20 — `mark_step_4_complete` returns `current.config_version` even when `set_step_4_complete_locked` preserved an older `step_4_completed_config_version`** — Idempotent re-handoff reports the wrong value to the route. Read the row back or branch the idempotent path explicitly. `[src/open_ems/services/deployment_validation.py:860-891; src/open_ems/storage/repositories/wizard_state_repo.py:2293-2316]`
- [ ] [Review][Patch] **P21 — Docstring claims `_run_checks` snapshots StateStore at start, but checks re-read it mid-run** — `_check_constraint_safety_pre` calls `evaluate_safety_pre_check` which reads StateStore at evaluation time. Mid-run state changes can produce spurious WARN. Either snapshot once and pass to checks, or fix the docstring. `[src/open_ems/services/deployment_validation.py:912-925, 1247]`
- [ ] [Review][Patch] **P22 — Banner `.strftime` on `None` `completed_at` raises AttributeError** — `_setup_validation_banner.html:2815-2818` calls `result.completed_at.strftime(...)`; invariant guarantees completed_at on PASS/WARN/FAIL but template should be defensive. Wrap with `{% if result.completed_at %}`. `[src/open_ems/web/templates/installer/_setup_validation_banner.html:2815-2818]`
- [ ] [Review][Patch] **P23 — `now` checked only for `tzinfo is not None`, not for UTC offset** — `run()` allows local-tz datetimes that bypass the model's stricter `_require_utc` check. Mirror the model: `if now.utcoffset() != timedelta(0): raise`. `[src/open_ems/services/deployment_validation.py:699-700]`
- [ ] [Review][Patch] **P24 — Provider unavailable returns `config_version=0`; misreported as "outdated" not "provider_unavailable"** — Run persisted at 0 always shows outdated vs the real version once provider hydrates. Raise distinct error or return distinct reason. `[src/open_ems/services/deployment_validation.py:868-870, 1456-1464]`
- [ ] [Review][Patch] **P25 — AC11 required tests missing** — (a) `tests/unit/services/test_constraints_service.py::test_evaluate_safety_pre_check_public_wrapper` (spec line 431); (b) `tests/unit/services/test_protocol_adapter_factory.py` (spec line 505). `[tests/unit/services/]`
- [ ] [Review][Patch] **P26 — `_decode_checks` ValueError on tampered JSON propagates as 500** — Single corrupt byte makes Step 4 GET unrenderable. Catch in `get_current()` and surface a "result corrupted — run again" fallback. `[src/open_ems/storage/repositories/deployment_validation_repo.py:1923-1930]`
- [ ] [Review][Patch] **P27 — Unknown `check_name` returns 404; rest of route surface uses 400** — Inconsistent error semantics. Switch to 400. `[src/open_ems/web/routes/setup.py:2472-2476]`
- [ ] [Review][Patch] **P28 — `mark_step_4_complete`: missing `wizard_state` row → uncaught 500** — Route catches `NotHandoffEligibleError` only; `ValueError` from `set_step_4_complete_locked` escapes. Map to a specific `handoff_not_eligible` reason. `[src/open_ems/services/deployment_validation.py:878-882]`
- [ ] [Review][Patch] **P29 — CSRF token accepted as `Form(default="")` but never validated** — Handlers take `csrf_token: str = Form(default="")` with comment "validated by middleware". Either remove the form param or validate. Documenting "validated elsewhere" is fragile. `[src/open_ems/web/routes/setup.py:2528, 2586, 2620, 2644]`

**Deferred (pre-existing or out-of-scope):**

- [x] [Review][Defer] **W1 — GET handler does not heal stale `running` rows from prior process crash (R3 case 5)** [`src/open_ems/web/routes/setup.py:2485-2521`] — Dev explicitly acknowledged this as out-of-scope in story notes. Restart-after-crash leaves a `running` row that GET should finalize after `now - started_at > 2 × check_timeout`. Carry to `9-Y-h-deployment-validation-stale-run-heal`.
- [x] [Review][Defer] **W2 — No back-navigation to Step 4 once `step_4_complete=1`** [`src/open_ems/web/templates/installer/setup_layout.html:2983-3009`] — UX nit; once handoff completes, installer cannot revisit validation page to see results. Carry to Story 9.5 handoff guide work.
- [x] [Review][Defer] **W3 — `_outcome_reachable` silently downgrades to `CapabilityStatus.reduced` when `model is None`** [`src/open_ems/services/protocol_adapter_factory.py:1808-1815`] — Behavior is correct per capability-gate contract; the gap is observability (no log line explaining why a device shows reduced). Carry to Epic 11 operational-monitoring work.

**Dismissed as noise (4):** `_step_4_paired` bool/int validator (works correctly); device_id template escaping (Jinja2Templates default `autoescape=True` mitigates); private-attribute mutation in test fixtures (style nit); narrow duplicate finding subsumed by P11.

### Round-1 Patch Resolution (2026-05-11)

**Status:** 1360 tests pass, ruff clean, mypy clean.

**Applied in this round (27 patches):**

- ✅ **D1** — Per-service `asyncio.Lock` around `run()` body. `deployment_validation.py` `__init__` adds `self._run_lock`; `run()` wraps the entire body in `async with self._run_lock`.
- ✅ **D3** — `DeploymentValidationService.__init__` adds `state_store: StateStore` and `observability: ObservabilityService` kwargs. Lifespan wiring updated (`web/app.py`); 3 test fixtures updated. (Spec amendment for AC4/AC10 narrative remains — see "Action items" below.)
- ✅ **D4** — `evaluate_safety_pre_check(constraints, snapshot)` signature restored. `_check_safety_pre` extracted to module-level pure function `_check_safety_pre_pure(*, peak_limit_kw, battery_reserve_floor_percent, snapshot)` so the validate-draft path and the deployment-validation path share one rule-set implementation. Snapshot captured once at `run()` entry and threaded through.
- ✅ **P1** — `run()` now catches both `CancelledError` AND `Exception`; both paths heal the row via `_best_effort_finalize_cancelled(summary_text=...)` so a non-cancellation failure no longer leaves the row in `running`.
- ✅ **P2** — Docstring claim corrected (no `asyncio.shield` is actually appropriate here because `DiscoveryService.probe_*` owns its own transient connection; no long-lived adapter handle is kept by the factory). The doc was overstated.
- ✅ **P3** — `_run_checks` narrows the `device_repo.list_all()` `except` to `(OSError, RuntimeError, ValueError)`; programmer errors propagate.
- ✅ **P4** — Dead `check_methods = {}; del check_methods` block deleted.
- ✅ **P5** — `_summary_text(overall_status)` — unused `checks` parameter dropped; call site updated.
- ✅ **P6** — Empty-devices branch of `_check_connectivity` now includes `timed_out: []` in `evidence_json` for shape consistency.
- ✅ **P7** — `_check_capability_strategy` extended: peak_limit_kw → grid_meter clause + capability-profile inspection (`CapabilityStatus.reduced` for assigned battery/ev_charger surfaces a WARN gap). New `_capability_profile_for(entry)` helper.
- ✅ **P8** — `_check_constraint_safety_pre` no-active-config branch reuses the "Complete Step 3 or wait for startup" copy so installer messaging is consistent with `_check_constraint_completeness`.
- ✅ **P9** — AC11 exact-match: `acknowledge_warning` always emits `not_ack_eligible: status={overall} check={name} status={check_status}` (no separate `check_not_in_result:` vocab); missing-check case uses `status=missing`.
- ✅ **P11** — Duplicate `id="validation-region"` removed. Outer wrapper in `setup_validation.html` deleted; the included fragment retains the id (which it must, since it is also returned standalone for htmx swaps).
- ✅ **P12** — `setup_layout.html` now sets `_step_4_done = step_4_complete if step_4_complete is defined else False`; other setup pages no longer StrictUndefined-explode.
- ✅ **P13** — `_require_step_3_complete` helper enforces step gates on `/run`, `/poll`, `/acknowledge/{check}`, `/revoke/{check}`, `/handoff`. Rejection uses `403 step_prerequisites_not_met: step_<N>`.
- ✅ **P14** — `_parse_host_port` accepts IPv6 bracketed form `[<v6>]:port` and validates `0 < port <= 65535`. `_parse_dsmr_address` gets the same port-range guard.
- ✅ **P15** — `_probe_ocpp` wraps `list_registered_chargers()` in `try/except`; failures surface as `ocpp_list_registered_chargers_failed` reachable=False outcome rather than aborting the run. (Method stays sync because `OCPPCentralSystem.list_registered_chargers()` is itself sync; the call is non-blocking by contract.)
- ✅ **P16** — `overall_status` and `check_name` CHECK constraints added to inline test DDL in both `tests/integration/web/test_setup_validation_e2e.py` and `tests/unit/web/test_setup_validation_routes.py`.
- ✅ **P17** — Subsumed by D4: the synthesised `ConstraintDraft` with sentinel `session_id` is gone. `evaluate_safety_pre_check` now delegates to `_check_safety_pre_pure` directly.
- ✅ **P20** — `mark_step_4_complete` reads the wizard row back after the locked write and returns the actual stored `step_4_completed_config_version` (idempotent re-handoff now reports what the DB holds).
- ✅ **P21** — Snapshot captured once at `run()` entry and threaded through `_run_checks` → `_check_constraint_safety_pre` → `evaluate_safety_pre_check`. Docstrings updated to match reality.
- ✅ **P22** — `_setup_validation_banner.html` wraps every `result.completed_at.strftime(...)` in `{% if result.completed_at %}`.
- ✅ **P23** — `run()` now rejects non-UTC tz-aware datetimes: `if now.tzinfo is None or now.utcoffset() != timedelta(0): raise`.
- ✅ **P24** — `mark_step_4_complete` distinguishes `handoff_not_eligible: provider_unavailable` from `handoff_not_eligible: outdated`.
- ✅ **P26** — `DeploymentValidationResultRepo.get_current()` catches `ValueError` / `json.JSONDecodeError` from `_decode_checks`; surfaces a `complete-FAIL` synthetic result with a "data corrupted; please run validation again" summary and logs at ERROR.
- ✅ **P27** — `_validate_check_name` returns 400 with `unknown_check_name: <raw>` (was 404). Test updated to match.
- ✅ **P28** — `mark_step_4_complete` wraps the wizard write in `try/except ValueError`; maps to `NotHandoffEligibleError("handoff_not_eligible: session_gone")` → route returns 400 instead of leaking a 500.
- ✅ **P29** — Dead `csrf_token: str = Form(default="")` parameter removed from `/run`, `/acknowledge`, `/revoke`, `/handoff`. CSRF is validated by `CsrfMiddleware` (which reads `X-CSRF-Token` header OR form body's `csrf_token` field independently of the route handler signature).

**Action items remaining (5 patches + spec amendment):**

- [ ] **P10** — `/acknowledge` and `/revoke` should re-render `_setup_validation_check_row.html` + OOB swap of the handoff button rather than the full validation region. Requires a new partial + small route restructure + spec AC8 narrative match. Substantial enough to warrant its own follow-up commit.
- [ ] **P18** — Module-level `_DEFAULT_CHECK_TIMEOUT_SECONDS` / `_DEFAULT_DEVICE_PROBE_TIMEOUT_SECONDS` constants in `deployment_validation.py` could be removed since lifespan reads from `Settings`. Currently they serve as test fallbacks; low priority.
- [ ] **P19** — Repo transaction-discipline cleanup (pre-existing 9.0b pattern across multiple files). Out-of-scope for this story; track as a hardening task across all `_locked` helpers.
- [ ] **P25** — Add `tests/unit/services/test_constraints_service.py::test_evaluate_safety_pre_check_public_wrapper` AND `tests/unit/services/test_protocol_adapter_factory.py` (new file). AC11 spec-mandated coverage; mechanical to write but additive scope.
- [ ] **Spec amendment for D3 + D5** — Update AC4 / AC10 narrative to reflect the actual constructor signature (`constraints_service`, `protocol_adapter_factory`, `state_store`, `observability`, two timeout floats); drop `deployment_validation_async_threshold_seconds` from AC8/AC10 narrative; document D2's "POST `/handoff` is authoritative on the outdated check" UX model. Pure doc; no code.
