# Story 8.1: Implement the runtime control loop with adapter polling and StateStore publishing

Status: done

## Story

As a developer,
I want a runtime control loop that polls device adapters, converts failures to DegradedDeviceState, publishes fresh system state to StateStore, and constructs evaluation input for each decision cycle,
so that the system continuously re-evaluates energy state from real device data and the StateStore always reflects the latest honest adapter output — never invented or stale values carried forward silently.

## Acceptance Criteria

**AC1 — Adapter polling and StateStore publishing**

GIVEN the control loop is running
WHEN the evaluation interval elapses (configurable via `settings.control_loop_interval_seconds`, default 10 s)
THEN it calls `adapter.get_state()` on every registered adapter before constructing evaluation input — each call is wrapped in `asyncio.wait_for(..., timeout=10.0)`
AND timeouts and all adapter exceptions are converted to `DegradedDeviceState(device_id=adapter.device_id, role=<role>, reason=str(exc), occurred_at=datetime.now(UTC))` — raw device state from a prior cycle is NEVER carried forward without a successful fresh read
AND `StateStore.publish(device_states, operating_mode=<last_evaluated_mode>)` is called once per tick with all fresh device states (degraded or live)
AND on the very first tick, `operating_mode=None` is passed (StateStore keeps its initialized value); from tick 2 onward, `operating_mode=result.recommended_operating_mode` from the previous cycle is passed

**AC2 — EvaluationInput construction and evaluation**

GIVEN adapter polling and StateStore publishing have completed for the current tick
WHEN evaluation input is assembled
THEN a snapshot is obtained from the published StateStore, and `EvaluationInput.from_snapshot(snapshot, peak_context=..., strategy=..., battery_control=..., ev_scheduling=...)` is called
AND `PeakContext` is built via `tracker.build_peak_context(configured_peak_limit_kw=settings.peak_limit_kw, current_monthly_recorded_peak_kw=self._monthly_peak_kw, at=now)`
AND `evaluate_cycle(evaluation_input)` is called and `result.recommended_operating_mode` is captured for the next tick
AND the `EvaluationResult` is forwarded to `_handle_evaluation_result(result)` — a stub that logs the result (Story 8.2 replaces this)

**AC3 — Finalize-before-evaluate invariant (from Story 8.0b AC6)**

GIVEN the control loop calls `tracker.update(power_kw, at=now)` every tick
WHEN `update()` returns a `CompletedInterval`
THEN `energy_repo.write_peak_interval(completed, data_quality=...)` is called BEFORE `tracker.build_peak_context()` is called — enforced by tick ordering, not convention
AND `self._monthly_peak_kw` is refreshed from `energy_repo.get_current_monthly_peak_kw()` immediately after writing the completed interval
AND the tracker is NOT reset manually — `update()` handles rollover internally; the caller only persists the returned `CompletedInterval`

**AC4 — Grid meter degradation and gap handling**

GIVEN the grid meter adapter returns `DegradedDeviceState` or raises on a tick
WHEN the tracker is updated
THEN `tracker.update(power_kw=0.0, at=now)` is still called to keep the tracker's clock advancing
AND if this causes a `CompletedInterval` to be returned (interval boundary crossed), it is written with `data_quality="incomplete"` to record the billing gap
AND `current_monthly_recorded_peak_kw` is refreshed after that write

GIVEN `completed.sample_count > 0`
THEN `data_quality="complete"` is used

GIVEN `completed.sample_count == 0`
THEN `data_quality="incomplete"` is used

**AC5 — Peak interval persistence**

GIVEN a completed 15-minute interval is finalized (any tick)
WHEN it is persisted
THEN one `peak_intervals` row is written via `energy_repo.write_peak_interval()`
AND persistence is NOT skipped when the operating mode is `degraded` or `fail_safe`
AND the `peak_intervals` table is the ONLY write path for peak intervals — no other component writes to it

**AC6 — New Settings fields**

Three fields are added to `Settings` in `src/open_ems/settings.py`:
- `control_loop_interval_seconds: float = Field(default=10.0, gt=0.0)` — tick cadence
- `peak_limit_kw: float = Field(default=25.0, gt=0.0)` — configured site peak limit passed to `build_peak_context()`
- `battery_reserve_floor_percent: float = Field(default=20.0, ge=0.0, le=100.0)` — passed to `BatteryControlContext`

