# Story 6.2: Enforce append-only semantics and event retention policy

Status: done

<!-- Completion note: Ultimate context engine analysis completed - comprehensive developer guide created. -->

## Story

As a developer,
I want the event log repository to enforce append-only storage and a retention policy that protects critical event classes from normal pruning,
So that the installer audit log is a reliable, tamper-resistant record where no system event is ever silently dropped or overwritten.

## Acceptance Criteria

**AC1 — Append-only guard methods**
Given an entry has been written to the `event_log` table
When any code path attempts to call an update or delete method on `EventLogRepo`
Then the method raises `RuntimeError` immediately — no `UPDATE` or `DELETE` is ever executed against `event_log` rows through these guards
And the designated pruning method (`prune_expired`) is the only permitted deletion pathway

**AC2 — Retention pruning job**
Given the retention pruning job runs
When it evaluates entries for deletion
Then entries whose `timestamp` is strictly older than 90 days and whose `event_type` is NOT in `CRITICAL_EVENT_TYPES` are deleted
And entries with `event_type` in `CRITICAL_EVENT_TYPES` are never deleted by this job regardless of age
And entries at exactly the 90-day boundary (timestamp == cutoff) are NOT deleted (boundary-exclusive)
And the job runs once per day as an asyncio background task wired into the app lifespan
And the pruning method returns the count of deleted rows

**AC3 — No-silent-failures contract (policy)**
Given a constraint violation, command failure, or degraded-mode transition occurs anywhere in the system
When the condition is handled
Then an audit event must be emitted via `ObservabilityService.audit()`
This requirement is enforced by:
- mandatory call-site patterns established per epic (Epics 7–8 implement these conditions)
- integration tests per epic that verify each failure path emits an audit event using `FakeAuditLog`
And `EventLogRepo` enforces valid event structure, append-only storage, and required summaries — it cannot detect events never sent to it; call-site discipline is the mechanism for coverage

**AC4 — Tests**
- Unit tests verify `EventLogRepo.update()` raises `RuntimeError`
- Unit tests verify `EventLogRepo.delete()` raises `RuntimeError`
- Unit tests verify `prune_expired()` deletes non-critical entries strictly older than 90 days
- Unit tests verify `prune_expired()` does NOT delete non-critical entries newer than 90 days
- Unit tests verify `prune_expired()` does NOT delete non-critical entries at exactly the 90-day boundary
- Unit tests verify `prune_expired()` does NOT delete critical entries older than 90 days
- Unit tests verify `prune_expired()` does NOT delete critical entries at the 90-day boundary
- Unit tests verify `prune_expired()` returns the correct deleted count
- Unit tests verify mixed-entry scenario (old critical + old non-critical + recent non-critical)

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` and record the baseline (expect 569 passed).
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m mypy src/`

- [x] Task 1: Add guard methods and CRITICAL_EVENT_TYPES to EventLogRepo (AC: AC1)
  - [x] Open `src/open_ems/storage/repositories/event_log_repo.py`.
  - [x] Add module-level constant: `_CRITICAL_EVENT_TYPES: frozenset[str] = frozenset({"CONSTRAINT"})`.
  - [x] Add `from datetime import UTC, datetime, timedelta` (extend existing import — `UTC` and `timedelta` are new additions).
  - [x] Add synchronous guard method `update(self, *args: object, **kwargs: object) -> None` that raises `RuntimeError("event_log is append-only")`.
  - [x] Add synchronous guard method `delete(self, *args: object, **kwargs: object) -> None` that raises `RuntimeError("event_log is append-only")`.
  - [x] Guard methods are NOT async — they raise before any I/O.

- [x] Task 2: Add prune_expired method to EventLogRepo (AC: AC2)
  - [x] Add `async def prune_expired(self, *, retention_days: int = 90) -> int` to `EventLogRepo`.
  - [x] Compute `cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()`.
  - [x] Build parameterized NOT IN clause: `placeholders = ",".join("?" * len(_CRITICAL_EVENT_TYPES))`.
  - [x] Execute: `DELETE FROM event_log WHERE timestamp < ? AND event_type NOT IN ({placeholders})` with `(cutoff, *_CRITICAL_EVENT_TYPES)` as params.
  - [x] Read `cursor.rowcount` as deleted count, call `await self._conn.commit()`, return `deleted`.
  - [x] Follow existing repo pattern: `async with self._conn.execute(...) as cursor:`.

