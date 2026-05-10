# Story 9.0d: Restart Hydration of Monthly Peak

Status: done

> **Origin:** Epic 8 retrospective (2026-05-10) — P2 prep story. Carried forward from Story 8.1's deferred review finding (`[control_loop.py:54]`: `_monthly_peak_kw` initializes to `0.0` and is only refreshed after the first interval rollover, so peak decisions use stale data for up to 15 minutes after every process restart). Installer workflow restarts the process (Epic 9 wizard steps); this becomes user-visible. **Hard gate before Story 9.4** (Deployment validation actually exercises peak-limit decisions).
>
> **A2-triggered:** True. Matched triggers: **T3** (persistence + recovery — DB-backed monthly peak hydrated on lifespan startup, rebuilt across process restart), **T6** (deployment / restart behavior — installer wizard restarts trigger this code path; cold-start invariant is user-visible). Partial: **T1** (lifecycle — the control loop now requires its initial monthly-peak value to be computed from the DB *before* it is constructed, formalizing an explicit "hydrated initial state" precondition). All R1–R7 orchestration artifacts are present below.

## Story

As an installer (and as the operator of an EMS site whose process has just been restarted),
I want the control loop's `_monthly_peak_kw` cache to be rebuilt from the `peak_intervals` table at lifespan startup — *before* the first evaluation cycle runs,
so that peak-limiting decisions on the first ≤15 minutes after restart use the real month-to-date peak instead of a `0.0` placeholder that would silently approve commands the configured peak limit was meant to block.

## Acceptance Criteria

### AC1 — `EnergyRepo` exposes the existing hydrate query unchanged; no schema change

**GIVEN** `src/open_ems/storage/repositories/energy_repo.py::EnergyRepo.get_current_monthly_peak_kw()` already computes `MAX(avg_power_kw)` from `peak_intervals` filtered by `interval_start_utc >= <UTC month start>` and returns `0.0` when the table holds no rows for the current month
**WHEN** Story 9.0d wires lifespan hydration
**THEN** no new method, schema, or migration is introduced — the existing `get_current_monthly_peak_kw()` is the canonical read path. The story is a **lifespan wiring change**, not a repository or schema change.

**AND** `EnergyRepo.get_current_monthly_peak_kw()` is invoked exactly once per lifespan as part of the cold-start hydrate step (AC4), in addition to its existing per-interval-rollover invocation inside `ControlLoop._update_tracker` (which remains unchanged).

**AND** the contract of `get_current_monthly_peak_kw()` is preserved:
- Returns `float`, always `>= 0.0`.
- `0.0` is the documented sentinel for "no rows for the current month" (empty `peak_intervals` table, or no rows whose `interval_start_utc` is `>=` the current UTC month boundary).
- Computes `month_start` from `datetime.now(UTC)` at call time — therefore the hydrate at lifespan correctly reads the month in which the *process is starting*, even if the process was down across a month boundary.

---

### AC2 — `ControlLoop.__init__` accepts `initial_monthly_peak_kw`

**GIVEN** `ControlLoop.__init__` currently hardcodes `self._monthly_peak_kw: float = 0.0`
**WHEN** Story 9.0d refactors the constructor
**THEN** a new **keyword-only** parameter `initial_monthly_peak_kw: float = 0.0` is added to `ControlLoop.__init__`, and the body assigns `self._monthly_peak_kw = initial_monthly_peak_kw` in place of the hardcoded `0.0`.

**AND** the parameter is validated at the constructor: if `initial_monthly_peak_kw < 0.0` or `not math.isfinite(initial_monthly_peak_kw)`, raise `ValueError("initial_monthly_peak_kw must be a finite non-negative float")` BEFORE assignment. Defense in depth — `EnergyRepo.get_current_monthly_peak_kw()` is contractually `>= 0.0`, but a future caller or test fixture could pass a bogus value; the engine cannot operate on a negative or NaN monthly peak.

**AND** the parameter default remains `0.0` so that existing test fixtures that construct `ControlLoop` without specifying it continue to compile. The semantic meaning of the default is unchanged from today: "no peak hydrated; the first interval rollover will refresh from DB."

**AND** the rest of `ControlLoop` — including `_update_tracker`'s post-rollover refresh of `self._monthly_peak_kw` via `await self._energy_repo.get_current_monthly_peak_kw()` — is unchanged. The hydrate value is the **seed**; the existing refresh loop continues to own the steady-state update path.

---

### AC3 — `_update_tracker` continues to own the steady-state refresh; no double-refresh on first tick

**GIVEN** the existing `ControlLoop._update_tracker` (lines 158–172 of `control_loop.py`) refreshes `self._monthly_peak_kw` only when `tracker.update(...)` returns a `CompletedInterval` (i.e. an interval boundary was crossed during the tick)
**WHEN** Story 9.0d adds hydration
**THEN** `_update_tracker`'s logic is **unchanged**. The hydrate value seeds `self._monthly_peak_kw` at construction time and is only superseded when an interval rollover triggers `_update_tracker` to write a new row + re-query `MAX`.

**AND** the first tick after restart does NOT additionally re-query `get_current_monthly_peak_kw()` — that would be a redundant DB round-trip when the hydrated value is by construction equal to what the query would return at boot. The seed-then-refresh-on-rollover cadence is the same as today, only the seed value changes.