**AC7 — EnergyRepo and peak_intervals schema**

Migration `migrations/versions/0007_add_peak_intervals_table.py` creates:

```sql
CREATE TABLE peak_intervals (
    interval_start_utc TEXT NOT NULL PRIMARY KEY,
    avg_power_kw REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    data_quality TEXT NOT NULL DEFAULT 'complete',
    is_monthly_peak INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_peak_intervals_is_monthly_peak ON peak_intervals (is_monthly_peak);
```

`EnergyRepo` at `src/open_ems/storage/repositories/energy_repo.py` provides:
- `write_peak_interval(completed: CompletedInterval, *, data_quality: str = "complete") -> None` — INSERT OR REPLACE
- `get_current_monthly_peak_kw() -> float` — `MAX(avg_power_kw)` WHERE `interval_start_utc >= <UTC month start>`; returns `0.0` if no rows

**AC8 — Startup wiring**

The `ControlLoop` asyncio task is started in `app.py` lifespan after StateStore and database are initialized
It follows the established done-callback pattern (see `_on_watchdog_done` / `_on_cleanup_done`)
It is cancelled cleanly during lifespan shutdown (same pattern as `_watchdog_task`)
Initial adapter dict is `{}` — Epic 9's installer setup will populate adapters at runtime

**AC9 — Tests**