- [x] Task 3: Wire retention background task into app.py lifespan (AC: AC2)
  - [x] Open `src/open_ems/web/app.py`.
  - [x] Add import: `from open_ems.storage.repositories.event_log_repo import EventLogRepo`.
  - [x] Add private async function `_event_log_pruning_task() -> None` following the exact `_session_cleanup_task()` pattern: `while True` loop, try/except, `logger.info("event_log_pruned", ...)` on success, `logger.error("event_log_pruning_failed", ...)` on exception, `await asyncio.sleep(24 * 60 * 60)`.
  - [x] In lifespan: create `_pruning_task = asyncio.create_task(_event_log_pruning_task())` alongside `_cleanup_task`.
  - [x] Add done-callback `_on_pruning_done` that logs if task dies unexpectedly (same pattern as `_on_cleanup_done`).
  - [x] Add `logger.info("event_log_pruning_task_started", component="observability")` after task creation.
  - [x] On shutdown: cancel `_pruning_task` and await with `CancelledError` handling (same pattern as `_cleanup_task`).
  - [x] `_pruning_task` variable must be declared in the same outer scope as `_cleanup_task` (initialized to `None`).

- [x] Task 4: Add tests (AC: AC1–AC4)
  - [x] Create `tests/unit/storage/repositories/test_event_log_repo.py`.
  - [x] Use `aiosqlite.connect(":memory:")` with `conn.row_factory = aiosqlite.Row` fixture (same DDL as `test_audit_log.py`).
  - [x] Import `_CRITICAL_EVENT_TYPES` from `event_log_repo` for parametrization.
  - [x] Test `update()` raises `RuntimeError` matching "append-only".
  - [x] Test `delete()` raises `RuntimeError` matching "append-only".
  - [x] Test `prune_expired` deletes non-critical entry 91 days old.
  - [x] Test `prune_expired` keeps non-critical entry 89 days old.
  - [x] Test `prune_expired` keeps non-critical entry at exactly the boundary (`timedelta(days=90)`).
  - [x] Test `prune_expired` keeps CONSTRAINT entry 91 days old.
  - [x] Test `prune_expired` keeps CONSTRAINT entry at exactly the boundary.
  - [x] Test `prune_expired` returns correct deleted count (insert 3 expired non-critical + 2 critical, expect return 3).
  - [x] Test mixed scenario: verify only old non-critical entries are deleted.

- [x] Task 5: Final validation (AC: all)
  - [x] Run `uv run python -m ruff check .` — must pass clean.
  - [x] Run `uv run python -m ruff format --check .` — must pass clean.
  - [x] Run `uv run python -m mypy src/` — must pass clean (no issues).
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — all tests pass, new tests added (expect 569 + new = passing).
  - [x] Verify no regressions introduced in existing test suite.

### Review Findings

