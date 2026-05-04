# Story 6.1: Define event log schema and implement ObservabilityService with dual-sink logging

Status: done

<!-- Completion note: Ultimate context engine analysis completed - comprehensive developer guide created. -->

## Story

As a developer,
I want a central ObservabilityService that routes audit-worthy events to both structlog stdout and the SQLite event_log table, while allowing operational-only logs to use structlog directly,
so that the installer audit log contains only meaningful, human-readable events and operational process logs are not polluted with audit noise.

## Acceptance Criteria

**AC1 — Dual-sink audit routing**
Given the observability package is initialized
When a component calls `ObservabilityService.audit(...)` to log an audit-worthy event
Then the call writes to both sinks:
- A structlog JSON line to stdout with `audit=True` to distinguish audit events from operational-only log lines
- A row to the `event_log` SQLite table
And operational-only debug and process logs (e.g., adapter polling cycles, cache operations, connection heartbeats) call `structlog` directly — they do NOT go through `ObservabilityService.audit()` and do NOT create `event_log` rows.

**AC2 — Event log schema**
Given an audit event is written
When the `event_log` row is constructed
Then the schema is exactly:
- `id` — INTEGER PRIMARY KEY AUTOINCREMENT
- `schema_version` — INTEGER NOT NULL (the `EVENT_LOG_SCHEMA_VERSION` constant)
- `timestamp` — TEXT NOT NULL (ISO 8601 UTC)
- `actor` — TEXT NOT NULL (`system` | `installer` | `homeowner`)
- `event_type` — TEXT NOT NULL (`DECISION` | `DEVICE` | `SYSTEM` | `CONSTRAINT` | `INSTALLER`)
- `summary` — TEXT NOT NULL (plain-language, validated non-empty)
- `detail` — TEXT (JSON blob or NULL)
- `device_id` — TEXT (nullable)
- `config_version` — TEXT (nullable)

And `schema_version` is present in every entry and equals `EVENT_LOG_SCHEMA_VERSION` at write time.

**AC3 — Write-time validation**
Given an audit event is submitted
When the service validates the payload
Then a blank or null `summary` raises `ValueError` immediately — not stored
And an `actor` value outside `{system, installer, homeowner}` raises `ValueError` immediately
And an `event_type` value outside `{DECISION, DEVICE, SYSTEM, CONSTRAINT, INSTALLER}` raises `ValueError` immediately
And validation fires before any database or structlog write.

**AC4 — FakeAuditLog test double**
Given the Epic 6 test utilities are imported in a subsequent epic's test suite
When a test needs to verify that an audit event was emitted
Then `FakeAuditLog` from `tests/helpers/fake_audit_log.py` captures and asserts audit events by `event_type` and `actor` without a real SQLite database.

**AC5 — Tests**
- Unit tests verify `ObservabilityService.audit()` produces both a structlog output with `audit=True` and a database row with matching fields.
- Unit tests verify blank/null `summary` raises at write time.
- Unit tests verify out-of-enum `actor` or `event_type` raises at write time.
- Unit tests verify `schema_version` is present and equals `EVENT_LOG_SCHEMA_VERSION` in every written row.
- Unit tests verify `FakeAuditLog` captures emitted events and supports assertion by `event_type` and `actor`.

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` and record the baseline.
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m mypy src/`

- [x] Task 1: Add Alembic migration for event_log table (AC: AC2)
  - [x] Create `migrations/versions/0005_add_event_log_table.py` following the exact format of `0004_add_csrf_token_to_sessions.py`.
  - [x] Set `revision = "0005"`, `down_revision = "0004"`.
  - [x] In `upgrade()`: create the `event_log` table with all columns defined in AC2. Use `sa.Text()` for all text columns, `sa.Integer()` for `id` and `schema_version`.
  - [x] In `downgrade()`: `op.drop_table("event_log")`.
  - [x] No `op.batch_alter_table` needed — this is a CREATE TABLE migration.
  - [x] Add an index on `timestamp` and `event_type` for future query performance: `op.create_index("ix_event_log_timestamp", "event_log", ["timestamp"])` and `op.create_index("ix_event_log_event_type", "event_log", ["event_type"])`.
  - [x] Verify Alembic chain resolves to head: `uv run python -m alembic --config alembic.ini heads` should show `0005`.