Unit tests in `tests/unit/engine/test_control_loop.py`:
- `test_poll_failure_converts_to_degraded_state()` — adapter raises → `DegradedDeviceState` in `publish()` call; no prior-cycle state retained
- `test_evaluation_input_constructed_from_fresh_snapshot()` — mocked adapters + tracker → `EvaluationInput` fields match expected values
- `test_finalize_before_evaluate_ordering()` — `tracker.update()` returning `CompletedInterval` triggers `energy_repo.write_peak_interval` BEFORE `evaluate_cycle()` (via `AsyncMock` spy; assert call order)
- `test_peak_intervals_data_quality_complete_when_samples_present()` — `completed.sample_count > 0` → `write_peak_interval` called with `data_quality="complete"`
- `test_peak_intervals_data_quality_incomplete_when_meter_degraded()` — grid meter `DegradedDeviceState` → `write_peak_interval` called with `data_quality="incomplete"`
- `test_peak_intervals_not_skipped_in_degraded_mode()` — `operating_mode=degraded` → `write_peak_interval` still called at boundary
- `test_peak_intervals_not_skipped_in_fail_safe_mode()` — `operating_mode=fail_safe` → `write_peak_interval` still called at boundary

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — record baseline (≥ 767)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`

- [x] Task 1: Add Settings fields (AC6)
  - [x] Open `src/open_ems/settings.py`
  - [x] Add `control_loop_interval_seconds`, `peak_limit_kw`, `battery_reserve_floor_percent` with `Field(default=..., ...)` validators
  - [x] Follow existing pattern: `ntp_drift_threshold_seconds: float = Field(default=2.0, gt=0.0)`

- [x] Task 2: Create peak_intervals migration (AC7)
  - [x] Create `migrations/versions/0007_add_peak_intervals_table.py`
  - [x] Set `revision = "0007"`, `down_revision = "0006"` (follow 0006 pattern exactly)
  - [x] Use `op.create_table()` with `sa.Column()` entries for all five columns
  - [x] Add index on `is_monthly_peak` column
  - [x] Implement `downgrade()` dropping the index then the table
  - [x] Verify: `uv run alembic upgrade head` runs cleanly

- [x] Task 3: Create EnergyRepo (AC7)
  - [x] Create `src/open_ems/storage/repositories/energy_repo.py`
  - [x] Follow `event_log_repo.py` pattern: `__init__(self, conn: aiosqlite.Connection | None = None)`; use `get_connection()` as fallback
  - [x] Implement `write_peak_interval`: `INSERT OR REPLACE INTO peak_intervals (interval_start_utc, avg_power_kw, sample_count, data_quality) VALUES (?, ?, ?, ?)` using `completed.interval_start_utc.isoformat()`; commit after insert
  - [x] Implement `get_current_monthly_peak_kw()`: query `MAX(avg_power_kw)` WHERE `interval_start_utc >= <month_start>.isoformat()`; return `0.0` on empty result
  - [x] Add `from __future__ import annotations` and UTC imports

- [x] Task 4: Implement ControlLoop class (AC1, AC2, AC3, AC4, AC5)
  - [x] Create `src/open_ems/engine/control_loop.py`
  - [x] Define `ControlLoop.__init__(self, *, state_store: StateStore, adapters: Mapping[DeviceRole, DeviceAdapter], energy_repo: EnergyRepo, settings: Settings) -> None`
  - [x] Initialize `self._tracker = PartialIntervalTracker.from_current_time(at=datetime.now(UTC))`
  - [x] Initialize `self._monthly_peak_kw: float = 0.0` and `self._last_mode: SystemOperatingMode | None = None`
  - [x] Implement `async def run(self) -> None`: `while True: await self._tick(); await asyncio.sleep(self._settings.control_loop_interval_seconds)`
  - [x] Implement `async def _tick(self) -> None`: poll → publish → update tracker → build input → evaluate → handle result (see ordering in Dev Notes)
  - [x] Implement `async def _poll_adapters(self) -> dict[DeviceRole, DeviceSlot]`
  - [x] Implement `async def _update_tracker(self, device_states: dict[DeviceRole, DeviceSlot], now: datetime) -> None` (finalize-before-evaluate + persist)
  - [x] Implement `def _build_evaluation_input(self, snapshot: SystemSnapshot, now: datetime) -> EvaluationInput`
  - [x] Implement `def _handle_evaluation_result(self, result: EvaluationResult) -> None` (stub: structlog debug + capture `self._last_mode`)
  - [x] Never use `time.sleep()` — always `await asyncio.sleep(...)`
  - [x] Use `datetime.now(UTC)` — never naive datetime

- [x] Task 5: Wire ControlLoop into app.py (AC8)
  - [x] Import `ControlLoop` and `EnergyRepo` at top of `src/open_ems/web/app.py`
  - [x] After database init and admin bootstrap, instantiate `ControlLoop(state_store=app.state.state_store, adapters={}, energy_repo=EnergyRepo(), settings=settings)`
  - [x] Create task with done-callback: `_control_loop_task = asyncio.create_task(control_loop.run(), name="control_loop")`
  - [x] Cancel and await in shutdown block (follow `_watchdog_task` pattern exactly)
  - [x] Log `control_loop_started` at INFO after task creation

- [x] Task 6: Write unit tests (AC9)
  - [x] Create `tests/unit/engine/test_control_loop.py`
  - [x] Use `AsyncMock` for adapters (`.get_state()`) and energy_repo (`.write_peak_interval()`, `.get_current_monthly_peak_kw()`)
  - [x] Use a real `PartialIntervalTracker` in tests (don't mock it — its behavior is critical)
  - [x] Test `_update_tracker` and `_poll_adapters` directly as methods; do not run the full `run()` loop
  - [x] Assert call order for finalize-before-evaluate test using `unittest.mock.call_args_list` or `MagicMock.assert_called_before`
  - [x] Check existing `tests/unit/engine/` for pytest-asyncio marker pattern

- [x] Task 7: Final validation (AC: all)
  - [x] Full suite passes (≥ 767 + new tests)
  - [x] `uv run python -m ruff check .` clean
  - [x] `uv run python -m ruff format --check .` clean
  - [x] `uv run python -m mypy src/` clean

## Dev Notes

### Tick Ordering — Critical Sequence

The exact per-tick sequence (do not reorder):

```
1.  now = datetime.now(UTC)
2.  device_states = await self._poll_adapters()          # fresh or DegradedDeviceState per role
3.  snapshot = await self._state_store.publish(          # write to StateStore FIRST
        device_states,
        operating_mode=self._last_mode,                  # None on first tick
    )
4.  grid_power, data_quality = _extract_grid_power(device_states, now)
5.  completed = self._tracker.update(grid_power, at=now)
6.  if completed is not None:                            # interval boundary crossed
        await self._energy_repo.write_peak_interval(
            completed, data_quality=data_quality         # MUST precede build_peak_context
        )
        self._monthly_peak_kw = await self._energy_repo.get_current_monthly_peak_kw()