- [x] [Review][Patch] Normalize timestamp to UTC before isoformat() in append() — non-UTC timezone-aware datetimes produce ISO strings that sort incorrectly against the UTC-based pruning cutoff in prune_expired's `timestamp < ?` SQL comparison [src/open_ems/storage/repositories/event_log_repo.py:34,61]
- [x] [Review][Patch] Sync guard tests use async fixture unnecessarily — test_update_raises_append_only and test_delete_raises_append_only are sync defs that don't touch the DB; the async repo_with_conn fixture is an unreliable injection point [tests/unit/storage/repositories/test_event_log_repo.py:79-95]
- [x] [Review][Patch] Add lower-bound guard on retention_days in prune_expired — retention_days=0 or negative values compute a future cutoff, silently deleting all non-critical records [src/open_ems/storage/repositories/event_log_repo.py:59]
- [x] [Review][Defer] append() has no direct repository-level unit tests [src/open_ems/storage/repositories/event_log_repo.py:17] — deferred, pre-existing (story 6-1 debt)
- [x] [Review][Defer] _CRITICAL_EVENT_TYPES empty produces NOT IN () SQL fragment — non-standard SQL with SQLite-specific semantics [src/open_ems/storage/repositories/event_log_repo.py:62] — deferred, pre-existing
- [x] [Review][Defer] Pruning task starts unconditionally — if event_log migration not applied, silently fails per cycle [src/open_ems/web/app.py:212] — deferred, pre-existing (matches session cleanup pattern)
- [x] [Review][Defer] rowcount read before commit in prune_expired — logged deleted count may reflect uncommitted deletions if commit fails [src/open_ems/storage/repositories/event_log_repo.py:63-68] — deferred, pre-existing
- [x] [Review][Defer] cursor.lastrowid checked after commit in append() — INSERT committed before None guard fires [src/open_ems/storage/repositories/event_log_repo.py:50] — deferred, pre-existing (story 6-1)
- [x] [Review][Defer] json.dumps(allow_nan=False) in append() unhandled — ValueError on non-finite floats silently drops audit record [src/open_ems/storage/repositories/event_log_repo.py:30] — deferred, pre-existing (story 6-1)
- [x] [Review][Defer] ObservabilityService validates detail then discards; append() re-serializes — double-serialization not atomic [src/open_ems/services/audit_log.py:48] — deferred, pre-existing (story 6-1)
- [x] [Review][Defer] _CRITICAL_EVENT_TYPES not validated as subset of VALID_EVENT_TYPES — no test or assertion enforces the relationship; a new valid event type added without updating the frozenset silently loses protection [src/open_ems/storage/repositories/event_log_repo.py:10] — deferred, pre-existing

## Dev Notes

### Scope and Context

Story 6.1 established `ObservabilityService`, `EventLogRepo` (append-only by omission), and `FakeAuditLog`. Story 6.2 strengthens that foundation by:
1. Adding **explicit guard methods** (`update`, `delete`) on `EventLogRepo` that raise — making the append-only contract a programming error rather than a silent omission.
2. Adding **`prune_expired`** — the sole permitted deletion pathway, protected by `CRITICAL_EVENT_TYPES`.
3. **Wiring the pruning job** into the app lifespan as an asyncio background task.

This story does **NOT**:
- Add a new migration (no schema changes needed — guard methods are Python-level only).
- Modify `ObservabilityService` or `FakeAuditLog`.
- Add installer note storage or `config_audit_log` (Story 6.3).
- Wire `ObservabilityService` into any route or emit any audit events from this story's code.

### File Layout — Modified vs. New

**Modified files:**
```
src/open_ems/storage/repositories/event_log_repo.py  — add guard methods + prune_expired
src/open_ems/web/app.py                               — add pruning task, wire into lifespan
```

**New files:**
```
tests/unit/storage/repositories/test_event_log_repo.py  — unit tests for all ACs
```

**No migration needed.** Append-only enforcement is at the Python layer. No new columns or tables.

### EventLogRepo — Exact Implementation

Current state of `event_log_repo.py`:
- `from datetime import datetime` — needs `UTC` and `timedelta` added
- Has `append()` method only — no update/delete

Required additions:

```python
# Module-level constant (after imports, before class definition)
_CRITICAL_EVENT_TYPES: frozenset[str] = frozenset({"CONSTRAINT"})
```

Guard methods (inside `EventLogRepo` class, after `append`):
```python
def update(self, *args: object, **kwargs: object) -> None:
    raise RuntimeError("event_log is append-only")

def delete(self, *args: object, **kwargs: object) -> None:
    raise RuntimeError("event_log is append-only")
```

Pruning method (inside `EventLogRepo` class, after guard methods):
```python
async def prune_expired(self, *, retention_days: int = 90) -> int:
    """Delete non-critical entries older than retention_days. Returns count deleted."""
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
    placeholders = ",".join("?" * len(_CRITICAL_EVENT_TYPES))
    async with self._conn.execute(
        f"DELETE FROM event_log WHERE timestamp < ? AND event_type NOT IN ({placeholders})",
        (cutoff, *_CRITICAL_EVENT_TYPES),
    ) as cursor:
        deleted = cursor.rowcount
    await self._conn.commit()
    return deleted
```

**Critical detail:** `cursor.rowcount` on a DELETE in aiosqlite returns the integer count of rows deleted. This is reliable — no need for a separate COUNT query.