**AND** if the process restart lands inside an interval, `self._monthly_peak_kw` carries the hydrated value through every tick of that interval and is refreshed once the interval rolls over (`_update_tracker` writes the new interval's row, then re-queries `MAX`). The hydrated value is therefore active for the entire ≤15 min that today's bug exposes to staleness.

---

### AC4 — Lifespan hydrate step `4c`

**GIVEN** `src/open_ems/web/app.py` lifespan currently runs (after `init_database` at line 230) `validate_capability_registry_alignment()` (step 4a) and then `ActiveConstraintsProvider.hydrate()` (step 4b), then constructs the `ControlLoop` at line 356
**WHEN** Story 9.0d adds the monthly-peak hydrate step
**THEN** a new **step 4c** is inserted *after* `active_constraints_provider.hydrate()` (step 4b) and *before* `ControlLoop(...)` construction (currently line 356):

```python
# Step 4c (Story 9.0d): hydrate the control loop's monthly-peak cache from
# peak_intervals BEFORE constructing the loop. Without this seed,
# _monthly_peak_kw stays 0.0 until the first interval rollover, so the first
# ≤15 minutes of peak-limit decisions after every restart silently use stale
# data. The installer wizard triggers restarts; this window is user-visible.
energy_repo = EnergyRepo()
try:
    initial_monthly_peak_kw = await energy_repo.get_current_monthly_peak_kw()
except Exception:
    logger.error(
        "startup_failed",
        reason="monthly_peak_hydrate_failed",
        exc_info=True,
        component="startup",
    )
    raise SystemExit(1) from None
logger.info(
    "monthly_peak_hydrated",
    initial_monthly_peak_kw=initial_monthly_peak_kw,
    component="startup",
)
```

**AND** the existing `EnergyRepo()` construction at line 359 (`energy_repo=EnergyRepo()`) is replaced with the locally-bound `energy_repo` instance from step 4c so the same repo is reused by both the hydrate call AND the `ControlLoop` — eliminating any risk of two `EnergyRepo` instances ever drifting (they share the same `get_connection()` today, but the single-instance discipline is the explicit form of the invariant).

**AND** the `ControlLoop(...)` construction at line 356 is updated to pass `initial_monthly_peak_kw=initial_monthly_peak_kw` as a new keyword argument.

**AND** the lifespan logs the hydrate outcome at `INFO` with `monthly_peak_hydrated` and the seed value, parallel to the existing `migrations_applied`, `constraints_hydrate_seeded_from_settings`, `system_ready` info-level events. The log fields are `initial_monthly_peak_kw` (float) and `component="startup"`.

---

### AC5 — Fail-loud on hydrate failure (mirror constraints-hydrate pattern)

**GIVEN** step 4b (constraints hydrate) escalates a DB read failure to `SystemExit(1)` via `logger.error("startup_failed", reason="constraints_hydrate_failed", ...)` followed by `raise SystemExit(1) from None`
**WHEN** Story 9.0d wires step 4c
**THEN** step 4c mirrors that pattern verbatim:
- On any exception from `await energy_repo.get_current_monthly_peak_kw()`, log `startup_failed` at `ERROR` with `reason="monthly_peak_hydrate_failed"`, `exc_info=True`, `component="startup"`.
- Then `raise SystemExit(1) from None`.
- Step 4c lives *inside* the outer `try:` whose `finally: await close_database()` at line 406–408 guarantees the aiosqlite connection is closed on this failure path — same defense in depth as 9.0b's D4→P10 patch.

**AND** the failure is **fail-loud**, not silent fallback-to-0.0, for two reasons:
1. **Symmetry with steps 4a / 4b.** `init_database` just succeeded; `validate_capability_registry_alignment()` just succeeded; `ActiveConstraintsProvider.hydrate()` just succeeded. A failure of a third `SELECT` against the same connection is a meaningful diagnostic signal — almost certainly a schema corruption, file-system fault, or process-permission issue. Silently degrading would mask it.
2. **Sympathetic to the fix's intent.** The entire purpose of the story is to remove a stale-window bug. A silent-fallback-to-0.0 path would preserve the bug in degraded conditions, which is the exact scenario where operators are most likely to debug peak-limit anomalies.

**Rejected alternative:** soft-fail with warning + fallback to `0.0`. Considered and rejected — the fallback is *exactly* the bug this story exists to fix. Logging the warning would not surface to the typical operator until the peak limit was already exceeded. Fail-loud forces correct DB plumbing.

---

### AC6 — Ordering invariant: hydrate runs BEFORE ControlLoop construction

**GIVEN** `ControlLoop.__init__` now accepts `initial_monthly_peak_kw` as a constructor argument
**WHEN** the lifespan executes
**THEN** the ordering is **structurally** enforced: `await energy_repo.get_current_monthly_peak_kw()` MUST complete before `ControlLoop(...)` is constructed, because the constructor cannot run without its argument. The Python language semantics make this impossible to reorder accidentally.

**AND** the lifespan ordering is documented inline (a one-line comment above the hydrate block) so a future refactor that moves the hydrate AFTER `ControlLoop(...)` construction would (a) break compilation (constructor missing arg) AND (b) fail the lifespan integration test in AC8.

**AND** the broader cold-start ordering is now:
1. Migrations (`_run_alembic_upgrade`)
2. `init_database`
3. **4a** `validate_capability_registry_alignment()` (Story 9.0c)
4. **4b** `active_constraints_provider.hydrate()` (Story 9.0b)
5. **4c** *(NEW)* `initial_monthly_peak_kw = await energy_repo.get_current_monthly_peak_kw()` (this story)
6. Admin bootstrap, `mark_ready`, observability, loop liveness
7. Watchdog task
8. Session cleanup, event-log pruning tasks
9. `IntentExecutor`, `PolicyGuard`, `RetryPolicy`
10. **`ControlLoop(..., initial_monthly_peak_kw=initial_monthly_peak_kw)`** ← seed value applied
11. `_control_loop_task = asyncio.create_task(_control_loop.run(), ...)`

---

### AC7 — Unit tests for `ControlLoop` constructor seed

**GIVEN** `tests/unit/engine/test_control_loop.py` already exercises the loop end-to-end with the `_control_loop()` factory at line 107 and the existing `make_active_constraints_provider` fixture
**WHEN** Story 9.0d adds tests
**THEN** three new tests are added:

1. `test_initial_monthly_peak_kw_seeds_evaluation_input()` — Construct a `ControlLoop` with `initial_monthly_peak_kw=14.7`, run `_build_evaluation_input` *before* any tick (so `_update_tracker` has not yet refreshed). Assert `evaluation_input.peak_context.current_monthly_recorded_peak_kw == 14.7`. Use the existing `_set_tracker_to_boundary` helper for deterministic tracker state. This proves the seed value reaches the engine on the very first cycle.

2. `test_initial_monthly_peak_kw_default_is_zero()` — Construct a `ControlLoop` *without* the new argument; assert `loop._monthly_peak_kw == 0.0`. Backwards-compatibility guard — existing test fixtures must remain functional.

3. `test_initial_monthly_peak_kw_rejects_negative_and_nan()` — Parametrized over `-0.001`, `-1.0`, `float("nan")`, `float("inf")`, `float("-inf")`. Each construction must raise `ValueError`. Lock the AC2 validator.

**AND** the existing `_control_loop()` factory at line 107 is **NOT** modified to require the new arg — it keeps the `initial_monthly_peak_kw=0.0` default behavior implicitly, so existing tests are diff-free.

---

### AC8 — Integration test for lifespan hydrate ordering

**GIVEN** `tests/integration/web/test_app_startup.py` already covers the capability-registry-drift failure path (`test_lifespan_aborts_with_system_exit_on_capability_registry_drift`)
**WHEN** Story 9.0d adds integration coverage
**THEN** three new tests are added to that same file (consistent with the existing convention of grouping lifespan-startup tests there):

1. `test_lifespan_hydrates_monthly_peak_before_control_loop_construction()` — Pre-populate `peak_intervals` with a row in the current month (`avg_power_kw=17.3`, `sample_count=1`, `data_quality="complete"`). Run the real lifespan via `async with lifespan(app):`. Capture the value the lifespan passes as `initial_monthly_peak_kw` by patching `open_ems.web.app.ControlLoop` to record its kwargs, OR by capturing it via the structured-log event `monthly_peak_hydrated` (preferred — non-invasive). Assert the captured value equals `17.3`. **AND** assert via task-creation order (spy on `asyncio.create_task` for `name="control_loop"`) that the `monthly_peak_hydrated` log event fires *before* the control-loop task is scheduled.

2. `test_lifespan_aborts_with_system_exit_on_monthly_peak_hydrate_failure()` — Patch `open_ems.storage.repositories.energy_repo.EnergyRepo.get_current_monthly_peak_kw` to raise `aiosqlite.OperationalError("disk I/O error")`. Run the lifespan inside `pytest.raises(SystemExit) as exc_info`. Assert `exc_info.value.code == 1`. Assert the structured-log `startup_failed` event was emitted with `reason="monthly_peak_hydrate_failed"`, `component="startup"`. **AND** assert (mirror 9.0c P1) that `close_database` is invoked via a `wraps=real_close_database` spy — the failure path must run the outer `finally`.

3. `test_lifespan_hydrates_zero_when_peak_intervals_is_empty()` — Fresh DB (no peak_intervals rows). Run lifespan; capture `monthly_peak_hydrated` log event. Assert `initial_monthly_peak_kw == 0.0`. **No SystemExit** — empty table is a valid state on a fresh deployment; `0.0` is the contractual sentinel. This is the cold-start branch.

**AND** the integration tests use the same test-DB fixture pattern as the existing `test_app_startup.py` tests (no extra fixtures introduced).

---

### AC9 — Documentation pass

**GIVEN** `src/open_ems/engine/control_loop.py` module docstring already includes a "Story 9.0b update" paragraph
**WHEN** Story 9.0d completes
**THEN** a new "Story 9.0d update" paragraph is appended to the module docstring:

```
Story 9.0d update: ``_monthly_peak_kw`` is now seeded via the
``initial_monthly_peak_kw`` constructor argument, populated by the lifespan
from ``EnergyRepo.get_current_monthly_peak_kw()`` BEFORE the loop is
constructed. The prior behaviour of initializing to ``0.0`` and refreshing
only on the next interval rollover meant the first ≤15 min of peak-limit
decisions after every restart silently used stale data; the installer
wizard restarts the process, so this window was user-visible. The
in-loop refresh inside ``_update_tracker`` (post-rollover re-query of
``MAX(avg_power_kw)``) is unchanged — the seed only affects the first
interval after restart.
```

**AND** `_bmad-output/planning-artifacts/architecture.md` is not modified — the schema (`peak_intervals`) and read path (`MAX(avg_power_kw)`) are unchanged; only the lifespan wiring sequence gained a step.

**AND** `_bmad-output/implementation-artifacts/deferred-work.md` is updated: the entry `[control_loop.py:54]` "`_monthly_peak_kw` not hydrated from DB on process restart" under "Deferred from: code review of 8-1-implement-the-runtime-control-loop-..." gets a strikethrough OR is replaced with a parenthetical "(resolved in Story 9.0d, 2026-05-11)". Keep the historical record; do not delete the line.

---

### AC10 — Quality gates

**GIVEN** the baseline at Story 9.0c close: `pytest tests/ --no-cov -q` → 1071 passed, ruff/format/mypy clean
**WHEN** Story 9.0d reaches `Status: review`
**THEN** all four gates pass:

- `uv run python -m pytest tests/ --no-cov -q` — must show baseline + new tests (expected: at least 1077, depending on parametrization expansion in AC7 #3). All green.
- `uv run python -m mypy src/` — clean.
- `uv run python -m ruff check .` — clean.
- `uv run python -m ruff format --check .` — clean.

**AND** the new test files (or new tests inside existing test files) are independently verifiable by the reviewer — Acceptance Auditor must be able to run them in isolation.

---

### AC11 — Adversarial 3-layer review with severity tagging (process AC)

**GIVEN** Story 9.0d is A2-triggered (T3 + T6 + partial T1 — see story header)
**WHEN** the story reaches `Status: review`
**THEN** the tiered review model from Epic 8 retro action A3 applies:

- **Layer 1 (Blind Hunter):** unsafe implementation patterns — hydrate query against wrong month, fail-silent fallback regression, double-refresh on first tick wasting a DB round-trip, missing constructor validation allowing NaN/negative seed, ordering refactor that constructs ControlLoop before hydrate.
- **Layer 2 (Edge Case Hunter):** lifecycle/runtime failures — DB read race against concurrent `_update_tracker` write (the database-module write lock is in scope), process restart across month boundary (does the hydrate read the *new* month?), startup with corrupt `peak_intervals` row (does `MAX(avg_power_kw)` crash or silently coerce?), what happens if step 4a or 4b fail — does step 4c run? (it shouldn't; lifespan SystemExit short-circuits, but verify via the integration test ordering).
- **Layer 3 (Acceptance Auditor):** AC drift — every AC1–AC10 clause traced to a test or a design decision; specifically AC5's fail-loud rejection of soft-fail must be verified to actually `SystemExit(1)`, not `pass` silently.

**AND** every finding carries a severity tag (HIGH / MEDIUM / LOW). `Status: review → done` is BLOCKED on any unresolved HIGH.

---

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: AC10)
  - [x] `uv run python -m pytest tests/ --no-cov -q` — record exact passing count as baseline (expected: 1071 from Story 9.0c close).
  - [x] `uv run python -m ruff check .` — must be clean.
  - [x] `uv run python -m ruff format --check .` — must be clean.
  - [x] `uv run python -m mypy src/` — must be clean.

- [x] Task 1: `ControlLoop.__init__` constructor change (AC: AC2)
  - [x] Add `initial_monthly_peak_kw: float = 0.0` as a keyword-only parameter (the constructor is already keyword-only via the leading `*,`).
  - [x] Add the `math.isfinite` + non-negative validator BEFORE assignment. Import `math` if not already imported.
  - [x] Replace `self._monthly_peak_kw: float = 0.0` with `self._monthly_peak_kw = initial_monthly_peak_kw` (drop the type annotation if assignment is now from a typed parameter — mypy infers).

- [x] Task 2: Lifespan wiring (AC: AC4, AC5, AC6)
  - [x] In `src/open_ems/web/app.py`, after the constraints-hydrate block (currently ends ~line 265) and BEFORE the `ControlLoop(...)` construction (currently line 356), add the step 4c block per AC4's pseudocode.
  - [x] Hoist the `EnergyRepo()` instantiation out of the `ControlLoop(...)` argument list and into the step 4c block; pass the same instance to `ControlLoop`.
  - [x] Add the `initial_monthly_peak_kw=initial_monthly_peak_kw` kwarg to the `ControlLoop(...)` call.
  - [x] Wrap the hydrate in try/except → `SystemExit(1)` per AC5; ensure it lives inside the outer `try:` whose `finally: await close_database()` runs.

- [x] Task 3: Unit tests for ControlLoop seed (AC: AC7)
  - [x] Add the three new tests to `tests/unit/engine/test_control_loop.py`.
  - [x] AC7 #3 uses `pytest.mark.parametrize` with named parameters; failure IDs must surface the offending value.
  - [x] Do NOT modify the existing `_control_loop()` factory at line 107; rely on the new default `initial_monthly_peak_kw=0.0` keeping it diff-free.

- [x] Task 4: Integration tests for lifespan hydration (AC: AC8)
  - [x] Append the three new tests to `tests/integration/web/test_app_startup.py`.
  - [x] AC8 #1 prefers structured-log capture via `mock_logger` (mirror the existing pattern at `test_app_startup.py:33`) over patching `ControlLoop`.
  - [x] AC8 #2 mirrors the existing `test_lifespan_aborts_with_system_exit_on_capability_registry_drift` structure exactly — including the `wraps=real_close_database` close-database spy.
  - [x] AC8 #3 must execute against a real test DB connection (existing `tests/conftest.py` pattern, post-migration); do NOT mock `aiosqlite` — the `MAX(...)` on empty table is a real-DB semantic.

- [x] Task 5: Documentation pass (AC: AC9)
  - [x] Append the "Story 9.0d update" paragraph to `src/open_ems/engine/control_loop.py`'s module docstring.
  - [x] Update `_bmad-output/implementation-artifacts/deferred-work.md` — annotate the `[control_loop.py:54]` line under the 8-1 review section as resolved by this story.

- [x] Task 6: Final quality gate (AC: AC10)
  - [x] `uv run python -m pytest tests/ --no-cov -q` — baseline + new tests, all green.
  - [x] `uv run python -m mypy src/` — clean.
  - [x] `uv run python -m ruff check .` — clean.
  - [x] `uv run python -m ruff format --check .` — clean.

- [x] Task 7: Submit for adversarial 3-layer review (AC: AC11)
  - [x] Status: review.
  - [ ] Layer 1 (Blind Hunter): unsafe-pattern sweep.  *(reviewer-owned)*
  - [ ] Layer 2 (Edge Case Hunter): lifecycle/runtime sweep — month-boundary, corrupt rows, write-lock concurrency.  *(reviewer-owned)*
  - [ ] Layer 3 (Acceptance Auditor): AC1–AC10 → test traceability matrix.  *(reviewer-owned)*
  - [ ] Severity tags HIGH/MEDIUM/LOW on every finding. No HIGH unresolved at `Status: review → done`.

---

## Dev Notes

### Why this is a "small" story (per retro framing)

- **No schema change.** `peak_intervals` already exists (migration 0007). `EnergyRepo.get_current_monthly_peak_kw()` already exists and is already invoked by the control loop on every interval rollover. The story re-uses that same query as a one-shot lifespan-time read.
- **No new module.** Unlike 9.0b (which introduced `ActiveConstraintsProvider`), there is no in-memory snapshot owner here. The control loop *already* owns `_monthly_peak_kw` as a private float attribute; the steady-state refresh path already exists in `_update_tracker`. The story only adds a seed value.
- **No new public API.** The constructor argument is internal to lifespan wiring; no external caller of `ControlLoop` exists outside `app.py` and tests.
- **No new audit events, no new structured-log event types** beyond a single `monthly_peak_hydrated` info-level startup log (parallel to existing startup logs).

The work is concentrated in: one constructor signature change (`control_loop.py`), one lifespan-step insertion (`app.py`), three unit tests, three integration tests, two documentation lines, one deferred-work annotation.

### Design rejected: a `MonthlyPeakProvider` service class

Considered modelling this exactly after 9.0b's `ActiveConstraintsProvider`. Rejected because:
1. `_monthly_peak_kw` has **one** consumer (`ControlLoop._build_evaluation_input`), unlike `peak_limit_kw` / `battery_reserve_floor_percent` which have two (`PolicyGuard` and `ControlLoop`). The R6 source-of-truth fragmentation problem that justified `ActiveConstraintsProvider` does not exist here.
2. The value is **refreshed by the same code that writes new rows** (`_update_tracker` writes, then re-queries `MAX`). A separate provider would force a "reload-after-write" plumbing detail back into `_update_tracker` — strictly more complexity than the current arrangement.
3. There is no external trigger to refresh — unlike active constraints which are refreshed on installer activation, the monthly peak is only refreshed when the loop itself writes a new completed interval. The "change-trigger" surface is internal.

The seed-via-constructor approach matches the value's lifecycle: write-once at startup, then owned by the loop's existing refresh path.

### Design rejected: soft-fail with warning + fallback to 0.0

See AC5 rationale. Soft-fail would preserve the exact bug this story exists to fix. Fail-loud is symmetric with steps 4a / 4b and forces correct DB plumbing.

### Design rejected: re-querying on first tick instead of constructor seed

An alternative would be to leave `__init__` unchanged and add an `async def hydrate(self)` method on `ControlLoop` that's called by lifespan after construction. Rejected because:
1. The constructor-arg form is **structurally** ordered (can't construct without the value), which is the strongest form of the AC6 invariant. An `async hydrate()` could be forgotten in a future lifespan refactor.
2. The existing 9.0b pattern uses a provider's `await provider.hydrate()` *because* the provider needs to own its own lock and idempotency. For a single float seed, that machinery is overkill.
3. `mypy` and the type checker see the dependency directly in the constructor.

### Why no new write lock acquisition

`EnergyRepo.get_current_monthly_peak_kw()` is a `SELECT`-only operation; it does not need to acquire the database-module write lock that 9.0b's D1→P7 patch added. The lock guards write paths against concurrent transactions. A read-only `MAX(...)` runs against SQLite's reader-friendly default isolation. The hydrate call happens before any task has been scheduled, so even theoretically there are no concurrent writers at hydrate time.

### Cold-start race: hydrate vs. concurrent `_update_tracker` write

There is **no such race**. At lifespan step 4c, no asyncio task has been created yet — `_control_loop.run()` is not scheduled until step 11 (after step 4c, after all middleware, after all dependent services). Therefore no `_update_tracker` invocation can be in flight during the hydrate read. The first `_update_tracker` call after hydrate runs ≥10s later (one `control_loop_interval_seconds` tick after the loop task starts).

### File structure references

| Path | Action |
|---|---|
| `src/open_ems/engine/control_loop.py` | UPDATE — constructor accepts `initial_monthly_peak_kw`; module docstring appended; `import math` added if not present. |
| `src/open_ems/web/app.py` | UPDATE — lifespan adds step 4c; `EnergyRepo()` hoisted into a local; `ControlLoop(...)` call gets new kwarg. |
| `tests/unit/engine/test_control_loop.py` | UPDATE — three new tests appended; existing `_control_loop()` factory unchanged. |
| `tests/integration/web/test_app_startup.py` | UPDATE — three new lifespan tests appended. |
| `_bmad-output/implementation-artifacts/deferred-work.md` | UPDATE — annotate `[control_loop.py:54]` entry as resolved by this story. |

**No new files. No new migrations. No schema changes.**

### Architecture references

- **R4 (architecture.md §Project structure)** — `energy_repo.py` placement and contract.
- **§Peak interval persistence** (architecture.md lines 1053–1080) — `peak_intervals` schema; "billing record is never modified by control actions"; the partial-window projection is *not* persisted. This story does not touch any of those invariants.
- **Story 8.1 AC2 + AC7** — original `_monthly_peak_kw` initialization and `EnergyRepo.get_current_monthly_peak_kw()` contract; this story preserves both.
- **Story 8.1 deferred review finding `[control_loop.py:54]`** — the specific bug this story closes.
- **Story 9.0b AC7** — lifespan-hydrate pattern for active constraints; this story mirrors the pattern for monthly peak.
- **Story 9.0c lifespan integration test pattern** — `tests/integration/web/test_app_startup.py`; AC8 mirrors its `wraps=real_close_database` style.
- **Epic 8 retrospective (2026-05-10) P2** — hard gate before Story 9.4; "small BMAD story" framing.

### Testing standards summary

- pytest + pytest-asyncio (per architecture §Testing).
- Unit tests: keep the existing fixture pattern in `tests/unit/engine/test_control_loop.py`; do NOT re-architect the `_control_loop()` factory.
- Integration tests: use real aiosqlite + Alembic migrations via the existing `tests/conftest.py` fixture; AC8 #3 explicitly requires a real-DB `MAX(...)` on empty result.
- Structured-log capture: prefer `unittest.mock.patch("open_ems.web.app.logger", mock_logger)` over `structlog.testing.capture_logs()` — the latter has a known cached-logger leakage issue documented in 9.0b Debug Log (Story 9.0c also follows this pattern).
- Sentinel-verify the fail-loud path: AC8 #2's assertion of `SystemExit.code == 1` AND the `startup_failed` log emission ensures the AC5 fail-loud branch is exercised, not just declared.

### Library/framework notes

- `aiosqlite` (existing) — used unchanged. No new transactions.
- `math.isfinite` (stdlib) — used in AC2 validator. Already idiomatic in the codebase (see `partial_interval_tracker.py:136`).
- `structlog` (existing) — one new info-level event: `monthly_peak_hydrated`. One reuse of the existing `startup_failed` event with a new `reason="monthly_peak_hydrate_failed"` discriminator.
- No new dependencies. `uv.lock` unchanged.

### Previous story intelligence — what we carry from Epic 8 / Story 9.0 / 9.0b / 9.0c

- **Story 8-1 deferred:** `_monthly_peak_kw` not hydrated on restart — **directly addressed by this story.**
- **Story 8-1 deferred:** ISO string comparison for monthly peak boundary fragile against non-UTC formats `[energy_repo.py:37-41]` — **safe-during.** Same query path; no new contributions to the storage-format hazard. The defensive fix (schema-level UTC suffix check) is acceptable-post.
- **Story 8-1 deferred:** Multi-interval stall silently drops intermediate `CompletedInterval`s `[control_loop.py:107]` — **safe-during.** Tracker-level concern; unaffected by hydrate semantics.
- **Story 8-1 deferred:** First tick publishes `operating_mode=None` `[control_loop.py:73-76]` — **safe-during.** Unrelated to monthly peak.
- **Story 8-2 deferred:** Degraded grid meter returns `0.0` to PartialIntervalTracker `[control_loop.py:_extract_grid_power]` — **safe-during.** Data-quality concern; orthogonal to hydrate.
- **Story 9.0b deferred (W7):** AC7 fail-loud `SystemExit(1)` branch not exercised by any test `[src/open_ems/web/app.py:229-236]` — **safe-during.** This story adds an *analogous* test for the new step-4c fail-loud branch (AC8 #2). The 9.0b deferred W7 case (step-4b's fail-loud path) remains separately deferred — extending that test is not in this story's scope. Reviewer may decide to close W7 by analogy, but that is a discretionary closure, not a hard requirement.
- **Story 9.0c lesson:** lifespan integration tests use `wraps=real_close_database` to ensure the failure path still runs `close_database` — AC8 #2 inherits this exactly.
- **Story 9.0c lesson:** structlog cached-logger leakage means `capture_logs()` is unreliable — use `unittest.mock.patch` on the module-level logger instead.
- **Style precedent:** Story 9.0c followed Story 9.0b's "small, focused, fail-loud, well-tested" shape and shipped clean. 9.0d's surface is even narrower; the same shape applies.

### Project Structure Notes

- All file touches are within existing modules / test files. No new packages, no new top-level files, no new fixtures.
- The `EnergyRepo()` hoist in `app.py` is a one-line refactor that does not change behavior — `EnergyRepo()` uses the global `get_connection()` singleton by default, so two `EnergyRepo()` instances share the same aiosqlite connection. The hoist is a code-clarity improvement (single instance, used for both the seed read and the control-loop's per-rollover refresh) — but functionally equivalent to the current two-instance pattern.

---

### Orchestration Risk Analysis (A2-triggered — REQUIRED)

> **A2 triggers matched:** **T3** (persistence + recovery — DB-backed monthly peak hydrated on startup, rebuilt across process restart), **T6** (deployment / restart behavior — installer wizard restarts trigger this code path; cold-start invariant is user-visible to peak-limit decisions for the first ≤15 minutes). Partial: **T1** (lifecycle — `ControlLoop` gains an explicit "hydrated initial state" precondition formalized at constructor time; the loop's lifecycle now includes a structural seed step before any tick).

#### R1 — Composition-risk analysis

This story converges three operational domains in a single shipping unit. The number of domains is smaller than 9.0b's five — and that asymmetry is itself a deliberate design outcome (see "Why this is a 'small' story" in Dev Notes). The three risks below are each story-specific, not generic.

1. **Persistence + recovery (T3) — wrong-month read at startup.** The hydrate uses `EnergyRepo.get_current_monthly_peak_kw()`, which computes `month_start = datetime.now(UTC).replace(day=1, ...)` at *call time*. If the process restarts across a month boundary (e.g., crashed at 23:59 on the 31st, restarted at 00:01 on the 1st), the hydrate correctly reads the new month — which is what we want. But: if the system clock is wrong at startup (NTP not yet synced; see Story 1.3's clock-status path), `now(UTC)` may return a value in the previous or next month, and the hydrate would read the wrong month's peak. **Mitigation:** lifespan step 3 (`check_clock`) runs *before* step 4c; if `clock_unsynced`, the structured-log warning fires. The hydrate proceeds anyway (we cannot block startup on clock sync — that would also block the entire control loop), but the warning provides a forensic trail. **Acceptable risk** for v1: any wrong-month read self-corrects on the first interval rollover. **R3 #4 captures the cold-start invariant.**

2. **Cold-start ordering (T6, partial T1) — hydrate runs after migrations and constraints, before ControlLoop construction.** The story's correctness depends on the lifespan executing steps in order: migrations → init_database → capability validation → constraints hydrate → **monthly-peak hydrate** → ControlLoop construct. A future refactor that reorders these (e.g., moves capability validation after ControlLoop construct) would silently break the seed invariant if the same refactor also moved monthly-peak hydrate. **Mitigation:** AC6 makes the ordering **structural** via the constructor signature — `ControlLoop(initial_monthly_peak_kw=...)` cannot compile without the seed value being computed first. AC8 #1's task-creation-order spy locks the runtime invariant. **Precedent:** 9.0b R1 #2 captured the same shape for active constraints; the same review rigor closes it.

3. **Fail-loud vs. soft-fail asymmetry (T3) — accidental regression to silent fallback.** A well-intentioned future contributor might "improve" the hydrate to log-and-continue on DB read failure ("the loop will refresh on first rollover anyway"). That regression would re-introduce the exact 15-min stale window this story exists to close. **Mitigation:** AC5's rationale is documented in the story (not just "raise SystemExit"); AC8 #2 codifies the SystemExit assertion as a regression test. Any future refactor that removes the SystemExit would fail AC8 #2 in CI. **No precedent from prior stories** — this is a new asymmetry specific to the monthly-peak-vs-constraints distinction.

#### R2 — State-transition table

Story 9.0d adds **no new state machine**. The only state-relevant change is at the `ControlLoop` construction boundary: previously, `self._monthly_peak_kw` was always `0.0` at construction; now it can be any non-negative finite float.

For audit completeness, the table below captures the (unchanged) state machine for `self._monthly_peak_kw` over a single lifespan, with the **new initial-value transition** at row 0 explicitly called out:

| State | Transitions out | Trigger | Side-effects |
|---|---|---|---|
| (pre-construction — value undefined) | → `seeded` | `ControlLoop(initial_monthly_peak_kw=X)` constructor runs | `self._monthly_peak_kw = X` (assignment; no log, no audit, no DB write). If `X < 0` or `not isfinite(X)`, the constructor raises `ValueError` and never reaches `seeded`. |
| `seeded` (= hydrated value from `peak_intervals`, OR `0.0` if table empty / no row for current month) | → `refreshed` | `_update_tracker` sees `tracker.update()` return a `CompletedInterval` (interval boundary crossed during this tick) | `_energy_repo.write_peak_interval(completed, data_quality=...)` writes the new interval, then `_monthly_peak_kw = await _energy_repo.get_current_monthly_peak_kw()` overwrites the seed. |
| `refreshed` (after at least one rollover) | → `refreshed` (new value) | each subsequent rollover | same as above |
| `refreshed` | (terminal — process shutdown) | lifespan teardown / `_control_loop_task.cancel()` | no audit; in-memory value discarded |

**Note:** `_monthly_peak_kw` may **decrease** intra-month if an interval row is re-written with a lower value (e.g. data correction via `INSERT OR REPLACE` on the same `interval_start_utc` PK). This is by design — the DB is the source of truth; `MAX(avg_power_kw)` reflects whatever is currently in the table. The seed inherits this property: a hydrate after a manual DB correction reads the corrected MAX, not the historical-maximum-ever-recorded.

There is no new structlog event for the `seeded → refreshed` transition (the existing post-rollover refresh is not logged in 8-1's design). The new `monthly_peak_hydrated` info log fires exactly once, at the moment of the `(pre-construction) → seeded` transition during lifespan step 4c.

#### R3 — Impossible-state analysis (with cold-start coverage)

**Forbidden state combinations and their structural invariants:**

1. **`ControlLoop` is constructed with `initial_monthly_peak_kw < 0` or `NaN`** — invariant: AC2's constructor validator raises `ValueError` before assignment. AC7 #3's parametrized test locks every named-bogus value (negative, NaN, `+inf`, `-inf`). Defense in depth: `EnergyRepo.get_current_monthly_peak_kw()` is contractually `>= 0.0` (it computes `COALESCE(MAX(avg_power_kw), 0.0)`); so under normal flow the validator never fires. The validator exists to catch test-fixture or future-refactor bugs.

2. **`_build_evaluation_input` runs before `_monthly_peak_kw` has been seeded** — invariant: Python language semantics. `_monthly_peak_kw` is assigned in `__init__`; no method of `ControlLoop` can run before `__init__` completes. The constructor cannot complete without `initial_monthly_peak_kw` (per AC2 — keyword-only with default `0.0`, but always assigned). There is no "unhydrated" state visible to consumers.

3. **The hydrate reads the wrong month** (e.g. process clock skewed) — invariant: AC1 documents `get_current_monthly_peak_kw()` computes `month_start` from `datetime.now(UTC)` at call time. Lifespan step 3 (`check_clock`) runs BEFORE step 4c; if clock is `unsynced`, the structured-log `clock_unsynced` warning is emitted, providing forensic trail. The hydrate proceeds with whatever the system clock reports. **Self-correcting:** the first interval rollover (≤15 min after first tick) refreshes the cached value using `now(UTC)` again — by then NTP will likely have synced (timesyncd typically converges within minutes of network availability). The wrong-month risk is bounded to one interval window in the absolute worst case.

4. **Hydrate succeeds but reads `0.0` because the DB is empty** — invariant: AC8 #3 documents and tests this as the cold-start branch. `0.0` is the contractual sentinel for "no rows in the current month"; this is the same value the loop would have at boot under the pre-9.0d behavior. The startup log emits `monthly_peak_hydrated initial_monthly_peak_kw=0.0` — operators can see that hydration occurred and the value was zero (versus hydration failing silently). **This is not an error state; it is the legitimate fresh-deployment / new-month state.**

5. **Hydrate fails silently** — invariant: AC5's fail-loud pattern raises `SystemExit(1)`. AC8 #2 codifies the assertion. There is no log-and-continue branch.

6. **A second hydrate fires later in lifespan** — invariant: lifespan step 4c is a single inline `await`. No retry, no reschedule, no daemon task that could re-fire it. The only update path after step 4c is `_update_tracker`'s post-rollover refresh, which is the existing 8-1 design.

7. **`_monthly_peak_kw` is read while a write is in flight** — invariant: no concurrency. `_update_tracker`'s write-then-read sequence (`await self._energy_repo.write_peak_interval(...)` then `self._monthly_peak_kw = await self._energy_repo.get_current_monthly_peak_kw()`) runs inside the single control-loop task. `_build_evaluation_input` runs synchronously after `_update_tracker` returns. No other task reads `_monthly_peak_kw`.

**Cold-start / startup-grace coverage (mandatory):**

The cold-start window for this story is the gap between process boot and the moment `_monthly_peak_kw` has a hydrated value. With this story, that gap shrinks from "≤15 min (until first interval rollover)" to "the duration of step 4c's DB SELECT" — typically <10 ms.

| Phase | t | State of `_monthly_peak_kw` | What is published / logged |
|---|---|---|---|
| Process boot | 0 | (undefined — `ControlLoop` not yet constructed) | uvicorn pre-lifespan stdlib log lines (pre-existing limitation) |
| Lifespan step 1 | small | (undefined) | `configure_logging` complete; JSON logging active |
| Lifespan step 2 | +tens of ms | (undefined) | `migrations_applied` |
| Lifespan step 3 | +tens to hundreds of ms | (undefined) | `clock_valid` or `clock_unsynced` warning |
| Lifespan step 4 | +ms | (undefined) | aiosqlite connection open |
| Lifespan step 4a | +ms | (undefined) | capability validator returns |
| Lifespan step 4b | +ms | (undefined) | `constraints_provider.hydrate()` complete; `constraints_hydrate_seeded_from_settings` if cold-start |
| **Lifespan step 4c (NEW)** | **+ms** | **→ (about to be assigned)** | **`monthly_peak_hydrated initial_monthly_peak_kw=<X>`** |
| Lifespan step 5 | +ms | (still undefined — `ControlLoop` not constructed yet) | `system_ready` |
| ... services constructed | +ms | (still undefined) | `watchdog_started`, `session_cleanup_task_started`, `event_log_pruning_task_started` |
| `ControlLoop(...)` construct | +ms | **= `<X>` (hydrated value)** | `control_loop_started` |
| First tick scheduled | +ms | = `<X>` | (no new log) |
| First `_tick()` runs | +`control_loop_interval_seconds` (10s default) | = `<X>` (still seed value) | StateStore publish, `evaluation_cycle_complete` at debug |
| First `_update_tracker` rollover | +up to 15 min | → refreshed (post-rollover MAX) | (no log on refresh) |
| **First `loop_liveness.mark_cycle_complete()`** | end of first tick | = `<X>` | watchdog observes progress; system in steady-state operation |

**Pre-step-4c window:** any consumer that tries to read `_monthly_peak_kw` before step 4c does not exist — there is no `ControlLoop` instance yet. The window is structurally empty.

**Step-4c-to-first-rollover window:** `_monthly_peak_kw == <hydrated seed>`. This is the window the story's fix targets. Pre-fix, this window held `0.0` (the bug). Post-fix, it holds the real current monthly peak from the DB — which is the goal.

**First-rollover-and-beyond:** Existing 8-1 behavior. `_update_tracker` writes the just-completed interval, then re-queries `MAX`. From here on the seed is irrelevant; the steady-state refresh path owns the value.

#### R4 — Cancellation ownership map

| Async operation | CancelledError owner | Cancellation audit owner | Cleanup owner | Reporting field semantics under cancel |
|---|---|---|---|---|
| `await energy_repo.get_current_monthly_peak_kw()` (lifespan step 4c) | lifespan task — an outer SIGTERM during startup propagates `CancelledError` up through the lifespan generator, which causes the lifespan `try/finally` to run `close_database()` | none — this is a startup-phase read; no audit semantics exist (no `DEVICE`, `CONSTRAINT`, `SYSTEM` audit event types apply) | the lifespan's existing outer `try/finally` (line 406–408) handles aiosqlite connection cleanup via `close_database()` | the `initial_monthly_peak_kw` local variable is never assigned; `ControlLoop` is never constructed; `_control_loop_task` is never created; process exits cleanly |
| `await energy_repo.write_peak_interval(...)` (control-loop runtime — UNCHANGED by this story) | control-loop task | RetryPolicy / engine layer (out of scope for 9.0d) | aiosqlite + database-module write lock from 9.0b's D1→P7 patch | unchanged |
| `await energy_repo.get_current_monthly_peak_kw()` (control-loop runtime — UNCHANGED) | control-loop task | none | none | unchanged |

**No new audit emissions are introduced by this story.** The lifespan-step-4c hydrate is a one-shot read with no audit semantics. The only new structured log is `monthly_peak_hydrated` at info level (not an audit event; just an operational log).

**No new cancellation paths are introduced** — the runtime control loop's cancellation behavior is unchanged. The startup hydrate is a single `await` inside the existing lifespan body, which inherits the lifespan's existing cancellation semantics.

#### R5 — Before-first-successful-cycle lifecycle review

Concrete trace from process boot to first successful normal evaluation cycle, focusing on what changes vs. the pre-9.0d trace (the broader trace was established in 9.0b R5):

| Phase | Event | State that this story affects | What relaxes |
|---|---|---|---|
| Boot | uvicorn starts; lifespan generator entered | none | (pre-existing limitations) |
| Lifespan step 1 | `configure_logging(settings.log_level)` | none | |
| Lifespan step 2 | Alembic `upgrade head` | none — `peak_intervals` already exists (migration 0007) | |
| Lifespan step 3 | Clock check | **subtle:** `now(UTC)` semantics propagate into step 4c; if `clock_unsynced`, the hydrate may read the wrong month. Warning is logged. | tariff tz logic temporarily relaxed (existing) |
| Lifespan step 4 | `init_database` opens aiosqlite connection | none | |
| Lifespan step 4a | `validate_capability_registry_alignment()` | none | |
| Lifespan step 4b | `active_constraints_provider.hydrate()` | none | |
| **Lifespan step 4c (NEW)** | **`initial_monthly_peak_kw = await energy_repo.get_current_monthly_peak_kw()`** | **the variable is bound; `monthly_peak_hydrated` log fires** | **none — no behavior is temporarily relaxed; the value is read-only at this point** |
| Lifespan step 5 | `mark_ready()` | none | systemd considers process ready |
| Lifespan step 6 | Watchdog task started | none | |
| Lifespan steps … | services constructed | **`ControlLoop(...)` constructor runs with `initial_monthly_peak_kw=<X>`; `self._monthly_peak_kw` is assigned to the hydrated value (previously: hardcoded `0.0`)** | |
| Lifespan step 11 | `_control_loop_task = asyncio.create_task(...)` | none | |
| First tick | `_tick()` runs | **`_build_evaluation_input` constructs `PeakContext.current_monthly_recorded_peak_kw=<X>` (previously: `0.0`)** | (pre-existing first-tick `operating_mode=None` behavior — unrelated) |
| First evaluation | `evaluate_cycle()` runs | **peak-limiting rule uses the real monthly peak from the very first cycle** | none |
| **First rollover (≤15 min later)** | `_update_tracker` writes the completed interval; re-queries `MAX` | `_monthly_peak_kw` refreshes via existing 8-1 path (unchanged) | |
| **Marker for normal operation** | `loop_liveness.mark_cycle_complete()` fires after the first complete tick | normal steady state | |

**During the lifespan-step-4c window:** the variable does not exist yet on the `ControlLoop` (the loop is not constructed yet). After step 4c completes, the value lives only in a local lifespan variable until passed to `ControlLoop(...)`. There is no consumer between step 4c and `ControlLoop` construction, so there is no race.

**Between `ControlLoop` construction and first tick:** `_monthly_peak_kw == <X>` and no method has run yet. The next read is in `_tick → _build_evaluation_input` ≥10s later (or whatever `control_loop_interval_seconds` is set to).

**Normal-operation marker:** unchanged from prior stories — `loop_liveness.mark_cycle_complete()` after the first end-to-end tick.

#### R6 — Source-of-truth ownership per datum

| Datum | Single owner | Read path | Drift risk |
|---|---|---|---|
| `peak_intervals` table rows | DB (`MAX(avg_power_kw)` query) | `EnergyRepo.get_current_monthly_peak_kw()` | None — DB is single source |
| `_monthly_peak_kw` (in-memory cache, runtime) | `ControlLoop._monthly_peak_kw` (private attribute) | `_build_evaluation_input` reads it; `_update_tracker` writes it after each interval rollover | **Bounded drift by design:** the cache is refreshed once per interval (~15 min). Within an interval, the cache may lag the DB if a manual `INSERT OR REPLACE` against `peak_intervals` happens (no such writer exists today except `_update_tracker` itself; the loop is the sole writer to that table). |
| `_monthly_peak_kw` (initial seed, this story) | Lifespan-local `initial_monthly_peak_kw` variable | Assigned at `ControlLoop.__init__`; never reassigned from this seed (subsequent rollover overwrites it) | **Eliminated:** previously the seed was hardcoded `0.0`; this story makes the seed equal to the DB read at startup, eliminating the seed-vs-DB drift that produced the 8-1 deferred finding. |

**Drift risks called out:**

- **Seed-vs-DB drift (pre-9.0d):** structurally eliminated by AC4 — the seed *is* the DB read.
- **In-memory cache vs. DB during a single interval:** unchanged from 8-1 design; the cache is refreshed per rollover, not per tick. Operationally acceptable because the peak-limit rule operates on a single instantaneous value, not a moving average; one refresh per interval is the contract.
- **Two `EnergyRepo` instances drifting:** structurally eliminated by AC4 — the lifespan hoists `EnergyRepo()` to a local and passes the same instance to `ControlLoop`. (Functionally moot today because `EnergyRepo.__init__` uses the global `get_connection()` singleton, but the single-instance discipline locks the invariant.)
- **Cross-process / multi-worker drift:** not applicable (single-process v1; documented in 9.0b Dev Notes "Multi-process futures").

#### R7 — Deferred-findings triage

Scanned `_bmad-output/implementation-artifacts/deferred-work.md` (290 lines, all sections); items whose component or invariant overlaps with this story:

| Finding (bracket ref) | Classification | Rationale |
|---|---|---|
| `[src/open_ems/engine/control_loop.py:54]` (8-1) — "`_monthly_peak_kw` not hydrated from DB on process restart" | **must-resolve-in-this-story** | This IS the resolve. AC4 + AC8 close it. Task 5 will annotate this entry in deferred-work.md as resolved by 9.0d. |
| `[src/open_ems/storage/repositories/energy_repo.py:37-41]` (8-1) — "ISO string comparison for monthly peak boundary fragile against non-UTC formats" | **safe-during-this-story** | The hydrate call uses the same query the runtime already uses every interval rollover. We do not make the storage-format hazard worse, and we do not fix it. The defensive fix (schema-level UTC-suffix check) is acceptable-post and would be a sweep across `write_peak_interval` callers, not a hydrate concern. |
| `[src/open_ems/engine/control_loop.py:107]` (8-1) — "Multi-interval stall silently drops intermediate `CompletedInterval`s" | **safe-during-this-story** | Tracker-level concern (`tracker.update()` returns one interval per call; >30 min stall drops intermediate windows). Hydrate semantics are unrelated. Pre-existing from Story 8.0b. |
| `[src/open_ems/engine/control_loop.py:73-76]` (8-2) — "First tick publishes `operating_mode=None`" | **safe-during-this-story** | Unrelated to monthly peak. The 8-2 first-tick behavior is preserved by AC3 (no changes to `_handle_evaluation_result` or `_last_mode`). |
| `[src/open_ems/engine/control_loop.py:_extract_grid_power]` (8-2) — "Degraded grid meter returns `0.0` to PartialIntervalTracker" | **safe-during-this-story** | Data-quality concern; `tick_quality="incomplete"` flag is the existing mitigation. Orthogonal to hydrate. |
| `[src/open_ems/engine/control_loop.py:84-98]` (8-1) — "Sequential adapter polling blocks all adapters when one is slow" | **acceptable-post-this-story** | Polling architecture; nothing to do with the hydrate path. Story 9.4 / Epic 10+ scope. |
| `[src/open_ems/engine/control_loop.py:33]` (8-1) — "Adapter timeout (10s) not bounded by `control_loop_interval_seconds`" | **acceptable-post-this-story** | Timing concern. Out of scope. |
| `[src/open_ems/engine/control_loop.py:61]` (8-1) — "No deadline-relative sleep" | **acceptable-post-this-story** | Cadence concern. Out of scope. |
| `[src/open_ems/engine/control_loop.py:77-80]` (8-1) — "Non-cooperative adapters may block shutdown despite cancel" | **acceptable-post-this-story** | Shutdown concern. Out of scope. |
| `[src/open_ems/web/app.py:229-236]` (9-0b W7) — "AC7 fail-loud `SystemExit(1)` branch not exercised by any test" | **safe-during-this-story** | This story adds an **analogous** test (AC8 #2) for the new step-4c fail-loud branch. The 9-0b deferred W7 case (step-4b's fail-loud path) remains separately deferred — extending the test to also cover the 9-0b path is *not* required by 9.0d's scope. A reviewer may decide to close W7 by analogy/proximity, but that is discretionary, not a hard requirement. |
| `[tests/conftest.py:51-63]` (9-0b W3) — "`_reset_structlog_config` autouse fixture has session-wide blast radius" | **safe-during-this-story** | Test hygiene; this story's tests are not specifically vulnerable to the leakage (we use `unittest.mock.patch` on the module-level logger, per 9.0b Debug Log lesson). |
| `[tests/fixtures/active_constraints.py:749]` (9-0b W1) — "Test fixtures bypass `hydrate()` contract by writing private `_current` field" | **safe-during-this-story** | Test-fixture pattern; this story does not introduce new private-field writes for monthly peak (the seed is a public constructor argument). |
| `[tests/fixtures/active_constraints.py:748]` (9-0b W2) — "`MagicMock(spec=ConfigRepo)` accepts unconfigured method calls silently" | **safe-during-this-story** | Same hygiene concern; no new mocks of `EnergyRepo` introduced (existing `AsyncMock(spec=EnergyRepo)` patterns are reused). |

No findings classified as `must-resolve` outside the explicit 9.0d target (`[control_loop.py:54]`).

**Explicit confirmation:** no findings overlap monthly-peak hydrate semantics in a way that would require additional resolution within this story's scope.

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#Peak-interval-persistence] — `peak_intervals` schema; billing-vs-control invariant; partial-window projection NOT persisted.
- [Source: _bmad-output/planning-artifacts/architecture.md#Project-structure] — `energy_repo.py` placement.
- [Source: _bmad-output/implementation-artifacts/epic-8-retro-2026-05-10.md#P2] — origin and gate semantics for this story (hard gate before Story 9.4).
- [Source: _bmad-output/implementation-artifacts/8-1-implement-the-runtime-control-loop-with-adapter-polling-and-statestore-publishing.md#AC3] — Finalize-before-evaluate invariant + `_monthly_peak_kw` refresh after `write_peak_interval`.
- [Source: _bmad-output/implementation-artifacts/8-1-implement-the-runtime-control-loop-with-adapter-polling-and-statestore-publishing.md#AC7] — `EnergyRepo` contract: `get_current_monthly_peak_kw()` returns `0.0` on empty result.
- [Source: _bmad-output/implementation-artifacts/9-0b-db-backed-active-constraints-reload.md#AC7] — lifespan-hydrate pattern precedent (mirrored here for monthly peak).
- [Source: _bmad-output/implementation-artifacts/9-0c-capability-profile-failure-semantics.md#AC5] — lifespan fail-loud + `close_database` cleanup pattern (AC8 #2 mirrors this).
- [Source: src/open_ems/engine/control_loop.py:80-114] — current `ControlLoop.__init__` and `run` implementation (the surface this story modifies).
- [Source: src/open_ems/engine/control_loop.py:158-172] — current `_update_tracker` (unchanged; preserved by AC3).
- [Source: src/open_ems/storage/repositories/energy_repo.py:36-46] — current `get_current_monthly_peak_kw()` implementation.
- [Source: src/open_ems/web/app.py:217-372] — current lifespan ordering; step 4c (this story) inserts between current line ~265 and ~356.
- [Source: tests/integration/web/test_app_startup.py:22-95] — lifespan-integration test pattern that AC8 mirrors.
- [Source: tests/unit/engine/test_control_loop.py:107-139] — `_control_loop()` factory and `make_active_constraints_provider` fixture (AC7 tests reuse them).

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Amelia, BMad dev-story workflow)

### Debug Log References

- Baseline (pre-story): pytest = 1071 passed; ruff check / ruff format / mypy all clean.
- First integration-test run failed with `TypeError: CompletedInterval.__init__() got an unexpected keyword argument 'interval_end_utc'`. Root cause: misread of the `CompletedInterval` dataclass — only `interval_start_utc`, `avg_power_kw`, `sample_count` exist (no `interval_end_utc`). Fixed by removing the spurious kwarg; the interval_start at `month_start + timedelta(hours=1)` is already 15-min aligned, so no further adjustment was needed.
- Final: pytest = 1081 passed (baseline 1071 + 7 new unit-test entries [AC7 #1, #2, and five parametrized AC7 #3 cases] + 3 new lifespan integration tests = 10 new); ruff/format/mypy all clean.

### Completion Notes List

- AC1 — `EnergyRepo.get_current_monthly_peak_kw()` reused unchanged; no schema or repo changes.
- AC2 — `ControlLoop.__init__` now accepts keyword-only `initial_monthly_peak_kw: float = 0.0`; validator (`< 0.0` or `not math.isfinite`) raises `ValueError("initial_monthly_peak_kw must be a finite non-negative float")` BEFORE assignment. `import math` added.
- AC3 — `_update_tracker` unchanged; the seed is overwritten only when an interval rollover triggers the existing post-rollover `MAX` re-query. No double-refresh on first tick.
- AC4 — Lifespan step 4c inserted between step 4b (constraints hydrate) and ControlLoop construction in `src/open_ems/web/app.py`. `EnergyRepo()` hoisted into a local; same instance passed to `ControlLoop` via `energy_repo=energy_repo`. `monthly_peak_hydrated` info log with `initial_monthly_peak_kw` + `component="startup"` fires exactly once.
- AC5 — Fail-loud: any exception from `get_current_monthly_peak_kw()` is converted to `logger.error("startup_failed", reason="monthly_peak_hydrate_failed", exc_info=True, component="startup")` + `raise SystemExit(1) from None`. Block lives inside the outer `try:` so `close_database()` runs in `finally`.
- AC6 — Ordering structurally enforced: ControlLoop construction cannot run without `initial_monthly_peak_kw`. AC8 #1's `asyncio.create_task` spy verifies the runtime ordering as a regression guard.
- AC7 — Three new unit tests in `tests/unit/engine/test_control_loop.py`: seed reaches `peak_context.current_monthly_recorded_peak_kw` on the very first cycle without a tracker rollover; default keeps `_monthly_peak_kw == 0.0` (factory diff-free); parametrized validator test covers `-0.001`, `-1.0`, `nan`, `+inf`, `-inf` with named pytest IDs.
- AC8 — Three new integration tests in `tests/integration/web/test_app_startup.py`: pre-populated peak_intervals row hydrates and log fires before `control_loop` task scheduled; patched `EnergyRepo.get_current_monthly_peak_kw` → SystemExit(1) + `startup_failed reason="monthly_peak_hydrate_failed"` + `close_database` runs via `wraps=real_close_database` spy; empty peak_intervals → seed 0.0, no SystemExit (cold-start branch, real-DB `MAX(...)` over zero rows).
- AC9 — "Story 9.0d update" paragraph appended to `control_loop.py` module docstring. `_bmad-output/implementation-artifacts/deferred-work.md` entry `[control_loop.py:54]` annotated as resolved by this story (historical record preserved).
- AC10 — All four gates green at story close. No regressions.

### File List

- `src/open_ems/engine/control_loop.py` — UPDATED (constructor `initial_monthly_peak_kw` + validator; `import math`; module-docstring "Story 9.0d update" paragraph).
- `src/open_ems/web/app.py` — UPDATED (lifespan step 4c hydrate block; `EnergyRepo()` hoisted; `ControlLoop(..., initial_monthly_peak_kw=initial_monthly_peak_kw)`).
- `tests/unit/engine/test_control_loop.py` — UPDATED (three new tests + `_control_loop_with_seed` helper; `import math`, `import pytest`).
- `tests/integration/web/test_app_startup.py` — UPDATED (three new lifespan tests + `_project_root` / `_seed_lifespan_env` helpers; module-docstring 9.0d paragraph; new imports for `aiosqlite`, `structlog`, `app_module`, `CompletedInterval`, `Settings`, `init_database`, `EnergyRepo`, `datetime`/`timedelta`/`UTC`/`pathlib`/`asyncio`).
- `_bmad-output/implementation-artifacts/deferred-work.md` — UPDATED (`[control_loop.py:54]` entry annotated as resolved by Story 9.0d, 2026-05-11).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — UPDATED (`9-0d-restart-hydration-of-monthly-peak`: `ready-for-dev` → `in-progress` → `review`; `last_updated` annotation).
- `_bmad-output/implementation-artifacts/9-0d-restart-hydration-of-monthly-peak.md` — UPDATED (Tasks/Subtasks checkboxes, Status, Dev Agent Record).

### Change Log

| Date | Change | Notes |
|---|---|---|
| 2026-05-11 | Constructor seed (`ControlLoop.__init__ initial_monthly_peak_kw` + validator) | AC2; structurally enforces hydrate-before-construct ordering (AC6). |
| 2026-05-11 | Lifespan step 4c hydrate + fail-loud SystemExit | AC4, AC5; `monthly_peak_hydrated` info log + `startup_failed reason="monthly_peak_hydrate_failed"`. |
| 2026-05-11 | Hoist `EnergyRepo()` into a local; reused by hydrate + ControlLoop | AC4; eliminates two-instance drift surface. |
| 2026-05-11 | Three unit tests for constructor seed (incl. parametrized validator) | AC7. |
| 2026-05-11 | Three lifespan integration tests (hydrate ordering, fail-loud, empty-table) | AC8; mirrors 9.0c `wraps=real_close_database` and 9.0b mock-logger patterns. |
| 2026-05-11 | Documentation: module-docstring update + deferred-work annotation | AC9. |
| 2026-05-11 | Status: ready-for-dev → review | AC11 entry into adversarial 3-layer review. |
| 2026-05-11 | Code review patch: corrupt-value guard at lifespan step 4c | Closes review decision-needed ECH-1; adds `startup_failed reason="monthly_peak_corrupt_value"` log + parametrized integration test for `+inf`/`NaN`/negative; 1084 tests pass. |
| 2026-05-11 | Status: review → done | All decision-needed and patch findings resolved; zero unresolved HIGH/MEDIUM (AC11 gate closed). |

### Review Findings

_Code review run 2026-05-11. Adversarial 3-layer review (Blind Hunter / Edge Case Hunter / Acceptance Auditor) per AC11. Total: 17 findings → 1 decision-needed (resolved by patch), 0 patches outstanding, 4 deferred, 12 dismissed as noise. Zero unresolved HIGH/MEDIUM → AC11 merge gate is closed; status advanced to `done`._

- [x] [Review][Decision→Patched] **`+inf` from `peak_intervals` row bypasses lifespan structured-log contract** — `EnergyRepo.get_current_monthly_peak_kw()` can return `float('inf')` if any `peak_intervals` row contains `avg_power_kw = +inf` (no `CHECK` constraint on the column; SQLite `MAX(inf, …) = inf`). The lifespan logs `monthly_peak_hydrated initial_monthly_peak_kw=inf component=startup` (no warning), exits the `try/except` cleanly, then `ControlLoop.__init__` raises `ValueError("initial_monthly_peak_kw must be a finite non-negative float")` OUTSIDE the `try:` of step 4c. The outer lifespan `finally: close_database()` still runs (good), but the operator sees an unhandled `ValueError` traceback instead of a structured `startup_failed reason=...` log — degraded observability vs. AC5's intent for sibling failure paths. Probability of `+inf` in the table is low (no writer produces it today). **Resolution (2026-05-11):** patched — `src/open_ems/web/app.py:283-290` now validates `initial_monthly_peak_kw` immediately after the hydrate returns; non-finite or negative values emit `startup_failed reason="monthly_peak_corrupt_value" initial_monthly_peak_kw=<value> component="startup"` and `SystemExit(1)`, symmetric with steps 4a / 4b. New parametrized integration test `test_lifespan_aborts_with_system_exit_on_monthly_peak_corrupt_value` (`tests/integration/web/test_app_startup.py`) covers `+inf`, `NaN`, and negative cases — asserts SystemExit, structured log payload (including offending value), absence of the success log, and that `close_database()` still runs on this failure path. Sources: edge-hunter ECH-1.
- [x] [Review][Defer] **`structlog.reset_defaults()` in three new lifespan tests has session-wide blast radius** [`tests/integration/web/test_app_startup.py:156,233,273`] — deferred, pre-existing (matches Story 9.0b W3 deferred-finding `[tests/conftest.py:51-63]`). The new tests follow the project's established pattern of resetting structlog defaults at test entry to neutralize the cached-logger leakage that breaks `structlog.testing.capture_logs()`. Scope the fixture to specific files in a future test-hygiene sweep. Sources: blind-hunter BH-5, edge-hunter implicit.
- [x] [Review][Defer] **Microsecond month-boundary skew between hydrate `now()` and tracker `now()`** [`src/open_ems/storage/repositories/energy_repo.py:37` + `src/open_ems/engine/control_loop.py:120`] — deferred, acceptable-post. Process restart spanning the exact UTC month boundary (millisecond window once/month) can have hydrate read the OLD month while the tracker initializes in the NEW month, seeding `_monthly_peak_kw` with last month's max. Self-corrects on the first interval rollover (≤15 min). Mitigation (accept an explicit `at: datetime` parameter through `get_current_monthly_peak_kw`) is a small, separate refactor. Sources: edge-hunter ECH-2.
- [x] [Review][Defer] **ISO lexicographic comparison fragility in `get_current_monthly_peak_kw` WHERE clause** [`src/open_ems/storage/repositories/energy_repo.py:36-46`] — deferred, pre-existing (already tracked under Story 8.1 deferred-finding "ISO string comparison for monthly peak boundary fragile against non-UTC formats" at `[energy_repo.py:37-41]`). Currently safe — all writers go through `CompletedInterval.interval_start_utc.isoformat()` which produces `+00:00` suffix uniformly. Sources: edge-hunter ECH-3.
- [x] [Review][Defer] **AC8 #1 integration test exercises the full lifespan body unpatched** [`tests/integration/web/test_app_startup.py:163-218`] — deferred, stability hint (not a 9.0d correctness gap). The ordering test runs real `_tick()` cycles with `adapters={}` before the lifespan teardown cancels everything; today this is a no-op, but a future side effect added inside `_tick` could turn this into a flake without changing 9.0d code. Patch `ControlLoop.run` to a no-op coroutine in this test when the next test-stability sweep happens. Sources: edge-hunter ECH-7.

**Dismissed (12, summary):** `raise … from None` in step 4c (BH-1 — `exc_info=True` carries the root cause; matches AC5 + Stories 9.0b/9.0c verbatim); spy-test brittleness against multiple `create_task(name="control_loop")` (BH-2 — only one such site exists in the codebase); failure / empty-table tests "skip migrations" (BH-3, BH-4 — lifespan runs migrations at step 2 itself); "ordering by comment only" (BH-6 — false; constructor arg makes ordering structural per AC6); helper `adapters={}` divergence (BH-7 — validator runs before `_adapters` assignment); default `0.0` could mask future caller-forgot-kwarg (BH-8 — explicitly mandated by AC2 for fixture compat); `== 17.3` float equality (BH-9 — round-trip is lossless, no arithmetic); `close_database` ordering not asserted relative to failure log (BH-10 — no invariant requires it); self-referential parametrize sanity assertion (BH-11 — harmless); AC8 #2 doesn't negatively assert success-log absent (AA-1 — `raise SystemExit(1)` placement before `logger.info` structurally guarantees it); AC8 #1 ordering assertion is one-sided / only on `create_task` (AA-2 — acceptable per AC11 layered-review delegation; ECH cleared via lifespan trace).