7.  peak_context = self._tracker.build_peak_context(
        configured_peak_limit_kw=self._settings.peak_limit_kw,
        current_monthly_recorded_peak_kw=self._monthly_peak_kw,
        at=now,
    )
8.  evaluation_input = self._build_evaluation_input(snapshot, now, peak_context)
9.  result = evaluate_cycle(evaluation_input)
10. self._handle_evaluation_result(result)               # sets self._last_mode
```

Swapping steps 6 and 7 causes `ValueError` from `build_peak_context()` once an interval is complete — this is the mechanical enforcement from Story 8.0b.

### DeviceAdapter Interface (from `src/open_ems/core/devices.py:262`)

```python
class DeviceAdapter(Protocol):
    device_id: str
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def get_state(self) -> DeviceState | DegradedDeviceState: ...
    async def get_capabilities(self) -> DeviceCapabilityProfile: ...
```

Poll pattern:
```python
try:
    state = await asyncio.wait_for(adapter.get_state(), timeout=10.0)
except Exception as exc:
    state = DegradedDeviceState(
        device_id=adapter.device_id,
        role=role,
        reason=str(exc),
        occurred_at=datetime.now(UTC),
    )
```

**`DeviceAdapter` has no `role` attribute** — the `adapters: Mapping[DeviceRole, DeviceAdapter]` dict provides the role mapping. Use the dict key as the role when constructing `DegradedDeviceState`.

### Grid Power Extraction

```python
def _extract_grid_power(
    device_states: dict[DeviceRole, DeviceSlot],
    now: datetime,
) -> tuple[float, str]:
    slot = device_states.get(DeviceRole.grid_meter)
    if isinstance(slot, GridMeterState):
        return slot.grid_power_kw, "complete"
    return 0.0, "incomplete"
```

Returns `(power_kw, data_quality)`. Use `0.0` for degraded/missing grid meter to advance the tracker's clock; resulting CompletedInterval will have `sample_count=0` and receive `data_quality="incomplete"`.

### EvaluationInput Assembly

```python
EvaluationInput.from_snapshot(
    snapshot,
    peak_context=peak_context,
    strategy=EnergyStrategy.maximize_self_consumption,  # hardcoded default until Epic 9/10
    battery_control=BatteryControlContext(
        reserve_floor_percent=self._settings.battery_reserve_floor_percent,
        capability_profile=None,  # Epic 9 will inject from adapter.get_capabilities()
    ),
    ev_scheduling=EVSchedulingContext(
        capability_profile=None,
        charging_window=None,      # Epic 9 installer config
        homeowner_override_active=False,  # Epic 10 homeowner dashboard
        evaluated_at=now,
        target_charge_rate_kw=None,
    ),
)
```

Use `EnergyStrategy.maximize_self_consumption` as hardcoded default for Story 8.1 — Epic 9/10 will make this configurable from DB.

### _handle_evaluation_result() Stub

```python
def _handle_evaluation_result(self, result: EvaluationResult) -> None:
    """Story 8.2 replaces this with IntentExecutor + PolicyGuard dispatch."""
    self._last_mode = result.recommended_operating_mode
    logger.debug(
        "evaluation_cycle_complete",
        cycle_id=str(result.cycle_id),
        operating_mode=result.recommended_operating_mode.value,
        cycle_duration_ms=result.cycle_duration_ms,
        intent_count=len(result.intents),
    )
```

Do NOT implement any command dispatch or PolicyGuard logic here.

### EnergyRepo aiosqlite Pattern

Follow `src/open_ems/storage/repositories/event_log_repo.py` exactly:
- Constructor: `def __init__(self, conn: aiosqlite.Connection | None = None)` with `get_connection()` fallback
- Execute + commit: `async with self._conn.execute(...) as cursor: ...` then `await self._conn.commit()`

```python
async def write_peak_interval(
    self,
    completed: CompletedInterval,
    *,
    data_quality: str = "complete",
) -> None:
    async with self._conn.execute(
        "INSERT OR REPLACE INTO peak_intervals"
        " (interval_start_utc, avg_power_kw, sample_count, data_quality)"
        " VALUES (?, ?, ?, ?)",
        (
            completed.interval_start_utc.isoformat(),
            completed.avg_power_kw,
            completed.sample_count,
            data_quality,
        ),
    ):
        pass
    await self._conn.commit()