- [x] Task 2: Create EventLogRepo (AC: AC1, AC2)
  - [x] Create `src/open_ems/storage/repositories/event_log_repo.py`.
  - [x] Follow `user_repo.py` conventions: `class EventLogRepo`, `__init__(self, conn=None)` accepting optional `aiosqlite.Connection`, defaulting to `get_connection()`.
  - [x] Implement `async def append(self, *, schema_version, timestamp, actor, event_type, summary, detail, device_id, config_version) -> int` — inserts and returns the new `id`.
  - [x] Store `timestamp` as `.isoformat()` string (all existing repos use this pattern).
  - [x] Store `detail` as `json.dumps(detail)` if not None, else NULL.
  - [x] Call `await self._conn.commit()` after insert (same as `user_repo`).
  - [x] **No UPDATE or DELETE methods** — append-only is enforced by omission. Story 6.2 adds the designated pruning job; it does not bypass the repo.
  - [x] Do NOT add raw SQL to route handlers or service code — all DB writes go through `EventLogRepo`.

- [x] Task 3: Create ObservabilityService in services/audit_log.py (AC: AC1, AC2, AC3)
  - [x] Create `src/open_ems/services/audit_log.py`.
  - [x] Define module-level constants: `EVENT_LOG_SCHEMA_VERSION`, `VALID_ACTORS`, `VALID_EVENT_TYPES`.
  - [x] Define `class ObservabilityService` with injectable `repo` parameter defaulting to `EventLogRepo()`.
  - [x] Implement `async def audit(...)` with validation-first ordering (raises before any I/O).
  - [x] structlog write with `audit=True` precedes DB write.
  - [x] Import `structlog` and use `logger = structlog.get_logger(__name__)` at module level.
  - [x] Import `from datetime import UTC, datetime`.

- [x] Task 4: Create FakeAuditLog test double (AC: AC4, AC5)
  - [x] Create `tests/helpers/__init__.py` (empty).
  - [x] Create `tests/helpers/fake_audit_log.py`.
  - [x] `FakeAuditLog` mirrors `ObservabilityService.audit()` interface, captures events in memory.
  - [x] Implements `assert_has_event`, `assert_event_count`, `clear` helpers.
  - [x] Applies same validation as real service (imports constants from `open_ems.services.audit_log`).

- [x] Task 5: Add tests (AC: AC1–AC5)
  - [x] Create `tests/unit/services/test_audit_log.py`.
  - [x] Test structlog output with `audit=True` key via `capture_logs()`.
  - [x] Test DB row written with all expected fields via in-memory SQLite.
  - [x] Test blank/whitespace summary raises `ValueError` before any write.
  - [x] Test out-of-enum `actor` raises `ValueError` with no DB write.
  - [x] Test out-of-enum `event_type` raises `ValueError` with no DB write.
  - [x] Test validation fires before structlog (no audit log emitted on invalid call).
  - [x] Test `schema_version` equals `EVENT_LOG_SCHEMA_VERSION`.
  - [x] Test `detail=None` stored as NULL; `detail` dict stored as JSON.
  - [x] Test all 3 valid actors and all 5 valid event types accepted.
  - [x] Test `FakeAuditLog` captures, asserts, counts, clears events.
  - [x] Test `FakeAuditLog` validation (blank summary, bad actor, bad event_type).
  - [x] 25 tests total; all pass.

- [x] Task 6: Final validation (AC: all)
  - [x] Run `uv run python -m ruff check .` — all checks passed
  - [x] Run `uv run python -m ruff format --check .` — 124 files already formatted
  - [x] Run `uv run python -m mypy src/` — success: no issues found in 60 source files
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — 569 passed (baseline 540 + 29 new)
  - [x] Verify Alembic chain: `uv run alembic heads` — `0005 (head)` confirmed
  - [x] Code review findings resolved and validated.

### Review Findings

- [x] [Review][Patch] Migration does not guarantee SQLite `AUTOINCREMENT` despite AC2 requiring `INTEGER PRIMARY KEY AUTOINCREMENT` [migrations/versions/0005_add_event_log_table.py:25]
- [x] [Review][Patch] `detail` JSON serialization can fail after the stdout audit event is emitted, leaving the two audit sinks inconsistent [src/open_ems/services/audit_log.py:39]
- [x] [Review][Patch] `FakeAuditLog` stores mutable `detail` by reference instead of snapshotting audit-time payloads [tests/helpers/fake_audit_log.py:32]

## Dev Notes

### Scope and Context

