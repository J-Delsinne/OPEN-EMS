# Story 9.0b: DB-Backed Active-Constraints Reload

Status: done

> **Origin:** Epic 8 retrospective (2026-05-10) — Story 9.0b is a hard-gate prep story (P1 in the retro action items). Until 9.0b is `Status: done` with all reviews resolved, Story 9.3 (constraint configuration) cannot be authored, and Story 9.1 should not begin (strongly preferred gate). Story 9.3's dev notes will cite the contracts produced here verbatim.
>
> **A2-triggered:** True. Matched triggers: **T3** (persistence + recovery — DB-backed state across process restart, durable config_version, reload on activation), **T6** (deployment / restart behavior — installer wizard restarts trigger reload of constraints; cold-start invariants are user-visible). Partial: **T1** (lifecycle — provider goes from `unhydrated` → `hydrated` and is the structural gate that makes PolicyGuard safe to run), **T7** (installer workflow orchestration — the activation endpoint built in 9.3 calls into the surface this story creates). All R1–R7 orchestration artifacts are mandatory and present below.

## Story

As a developer composing the installer site-setup wizard on top of the runtime safety pipeline,
I want `PolicyGuard` and the decision engine to read `peak_limit_kw` and `battery_reserve_floor_percent` from a DB-backed `ActiveConstraintsProvider` whose snapshot is hydrated at lifespan startup and atomically reloaded on installer activation — never from process-startup `Settings` at runtime,
so that constraint changes from the installer wizard (Story 9.3) take effect on the next evaluation cycle without a process restart, AND so that the source-of-truth for two safety-critical values is a single in-memory snapshot owned by one named component instead of a hidden read from `Settings` scattered across PolicyGuard and the control loop.

## Acceptance Criteria

### AC1 — Schema: `active_constraints` table

**GIVEN** the installer activates a new constraint set in Story 9.3
**WHEN** the activation transaction commits
**THEN** a new row is inserted (insert-only — never UPDATE) into a new `active_constraints` table with columns:

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `peak_limit_kw` | REAL | NOT NULL, CHECK > 0 |
| `battery_reserve_floor_percent` | REAL | NOT NULL, CHECK >= 0 AND <= 100 |
| `config_version` | INTEGER | NOT NULL, UNIQUE |
| `activated_at` | TEXT | NOT NULL (UTC ISO-8601) |
| `actor` | TEXT | NOT NULL, CHECK IN ('system', 'installer') |

**AND** an index `ix_active_constraints_config_version` is created on `config_version DESC` to support the "current row = highest config_version" query in O(log N).

**AND** the migration is `migrations/versions/0008_add_active_constraints_table.py` and follows the existing Alembic upgrade/downgrade pattern (see `0006_add_config_audit_log_table.py` and `0007_add_peak_intervals_table.py` for the exact style).

**AND** the table schema accommodates Story 9.3's future `draft_constraints` table without rework — `draft_constraints` is explicitly out of scope here (a single-row staged-draft companion is 9.3's responsibility per architecture BAD-3). 9.0b lays only the activated path.

---

### AC2 — `ConfigRepo.get_active()` and `ConfigRepo.activate()`

**GIVEN** a new `src/open_ems/storage/repositories/config_repo.py` is added following the repository pattern (see `config_audit_repo.py` for the precedent)
**WHEN** `ConfigRepo.get_active()` is awaited
**THEN** it returns the row with the maximum `config_version` from `active_constraints`, mapped to a typed `ActiveConstraints` Pydantic model — OR `None` if the table is empty (cold-start path before any activation has occurred).

**AND** `ConfigRepo.activate(input: ActiveConstraintsInput, *, actor: Literal["system", "installer"]) -> int` performs the activation in a single SQLite transaction:
1. Compute `next_config_version = MAX(config_version) + 1` (defaulting to 1 if empty)
2. INSERT the new row
3. INSERT one `config_audit_log` row per changed field (delegated to `ConfigAuditRepo.append_activation` — the existing repo from Story 6.3) — using audit field names `peak_consumption_limit` and `battery_reserve_floor` (the audit vocabulary established in `SAFETY_RELEVANT_CONFIG_FIELDS`)
4. COMMIT