async def get_current_monthly_peak_kw(self) -> float:
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    async with self._conn.execute(
        "SELECT COALESCE(MAX(avg_power_kw), 0.0) FROM peak_intervals"
        " WHERE interval_start_utc >= ?",
        (month_start,),
    ) as cursor:
        row = await cursor.fetchone()
    return float(row[0]) if row else 0.0
```

### Migration Pattern

Follow `migrations/versions/0006_add_config_audit_log_table.py`:
```python
revision: str = "0007"
down_revision: str | None = "0006"
```

Use `sa.Text()` for string columns (consistent with other migrations) and `sa.Real()` / `sa.Integer()` for numeric columns.

### app.py Wiring

Add the control loop after the admin bootstrap block and before `yield`:
```python
_control_loop: ControlLoop | None = None
_control_loop_task: asyncio.Task[None] | None = None
...
_control_loop = ControlLoop(
    state_store=app.state.state_store,
    adapters={},     # Epic 9 installer setup will populate this
    energy_repo=EnergyRepo(),
    settings=settings,
)

def _on_control_loop_done(t: asyncio.Task[None]) -> None:
    if not t.cancelled():
        exc = t.exception()
        if exc is not None:
            logger.error("control_loop_died", exc_info=exc, component="engine")

_control_loop_task = asyncio.create_task(_control_loop.run(), name="control_loop")
_control_loop_task.add_done_callback(_on_control_loop_done)
logger.info("control_loop_started", component="engine")
```

Add shutdown block (before `yield` teardown ends) matching `_watchdog_task` pattern exactly.

### Import Boundary for control_loop.py

ALLOWED:
- `open_ems.engine` — `evaluate_cycle`, `EvaluationInput`, `EvaluationResult`, `PartialIntervalTracker`, `CompletedInterval`
- `open_ems.engine.models` — `BatteryControlContext`, `EVSchedulingContext`, `PeakContext`
- `open_ems.core` — `StateStore`, `DeviceAdapter`, `DeviceRole`, `DeviceState`, `DegradedDeviceState`, `GridMeterState`, `SystemSnapshot`, `SystemOperatingMode`, `EnergyStrategy`
- `open_ems.storage.repositories.energy_repo` — `EnergyRepo`
- `open_ems.settings` — `Settings`
- stdlib: `asyncio`, `datetime`, `collections.abc`, `typing`
- `structlog`

FORBIDDEN: `open_ems.web`, `open_ems.adapters` (adapters injected via constructor), `open_ems.services` (watchdog is in 8.4)

### StateStore.publish() Return Value

`publish()` returns the new `SystemSnapshot`. Capture it immediately for `EvaluationInput.from_snapshot()` — do NOT call `state_store.get_snapshot()` separately afterward (race-free because StateStore is single-writer).

```python
snapshot = await self._state_store.publish(device_states, operating_mode=self._last_mode)
```

### Handling Empty Adapter Dict

When `adapters={}`, `_poll_adapters()` returns `{}`. `StateStore.publish({})` publishes no device states (existing behavior — the snapshot retains any prior device states). The tracker still advances with `0.0` power. EvaluationInput is constructed normally. This is correct startup behavior before Epic 9 configures devices.

Check `StateStore.publish()` in `src/open_ems/core/state_store.py` for how it handles empty `device_states` (do not assume — read the implementation).

### Testing Approach

Test `_update_tracker` and `_poll_adapters` as standalone async methods — do not drive the full `run()` loop:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_finalize_before_evaluate_ordering():
    energy_repo = AsyncMock()
    energy_repo.write_peak_interval = AsyncMock()
    energy_repo.get_current_monthly_peak_kw = AsyncMock(return_value=0.0)
    
    # ... construct ControlLoop with mocked adapters, state_store, energy_repo
    # ... advance tracker to near boundary via _update_tracker
    # ... then call _update_tracker again past the boundary
    # ... assert energy_repo.write_peak_interval was called
    # ... assert energy_repo.write_peak_interval call precedes evaluate_cycle call
    #     (check by inspecting mock call history on a combined mock or using call_args_list)
```