Epic 5 built the real-time state distribution layer (StateStore, SSE, HTMX polling). Epic 6 builds the durable audit trail. Story 6.1 establishes:
- The `event_log` table schema (consumed by Epics 7–11 for writing; Epic 11 for reading/UI).
- `ObservabilityService` as the **sole entry point** for all audit writes — no direct INSERTs from outside the observability boundary.
- `FakeAuditLog` used by every subsequent epic's test suite to assert audit emission without a real database.

This story does **not**:
- Add append-only enforcement logic (Story 6.2).
- Add installer note storage or `config_audit_log` (Story 6.3).
- Wire `ObservabilityService` into the app lifespan or any existing route.
- Emit any audit events from Epic 5 code (that comes per-epic as each epic produces audit-worthy conditions).

### File Layout — New vs. Modified

**New files (create these):**
```
migrations/versions/0005_add_event_log_table.py
src/open_ems/services/audit_log.py
src/open_ems/storage/repositories/event_log_repo.py
tests/helpers/__init__.py
tests/helpers/fake_audit_log.py
tests/unit/services/test_audit_log.py
```

**No existing files need modification.** `ObservabilityService` is not wired into `app.py` lifespan or any router in this story — it is constructed by the caller (or tested directly). Future epics wire it in.

### Migration Chain — Critical Detail

Migrations live in `migrations/versions/` (NOT `alembic/`). The `alembic.ini` sets `script_location = migrations`. Current chain:
```
0001_initial_schema → 0002_add_users_table → 0003_add_sessions_table → 0004_add_csrf_token_to_sessions → [NEW: 0005_add_event_log_table]
```

Follow the exact header format from `0004_add_csrf_token_to_sessions.py`:
```python
from __future__ import annotations
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
```

Full `upgrade()` DDL for the `event_log` table:
```python
def upgrade() -> None:
    op.create_table(
        "event_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("device_id", sa.Text(), nullable=True),
        sa.Column("config_version", sa.Text(), nullable=True),
    )
    op.create_index("ix_event_log_timestamp", "event_log", ["timestamp"])
    op.create_index("ix_event_log_event_type", "event_log", ["event_type"])
```

### Repository Pattern — Follow user_repo.py Exactly

`src/open_ems/storage/repositories/user_repo.py` is the canonical pattern. Key points:
- Constructor: `def __init__(self, conn: aiosqlite.Connection | None = None) -> None: self._conn = conn if conn is not None else get_connection()`
- Direct `await self._conn.execute(...)` for writes
- `await self._conn.commit()` after each write
- `aiosqlite.Row` is already set on the connection (set in `init_database`)
- No ORM — raw SQL strings with positional `?` parameters

`detail` column: store as `json.dumps(detail) if detail is not None else None` — TEXT column, NULL when no detail.

`timestamp` column: store as `datetime.now(UTC).isoformat()` — same convention as `created_at` in `user_repo.py` and `sessions` table.

### ObservabilityService — Structlog Dual-Sink Pattern

Structlog is already fully configured via `src/open_ems/logging_config.py` with JSON output to stdout. Every module uses `logger = structlog.get_logger(__name__)` at module level.

The `audit=True` key distinguishes audit events from operational logs in the JSON output stream. The structlog call must come **before** the DB write to ensure the log exists even if the DB write fails:

```python
import structlog
logger = structlog.get_logger(__name__)

async def audit(self, *, actor, event_type, summary, ...):
    # 1. Validate (raises before any I/O)
    # 2. structlog write (fast, in-process)
    logger.info("audit_event", audit=True, actor=actor, event_type=event_type,
                summary=summary, device_id=device_id, config_version=config_version)
    # 3. DB write (async I/O)
    timestamp = datetime.now(UTC)
    await self._repo.append(schema_version=EVENT_LOG_SCHEMA_VERSION,
                            timestamp=timestamp, ...)
```

Do NOT use `logger.audit(...)` — structlog has no dedicated `audit` level. Use `logger.info(...)` with the `audit=True` bound argument.

### Testing Pattern — In-Memory SQLite

Do not use `get_connection()` in unit tests (requires a running app). Use `aiosqlite.connect(":memory:")` and create the table manually with the same DDL as the migration. Pattern from existing storage tests:

```python
@pytest.mark.asyncio
async def test_audit_writes_db_row():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
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
        """)
        await conn.commit()
        repo = EventLogRepo(conn)
        svc = ObservabilityService(repo=repo)
        await svc.audit(actor="system", event_type="SYSTEM", summary="test event")
        async with conn.execute("SELECT * FROM event_log") as cur:
            row = await cur.fetchone()
        assert row["actor"] == "system"
        assert row["schema_version"] == EVENT_LOG_SCHEMA_VERSION
```