**AND** the entire flow above runs inside `BEGIN IMMEDIATE` so that `config_audit_log.config_version` and `active_constraints.config_version` cannot diverge under any failure mode (this is structural; closes the spirit of Story 6.3's deferred non-atomic-config-version finding for the activation path).

**AND** `activate()` returns the assigned `config_version` (int).

**AND** `actor` is validated against the `CHECK IN ('system', 'installer')` set; an unknown actor raises `ValueError` BEFORE any DB write.

**AND** if `previous_value == new_value` for a given field, no `config_audit_log` row is emitted for that field (audit captures *changes*, not no-ops). If both fields are unchanged, `activate()` raises `ValueError("activate called with no changed fields")` — a no-op activation is a programmer error.

---

### AC3 — `ActiveConstraints` typed model

**GIVEN** `src/open_ems/core/constraints.py` is added
**WHEN** the `ActiveConstraints` Pydantic model is constructed
**THEN** it is `frozen=True, extra="forbid"` and carries:

| Field | Type | Constraint |
|---|---|---|
| `peak_limit_kw` | float | `gt=0.0` |
| `battery_reserve_floor_percent` | float | `ge=0.0, le=100.0` |
| `config_version` | int | `ge=1` |
| `activated_at` | datetime | timezone-aware UTC (reuse `require_utc` from `engine/models.py` or equivalent) |

**AND** `ActiveConstraintsInput` (a sibling Pydantic model with only the two value fields, no `config_version` / `activated_at`) is the input type for `ConfigRepo.activate()`.

**AND** the file lives under `core/` so it is importable from both `engine/` and `services/` without violating the engine→core import boundary (architecture import-rule).

---

### AC4 — `ActiveConstraintsProvider` in-memory snapshot owner

**GIVEN** `src/open_ems/services/active_constraints.py` defines `ActiveConstraintsProvider`
**WHEN** the provider is constructed and used
**THEN** the contract is:

```python
class ActiveConstraintsProvider:
    def __init__(self, *, repo: ConfigRepo, settings: Settings) -> None: ...
    async def hydrate(self) -> None: ...   # called once at lifespan startup
    async def reload(self) -> None: ...    # called by Story 9.3 after activation commit
    def get(self) -> ActiveConstraints: ...  # synchronous hot-path read
    @property
    def is_hydrated(self) -> bool: ...
```

**AND** `hydrate()` semantics:
- Read the active row via `repo.get_active()`.
- If a row exists → cache it.
- If no row exists (fresh DB; no installer activation has occurred yet) → seed an in-memory `ActiveConstraints` from `Settings.peak_limit_kw` and `Settings.battery_reserve_floor_percent` with `config_version=0` and `activated_at=datetime.now(UTC)`. **The DB is NOT written**; seed values are runtime-only until an installer activation persists real values.
- Set `is_hydrated = True`.

**AND** `reload()` semantics:
- Read the active row via `repo.get_active()`.
- If a row exists → atomically replace the in-memory snapshot.
- If no row exists → keep the existing snapshot unchanged (a reload after activation should always find a row; finding none post-hydrate would be a DB regression we do NOT want to mask by re-seeding from Settings — log a `constraints_reload_found_empty` warning and do nothing).

**AND** `get()` is **synchronous**, returns the cached `ActiveConstraints`, and raises `RuntimeError("ActiveConstraintsProvider.get() called before hydrate()")` if `is_hydrated is False`. PolicyGuard's hot path MUST never block on DB I/O.

**AND** snapshot replacement is atomic — `self._current = new_constraints` is a single CPython attribute assignment; PolicyGuard's `get().peak_limit_kw` either sees the old immutable snapshot or the new one, never a torn read.

**AND** `reload()` is serialized: an `asyncio.Lock` guards the `repo.get_active()` + assignment so two concurrent reload requests cannot interleave with each other (defense in depth — Story 9.3's activation endpoint is the only caller in v1, but the lock makes the contract drift-resistant).

---

### AC5 — `PolicyGuard` reads from provider, not Settings

**GIVEN** `PolicyGuard.__init__` currently accepts `settings: Settings`
**WHEN** Story 9.0b refactors PolicyGuard
**THEN** `__init__` accepts `active_constraints: ActiveConstraintsProvider` in addition to (or replacing — see AC10) the `settings` parameter for the two refactored fields:
- `_check_safety_constraints` reads `self._active_constraints.get().battery_reserve_floor_percent` instead of `self._settings.battery_reserve_floor_percent`.
- `_check_safety_constraints` reads `self._active_constraints.get().peak_limit_kw` instead of `self._settings.peak_limit_kw`.

**AND** PolicyGuard's safety-check logic is otherwise **unchanged** — order of checks, rejection reasons, audit emission, capability gate. This story is a source-substitution refactor only, not a behavior change.

**AND** every existing PolicyGuard test in `tests/unit/engine/test_policy_guard.py` continues to pass after a single signature update — the test fixture builds an `ActiveConstraintsProvider` seeded from a `Settings(_env_file=None)` instance and passes it. A regression in any PolicyGuard rejection reason is treated as a HIGH-severity finding.

---

### AC6 — `ControlLoop` reads from provider, not Settings

**GIVEN** `ControlLoop._build_evaluation_input` reads `self._settings.peak_limit_kw` and `self._settings.battery_reserve_floor_percent`
**WHEN** Story 9.0b refactors ControlLoop
**THEN** ControlLoop accepts `active_constraints: ActiveConstraintsProvider` in `__init__` and reads both values from `self._active_constraints.get()` inside `_build_evaluation_input`.

**AND** PolicyGuard and ControlLoop read from the **same provider instance** — `app.py` constructs one `ActiveConstraintsProvider` and passes it to both. This eliminates the drift risk where PolicyGuard sees the new value but the engine sees the old one (the source-of-truth fragmentation explicitly called out in R6).

**AND** `ControlLoop._build_evaluation_input` continues to read all OTHER values (control_loop_interval_seconds, watchdog_*, command_max_retries, etc.) from `self._settings`. Only the two safety constraints move; nothing else changes.

---

### AC7 — Lifespan hydration ordering (cold-start invariant)

**GIVEN** `src/open_ems/web/app.py` lifespan currently constructs PolicyGuard and ControlLoop after `init_database()`
**WHEN** Story 9.0b adds the provider
**THEN** the lifespan executes provider construction + hydration BEFORE PolicyGuard / ControlLoop construction:

```
... init_database(settings.db_path) ...
config_repo = ConfigRepo()
active_constraints_provider = ActiveConstraintsProvider(repo=config_repo, settings=settings)
await active_constraints_provider.hydrate()   # ← MUST run before PolicyGuard / ControlLoop construction
app.state.active_constraints_provider = active_constraints_provider
... existing PolicyGuard / ControlLoop construction, now passing the provider ...
```

**AND** the `app.state.active_constraints_provider` attribute is set so Story 9.3's activation endpoint can call `request.app.state.active_constraints_provider.reload()` without re-resolving the dependency graph.

**AND** if `hydrate()` raises (DB read error), the lifespan logs `startup_failed` with `reason="constraints_hydrate_failed"` and `raise SystemExit(1)`. A process that cannot determine its safety constraints is a process that must not start the control loop. This matches the existing migration-failure handling pattern in app.py:196.

**AND** the provider is hydrated EXACTLY ONCE per process; `hydrate()` is idempotent (calling it a second time is a no-op + logs a warning). `reload()` is the post-hydrate update path.

---

### AC8 — End-to-end reload trigger picks up new DB state

**GIVEN** a running process with an `ActiveConstraintsProvider` hydrated at startup
**WHEN** a new row is inserted into `active_constraints` via `ConfigRepo.activate(...)` AND `provider.reload()` is awaited
**THEN** the next call to `provider.get()` returns the new constraint values, and the next PolicyGuard rejection decision uses the new values.

**AND** `provider.get().config_version` reflects the newly assigned version (proves the snapshot was actually replaced — not a memoization bug).

**AND** the integration test in `tests/integration/engine/test_constraints_reload.py` exercises this end-to-end:
1. Start with `peak_limit_kw=25.0`, hydrate provider.
2. Send a `SetEVChargingRateCommand(rate_kw=30.0)` — PolicyGuard rejects with `commanded_rate_exceeds_peak_limit`.
3. Activate `peak_limit_kw=40.0` via `ConfigRepo.activate()`.
4. Call `provider.reload()`.
5. Re-send the same command — PolicyGuard now allows it (capability + state assumed satisfied; only the constraint changed).

**AND** the test runs against the real aiosqlite test connection (in-memory + Alembic migrations applied via existing `tests/conftest.py` fixtures), NOT a mocked repo. Drift between the unit-test mock contract and the real DB schema would otherwise hide.

---

### AC9 — `Settings.peak_limit_kw` / `Settings.battery_reserve_floor_percent` become seed-only

**GIVEN** `Settings.peak_limit_kw` and `Settings.battery_reserve_floor_percent` remain in `src/open_ems/settings.py` (do NOT remove)
**WHEN** the runtime reads either value
**THEN** the **only** legal runtime read is `ActiveConstraintsProvider.hydrate()` — and that is exclusively in the cold-start fallback branch (no `active_constraints` row yet).

**AND** to make this enforceable, a structural test in `tests/unit/test_settings_seed_only.py` greps the `src/` tree (excluding `settings.py`, `services/active_constraints.py`, and `__init__.py` re-exports) and asserts that no module reads `settings.peak_limit_kw` or `settings.battery_reserve_floor_percent`. Ruff custom rules are out of scope for v1; a grep-based pytest is sufficient and cheap.

**AND** the docstring on each of those two `Settings` fields is updated to read:
```
# Cold-start seed only. Runtime reads MUST go through ActiveConstraintsProvider.
# After the first installer activation (Story 9.3), the DB row is authoritative
# and the value here is unread.
```

**AND** removing these fields from Settings is explicitly OUT OF SCOPE for v1 — they are still required to seed a fresh deployment that boots before any installer activation.

---

### AC10 — Test matrix

**Unit tests** (use mocked aiosqlite or in-memory SQLite via existing test fixtures):

For `ConfigRepo`:
1. `get_active()` on empty table returns `None`.
2. `get_active()` after one `activate()` returns the inserted row with `config_version=1`.
3. `get_active()` after two `activate()` calls returns the row with `config_version=2` (highest wins).
4. `activate()` writes one `config_audit_log` row per changed field; `peak_limit_kw` change → field=`peak_consumption_limit`, `battery_reserve_floor_percent` change → field=`battery_reserve_floor`.
5. `activate()` with both fields unchanged raises `ValueError`.
6. `activate(actor="bogus")` raises `ValueError` with no DB write.
7. `activate()` failure mid-transaction (simulated via mock raising on the audit insert) leaves `active_constraints` unchanged — atomicity verified.
8. CHECK constraint enforcement: `peak_limit_kw=0.0` → SQLite `IntegrityError` (Pydantic catches it earlier on the input model, but the DB-side guard is real defense in depth).
9. `config_version` is monotonically increasing across N activations (parametrize N=5).

For `ActiveConstraintsProvider`:
10. `get()` before `hydrate()` → `RuntimeError`.
11. `hydrate()` with empty DB → seeds from Settings; `config_version=0`; `is_hydrated=True`.
12. `hydrate()` with one DB row → uses DB row; `config_version=1`.
13. `hydrate()` called twice → second call is a no-op + logs `constraints_provider_already_hydrated` warning.
14. `reload()` after a fresh `activate()` → new values visible via `get()`; old reference still immutable (proves snapshot replacement, not in-place mutation).
15. `reload()` with empty DB after hydrate (regression) → existing snapshot preserved; `constraints_reload_found_empty` warning logged.
16. Concurrent `reload()` calls are serialized — two `asyncio.gather(reload(), reload())` complete without raising; the lock is exercised.

For refactored `PolicyGuard`:
17. Existing `tests/unit/engine/test_policy_guard.py` suite passes after the signature change. Specifically: `battery_soc_at_or_below_reserve_floor`, `commanded_rate_exceeds_peak_limit`, and `conservative_mode_blocks_load_increase` rejections all continue to fire on the same inputs. (No new tests added here — regression coverage is the contract.)

For refactored `ControlLoop`:
18. Existing `tests/unit/engine/test_control_loop.py` — verify `_build_evaluation_input` produces a `PeakContext` with `configured_peak_limit_kw` matching the provider's value; same for `BatteryControlContext.reserve_floor_percent`. One new parametrized test asserting the value is sourced from the provider (not from `settings`).

For seed-only enforcement (AC9):
19. `tests/unit/test_settings_seed_only.py` — greps `src/` and asserts no forbidden reads.

**Integration tests:**

20. `tests/integration/engine/test_constraints_reload.py::test_reload_picks_up_new_peak_limit` — the AC8 end-to-end scenario.
21. `tests/integration/engine/test_constraints_reload.py::test_reload_picks_up_new_reserve_floor` — same shape, but for the battery reserve floor (rejection reason `battery_soc_at_or_below_reserve_floor`).
22. `tests/integration/engine/test_constraints_reload.py::test_lifespan_hydrates_before_control_loop` — start a real lifespan, assert `app.state.active_constraints_provider.is_hydrated is True` BEFORE the control-loop task is created (verified by capturing task-creation order via a spy on `asyncio.create_task`).
23. `tests/integration/engine/test_constraints_reload.py::test_cold_start_seeds_from_settings` — fresh DB (post-migration, zero rows in `active_constraints`); assert `provider.get().config_version == 0` and values match `Settings` defaults.

**Atomicity / cross-table consistency:**

24. `tests/unit/storage/repositories/test_config_repo.py::test_activate_atomic_under_audit_failure` — patch `ConfigAuditRepo.append_activation` to raise mid-transaction; assert `active_constraints` row count is unchanged AND no partial audit row exists. Closes the spirit of Story 6.3's deferred atomicity concern for the activation path.

---

### AC11 — Existing test suite passes; type checks clean

- Regression: `uv run python -m pytest tests/ --no-cov -q` — all existing tests (Epic 8 close: 871; Story 9.0 close: 972) plus the new tests from AC10 pass.
- `uv run python -m mypy src/` — no new type errors.
- `uv run python -m ruff check .` — clean.
- `uv run python -m ruff format --check .` — clean.

**AND** the migration `0008_add_active_constraints_table.py` is exercised by the CI migration-validation step (already covered by the existing CI workflow from Story 1.6 — no CI change required, just verify the migration applies cleanly against an empty DB and via `alembic downgrade -1` then `upgrade head`).

---

### AC12 — Adversarial 3-layer review with severity tagging (process AC)

**GIVEN** Story 9.0b is A2-triggered (T3 + T6, partial T1 + T7 — see story header)
**WHEN** the story reaches `Status: review`
**THEN** the tiered review model from Epic 8 retro action A3 applies:
- **Layer 1 (Blind Hunter):** unsafe implementation patterns — provider read before hydrate, non-atomic snapshot replacement, missing transaction in activate(), Settings drift, lifespan ordering bugs.
- **Layer 2 (Edge Case Hunter):** lifecycle/runtime failures — concurrent reload, hydrate failure during lifespan, DB row deleted between hydrate and first read, config_version regression after a manual DB edit, cold-start race where control loop starts before hydrate completes.
- **Layer 3 (Acceptance Auditor):** AC drift — every AC1–AC11 clause traced to a test or a design decision; specifically AC9's grep-based seed-only enforcement test is verified to actually fail when a forbidden read is reintroduced.

**AND** every finding carries a severity tag (HIGH / MEDIUM / LOW). `Status: review → done` is BLOCKED on any unresolved HIGH.

---

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: AC11)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — record exact passing count as baseline (expected: 972 from Story 9.0 close).
  - [x] Run `uv run python -m ruff check .` — must be clean.
  - [x] Run `uv run python -m ruff format --check .` — must be clean.
  - [x] Run `uv run python -m mypy src/` — must be clean.

- [x] Task 1: Migration `0008_add_active_constraints_table.py` (AC: AC1)
  - [x] Author the migration (model on `0007_add_peak_intervals_table.py`).
  - [x] CHECK constraints on `peak_limit_kw > 0`, `battery_reserve_floor_percent BETWEEN 0 AND 100`, `actor IN ('system', 'installer')`.
  - [x] Index on `config_version DESC`.
  - [x] Verify `alembic upgrade head` then `alembic downgrade -1` then `alembic upgrade head` round-trips cleanly against the test DB.

- [x] Task 2: `ActiveConstraints` / `ActiveConstraintsInput` Pydantic models (AC: AC3)
  - [x] Add `src/open_ems/core/constraints.py` with both models.
  - [x] `frozen=True, extra="forbid"`; reuse the `require_utc` pattern for `activated_at`.
  - [x] Unit tests: range validation, frozen behavior, UTC tz-awareness.

- [x] Task 3: `ConfigRepo` (AC: AC2)
  - [x] Add `src/open_ems/storage/repositories/config_repo.py` mirroring the `ConfigAuditRepo` style.
  - [x] `get_active()` and `activate()` per AC2.
  - [x] Use `BEGIN IMMEDIATE` and ensure both inserts (active_constraints + audit log) commit together.
  - [x] Delegate the audit emission to the existing `ConfigAuditRepo.append_activation` (do NOT duplicate audit logic).
  - [x] Validate `actor` against the same set used in the DB CHECK constraint — fail BEFORE the transaction begins.
  - [x] Unit tests per AC10 items 1–9 + 24.

- [x] Task 4: `ActiveConstraintsProvider` (AC: AC4)
  - [x] Add `src/open_ems/services/active_constraints.py`.
  - [x] Implement `__init__`, `hydrate`, `reload`, `get`, `is_hydrated` per AC4.
  - [x] `asyncio.Lock` around the DB-read + assignment in both `hydrate` and `reload`.
  - [x] structlog warnings for `constraints_provider_already_hydrated`, `constraints_reload_found_empty`, `constraints_hydrate_seeded_from_settings`.
  - [x] Unit tests per AC10 items 10–16.

- [x] Task 5: Refactor `PolicyGuard` (AC: AC5)
  - [x] Add `active_constraints: ActiveConstraintsProvider` parameter to `__init__`.
  - [x] Replace `self._settings.peak_limit_kw` and `self._settings.battery_reserve_floor_percent` reads with `self._active_constraints.get().peak_limit_kw` / `.battery_reserve_floor_percent`.
  - [x] Leave the rejection-reason strings, ordering, and audit emission untouched.
  - [x] Update `tests/unit/engine/test_policy_guard.py` fixtures to construct a provider seeded from `Settings(_env_file=None)` and assert all existing rejection paths still fire identically.

- [x] Task 6: Refactor `ControlLoop` (AC: AC6)
  - [x] Add `active_constraints: ActiveConstraintsProvider` parameter to `__init__`.
  - [x] Replace the two reads in `_build_evaluation_input` with provider reads.
  - [x] Leave all other Settings reads in place.
  - [x] Update `tests/unit/engine/test_control_loop.py` to thread a provider through the existing fixtures.

- [x] Task 7: Lifespan wiring (AC: AC7)
  - [x] In `src/open_ems/web/app.py` lifespan, after `init_database` and before PolicyGuard / ControlLoop construction, instantiate `ConfigRepo()`, `ActiveConstraintsProvider(repo=..., settings=settings)`, `await provider.hydrate()`.
  - [x] Pass the provider to PolicyGuard and ControlLoop.
  - [x] Stash `app.state.active_constraints_provider = provider`.
  - [x] Wrap `hydrate()` in try/except → on failure log `startup_failed reason=constraints_hydrate_failed` + `raise SystemExit(1) from None`.

- [x] Task 8: Seed-only enforcement (AC: AC9)
  - [x] Update `Settings.peak_limit_kw` and `Settings.battery_reserve_floor_percent` field docstrings per AC9.
  - [x] Add `tests/unit/test_settings_seed_only.py` — grep-based enforcement test.
  - [x] Verify the test FAILS deliberately when a sentinel forbidden read is added (then remove the sentinel) — proves the test is not a no-op.

- [x] Task 9: Integration tests (AC: AC8, AC10)
  - [x] Add `tests/integration/engine/test_constraints_reload.py`.
  - [x] Implement AC10 items 20–23 against the real test DB.
  - [x] For AC10 item 22, spy on `asyncio.create_task` to capture the order in which `provider.hydrate()` completes vs `ControlLoop.run` is scheduled.

- [x] Task 10: Documentation pass
  - [x] PolicyGuard module docstring: note that `peak_limit_kw` and `battery_reserve_floor_percent` are now read via `ActiveConstraintsProvider`, not Settings.
  - [x] ControlLoop module docstring: same note for `_build_evaluation_input`.
  - [x] `architecture.md` § BAD-3 — add a one-paragraph implementation note: "Story 9.0b implements the active-constraints reload contract: `ActiveConstraintsProvider` is the single in-memory owner; `ConfigRepo.activate()` is atomic with audit emission; reload is change-triggered (called by the activation endpoint), not TTL-based."
  - [x] Add a top-level constant `ACTIVE_CONSTRAINTS_PROVIDER_VERSION = "1.0"` in `services/active_constraints.py` for change-tracking parity with Story 9.0's `COMMAND_CONTRACT_VERSION` pattern.

- [x] Task 11: Final quality gate (AC: AC11)
  - [x] `uv run python -m pytest tests/ --no-cov -q` — must show baseline + new tests, all green.
  - [x] `uv run python -m mypy src/` — clean.
  - [x] `uv run python -m ruff check .` — clean.
  - [x] `uv run python -m ruff format --check .` — clean.

- [x] Task 12: Submit for adversarial 3-layer review (AC: AC12)
  - [x] Status: review.
  - [x] Layer 1 (Blind Hunter): unsafe-pattern sweep.  *(reviewer-owned, run via code-review workflow)*
  - [x] Layer 2 (Edge Case Hunter): lifecycle/runtime sweep.  *(reviewer-owned)*
  - [x] Layer 3 (Acceptance Auditor): AC1–AC11 → test traceability matrix.  *(reviewer-owned)*
  - [x] Severity tags HIGH/MEDIUM/LOW on every finding. No HIGH unresolved at `Status: review → done`.  *(reviewer-owned — both HIGH findings resolved: P1 audit-trail race fixed in-transaction; D1 stress-test reproduced the BEGIN IMMEDIATE race and the database-module write lock was applied; full suite 1020 green)*

### Review Findings (2026-05-10 — bmad-code-review)

**Triage:** 5 decision-needed, 6 patch, 7 deferred, 2 dismissed (after merge of F1≡E2 audit-trail finding). Layer-1/2/3 raw findings recorded under `_bmad-output/implementation-artifacts/9-0b-review-diff.patch` review run.

#### Decision-needed

- [x] [Review][Patch] **D1→P7 [HIGH] Empirically verified `BEGIN IMMEDIATE` race AND applied database-module-level write lock** [`src/open_ems/storage/database.py`, `config_repo.py`, `event_log_repo.py`, `energy_repo.py`] — Stress test (`tests/integration/storage/test_config_repo_concurrent_writers.py`) running `ConfigRepo.activate()` interleaved with `EventLogRepo.append` on the shared aiosqlite connection REPRODUCED `sqlite3.OperationalError: cannot start a transaction within a transaction` (both stress tests failed). Resolution applied in-story: added `_write_lock: asyncio.Lock` to `storage/database.py` (initialized in `init_database`, cleared in `close_database`, lazy fallback for unit tests bypassing the lifespan); every concurrent writer on the shared connection acquires the lock for its full `execute → commit` window — `ConfigRepo.activate` (wraps the BEGIN IMMEDIATE block), `EventLogRepo.append` and `prune_expired`, `EnergyRepo.write_peak_interval`. `ConfigAuditRepo.append_activation(commit=False)` deliberately does NOT acquire the lock — it's called from inside `ConfigRepo.activate`'s lock window (avoids deadlock; commented). Post-fix: both stress tests pass, full suite 1020 tests green. UserRepo / SessionRepo writes are NOT yet locked — concurrency with `activate()` arises only via HTTP request handlers (login/logout/admin-bootstrap); flagged as Epic 11 / Epic 12 hardening item in deferred-work.
- [x] [Review][Patch] **D2→P8 [MEDIUM] Make `config_version` assignment monotonic across `active_constraints` and `config_audit_log`** [`src/open_ems/storage/repositories/config_repo.py:559`] — Replace `next_version = COALESCE(MAX(config_version), 0) + 1 FROM active_constraints` with the cross-table maximum: `SELECT COALESCE(MAX(v), 0) + 1 FROM (SELECT MAX(config_version) AS v FROM active_constraints UNION ALL SELECT MAX(config_version) AS v FROM config_audit_log)`. Reload's empty-DB warning behavior stays unchanged. Closes the "DB-tampered-then-activated" version-regression hole without escalating to fail-safe. Add a unit test that DELETE-s all `active_constraints` rows, calls `activate()`, and asserts the new row's `config_version` continues from the audit log's high-water mark, not from 1.
- [x] [Review][Patch] **D3→P9 [MEDIUM] `reload()` must reject a lower-version DB row and preserve the cached snapshot** [`src/open_ems/services/active_constraints.py:64-83`] — Inside the lock, after `row = await self._repo.get_active()` and before `self._current = row`, add a guard: if `self._current is not None and row.config_version < self._current.config_version`, log a new structured event `constraints_reload_version_regressed` (`logger.warning`, fields: `cached_version`, `db_version`, `component="config"`), and return without replacing the snapshot. Symmetric with the empty-DB branch's "don't mask a regression" philosophy. Update AC4 semantics in this story file accordingly. Add a unit test: hydrate at `v=5`, monkey-patch repo to return a row at `v=3`, call `reload()`, assert (a) snapshot still at `v=5`, (b) warning emitted, (c) `is_hydrated` remains True.
- [x] [Review][Patch] **D4→P10 [MEDIUM] Restructure lifespan so `init_database` is inside the outer try/finally; cleanup runs on ANY startup failure (new hydrate path AND pre-existing migration path)** [`src/open_ems/web/app.py:196-243`] — Move `await init_database(settings.db_path)` (line 218) and the migration-failure branch (lines 199-200) inside the same `try:` block whose `finally: await close_database()` already exists at line 243. Both the `migration_failed → SystemExit(1)` and new `constraints_hydrate_failed → SystemExit(1)` paths must execute `close_database()` before exit. Verify with an integration test that monkey-patches `ConfigRepo.get_active` to raise mid-hydrate, captures the lifespan exit, and asserts the aiosqlite connection is closed (e.g. `_connection is None` post-lifespan, or no open file handle on the DB path). This addresses both the new and pre-existing leak in one patch.
- [x] [Review][Patch] **D5→P11 [MEDIUM] Enrich the hydrate-failure error so operator can locate the bad `active_constraints` row** [`src/open_ems/storage/repositories/config_repo.py:42-57`] — In `ConfigRepo.get_active`, wrap the `ActiveConstraints(...)` construction in a try/except `pydantic.ValidationError` that re-raises with a message containing `row.id` and `row.config_version` (and the offending field name if extractable from the validation error). Fail-loud is retained — refusing to start on bad timestamp data is the correct posture — but the operator now has a `UPDATE active_constraints SET activated_at='...' WHERE id=...` target. Add a unit test that inserts a raw row with a naive `activated_at` via direct SQL, calls `get_active`, and asserts the raised error message contains the row id.

#### Patch

- [x] [Review][Patch] **P1 [HIGH] Move `previous = await self.get_active()` INSIDE `BEGIN IMMEDIATE` so audit-log `previous_value`/`new_value` cannot be staled by a concurrent activation** [`src/open_ems/storage/repositories/config_repo.py:74-111`] — Two writers can both observe `previous=Vn` before A enters BEGIN IMMEDIATE. A commits Vn+1; B's BEGIN IMMEDIATE then succeeds (after A's commit), inserts at Vn+2, but B's audit row records `previous_value` from Vn — masking the Vn → Vn+1 transition. AC2 atomicity holds at the version-number level but the audit-log payload silently lies. Two-line fix: relocate `previous = await self.get_active()` and `_build_audit_changes(previous, input)` to immediately after `BEGIN IMMEDIATE`. Also subsumes F2 (no-changes-path connection-state leak — eliminated by the move).
- [x] [Review][Patch] **P2 [LOW] AC9 grep test: drop the `__init__.py` blanket allow** [`tests/unit/test_settings_seed_only.py:2283-2290`] — `_is_allowed` short-circuits TRUE for any `__init__.py`, so a forbidden read introduced in a package init silently passes. Replace with an explicit allowlist of init files that legitimately re-export `Settings`.
- [x] [Review][Patch] **P3 [LOW] AC9 regex bypassed by identifier aliases ending in `settings`** [`tests/unit/test_settings_seed_only.py:33-37`] — `(?:^|[^A-Za-z0-9_])_?settings\.` only matches bare `settings` or `_settings`; `cfg_settings.peak_limit_kw` or `app_settings.peak_limit_kw` slip through. Tighten to `(?:^|[^A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_]*_)?settings\.(...)` and add a sentinel test asserting `cfg_settings.peak_limit_kw` matches.
- [x] [Review][Patch] **P4 [LOW] Test reads ambient `.env` via bare `Settings()`** [`tests/integration/engine/test_constraints_reload.py:1197`] — `lambda: Settings()` reads project-root `.env` if present; flaky on dev machines. Replace with `Settings(_env_file=None)` for parity with other test sites in the diff.
- [x] [Review][Patch] **P5 [LOW] AC9 sentinel test only validates regex shape, not full grep traversal** [`tests/unit/test_settings_seed_only.py:2315-2330`] — Codify the manual sentinel verification: temp-write a forbidden read into `src/`, run the traversal, assert violation, clean up. Currently the regex is unit-tested but the test doesn't prove the walker actually catches an in-tree violation.
- [x] [Review][Patch] **P6 [LOW] Remove `core/constraints.py` from AC9 allowlist** [`tests/unit/test_settings_seed_only.py:2274-2280`] — Spec lists three exclusions; impl adds a fourth that doesn't actually need exemption (file has no forbidden reads today). Removing tightens the guard against future drift.