Check `tests/unit/engine/` for existing `@pytest.mark.asyncio` usage — do NOT invent a new pattern.

### Project Structure Notes

- `src/open_ems/engine/control_loop.py` — NEW (architecture mandated this exact path)
- `src/open_ems/storage/repositories/energy_repo.py` — NEW
- `migrations/versions/0007_add_peak_intervals_table.py` — NEW (next revision after 0006)
- `src/open_ems/settings.py` — MODIFY (add 3 fields)
- `src/open_ems/web/app.py` — MODIFY (wire control loop task)

Do NOT create `energy_repo.py` in `src/open_ems/services/` — it belongs in `storage/repositories/` with all other DB repos.

### References

- [Source: epics.md — Epic 8 preamble + Story 8.1 AC]
- [Source: architecture.md — Startup sequence, Engine module structure, BAD-1 (peak tracking), State flow, Asyncio patterns]
- [Source: 8-0b story — Dev Notes: Finalize-before-evaluate invariant, Clock alignment, gap handling, Story 8.1 AC update section]
- [Source: src/open_ems/engine/models.py — `EvaluationInput.from_snapshot()`, `BatteryControlContext`, `EVSchedulingContext`]
- [Source: src/open_ems/engine/partial_interval_tracker.py — `PartialIntervalTracker.from_current_time()`, `update()`, `build_peak_context()`]
- [Source: src/open_ems/core/devices.py:262 — `DeviceAdapter` protocol, `DegradedDeviceState`]
- [Source: src/open_ems/core/state_store.py — `StateStore.publish()` signature and return type]
- [Source: src/open_ems/storage/repositories/event_log_repo.py — aiosqlite repo pattern]
- [Source: migrations/versions/0006_add_config_audit_log_table.py — migration pattern]
- [Source: src/open_ems/web/app.py — asyncio task startup/shutdown pattern]

## Dev Agent Record

### Agent Model Used

claude-opus-4-7

### Debug Log References

- Baseline: 767 tests pass, ruff/format/mypy clean (2026-05-05).
- Final: 774 tests pass (767 + 7 new), 89.55% total coverage; ruff/format/mypy clean.

### Completion Notes List

- AC1–AC5 implemented in `src/open_ems/engine/control_loop.py`. Tick ordering matches Dev Notes exactly: poll → publish → tracker.update → (if completed) write_peak_interval + refresh monthly peak → build_peak_context → EvaluationInput.from_snapshot → evaluate_cycle → handle result.
- AC6: three new `Settings` fields added with the required validators.
- AC7: migration `0007_add_peak_intervals_table.py` created and applied cleanly via `alembic upgrade head`. `EnergyRepo` follows the `event_log_repo.py` pattern.
- AC8: `ControlLoop` task wired into `app.py` lifespan with done-callback (`_on_control_loop_done`) and a shutdown cancel-and-await block matching `_watchdog_task`. Initial adapter dict is `{}`; Epic 9 will populate it.
- AC9: All 7 unit tests added in `tests/unit/engine/test_control_loop.py`. `_tick`, `_poll_adapters`, and `_update_tracker` are exercised directly without driving `run()`.
- **Edge case clarification (deviation noted for review):** With `adapters={}` at startup, `_build_evaluation_input` would raise `ValidationError` because `EvaluationInput` requires non-None `inverter` and `grid_meter` slots. To prevent the loop dying on the first tick before Epic 9 wiring, `_tick` skips evaluation (with a `evaluation_skipped_missing_required_slots` debug log) when either required slot is None on the published snapshot. Peak tracking still runs every tick so AC3/AC4/AC5 invariants hold even before adapters are wired. This preserves the spirit of AC2 (evaluation runs whenever required inputs are available) without violating AC8 (loop must not crash at startup).
- `data_quality` for completed intervals is `"complete"` only when `completed.sample_count > 0` AND the boundary-crossing tick had a healthy grid meter; otherwise `"incomplete"`. This satisfies AC4 + AC5 (incomplete on degraded meter, complete on healthy data, persistence not skipped in degraded/fail_safe modes).