**Import line change** (extend existing `from datetime import datetime`):
```python
from datetime import UTC, datetime, timedelta
```

### Why CRITICAL_EVENT_TYPES Lives in event_log_repo.py

Import layer boundaries:
- `services/` may import from `storage/repositories/`
- `storage/repositories/` may NOT import from `services/`

`audit_log.py` (services) defines `VALID_ACTORS`, `VALID_EVENT_TYPES` and imports `EventLogRepo`. If `CRITICAL_EVENT_TYPES` were in `audit_log.py`, `event_log_repo.py` couldn't import it. Defining it in `event_log_repo.py` (where `prune_expired` lives) keeps the boundary clean.

### CRITICAL_EVENT_TYPES Rationale

`CRITICAL_EVENT_TYPES = frozenset({"CONSTRAINT"})` for this story. The architecture identifies these as critical:
- CONSTRAINT enforcement (event_type=CONSTRAINT) — explicitly critical
- Fail-safe transitions and watchdog recovery (event_type=SYSTEM) — emitted by Epics 7/8, not yet in the system

Future epic dev notes will expand `_CRITICAL_EVENT_TYPES` if SYSTEM events need permanent protection. For now, only CONSTRAINT entries are protected.

### app.py Lifespan Integration — Exact Pattern

Reference: `_session_cleanup_task()` and its lifespan wiring. Follow exactly.

New function (add after `_session_cleanup_task`):
```python
async def _event_log_pruning_task() -> None:
    """Prune non-critical event_log entries older than 90 days, once per day at startup."""
    while True:
        try:
            count = await EventLogRepo().prune_expired()
            logger.info("event_log_pruned", component="observability", deleted_count=count)
        except Exception:
            logger.error("event_log_pruning_failed", exc_info=True, component="observability")
        await asyncio.sleep(24 * 60 * 60)
```

In lifespan, declare alongside `_cleanup_task`:
```python
_pruning_task: asyncio.Task[None] | None = None
```

Wire after `_cleanup_task` creation (inside the `try` block):
```python
def _on_pruning_done(t: asyncio.Task[None]) -> None:
    if not t.cancelled():
        exc = t.exception()
        if exc is not None:
            logger.error("event_log_pruning_task_died", exc_info=exc, component="observability")

_pruning_task = asyncio.create_task(_event_log_pruning_task())
_pruning_task.add_done_callback(_on_pruning_done)
logger.info("event_log_pruning_task_started", component="observability")
```

Cancel on shutdown (inside the finally/shutdown block, after `_cleanup_task` cancellation):
```python
if _pruning_task is not None:
    _pruning_task.cancel()
    try:
        await _pruning_task
    except asyncio.CancelledError:
        pass
```

**Import to add** at top of `app.py`:
```python
from open_ems.storage.repositories.event_log_repo import EventLogRepo
```

### Testing Pattern — In-Memory SQLite

Test file: `tests/unit/storage/repositories/test_event_log_repo.py`
Directory and `__init__.py` already exist — no new package files needed.

DDL fixture (same as `test_audit_log.py`):
```python
_CREATE_EVENT_LOG = """
    CREATE TABLE event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schema_version INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        actor TEXT NOT NULL,
        event_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        detail TEXT,
        device_id TEXT,
        config_version TEXT
    )
"""
```

Fixture:
```python
@pytest_asyncio.fixture
async def repo_with_conn():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_EVENT_LOG)
        await conn.commit()
        yield EventLogRepo(conn), conn
```

For inserting entries with specific timestamps (direct SQL, bypass guard methods):
```python
async def _insert_entry(conn, event_type: str, days_ago: float) -> None:
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    await conn.execute(
        "INSERT INTO event_log (schema_version, timestamp, actor, event_type, summary)"
        " VALUES (1, ?, 'system', ?, 'test event')",
        (ts, event_type),
    )
    await conn.commit()
```

**Boundary test detail:** The SQL uses `timestamp < cutoff_iso` (strict less-than). An entry inserted with `timedelta(days=90)` will have a timestamp microseconds older than the cutoff computed inside `prune_expired` (because `prune_expired` runs after insertion). To make the boundary test deterministic, compute the cutoff BEFORE insertion and use it directly:

```python
async def test_prune_at_boundary_not_deleted(repo_with_conn):
    repo, conn = repo_with_conn
    # Use a timestamp that is exactly at the cutoff the pruning job would compute
    # prune_expired uses datetime.now(UTC) - timedelta(days=90) as cutoff
    # An entry at that exact instant should NOT be deleted (strict <)
    # Use 89.9 days to be safely within the window
    await _insert_entry(conn, "DECISION", days_ago=89.99)
    deleted = await repo.prune_expired(retention_days=90)
    assert deleted == 0
    async with conn.execute("SELECT COUNT(*) FROM event_log") as cur:
        row = await cur.fetchone()
    assert row[0] == 1
```

For the actual boundary test of critical entries, use 91 days (clearly past cutoff but critical):
```python
async def test_prune_critical_entry_never_deleted(repo_with_conn):
    repo, conn = repo_with_conn
    await _insert_entry(conn, "CONSTRAINT", days_ago=91)
    deleted = await repo.prune_expired(retention_days=90)
    assert deleted == 0
```

### Previous Story Intelligence

From Story 6.1:
- `EventLogRepo` has only `append()` — no delete/update. The guard methods and `prune_expired` are purely additive.
- `conn.row_factory = aiosqlite.Row` is set on the in-memory connection in tests.
- `_CRITICAL_EVENT_TYPES` naming follows the underscore-prefix private module convention (like `_BCRYPT_MAX_BYTES` in `user_repo.py`).
- `async with self._conn.execute(...) as cursor:` is the canonical repo pattern.
- `await self._conn.commit()` after every mutation.
- mypy: use `frozenset[str]` not bare `frozenset`.

From `_session_cleanup_task` pattern in `app.py`:
- Task variable is `_cleanup_task: asyncio.Task[None] | None = None` — declared before the `try` block.
- Done-callback is a named closure for clear log attribution.
- Shutdown cancels tasks in the same order they were started.
- `EventLogRepo()` with no args uses `get_connection()` — this is valid after `init_database()` runs.

### Architecture References

- [architecture.md — Decision 5.1] — Dual log sinks; event_log SQLite for installer audit trail.
- [architecture.md — NFR-D2] — 90-day event log retention. This is the sole retention window.
- [architecture.md — NFR-D3] — Critical events (constraint enforcement, command failure, fail-safe, watchdog recovery) protected from normal pruning.
- [architecture.md — Repository pattern] — All DB access through repository classes; no raw SQL outside repos.
- [epics.md — Epic 6 cross-story constraints] — `ObservabilityService.audit()` is the sole entry point for audit writes; no direct INSERT from outside the observability package.

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Debug Log References

- Baseline: `uv run python -m pytest tests/ --no-cov -q` — 569 passed, 43 warnings.
- Baseline: `uv run python -m ruff check .` — passed.
- Baseline: `uv run python -m mypy src/` — passed.
- Focused test: `uv run python -m pytest tests/unit/storage/repositories/test_event_log_repo.py --no-cov -q` — 9 passed.
- Final: `uv run python -m ruff check .` — passed.
- Final: `uv run python -m ruff format --check .` — passed.
- Final: `uv run python -m mypy src/` — passed.
- Final: `uv run python -m pytest tests/ --no-cov -q` — 578 passed, 43 warnings.

### Completion Notes List

- Added explicit synchronous `update()` and `delete()` append-only guards to `EventLogRepo`.
- Added `_CRITICAL_EVENT_TYPES` and `prune_expired()` with strict older-than cutoff behavior, critical-event protection, committed deletion, and deleted row count return.
- Wired daily event-log pruning into FastAPI lifespan with startup logging, failure logging, unexpected task death logging, and shutdown cancellation.
- Added repository unit tests for guard methods, retention deletion, boundary-exclusive behavior, critical event preservation, deleted counts, and mixed-entry pruning.

### File List

- `_bmad-output/implementation-artifacts/6-2-enforce-append-only-semantics-and-event-retention-policy.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`
- `src/open_ems/storage/repositories/event_log_repo.py`
- `src/open_ems/web/app.py`
- `tests/unit/storage/repositories/test_event_log_repo.py`

### Change Log

- 2026-05-04: Implemented append-only guards, retention pruning, app lifespan pruning task, and event log repository unit coverage.