#### Deferred (logged to deferred-work.md)

- [x] [Review][Defer] **W1 [LOW] Test fixtures bypass hydrate contract by writing private `_current` field** [`tests/fixtures/active_constraints.py:749` and 14 PolicyGuard/ControlLoop fixture sites] — `# noqa: SLF001` private-field-write means tests don't exercise the public hydrate API; couples to private representation.
- [x] [Review][Defer] **W2 [LOW] `MagicMock(spec=ConfigRepo)` accepts unconfigured method calls silently** [`tests/fixtures/active_constraints.py:748`] — Configure the mock to raise on any invocation so a future hot-path-DB-read regression fails loud.
- [x] [Review][Defer] **W3 [LOW] `_reset_structlog_config` autouse fixture has session-wide blast radius** [`tests/conftest.py:51-63`] — Scope reset to specific test files that need it instead of every test in the suite.
- [x] [Review][Defer] **W4 [LOW] `constraints_hydrate_seeded_from_settings` emitted at info, Task 4 says warning** [`src/open_ems/services/active_constraints.py:357-362`] — info is arguably more appropriate for cold-start; Task 4 wording divergence only.
- [x] [Review][Defer] **W5 [LOW] DB-layer CHECK constraint coverage incomplete (reserve floor, actor)** [`tests/unit/storage/repositories/test_config_repo.py`] — AC10 #8 only required peak case (covered); reserve-floor `BETWEEN 0 AND 100` and `actor IN ('system','installer')` CHECKs are exercised only via Pydantic, not by raw-INSERT bypass.
- [x] [Review][Defer] **W6 [LOW] Migration 0008 upgrade→downgrade→upgrade round-trip not codified as a test** [`migrations/versions/0008_add_active_constraints_table.py`] — Verified out-of-band per Debug Log; CI's generic migration validation covers it implicitly. Codify when migration count grows.
- [x] [Review][Defer] **W7 [LOW] AC7 fail-loud `SystemExit(1)` branch not exercised by any test** [`src/open_ems/web/app.py:229-236`] — Spec did not require it in AC10. Add a regression test that monkey-patches `ConfigRepo.get_active` to raise.