### File List

- `src/open_ems/settings.py` — MODIFIED: added 3 settings fields.
- `src/open_ems/engine/control_loop.py` — NEW: ControlLoop class.
- `src/open_ems/storage/repositories/energy_repo.py` — NEW: EnergyRepo with `write_peak_interval` and `get_current_monthly_peak_kw`.
- `migrations/versions/0007_add_peak_intervals_table.py` — NEW: peak_intervals table + index.
- `src/open_ems/web/app.py` — MODIFIED: wired ControlLoop task into lifespan.
- `tests/unit/engine/test_control_loop.py` — NEW: 7 unit tests for AC9.

### Review Findings

- [x] [Review][Decision] Evaluation silently skipped when inverter or grid_meter slot is absent — resolved: replaced upfront `None` guard with `try/except ValidationError` around `_build_evaluation_input`; evaluation attempted every tick (AC2 conformant); `ValidationError` caught and logged gracefully. [`control_loop.py`]
- [x] [Review][Patch] `data_quality` AND condition — re-evaluated as correct per AC4: degraded meter at boundary crossing → `"incomplete"` regardless of prior sample count; `tick_quality == "complete"` guard implements this correctly. Reverted; original logic retained. [`control_loop.py:109-113`]
- [x] [Review][Patch] `DegradedDeviceState.reason` loses exception message for zero-str exceptions — fixed: `str(exc)` → `repr(exc)`; `repr()` always includes class name and args. [`control_loop.py:95`]
- [x] [Review][Patch] Dead `if row else 0.0` branch in `get_current_monthly_peak_kw` — fixed: replaced with `assert row is not None`; COALESCE guarantees a row, assert provides loud failure on impossible None. [`energy_repo.py:43-44`]
- [x] [Review][Defer] `_monthly_peak_kw` not hydrated from DB on process restart — stays `0.0` until first interval completes; peak decisions use stale data for up to 15 min post-restart. [`control_loop.py:54`] — deferred, requires async init pattern not in this story's scope
- [x] [Review][Defer] Sequential adapter polling blocks all adapters when one is slow — `for role, adapter` loop awaits each adapter serially; N × timeout maximum poll time. Relevant when Epic 9 wires real adapters. [`control_loop.py:84-98`] — deferred, pre-existing design; Epic 9 concern
- [x] [Review][Defer] Adapter timeout (10 s) not bounded by `control_loop_interval_seconds` — with multiple failing adapters, poll phase alone can exceed the configured tick cadence. [`control_loop.py:33`, `settings.py`] — deferred, pre-existing architectural tradeoff
- [x] [Review][Defer] No deadline-relative sleep — `await asyncio.sleep(interval)` after `_tick()` means sustained adapter overload silently inflates effective tick period. [`control_loop.py:61`] — deferred, pre-existing design decision
- [x] [Review][Defer] Multi-interval stall silently drops intermediate `CompletedInterval`s — `tracker.update()` returns one `CompletedInterval` per call regardless of elapsed gaps; loop stalls >30 min drop billing windows. [`control_loop.py:107`] — deferred, tracker limitation from Story 8.0b; not fixable here
- [x] [Review][Defer] Control loop task death not escalated to fail-safe — `_on_control_loop_done` logs and continues; system serves requests with stale StateStore indefinitely. [`app.py:_on_control_loop_done`] — deferred, Epic 8.4 scope (watchdog + fail-safe transition)
- [x] [Review][Defer] Non-cooperative adapters may block shutdown despite cancel — `asyncio.wait_for` cancels the coroutine, but blocking I/O in adapter internals may continue running. [`control_loop.py:77-80`] — deferred, adapter protocol contract concern; not fixable at this layer
- [x] [Review][Defer] ISO string comparison for monthly peak boundary fragile against non-UTC timestamp formats — works correctly with current `datetime.now(UTC).isoformat()` usage but no schema-level enforcement of UTC suffix. [`energy_repo.py:37-41`] — deferred, pre-existing pattern; consistent in current codebase

## Change Log

- 2026-05-05 — Initial implementation of Story 8.1: control loop, EnergyRepo, peak_intervals migration, settings, app.py wiring, and unit tests.