For structlog capture, use `structlog.testing.capture_logs()`:
```python
from structlog.testing import capture_logs

async def test_audit_writes_structlog():
    ...
    with capture_logs() as logs:
        await svc.audit(actor="system", event_type="SYSTEM", summary="test")
    assert any(e.get("audit") is True and e["event_type"] == "SYSTEM" for e in logs)
```

### Import Layer Boundaries — Unchanged

```
core/     → no imports from adapters/, storage/, web/, or services/
adapters/ → may import from core/
web/      → may call services/ and storage/repositories/, must not call adapters/ directly
services/ → may import from storage/repositories/ (audit_log.py imports EventLogRepo)
```

`ObservabilityService` lives in `services/` and imports `EventLogRepo` from `storage/repositories/`. This matches the architecture service layer pattern.

**Never** import `ObservabilityService` from `core/`. Core models have no observability dependency.

### Previous Story Intelligence — Patterns Established

**From Story 5.3 (most recent):**
- All `settings.py` additions follow `pydantic.Field(default=..., gt=0)` with env-var binding via pydantic-settings.
- Test isolation uses either `aiosqlite.connect(":memory:")` or `pytest-asyncio` fixtures with controlled state injection.
- All async route tests use `httpx.AsyncClient(app=app, base_url="http://test")` — not relevant for this story (no routes added).
- `pytest.mark.asyncio` is required for all `async def test_` functions.
- `structlog.testing.capture_logs()` is the correct way to capture structlog output in tests.

**From Epics 1–5 migration convention:**
- Migrations use SQLAlchemy Core ops (`op.create_table`, `op.add_column`, etc.) — no raw DDL strings.
- All migration files have `from __future__ import annotations` as first line.
- Timestamp fields stored as TEXT ISO 8601 in all existing tables.
- Index creation is part of the same migration as the table creation.

### Architecture References

- [Source: architecture.md — Decision 5.1 — Structured logging] — Two separate sinks: structlog stdout for operational, event_log SQLite for audit.
- [Source: architecture.md — Service layer pattern] — `services/audit_log.py` is the designated file; `audit_log.record(...)` is the architecture's expected call pattern; this story implements it as `ObservabilityService.audit(...)` per Epic 6 contract.
- [Source: architecture.md — Repository pattern] — All DB access through `storage/repositories/`. No raw SQL outside repo classes.
- [Source: architecture.md — Project structure: services/audit_log.py] — confirms file location.
- [Source: epics.md — Epic 6 Story 6.1 — cross-story constraints] — `ObservabilityService.audit()` is the **sole entry point** for all audit writes; no direct INSERT from outside the observability package.

## Dev Agent Record

### Agent Model Used

claude-sonnet-4-6

### Debug Log References

### Completion Notes List

- Baseline: 540 tests, ruff clean, mypy clean (60 source files).
- Migration `0005_add_event_log_table` added; Alembic chain verified to `0005 (head)`.
- `EventLogRepo` created following `user_repo.py` pattern exactly (injectable conn, isoformat timestamp, json.dumps for detail, commit after insert, no update/delete methods).
- `ObservabilityService` in `services/audit_log.py`: validation-first ordering (raises before any I/O), structlog dual-sink with `audit=True`, injectable `repo` for test isolation.
- `FakeAuditLog` in `tests/helpers/fake_audit_log.py`: in-memory test double with same validation, importable by all future epic test suites.
- 29 new tests covering all ACs and review fixes; full suite: 569 passed, 0 regressions.
- Code review fixes applied: SQLite `AUTOINCREMENT` migration flag, pre-stdout strict JSON detail validation, and FakeAuditLog detail snapshotting.
- Fixed mypy issue: `dict` → `dict[str, object]` in both `event_log_repo.py` and `audit_log.py`.

### File List

- `migrations/versions/0005_add_event_log_table.py` — new: event_log table + indexes migration
- `src/open_ems/services/audit_log.py` — new: ObservabilityService with dual-sink audit method
- `src/open_ems/storage/repositories/event_log_repo.py` — new: append-only EventLogRepo
- `tests/helpers/__init__.py` — new: helpers package init
- `tests/helpers/fake_audit_log.py` — new: FakeAuditLog in-memory test double
- `tests/unit/services/test_audit_log.py` — new: 25 unit tests for all ACs