#### Dismissed

- F2 (LOW — `activate()` no-changes path connection-state leak) — superseded by P1 (moving `get_active()` inside BEGIN IMMEDIATE eliminates the pre-flush window entirely).
- A1 (LOW — `ActiveConstraints.config_version: ge=0` diverges from AC3 literal `ge=1`) — spec internal contradiction (AC3 vs AC4/AC10 #11/R3 cold-start) acknowledged in Completion Notes; DB rows still guaranteed `≥ 1` via UNIQUE + AUTOINCREMENT; no safety impact.

---

## Dev Notes

### Provider contract (the canonical reference)

All runtime reads of `peak_limit_kw` and `battery_reserve_floor_percent` MUST go through `ActiveConstraintsProvider.get()`. Directly reading `Settings.peak_limit_kw` or `Settings.battery_reserve_floor_percent` outside `ActiveConstraintsProvider.hydrate()` is a contract violation and is enforced by the AC9 grep test.

**Lifecycle:**

1. **Construction:** `ActiveConstraintsProvider(repo, settings)` — no I/O.
2. **Hydration (once per process, in lifespan):** `await provider.hydrate()` — reads DB; falls back to Settings seed if empty. Sets `is_hydrated=True`.
3. **Steady state:** `provider.get()` is the only read API; sync; raises if not hydrated.
4. **Reload (zero-or-many, called by Story 9.3's activation endpoint):** `await provider.reload()` — re-reads DB; atomically replaces snapshot.
5. **Shutdown:** none — the provider holds no resources beyond an in-memory snapshot and the asyncio.Lock.

**Snapshot atomicity:** `self._current` is a single attribute holding an immutable `ActiveConstraints` Pydantic instance. CPython attribute assignment is atomic at the bytecode level. Readers (`get()`) either see the old reference or the new one; never a partially-constructed object.

**Why change-trigger and not TTL:**
- The activation flow is the ONLY reason values change. There is no external clock or upstream feed to poll.
- TTL would either lag activation (unsafe — the installer expects immediate effect) or thrash with frequent DB reads (wasteful).
- Change-trigger keeps the hot path pure-memory and the cold path explicit.
- TTL is reintroducible later if multi-process deployments ever land (see "Multi-process futures" below).

**Multi-process futures (out of scope for v1):**
The current single-process asyncio architecture means one provider instance owns the snapshot. If the project ever moves to multiple processes (e.g. uvicorn workers > 1), each worker would need its own provider AND a shared notification channel (postgres LISTEN/NOTIFY, Redis pubsub, or a SQLite polling fallback). v1 single-process model is documented in `architecture.md` and is not changing in Epic 9. Flag this in deferred-work if the multi-worker question ever resurfaces.

### Schema design rationale

**Why insert-only instead of UPDATE-in-place:**
- Audit trail is intrinsic — every activation produces a new row + a `config_audit_log` entry. UPDATE would lose the historical sequence.
- `config_version` becomes a natural foreign key for `decision` and `device` audit events that want to record "which constraint version was active when this command was authorized" (a future feature; the column is in place now).
- Downside: unbounded growth. Mitigation: not a real concern (a typical site does < 100 activations over its lifetime). If it ever becomes one, a retention pruner mirroring `event_log_repo.prune_expired` is trivial.

**Why a separate `active_constraints` table instead of extending `config_audit_log`:**
- Audit log is append-only logical event records. Constraints are typed values with CHECK constraints and a clear "current row = highest version" query pattern.
- Mixing the two would force the audit log schema to grow numeric value columns AND would make `get_active()` a JSON parse over the audit table — slower and shape-coupled.
- Architecture BAD-3 explicitly mandates two separate tables (`config_repo` for active/draft, `config_audit_repo` for changes). 9.0b honors that.

**Why `actor` is a column on the active row, not just on the audit log:**
- Lets future audit-UI queries reconstruct "who activated this version" without an extra join. Cheap and sets up Story 11.2's event-log UI.

### File structure references

| Path | Action |
|---|---|
| `migrations/versions/0008_add_active_constraints_table.py` | NEW |
| `src/open_ems/core/constraints.py` | NEW — `ActiveConstraints` + `ActiveConstraintsInput` Pydantic models |
| `src/open_ems/storage/repositories/config_repo.py` | NEW |
| `src/open_ems/services/active_constraints.py` | NEW — `ActiveConstraintsProvider` |
| `src/open_ems/engine/policy_guard.py` | UPDATE — `__init__` accepts provider; two read sites change |
| `src/open_ems/engine/control_loop.py` | UPDATE — `__init__` accepts provider; one method changes |
| `src/open_ems/web/app.py` | UPDATE — lifespan adds hydrate step + threads provider |
| `src/open_ems/settings.py` | UPDATE — docstrings on the two seed fields only |
| `tests/unit/storage/repositories/test_config_repo.py` | NEW |
| `tests/unit/services/test_active_constraints.py` | NEW |
| `tests/unit/core/test_constraints.py` | NEW |
| `tests/unit/engine/test_policy_guard.py` | UPDATE — fixture builds provider |
| `tests/unit/engine/test_control_loop.py` | UPDATE — fixture builds provider |
| `tests/unit/test_settings_seed_only.py` | NEW |
| `tests/integration/engine/test_constraints_reload.py` | NEW |

### Architecture references

- **AR6** (staged constraint validation flow per BAD-3) — `_bmad-output/planning-artifacts/architecture.md:1112-1153`. Story 9.0b implements the *activation* and *reload* halves; the draft + validate halves are Story 9.3.
- **AR15** (`PolicyGuard.authorize_and_dispatch()` is the single required path) — `_bmad-output/planning-artifacts/epics.md:152`. Unchanged; this story refactors the constraint-source read inside PolicyGuard, not the authorization path.
- **AR16** (typed `CommandResult` from every `send_command()`) — Unaffected.
- **AR19** (config audit table + audit emission on activation) — `_bmad-output/planning-artifacts/architecture.md:343`. `ConfigRepo.activate()` honors AR19 by delegating audit emission to `ConfigAuditRepo.append_activation` inside the same transaction.
- **BAD-3** (constraint change validation flow) — `_bmad-output/planning-artifacts/architecture.md:1112`. Story 9.0b implements the schema (`active_constraints` table) + the in-memory provider (`PolicyGuard reads active_constraints at initialization and reloads on receipt of a ConstraintsActivated signal`).
- **FR16** — `_bmad-output/planning-artifacts/epics.md:44`. Per-site safety constraints are configurable; this story is the runtime-read path that makes the configuration *take effect*.

### Testing standards summary

- pytest + pytest-asyncio (per architecture §Testing).
- For unit tests of `ConfigRepo` and `ActiveConstraintsProvider`, use the existing in-memory aiosqlite fixture pattern in `tests/conftest.py` (the same one that backs `ConfigAuditRepo` tests). Do NOT mock SQLite — the CHECK constraints, `BEGIN IMMEDIATE` semantics, and monotonic config_version generation are part of the contract and must be exercised against a real engine.
- For PolicyGuard and ControlLoop refactors, the existing test files have established `_settings()` helper functions; introduce a parallel `_active_constraints_provider(settings)` helper that returns a hydrated provider seeded from the supplied `Settings` instance. This keeps the change to existing tests minimal — one call-site swap per fixture.
- Integration tests live under `tests/integration/engine/` — colocated with the `test_decision_engine_simulation.py` precedent established in Story 8.5.

### Library/framework notes

- `aiosqlite` (existing): `BEGIN IMMEDIATE` is invoked via `await self._conn.execute("BEGIN IMMEDIATE")`. SQLite serialized write-lock makes this sufficient for single-process atomicity. (See `event_log_repo.py:append` for an existing precedent of the multi-statement-in-one-commit pattern; `ConfigRepo.activate` extends it with an explicit BEGIN IMMEDIATE because two table inserts are involved.)
- `pydantic` v2 (existing): `frozen=True, extra="forbid"` on `ActiveConstraints` — same pattern as `CommandResult`, `EvaluationResult`, etc.
- `alembic` (existing): migration 0008 is a straightforward `op.create_table` / `op.create_index`. CHECK constraints use `sa.CheckConstraint(...)` per the SQLAlchemy idiom.
- `structlog` (existing): three new structured log events — `constraints_provider_already_hydrated`, `constraints_reload_found_empty`, `constraints_hydrate_seeded_from_settings` — all with `component="config"`.
- No new dependencies. uv.lock unchanged.

### Previous story intelligence — what we carry from Epic 8 and Story 9.0

From the Epic 8 retro and Story 9.0:

- **Story 8-2 deferred:** "Timeout constants not exposed via Settings" [policy_guard.py:44-45] — out of scope for 9.0b; PolicyGuard's hardcoded timeouts are unrelated to constraint reload. Acceptable-post.
- **Story 8-2 deferred:** "PolicyGuard re-fetches snapshot independently (stale-read window)" [policy_guard.py:67] — unchanged by this story; constraint reload is a separate read path. Safe-during.
- **Story 8-2 deferred:** "Constraint ordering emits wrong audit reason when conservative mode + below-floor SoC" [policy_guard.py:137-150] — ordering of safety checks is intentionally NOT touched by this story. Safe-during.
- **Story 6-3 deferred:** "Non-atomic `config_version` increment" [config_audit_repo.py:55] — `ConfigRepo.activate()` is the *new* activation surface and is atomic by AC2. The pre-existing direct `ConfigAuditRepo.append_activation` callers (none yet outside tests) remain non-atomic for now; that is a separate cleanup. AC2 narrows the atomicity guarantee to the activation path that Story 9.3 will use.
- **Story 9.0 pattern:** A2-triggered prep stories with explicit R1–R7 dev notes shipped cleanly (4 HIGH findings caught + resolved before merge). The pattern of "small contract story before large consumer story" succeeded; 9.0b follows it for the same reason.
- **Style precedent:** Story 9.0's adversarial 3-layer review with severity tagging caught the OCPP transaction-id reconnect race (HIGH). 9.0b's analog is the cold-start hydration race (R3 below) — apply the same review rigor to make sure that hazard is closed before merge.

### Project Structure Notes

- Alignment: `src/open_ems/services/` already contains cross-cutting runtime services (`audit_log.py`, `watchdog.py`, `loop_liveness.py`). `active_constraints.py` fits naturally as another runtime service.
- `src/open_ems/core/constraints.py` is a new file under `core/`. Consistent with `core/devices.py`, `core/state.py`, `core/commands.py` — pure typed models, no I/O, importable from anywhere.
- `src/open_ems/storage/repositories/config_repo.py` is a new file under `storage/repositories/`. Consistent with `config_audit_repo.py`, `event_log_repo.py`, `energy_repo.py`.
- No new top-level packages or directories.
- Decision: lifespan ordering puts provider hydration **between** `init_database()` and the watchdog/loop-liveness construction. Specifically: the provider needs the DB connection (so after `init_database`), but PolicyGuard/ControlLoop construction needs the provider (so before them). The sequencing is unambiguous and is the only intrusive change to `app.py`.

---

### Orchestration Risk Analysis (A2-triggered — REQUIRED)

> **A2 triggers matched:** **T3** (persistence + recovery — DB-backed state hydrated on startup, reloaded on installer activation, durable across process restart), **T6** (deployment / restart behavior — installer wizard restarts trigger reload of constraints; cold-start invariants are user-visible). Partial: **T1** (lifecycle — provider transitions from `unhydrated` to `hydrated` and that transition is the structural gate that makes PolicyGuard safe to run), **T7** (installer workflow orchestration — Story 9.3's activation endpoint calls into the surface this story creates).

#### R1 — Composition-risk analysis

This story converges four operational domains in a single shipping unit:

1. **Persistence + recovery (T3).** A new DB-backed runtime value with a clear ownership story. Risk: an under-specified hydrate path silently regressing to Settings defaults after a row was supposed to exist (e.g. a row written but `MAX(config_version)` query off-by-one). Mitigation: AC2's atomic `activate()` + AC10's monotonic-config_version test (item 9) + AC4's `reload()` semantics that explicitly preserve the existing snapshot if the DB unexpectedly reads empty post-hydrate (rather than re-seeding from Settings — re-seeding would mask the regression). Precedent: Story 8-1 deferred `_monthly_peak_kw` not hydrated on restart — this story applies the same hydrate-or-fail-loud lesson to constraints.

2. **Cold-start invariant (T6, partial T1).** The provider must be hydrated before the control loop starts; otherwise PolicyGuard's `get()` raises `RuntimeError` mid-evaluation. Risk: lifespan code path orders task creation incorrectly — e.g. `_control_loop_task = asyncio.create_task(...)` runs BEFORE `await provider.hydrate()` because of a refactor that misses the dependency. Mitigation: AC7 specifies the exact lifespan ordering; AC10 item 22 spies on `asyncio.create_task` to assert the order at runtime. Precedent: Story 8-4 review caught cold-start `sd_notify` suppression — same shape: a "feels obvious in code review, hard to verify without a runtime spy" cold-start hazard.

3. **Source-of-truth fragmentation (T1).** Two consumers (PolicyGuard and ControlLoop) both currently read from Settings. If only PolicyGuard is migrated (the retro narrowly mentioned PolicyGuard), the engine's `PeakContext` would carry an old `configured_peak_limit_kw` while PolicyGuard's `_check_safety_constraints` uses a new value. The engine could produce a load-reduction intent that PolicyGuard then approves against a different limit — a coherent-looking but actually incoherent decision. Mitigation: AC6 explicitly migrates ControlLoop too; AC10 item 18 verifies the engine reads from the provider; AC8's integration test exercises the reload at the PolicyGuard layer (which depends on the engine producing the command in the first place).

4. **Settings vs provider drift (T3).** Settings still holds the two values as cold-start seeds. A future contributor could "helpfully" read from Settings somewhere new (e.g. a new service). Mitigation: AC9's grep-based seed-only enforcement test makes the violation a CI failure, not a code review judgment call. The test must be verified to actually fail when a forbidden read is reintroduced (Task 8 includes the sentinel test).

5. **Atomic activation across two tables (partial T1).** `ConfigRepo.activate()` writes `active_constraints` AND `config_audit_log` in the same transaction. Risk: a partial commit (e.g. audit insert raises, active row already inserted) leaves config_version assigned to a row with no audit trail — exactly the auditability hole that AR19 was designed to prevent. Mitigation: AC2 mandates `BEGIN IMMEDIATE` + AC10 item 7 + 24 verify atomicity by patching the audit insert to raise. Precedent: Story 6-3 review deferred this exact concern as a non-atomic-config_version finding; 9.0b closes it for the activation path.

#### R2 — State-transition table

Story 9.0b modifies state machines in two places: the `ActiveConstraintsProvider` (new) and the `active_constraints` table row sequence (DB-side, derived).

**`ActiveConstraintsProvider` lifecycle:**

| State | Allowed transitions | Trigger | Side-effects |
|---|---|---|---|
| `unhydrated` (post-construction) | → `hydrated_from_db`, → `hydrated_from_seed` | `await hydrate()` | structlog `constraints_hydrate_seeded_from_settings` if seed branch; none if DB branch |
| `unhydrated` | (terminal — error) | `await hydrate()` raises | lifespan logs `startup_failed reason=constraints_hydrate_failed`; SystemExit(1) |
| `hydrated_from_seed` (config_version=0) | → `hydrated_from_db` | `await reload()` succeeds with non-empty DB | atomic `_current` replacement |
| `hydrated_from_db` (config_version≥1) | → `hydrated_from_db` (new row) | `await reload()` succeeds | atomic `_current` replacement |
| `hydrated_from_db` | → `hydrated_from_db` (unchanged) | `await reload()` finds DB empty (regression) | `constraints_reload_found_empty` warning; snapshot preserved |
| any hydrated | → same | `provider.get()` | none (sync read) |
| any hydrated | (terminal — error) | concurrent reload exception | lock released; warning logged |

Note: there is no `unhydrated → unhydrated` self-transition — `hydrate()` is idempotent and transitions to a hydrated state (or raises). A second `hydrate()` call logs `constraints_provider_already_hydrated` and returns without touching state.

**`active_constraints` table row sequence:**

| State | Transitions | Trigger | Side-effects |
|---|---|---|---|
| empty (zero rows) | → `version=1` row exists | `ConfigRepo.activate()` first call | one INSERT into active_constraints + N INSERTs into config_audit_log (all in one BEGIN IMMEDIATE transaction) |
| `version=N` exists | → `version=N+1` row exists | `ConfigRepo.activate()` subsequent call | same; N+1 strictly monotonic |
| `version=N` exists | (terminal — error) | activation transaction fails mid-flight | DB unchanged; both inserts rolled back atomically |

`ConfigRepo.get_active()` reads `ORDER BY config_version DESC LIMIT 1`; the highest version is always the active one.

#### R3 — Impossible-state analysis (with cold-start coverage)

**Forbidden state combinations and their structural invariants:**

1. **`PolicyGuard._check_safety_constraints()` is called while `provider.is_hydrated is False`** — invariant: AC7 mandates `await provider.hydrate()` runs in lifespan BEFORE `ControlLoop.__init__` is constructed and BEFORE its task is scheduled. AC10 item 22 verifies this with a `create_task` spy. Defense in depth: `provider.get()` raises `RuntimeError` if called pre-hydrate; PolicyGuard's stack trace makes the violation impossible to ignore.

2. **`provider.get()` returns a torn / partially-constructed `ActiveConstraints`** — invariant: snapshot is replaced via a single attribute assignment of an immutable Pydantic instance. CPython attribute assignment is atomic. No half-state is observable.

3. **`config_version` regresses (newer activation has lower version than older)** — invariant: AC2's `next_config_version = MAX(config_version) + 1` computed inside `BEGIN IMMEDIATE` + the `UNIQUE` constraint on `config_version`. Any race that produced two equal versions would fail the UNIQUE constraint and roll back. AC10 item 9 verifies monotonicity over N activations.

4. **`active_constraints` row exists without a corresponding `config_audit_log` row** — invariant: AC2's atomic transaction ensures both inserts commit together or neither does. AC10 item 7 + 24 verify under simulated audit-insert failure.

5. **`PolicyGuard` reads `peak_limit_kw=X` while `ControlLoop._build_evaluation_input` reads `peak_limit_kw=Y` for the same evaluation cycle** — invariant: both read from the SAME provider instance, populated by `app.py` lifespan once. The provider returns the same immutable snapshot to both callers within one cycle. A `reload()` between cycles updates both atomically (the snapshot is shared).

6. **A future module reads `Settings.peak_limit_kw` at runtime** — invariant: AC9's grep-based pytest. CI failure on violation.

**Cold-start / startup-grace coverage (mandatory):**

Story 9.0b's provider becomes live during the lifespan startup sequence. Cold-start sequence specific to constraints:

1. **t=0 (process start):** `app.py` lifespan begins. Provider does not exist yet.
2. **t=migrations done:** `active_constraints` table exists (migration 0008 applied). May be empty (fresh deployment) or populated (warm restart with prior installer activations).
3. **t=DB initialized:** `ConfigRepo()` constructed. Still no provider.
4. **t=provider constructed:** `ActiveConstraintsProvider(repo, settings)` exists. `is_hydrated=False`. Calling `get()` would raise. No reads have occurred yet.
5. **t=hydrate() begins:** Provider acquires lock; reads DB.
   - **Branch A (DB has a row):** Provider replaces `_current` with the latest active row. `is_hydrated=True`. `_current.config_version >= 1`.
   - **Branch B (DB empty):** Provider builds an `ActiveConstraints` from `Settings.peak_limit_kw` / `Settings.battery_reserve_floor_percent` with `config_version=0` and `activated_at=now`. Logs `constraints_hydrate_seeded_from_settings`. `is_hydrated=True`.
6. **t=PolicyGuard / ControlLoop constructed:** Both receive the (now hydrated) provider. Their hot paths can call `provider.get()` immediately.
7. **t=control loop task started:** `_control_loop.run()` begins ticking. Each tick's `_build_evaluation_input` reads the provider; PolicyGuard's `_check_safety_constraints` reads the provider.
8. **First successful cycle (marker for normal operation):** `loop_liveness.mark_cycle_complete()` fires. From this point, the system is operating in steady state.

**Pre-9.3 window:** between lifespan start and the first installer activation, the provider serves seed-from-Settings values (`config_version=0`). The system is fully functional — every command is constrained against the configured Settings defaults. This is the safe state for a fresh deployment; an installer who never opens the wizard inherits the deployer's Settings defaults, which are themselves constrained by the `Settings` field validators (`peak_limit_kw > 0`, `battery_reserve_floor_percent` ∈ [0, 100]).

**Post-first-activation window:** the DB row exists with `config_version=1`. Any subsequent process restart reads from DB, not Settings. Settings values are runtime-unread from this point forward (per AC9).

#### R4 — Cancellation ownership map

| Async operation | CancelledError owner | Cancellation audit owner | Cleanup owner | Reporting field semantics under cancel |
|---|---|---|---|---|
| `await provider.hydrate()` (in lifespan) | lifespan task (lifespan body itself owns CancelledError; an outer SIGTERM during startup propagates via the lifespan generator) | none — startup-phase operation, no audit semantics | `asyncio.Lock` released via `async with` ensures lock is freed; no DB cleanup needed (read-only) | `is_hydrated` remains `False` if cancelled mid-hydrate — control loop will not start |
| `await provider.reload()` (called by Story 9.3 endpoint) | endpoint handler task | none — reload is observable via the snapshot's `config_version`, not via a separate audit | `asyncio.Lock` released via `async with`; no DB cleanup (read-only); existing snapshot preserved if cancellation lands before assignment | snapshot reflects pre-reload state if cancelled before assignment, post-reload state if cancelled after |
| `await config_repo.activate(...)` (called by Story 9.3 endpoint) | endpoint handler task | the activation flow's own audit emission (delegated to `ConfigAuditRepo.append_activation` inside the same transaction) | `BEGIN IMMEDIATE` ensures rollback on cancellation between BEGIN and COMMIT; SQLite handles transactional cleanup | partial commit is impossible by SQL semantics |
| `provider.get()` (sync, hot path) | N/A — synchronous, no await | N/A | N/A | N/A |

**No new audit emissions are introduced by this story.** The provider is a pure read cache; activation audits are delegated to the existing `ConfigAuditRepo.append_activation` (which Story 6.3 owns). Story 9.0b does not add any new audit event types.

#### R5 — Before-first-successful-cycle lifecycle review

Concrete trace from process boot to first successful normal evaluation cycle:

| Phase | Event | What is published / logged / DB state | What relaxes |
|---|---|---|---|
| Boot | `uvicorn` starts; lifespan generator entered | structlog uninitialized for the first few uvicorn lines (pre-existing limitation from Story 1.2) | logging format is plain text |
| Lifespan step 1 | `configure_logging(settings.log_level)` | structlog now JSON-formatted | logging format normal |
| Lifespan step 2 | Alembic `upgrade head` runs | migration 0008 applied if first boot; `active_constraints` table exists | none |
| Lifespan step 3 | Clock check | `clock_valid` / `clock_unsynced` log | tariff tz logic temporarily relaxed (existing) |
| Lifespan step 4 | `init_database` opens aiosqlite connection | DB ready | none |
| **Lifespan step 4b (NEW)** | **`provider.hydrate()` called** | **structlog `constraints_hydrate_seeded_from_settings` (Branch B) OR no log (Branch A); `provider.is_hydrated=True`** | **none — provider is fully ready before any consumer is constructed** |
| Lifespan step 5 | `mark_ready()` + `sd_notify("READY=1")` | systemd considers process ready | readiness flag flips to True |
| Lifespan step 6 | Watchdog task started | `watchdog_started`/`stall_monitor_started` log | none |
| Lifespan step 7 | Control loop task created | `control_loop_started` log; `loop_liveness.in_fail_safe=False` | none |
| First tick | `ControlLoop._tick()` runs | first state publish (operating_mode may be None — pre-existing from Story 8-1 review, deferred) | snapshots may carry None operating_mode for one tick |
| First evaluation | `evaluate_cycle()` runs | first DECISION audit (if intents produced) | none |
| **Marker for normal operation** | `loop_liveness.mark_cycle_complete()` fires after the first complete tick | normal steady state | n/a — operating normally |

**During the lifespan-step-4b window:** the provider is the only new participant. PolicyGuard and ControlLoop do not exist yet, so a hydrate failure cannot be observed by them. A hydrate failure escalates to `SystemExit(1)` before any task is created — fail-loud is intentional.

**Between hydrate completion and the first tick:** the provider is hydrated but no consumer has read it yet. If some hypothetical actor called `provider.reload()` during this window, the reload would succeed and the first tick would see the new value. There is no race because Python attribute assignment is atomic; the first tick reads whatever was current when `get()` was called.

#### R6 — Source-of-truth ownership per datum

| Datum | Single owner | Read path | Drift risk |
|---|---|---|---|
| `peak_limit_kw` (runtime) | `ActiveConstraintsProvider._current.peak_limit_kw` | `PolicyGuard._check_safety_constraints` and `ControlLoop._build_evaluation_input` both call `provider.get().peak_limit_kw` | **Must be eliminated:** Settings still has the field. AC9's grep test enforces no other reader. |
| `peak_limit_kw` (cold-start seed) | `Settings.peak_limit_kw` | `ActiveConstraintsProvider.hydrate()` only — exactly one call site | None — single-call-site by construction |
| `battery_reserve_floor_percent` (runtime) | `ActiveConstraintsProvider._current.battery_reserve_floor_percent` | `PolicyGuard._check_safety_constraints` and `ControlLoop._build_evaluation_input` | Same as peak_limit_kw — AC9 enforces |
| `battery_reserve_floor_percent` (cold-start seed) | `Settings.battery_reserve_floor_percent` | `ActiveConstraintsProvider.hydrate()` only | None |
| `config_version` (DB authoritative) | `active_constraints` table; `MAX(config_version)` is the current | `ConfigRepo.get_active()` | None — DB is single source |
| `config_version` (in-memory cached) | `provider._current.config_version` | `provider.get().config_version` | Stale if `reload()` not called after activation. Mitigation: Story 9.3's activation endpoint MUST call `await provider.reload()` after `activate()` returns. AC8's integration test verifies. |
| `activated_at` | `active_constraints` table column | Read via `ConfigRepo.get_active()`; cached in provider | None — read-only after insert |
| `actor` (per activation) | `active_constraints.actor` column + corresponding `config_audit_log.actor` rows | Read via `ConfigRepo.get_active()` and via the audit log | None — written atomically by `activate()` |

**Drift risks called out:**

- **Settings ↔ provider runtime drift:** structurally eliminated by AC9 grep test + the documented seed-only field comments.
- **Provider snapshot ↔ DB drift:** prevented by the contract that Story 9.3's activation endpoint MUST call `provider.reload()` after `ConfigRepo.activate()`. This is documented here and will be a checklist item in Story 9.3's dev notes.
- **PolicyGuard ↔ ControlLoop drift within a single cycle:** structurally eliminated by both reading from the same provider instance.

#### R7 — Deferred-findings triage

Scanned `_bmad-output/implementation-artifacts/deferred-work.md` (271 lines, all sections); items whose component or invariant overlaps with this story:

| Finding (bracket ref) | Classification | Rationale |
|---|---|---|
| `[src/open_ems/storage/repositories/config_audit_repo.py:55]` "Non-atomic `config_version` increment" | **must-resolve-in-this-story (for the activation path)** | AC2 requires `BEGIN IMMEDIATE` around the new `ConfigRepo.activate()`. Pre-existing direct callers of `ConfigAuditRepo.append_activation` outside the activation path remain non-atomic; that is a separate cleanup that does NOT block 9.0b. The activation path is the only path 9.3 will use — that one must be atomic. |
| `[src/open_ems/engine/policy_guard.py:67]` "PolicyGuard re-fetches snapshot independently (stale-read window)" | **safe-during-this-story** | Snapshot read path is unchanged by 9.0b; only the constraint read path moves. |
| `[src/open_ems/engine/policy_guard.py:137-150]` "Constraint ordering emits wrong audit reason when conservative mode + below-floor SoC" | **safe-during-this-story** | Ordering of `_check_safety_constraints` is intentionally NOT touched. Risk that a careless refactor reorders checks → mitigated by AC5's "rejection-reason strings, ordering, and audit emission untouched" clause + AC10 item 17's regression coverage. |
| `[src/open_ems/engine/policy_guard.py:137-141]` "Discharge command bypasses SoC floor check when battery state unavailable" | **safe-during-this-story** | Same area, different concern. The substitution-only refactor preserves this behavior. Story 9-0c (capability-profile failure semantics) is the right place to revisit. |
| `[src/open_ems/engine/policy_guard.py:143-145]` "Peak limit check uses strict `>` — command at exactly `peak_limit_kw` passes" | **safe-during-this-story** | Comparison operator is unchanged. Boundary semantics preserved. |
| `[src/open_ems/engine/policy_guard.py:44-45]` "Timeout constants not exposed via Settings" | **acceptable-post-this-story** | Unrelated to the constraint reload path. Constants stay where they are. |
| `[src/open_ems/storage/repositories/config_audit_repo.py:35]` "`ConfigAuditRepo.get_connection()` fallback path untested" | **acceptable-post-this-story** | Pre-existing pattern; 9.0b uses the documented dependency-injection path. |
| `[migrations/versions/0006_add_config_audit_log_table.py]` "Migration `0006` downgrade path not tested" | **acceptable-post-this-story** | Migration 0008's downgrade IS tested per Task 1; the broader downgrade-CI question is a separate quality-hardening pass. |
| `[src/open_ems/engine/control_loop.py:54]` "`_monthly_peak_kw` not hydrated from DB on process restart" | **safe-during-this-story** | Adjacent concern (also a hydrate-on-restart gap), but addressed by sister story 9-0d (Restart hydration of monthly peak). 9.0b touches `ControlLoop._build_evaluation_input` reads only — does NOT address `_monthly_peak_kw`. The two stories are independently scoped. |
| `[src/open_ems/web/app.py:223-236]` (from Story 8-2 review) "`adapters={}` in production silently rejects all commands" | **safe-during-this-story** | 9.0b does not change adapter wiring. Story 9.1 will populate the adapter map. |

No findings classified as `must-resolve` outside the activation-path atomicity item, which is structurally addressed by AC2.

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#BAD-3] — staged constraint validation flow; `config_repo` schema; PolicyGuard reload semantics.
- [Source: _bmad-output/planning-artifacts/architecture.md#Behavioral-Decisions-AR6-AR19] — staged validation + audit table contracts.
- [Source: _bmad-output/planning-artifacts/architecture.md#Project-structure] — `config_repo.py` and `services/` placement.
- [Source: _bmad-output/planning-artifacts/epics.md#Story-9.3] — downstream consumer of the surface this story creates.
- [Source: _bmad-output/implementation-artifacts/epic-8-retro-2026-05-10.md#P1] — the origin and gate semantics for this story.
- [Source: src/open_ems/engine/policy_guard.py:141-161] — current `_check_safety_constraints` implementation.
- [Source: src/open_ems/engine/control_loop.py:164-189] — current `_build_evaluation_input` implementation.
- [Source: src/open_ems/web/app.py:166-326] — current lifespan ordering.
- [Source: src/open_ems/storage/repositories/config_audit_repo.py] — `ConfigAuditRepo.append_activation` precedent + `SAFETY_RELEVANT_CONFIG_FIELDS` audit vocabulary.
- [Source: migrations/versions/0007_add_peak_intervals_table.py] — migration style precedent for migration 0008.
- [Source: _bmad-output/implementation-artifacts/9-0-real-adapter-command-execution-contracts-and-implementations.md] — adversarial 3-layer review pattern + dev-notes structure precedent.

## Dev Agent Record

### Agent Model Used

Claude Opus 4.7 (claude-opus-4-7) via `/bmad-dev-story` workflow on 2026-05-10.

### Debug Log References

- Pre-story baseline: `pytest tests/ --no-cov -q` → **972 passed** (matches Story 9.0 close); ruff lint / ruff format / mypy all clean.
- Migration 0008 round-trip verified out-of-band via an ad-hoc Alembic `upgrade head` → `downgrade -1` → `upgrade head` against a temp SQLite DB; CHECK constraints + UNIQUE on `config_version` + index on `config_version DESC` all confirmed; the scratch script was deleted after verification.
- Final gate: `pytest tests/ --no-cov -q` → **1014 passed, 43 warnings, 0 failed**; mypy clean (83 files); ruff lint clean; ruff format clean (193 files).
- Two test-isolation issues surfaced and were fixed:
  1. `structlog.testing.capture_logs()` does not catch events when the active_constraints module logger was first bound during an earlier test that ran `configure_logging()` (the `cache_logger_on_first_use=True` proxy holds onto the configured logger across `reset_defaults`). Fix: assert log emission via `unittest.mock.patch("open_ems.services.active_constraints.logger")` instead of `capture_logs()` in the three affected unit tests.
  2. The same cached-logger leakage affected the pre-existing `tests/unit/services/test_audit_log.py::test_audit_writes_structlog` once the new integration test ran first in collection order. Fix: added a session-wide `structlog.reset_defaults()` autouse fixture to `tests/conftest.py` so structlog config never leaks between tests.

### Completion Notes List

- **AC3 ↔ AC4/AC10/R3 reconciliation:** the AC3 cell prescribes `config_version: int, ge=1` but AC4 / AC10 #11 / R3 cold-start coverage all explicitly require the seed-from-Settings branch to produce `config_version=0`. The two-shape contradiction was resolved by relaxing the `ActiveConstraints` lower bound to `ge=0` (DB rows are guaranteed `>= 1` by `AUTOINCREMENT + UNIQUE`; the in-memory seed value of `0` is the only legal `0`). Documented inline in `tests/unit/core/test_constraints.py` module docstring.
- **PolicyGuard signature:** kept `settings: Settings` parameter even though it is no longer read after the two attribute substitutions — the story header AC5 prescribes "single signature update" and the existing parameter does no harm. AC9's grep test is on the attribute *reads*, not the parameter presence; the grep is clean.
- **`ConfigAuditRepo.append_activation` refactor:** added optional `config_version: int | None = None` and `commit: bool = True` parameters so `ConfigRepo.activate()` can drive a single atomic transaction across both inserts. Existing direct callers and the 17 existing `test_config_audit_repo.py` tests are backwards-compatible.
- **Atomicity:** `ConfigRepo.activate()` commits any implicit transaction, then issues `BEGIN IMMEDIATE`, then inserts active row + audit rows, then commits — or rolls back on any exception. `tests/unit/storage/repositories/test_config_repo.py::test_activate_atomic_under_audit_failure` patches the audit insert to raise mid-transaction and verifies both tables are unchanged. This closes the spirit of Story 6.3's deferred non-atomic-config_version finding for the activation path (other direct `ConfigAuditRepo.append_activation` callers, none in production today, remain non-atomic and are out of scope per R7).
- **Lifespan ordering (AC7):** provider construction + hydrate runs between `init_database()` and PolicyGuard/ControlLoop construction. `tests/integration/engine/test_constraints_reload.py::test_lifespan_hydrates_before_control_loop` spies on `asyncio.create_task` and asserts `provider.is_hydrated is True` at the moment the `control_loop` task is scheduled — caught at the boundary R3 #1 / R5 lifespan-step-4b were designed to lock.
- **Source-of-truth fragmentation (R6):** PolicyGuard and ControlLoop both receive the same provider instance from `app.py`; `tests/unit/engine/test_control_loop.py::test_evaluation_input_reads_constraints_from_provider_not_settings` proves the loop reads from the provider (it sets the provider's snapshot to values that disagree with Settings and asserts the engine input matches the provider, not Settings).
- **AC9 seed-only enforcement:** `tests/unit/test_settings_seed_only.py` uses a regex `(?:^|[^A-Za-z0-9_])_?settings\.(?:peak_limit_kw|battery_reserve_floor_percent)\b` so it catches `settings.X`, `_settings.X`, `self._settings.X` but NOT `constraints.X` / `input.X` / `previous.X` (the legal provider-snapshot reads). Sentinel-verified: temporarily inserting a forbidden read into `policy_guard.py` made the test fail with a precise diagnostic, then the sentinel was removed.
- **Test-fixture sharing:** added `tests/fixtures/active_constraints.py::make_active_constraints_provider(settings)` — a sync helper that builds a pre-hydrated provider by setting `_current` directly. Used by every integration test that constructs PolicyGuard / ControlLoop, keeping per-call-site diff to one new kwarg line.
- **No new audit event types introduced.** Audit emission on activation is delegated to the existing `ConfigAuditRepo.append_activation` (Story 6.3). The provider is a pure read cache — see R4 cancellation-ownership map.
- **No new dependencies.** `uv.lock` unchanged.

### File List

**New files:**

- `migrations/versions/0008_add_active_constraints_table.py`
- `src/open_ems/core/constraints.py`
- `src/open_ems/storage/repositories/config_repo.py`
- `src/open_ems/services/active_constraints.py`
- `tests/unit/core/test_constraints.py`
- `tests/unit/storage/repositories/test_config_repo.py`
- `tests/unit/services/test_active_constraints.py`
- `tests/unit/test_settings_seed_only.py`
- `tests/integration/engine/test_constraints_reload.py`
- `tests/fixtures/active_constraints.py`

**Modified files:**

- `src/open_ems/engine/policy_guard.py` — added `active_constraints: ActiveConstraintsProvider` constructor param; replaced two `self._settings.*` reads with provider reads inside `_check_safety_constraints`; module docstring updated.
- `src/open_ems/engine/control_loop.py` — added `active_constraints` constructor param; replaced two reads in `_build_evaluation_input`; module docstring updated.
- `src/open_ems/web/app.py` — added `ActiveConstraintsProvider`/`ConfigRepo` imports; inserted hydrate step between `init_database` and PolicyGuard/ControlLoop construction; provider stored on `app.state.active_constraints_provider`; fail-loud on hydrate failure.
- `src/open_ems/settings.py` — added seed-only docstrings on `peak_limit_kw` and `battery_reserve_floor_percent`.
- `src/open_ems/storage/repositories/config_audit_repo.py` — `append_activation` accepts optional `config_version` and `commit` kwargs so `ConfigRepo.activate` can drive a single transaction.
- `tests/conftest.py` — added autouse `_reset_structlog_config` fixture.
- `tests/unit/engine/test_policy_guard.py` — added `_active_constraints_provider` helper; threaded `active_constraints=...` into all 14 `PolicyGuard(...)` call sites.
- `tests/unit/engine/test_control_loop.py` — threaded provider through `_control_loop` factory; added `test_evaluation_input_reads_constraints_from_provider_not_settings`.
- `tests/integration/engine/test_retry_pipeline.py` — threaded provider into both PolicyGuard constructions.
- `tests/integration/engine/test_fail_safe_lifecycle.py` — threaded provider into PolicyGuard and ControlLoop constructions.
- `tests/integration/engine/test_decision_engine_simulation.py` — threaded provider into both PolicyGuard constructions.
- `tests/integration/engine/test_command_pipeline.py` — threaded provider into both PolicyGuard constructions.
- `tests/integration/adapters/test_battery_send_command.py` — threaded provider into all four PolicyGuard constructions.
- `tests/integration/adapters/test_ev_charger_send_command.py` — threaded provider into all five PolicyGuard constructions.
- `_bmad-output/planning-artifacts/architecture.md` — BAD-3 implementation note appended.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — `9-0b-db-backed-active-constraints-reload: review`; `last_updated` bumped.

### Change Log

| Date | Change | Author |
|---|---|---|
| 2026-05-10 | Story 9.0b implementation complete; status → review; 1014 pytest passed (baseline 972 + 42 new); mypy / ruff / ruff-format clean | dev-story (Claude Opus 4.7) |
