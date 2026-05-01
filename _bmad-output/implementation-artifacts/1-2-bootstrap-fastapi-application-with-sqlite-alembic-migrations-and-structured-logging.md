# Story 1.2: Bootstrap FastAPI application with SQLite, Alembic migrations, and structured logging

Status: done

## Story

As a developer,
I want a running FastAPI app with crash-safe SQLite storage, auto-running migrations, and standardized structured logging,
So that subsequent features have a stable data layer and consistent observability without rework.

## Acceptance Criteria

1. **Given** the project is initialized per Story 1.1  
   **When** the FastAPI application starts  
   **Then** an SQLite database file is created or opened at the path from configuration  
   **And** SQLite WAL mode is enabled on the connection  
   **And** Alembic runs all pending migrations automatically before any route handler or adapter initializes  
   **And** a migration failure terminates startup with a non-zero exit code and a structured error log entry  
   **And** raw SQL is permitted only inside Alembic migration files; all application runtime database access uses repository classes in `storage/repositories/` exclusively  
   **And** an initial empty-schema migration exists as the versioned baseline

2. **Given** the project is initialized per Story 1.1  
   **When** the FastAPI application starts  
   **Then** structlog is configured as the sole logging backend with JSON output to stdout  
   **And** every log entry includes at minimum: `timestamp`, `level`, `event`, `component`  
   **And** no plain-text log output exists anywhere in the codebase — all logging goes through structlog

## Tasks / Subtasks

- [x] Task 1: Create minimal Settings module (AC: 1 — path from configuration, AC: 2 — log_level)
  - [x] Create `src/open_ems/settings.py` with a `Settings(BaseSettings)` class
  - [x] Include only: `db_path: str = "open_ems.db"` and `log_level: str = "INFO"`
  - [x] Add `SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")` so .env is loaded if present
  - [x] Confirm `python -m uv run mypy src/` still exits 0

- [x] Task 2: Configure structlog as sole logging backend (AC: 2)
  - [x] Create `src/open_ems/logging_config.py` with a `configure_logging(log_level: str) -> None` function
  - [x] Build the shared processor chain (see template in Dev Notes — exact order matters)
  - [x] Configure structlog itself via `structlog.configure()`
  - [x] Bridge stdlib logging to structlog JSON via `ProcessorFormatter` + `StreamHandler` on root logger (captures uvicorn, alembic, SQLAlchemy logs)
  - [x] Remove any default stdlib logging handlers before adding the structlog one
  - [x] Confirm `python -m uv run mypy src/` still exits 0

- [x] Task 3: Create storage package and database module (AC: 1 — WAL mode)
  - [x] Create `src/open_ems/storage/__init__.py` (empty)
  - [x] Create `src/open_ems/storage/repositories/__init__.py` (empty — placeholder, no repos in this story)
  - [x] Create `src/open_ems/storage/database.py` with `init_database()`, `close_database()`, `get_connection()` (see template in Dev Notes)
  - [x] `init_database()` must: open the aiosqlite connection, execute `PRAGMA journal_mode=WAL`, execute `PRAGMA foreign_keys=ON`
  - [x] `get_connection()` must raise `RuntimeError` if not initialized (guards against misuse)
  - [x] Confirm `python -m uv run mypy src/` still exits 0

- [x] Task 4: Set up Alembic migration framework (AC: 1 — Alembic runs before routes)
  - [x] Create `alembic.ini` at project root (see exact template in Dev Notes)
  - [x] Create `migrations/env.py` (see exact template — omit fileConfig, no stdlib log capture conflict)
  - [x] Create `migrations/script.py.mako` (standard Alembic template)
  - [x] Create `migrations/__init__.py` (empty)
  - [x] Create `migrations/versions/__init__.py` (empty)
  - [x] Confirm `python -m uv run alembic check` (or `heads`) does not crash

- [x] Task 5: Create baseline migration (AC: 1 — initial empty-schema migration)
  - [x] Create `migrations/versions/0001_initial_schema.py` (see template in Dev Notes)
  - [x] `upgrade()` and `downgrade()` are both no-ops (`pass`) — this is a baseline marker only
  - [x] Confirm `python -m uv run alembic upgrade head` exits 0 against a fresh `open_ems.db`

- [x] Task 6: Create web package, app factory, and main.py (AC: 1 — Alembic before routes)
  - [x] Create `src/open_ems/web/__init__.py` (empty)
  - [x] Create `src/open_ems/web/app.py` with `create_app() -> FastAPI` using lifespan context (see template in Dev Notes)
  - [x] Lifespan sequence (exact order): configure logging → run migrations (asyncio.to_thread) → init database → yield → close database
  - [x] On migration exception: log `migration_failed` at error level with `exc_info=True`, then `raise SystemExit(1)`
  - [x] Create `src/open_ems/main.py` as uvicorn entry point — imports `create_app()` and exposes `app = create_app()` plus `if __name__ == "__main__": uvicorn.run(...)` (see template)
  - [x] Confirm `python -m uv run mypy src/` still exits 0

- [x] Task 7: Write tests (AC: all)
  - [x] Create `tests/conftest.py` with `tmp_db_path` fixture (temp file) and `initialized_db` async fixture
  - [x] Create `tests/unit/__init__.py` (empty)
  - [x] Create `tests/unit/storage/__init__.py` (empty)
  - [x] Create `tests/unit/storage/test_database.py` — test WAL mode enabled, init/close lifecycle, get_connection raises before init
  - [x] Create `tests/unit/test_logging.py` — test that configure_logging() produces JSON-parseable output with required fields (timestamp, level, event, component)
  - [x] Create `tests/integration/__init__.py` (empty)
  - [x] Create `tests/integration/test_migrations.py` — test alembic `upgrade head` runs to completion against an in-memory SQLite URL; test migration failure triggers SystemExit
  - [x] Run `python -m uv run pytest` — all tests must pass

- [x] Task 8: Final validation
  - [x] Run `python -m uv run ruff check .` — must exit 0
  - [x] Run `python -m uv run mypy src/` — must exit 0
  - [x] Run `python -m uv run pytest` — all tests must pass
  - [x] Confirm `uv.lock` has not changed (no new dependencies added without Story approval)

### Review Follow-ups (AI)

- [x] [Review][Patch][High] Migration failure test is self-referential — lifespan error path has no coverage [tests/integration/test_migrations.py:30]
- [x] [Review][Patch][High] `exc_info=True` without `format_exc_info` processor — traceback not JSON-serializable, error log crashes at runtime [src/open_ems/logging_config.py:37]
- [x] [Review][Patch][High] `init_database` silently leaks connection on double-call — no guard before overwriting `_connection` [src/open_ems/storage/database.py:12]
- [x] [Review][Patch][High] Partial `init_database` failure leaves broken `_connection` — PRAGMA failure after `connect()` leaves uninitialised global [src/open_ems/storage/database.py:14]
- [x] [Review][Patch][High] `Config("alembic.ini")` resolves relative to CWD — fails when service is launched from non-project-root (systemd, Docker) [src/open_ems/web/app.py:22]
- [x] [Review][Patch][Med] WAL PRAGMA result not validated — silent fallback to DELETE mode on FAT32/read-only filesystems (RPi SD card) [src/open_ems/storage/database.py:14]
- [x] [Review][Patch][Med] `log_level` accepts any string, silently falls back to INFO on typos — no `Literal` type or validator [src/open_ems/settings.py:13]
- [x] [Review][Patch][Med] Idempotency test uses separate in-memory DBs — `sqlite:///:memory:` creates fresh DB per connection, does not test same-DB idempotency [tests/integration/test_migrations.py:22]
- [x] [Review][Patch][Low] `tmp_db_path` fixture has wrong type annotation — `pytest.TempPathFactory` should be `pathlib.Path` [tests/conftest.py:10]
- [x] [Review][Patch][Low] `initialized_db` fixture typed as `AsyncGenerator[None, None]` but yields `aiosqlite.Connection` [tests/conftest.py:15]
- [x] [Review][Defer] Pre-lifespan uvicorn startup logs go through stdlib before `configure_logging` is called [src/open_ems/web/app.py:53] — deferred, inherent FastAPI lifespan ordering constraint; fix requires pre-configuring logging before app creation (architectural decision for future story)

## Dev Notes

### Critical Context — Read First

**Scope boundary — Story 1.2 only creates:**
- Minimal settings module (DB_PATH, LOG_LEVEL only)
- structlog configuration
- aiosqlite database connection with WAL mode
- Alembic scaffolding + baseline migration
- FastAPI app skeleton with lifespan (no routes yet)
- Storage package scaffold

**Do NOT create in this story** (belongs to later stories):
- `/health/live`, `/health/ready` endpoints → Story 1.3
- NTP/clock check → Story 1.3
- Full Settings schema (PORT, TLS_CERT_PATH, SECRET_KEY, etc.) → Story 1.7
- Any route handlers or templates → Epic 2+
- Docker/docker-compose → Story 1.4
- systemd service files → Story 1.5
- GitHub Actions CI → Story 1.6
- Any subdirectories under `src/open_ems/core/`, `engine/`, `adapters/`, `services/` — belong to later epics
- Any `storage/repositories/` implementation files (only empty `__init__.py` placeholder) — populated in Epic 2+

**Settings scope boundary (important):**  
Story 1.2 creates a *minimal* `settings.py` with only `db_path` and `log_level`. Story 1.7 will complete the Settings class with PORT, TLS_CERT_PATH, TLS_KEY_PATH, SECRET_KEY, and all other fields. Design the minimal class so Story 1.7 can extend it by adding fields — do not hardcode anything or use `os.environ.get()` directly.

**Repository pattern — enforced from the start:**  
Even though no concrete repositories exist in this story, the architecture mandates that ALL runtime database access goes through `storage/repositories/`. The empty `repositories/__init__.py` placeholder signals this boundary. No raw SQL may appear outside Alembic migration files. Future stories add concrete repos — they never bypass them.

**Development machine note (from Story 1.1):**  
`uv` is NOT on system PATH. All `uv` commands run as `python -m uv`. Target runtime is Linux (Raspberry Pi 4, Python 3.12). Development is on Windows (Python 3.14).

---

### File Structure After This Story

```
OPEN-EMS/                              ← project root
├── alembic.ini                        ← NEW: Alembic config (script_location = migrations)
├── pyproject.toml                     ← UNCHANGED
├── uv.lock                            ← UNCHANGED
├── .gitignore                         ← UNCHANGED
├── .env.example                       ← UNCHANGED
├── README.md                          ← UNCHANGED
├── migrations/                        ← NEW
│   ├── __init__.py                    ← empty
│   ├── env.py                         ← Alembic env (no fileConfig)
│   ├── script.py.mako                 ← standard Alembic template
│   └── versions/
│       ├── __init__.py                ← empty
│       └── 0001_initial_schema.py     ← baseline no-op migration
├── src/
│   └── open_ems/
│       ├── __init__.py                ← UNCHANGED (__version__ = "0.1.0")
│       ├── main.py                    ← NEW: uvicorn entrypoint
│       ├── settings.py                ← NEW: minimal Settings(BaseSettings)
│       ├── logging_config.py          ← NEW: configure_logging()
│       ├── storage/                   ← NEW
│       │   ├── __init__.py            ← empty
│       │   ├── database.py            ← aiosqlite + WAL mode
│       │   └── repositories/
│       │       └── __init__.py        ← empty placeholder
│       └── web/                       ← NEW
│           ├── __init__.py            ← empty
│           └── app.py                 ← FastAPI app factory + lifespan
└── tests/
    ├── __init__.py                    ← UNCHANGED
    ├── test_baseline.py               ← UNCHANGED
    ├── conftest.py                    ← NEW: shared fixtures
    ├── unit/
    │   ├── __init__.py                ← NEW: empty
    │   └── storage/
    │       ├── __init__.py            ← NEW: empty
    │       └── test_database.py       ← NEW
    └── integration/
        ├── __init__.py                ← NEW: empty
        └── test_migrations.py         ← NEW
```

---

### Settings Template

```python
# src/open_ems/settings.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # tolerate unknown env vars
    )

    db_path: str = "open_ems.db"
    log_level: str = "INFO"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
```

**Why a singleton `get_settings()`?** FastAPI dependency injection uses `Depends(get_settings)`. Tests can monkeypatch `get_settings` or construct `Settings()` directly with env overrides.

---

### Structlog Configuration Template

Every log entry must include `timestamp`, `level`, `event`, `component`. The `component` field maps to the Python module/logger name and is added automatically by the processor chain — callers do not need to include it manually (but may override it explicitly).

```python
# src/open_ems/logging_config.py
import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger


def _add_component(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Map logger name to 'component' field if not explicitly set."""
    if "component" not in event_dict:
        name = getattr(logger, "name", None)
        event_dict["component"] = name if name else repr(logger)
    return event_dict


def configure_logging(log_level: str = "INFO") -> None:
    """Configure structlog as sole logging backend with JSON stdout output.

    Must be called ONCE at application startup before any logger is created.
    Bridges stdlib logging (uvicorn, alembic, SQLAlchemy) to the same JSON sink.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_component,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    # Configure structlog
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Formatter that also handles stdlib log records
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    # Replace ALL root logger handlers to prevent plain-text output
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
```

**Usage in application code:**
```python
import structlog

logger = structlog.get_logger(__name__)

# Component is auto-populated from __name__. Override explicitly when needed:
logger.info("database_initialized", db_path=db_path, component="storage.database")
logger.error("migration_failed", exc_info=True, component="startup")
```

**Required JSON output shape:**
```json
{
  "timestamp": "2026-05-01T12:00:00.000000Z",
  "level": "info",
  "event": "database_initialized",
  "component": "open_ems.storage.database",
  "db_path": "open_ems.db"
}
```

**Why clear root handlers?** Uvicorn and Python's `logging.basicConfig` add a `StreamHandler` with `%(levelname)s: %(message)s` format. If we don't clear them, plain-text output appears alongside JSON, violating AC 2. Clearing before adding our handler ensures JSON-only output.

---

### Alembic Configuration Template

**`alembic.ini`** (project root):
```ini
[alembic]
script_location = migrations
prepend_sys_path = .
version_path_separator = os

# Placeholder URL — overridden by env.py at runtime
sqlalchemy.url = sqlite:///open_ems.db

# DO NOT include [loggers]/[handlers]/[formatters] sections.
# Alembic's fileConfig call is disabled in env.py to prevent stdlib log config
# conflict with our structlog setup.
```

**`migrations/env.py`**:
```python
from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

# NOTE: fileConfig() is intentionally omitted — structlog manages all logging.
# Including fileConfig would reconfigure stdlib logging with plain-text output,
# overriding the JSON structlog setup.

target_metadata = None  # Raw SQL migrations only — no SQLAlchemy ORM models


def get_url() -> str:
    """Return DB URL from Alembic config (overridable by migration runner)."""
    return config.get_main_option("sqlalchemy.url", default="sqlite:///open_ems.db")


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        {"sqlalchemy.url": get_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

**`migrations/script.py.mako`**:
```
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

**`migrations/versions/0001_initial_schema.py`**:
```python
"""Initial schema baseline

Revision ID: 0001
Revises:
Create Date: 2026-05-01

"""
from typing import Sequence, Union

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass  # Baseline only — no tables yet; all schema added in later stories


def downgrade() -> None:
    pass
```

**Why the baseline migration is empty:** All v1 tables (device_registry, sessions, event_log, etc.) are defined in their respective epics. The baseline establishes the migration chain's starting point. If `alembic upgrade head` is run against a fresh DB, this migration runs (no-op) and sets the schema version. Future migrations append to this chain.

---

### Alembic Migration Runner (in lifespan)

```python
import asyncio
from alembic.config import Config
from alembic import command as alembic_command


def _run_alembic_upgrade(db_url: str) -> None:
    """Synchronous Alembic upgrade — called via asyncio.to_thread in lifespan."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(cfg, "head")
```

**Critical:** `_run_alembic_upgrade` must be synchronous (Alembic/SQLAlchemy use sync connections). Call it with `await asyncio.to_thread(...)` from the async lifespan.

---

### Database Connection Template

```python
# src/open_ems/storage/database.py
from __future__ import annotations

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

_connection: aiosqlite.Connection | None = None


async def init_database(db_path: str) -> None:
    """Open aiosqlite connection and enable WAL mode + foreign keys."""
    global _connection
    _connection = await aiosqlite.connect(db_path)
    _connection.row_factory = aiosqlite.Row
    await _connection.execute("PRAGMA journal_mode=WAL")
    await _connection.execute("PRAGMA foreign_keys=ON")
    await _connection.commit()
    logger.info("database_initialized", db_path=db_path, component="storage.database")


async def close_database() -> None:
    """Close the shared connection."""
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None
        logger.info("database_closed", component="storage.database")


def get_connection() -> aiosqlite.Connection:
    """Return the shared connection. Raises if not yet initialized."""
    if _connection is None:
        raise RuntimeError(
            "Database not initialized. Call init_database() before using get_connection()."
        )
    return _connection
```

**Why a module-level connection (not per-request)?** OPEN-EMS is a single-process, single-site embedded system. SQLite with WAL mode supports concurrent reads from a single connection. A shared connection avoids per-request connection overhead and simplifies lifecycle management in the lifespan. All repository classes will call `get_connection()`.

**Why `row_factory = aiosqlite.Row`?** Enables column access by name (`row["column_name"]`) instead of index in repository classes.

---

### FastAPI App and Lifespan Template

```python
# src/open_ems/web/app.py
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI

from open_ems.logging_config import configure_logging
from open_ems.settings import get_settings
from open_ems.storage.database import close_database, init_database

logger = structlog.get_logger(__name__)


def _run_alembic_upgrade(db_url: str) -> None:
    from alembic import command as alembic_command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()

    # Step 1: Configure logging first — all subsequent logs must be JSON
    configure_logging(settings.log_level)

    # Step 2: Run Alembic migrations — fatal on failure
    db_url = f"sqlite:///{settings.db_path}"
    try:
        await asyncio.to_thread(_run_alembic_upgrade, db_url)
        logger.info("migrations_applied", component="startup")
    except Exception:
        logger.error("migration_failed", exc_info=True, component="startup")
        raise SystemExit(1)

    # Step 3: Open database connection
    await init_database(settings.db_path)
    logger.info("startup_complete", component="startup")

    yield  # Application serves requests here

    # Shutdown: close database
    await close_database()


def create_app() -> FastAPI:
    return FastAPI(
        title="OPEN-EMS",
        lifespan=lifespan,
        # Disable OpenAPI in production (enabled by default in dev only)
        # Story 1.7+ will gate this on an environment flag
    )
```

```python
# src/open_ems/main.py
import uvicorn

from open_ems.web.app import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        "open_ems.main:app",
        host="0.0.0.0",
        port=8443,
        reload=False,
    )
```

**Startup sequence alignment (from architecture.md):**
1. Load configuration — `get_settings()` in lifespan
2. Configure logging — `configure_logging()` 
3. Run Alembic migrations — `_run_alembic_upgrade()` via `asyncio.to_thread`
4. Initialize StateStore — Story 1.3+ (placeholder in yield)
5. Initialize database connection — `init_database()`
6. (Remaining steps in later stories)

Story 1.3 will add the `ReadinessService`, `/health/ready`, and NTP check into this sequence. Story 1.2 must not pre-create those.

---

### Testing Requirements

**`tests/conftest.py`**:
```python
import pytest
import pytest_asyncio
import tempfile
import os


@pytest.fixture
def tmp_db_path(tmp_path):
    """Temporary SQLite file path for tests that need an on-disk DB."""
    return str(tmp_path / "test.db")


@pytest_asyncio.fixture
async def initialized_db(tmp_db_path):
    """Initialized aiosqlite connection with WAL mode for unit tests."""
    from open_ems.storage.database import init_database, close_database, get_connection
    await init_database(tmp_db_path)
    yield get_connection()
    await close_database()
```

**`tests/unit/storage/test_database.py`** — required coverage:
- `test_wal_mode_enabled`: after `init_database()`, `PRAGMA journal_mode` returns `"wal"` 
- `test_foreign_keys_enabled`: `PRAGMA foreign_keys` returns `1`
- `test_get_connection_before_init_raises`: `get_connection()` raises `RuntimeError` before `init_database()`
- `test_init_close_lifecycle`: full init → get → close cycle without error
- `test_row_factory`: rows support column name access

**`tests/unit/test_logging.py`** — required coverage:
- `test_json_output_format`: `configure_logging()` produces JSON-parseable output to stdout
- `test_required_fields_present`: output contains `timestamp`, `level`, `event`, `component`
- `test_log_level_filtering`: DEBUG messages are suppressed at INFO level

**`tests/integration/test_migrations.py`** — required coverage:
- `test_alembic_upgrade_head_runs`: run `upgrade head` against `sqlite:///:memory:` — no exception, exit 0
- `test_migration_failure_raises`: patch `alembic_command.upgrade` to raise, confirm `SystemExit(1)` is triggered

**Important test isolation:** All tests that call `init_database()` must call `close_database()` in teardown. Use async fixtures with `asyncio_mode = "auto"` (already set in pyproject.toml). The global `_connection` in `database.py` is reset to `None` by `close_database()` — tests must not share connection state.

**Migration test pattern:**
```python
# tests/integration/test_migrations.py
import pytest
from alembic.config import Config
from alembic import command as alembic_command


def test_alembic_upgrade_head_runs() -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", "sqlite:///:memory:")
    alembic_command.upgrade(cfg, "head")  # must not raise
```

---

### mypy Considerations

- `logging_config.py` uses `structlog.types.EventDict` and `structlog.types.WrappedLogger` for typing — these are available in structlog 25.x
- `from __future__ import annotations` at top of files using `X | Y` union syntax (Python 3.10+ style) — needed for mypy under 3.12 target
- The `_connection: aiosqlite.Connection | None = None` global — mypy strict requires explicit `None` default
- aiosqlite is in the mypy `ignore_missing_imports` override (set in Story 1.1) — no stubs needed
- `list[Any]` in `logging_config.py` requires `from typing import Any`

---

### Previous Story Learnings (from Story 1.1)

- **`uv` invocation:** Always `python -m uv`, never bare `uv` (not on PATH)
- **ruff exclude list:** `.agents`, `.claude`, `_bmad`, `_bmad-output`, `docs`, `.venv` — these are already in `pyproject.toml`. Do not modify.
- **mypy strict overrides:** `dsmr_parser.*`, `pymodbus.*`, `ocpp.*`, `aiosqlite.*`, `alembic.*` all have `ignore_missing_imports = true` — no need to add stub packages
- **asyncio_default_fixture_loop_scope = "function"** already set in pyproject.toml — do not add `loop_scope` to fixtures manually
- **pytest asyncio_mode = "auto"** — all `async def test_*` functions are automatically treated as async tests; no `@pytest.mark.asyncio` decorator needed
- **src layout:** `src/open_ems/` — imports are `from open_ems.storage.database import ...` not `from storage.database import ...`
- **hatchling build backend:** Do not modify `[build-system]` section in pyproject.toml
- **`py.typed` exists** in `src/open_ems/` — mypy strict type checking applies to the full package

---

### Architecture Alignment

| Architecture Decision | Story 1.2 implementation |
|---|---|
| Decision 5.5: Alembic auto-runs before adapters/control loop | lifespan runs migration before yield |
| Decision 5.1: structlog dual-sink logging | `logging_config.py` configures JSON stdout; audit log SQLite sink deferred to Epic 6 |
| Decision 1.2: SQLite as authoritative store, WAL mode | `database.py` enables WAL PRAGMA on init |
| AR20: Repository pattern — no raw SQL outside `storage/repositories/` | Empty `repositories/__init__.py` placeholder; enforcement in code review |
| Decision 5.3: pydantic-settings for deployment config | Minimal `settings.py` with `db_path`, `log_level` — full schema in Story 1.7 |
| Import boundary: `core/` may not import from `storage/`, `web/`, `services/` | This story only creates `storage/` and `web/` — no `core/` yet |
| Startup order: load config → migrations → ... → serve traffic | Exact lifespan sequence in `create_app()` |

**Import boundary reminder:**
- `web/app.py` may import from `storage/` and `settings.py` ✅
- `storage/database.py` may NOT import from `web/` ✅
- No circular imports between packages

---

### References

- [Source: architecture.md#Decision 5.5] — Alembic auto-runs before adapters; migration failure is fatal
- [Source: architecture.md#Decision 5.1] — structlog two sinks; JSON to stdout
- [Source: architecture.md#Decision 1.2] — SQLite WAL mode, single authoritative store
- [Source: architecture.md#Decision 5.3] — pydantic-settings; DB_PATH, LOG_LEVEL from env
- [Source: architecture.md#Startup Sequence Pattern] — exact 9-step startup order
- [Source: architecture.md#Project Structure Patterns] — `storage/repositories/`, `web/app.py`, `main.py`
- [Source: architecture.md#Repository Pattern] — all DB access through repository classes
- [Source: architecture.md#AR20] — raw SQL forbidden outside `storage/repositories/`
- [Source: architecture.md#Logging Patterns] — structlog `logger.event(...)` with kwarg context
- [Source: epics.md#Story 1.2] — Acceptance Criteria source
- [Source: epics.md#Epic 1] — AR1-AR5, NFR-D4, AR20 coverage

## Dev Agent Record

### Agent Model Used

claude-sonnet-4-6

### Debug Log References

- `_run_alembic_upgrade` imports `alembic_command` inside the function body (local import for deferred loading). Patching `alembic.command.upgrade` (not `open_ems.web.app.alembic_command`) is the correct target because the module attribute is resolved at call time from the already-imported `alembic.command` module.
- `alembic.ini` required both `path_separator = os` (new in alembic 1.18, for `prepend_sys_path`) AND `version_path_separator = os` (for `version_locations`). Without `path_separator`, alembic emits a `DeprecationWarning` on every migration run.
- ruff UP035/UP007: migration file generated from story template used `from typing import Sequence, Union`. Fixed to `from __future__ import annotations` + `from collections.abc import Sequence` + `str | None` syntax.
- ruff B904: `raise SystemExit(1)` inside `except` clause requires `from None` to suppress exception chaining. Applied in both `app.py` and the integration test.
- `_add_component` processor handles two logger types: structlog `BoundLogger` (has `.name` via `__getattr__` delegation) and plain `str` (used by `ProcessorFormatter.foreign_pre_chain` for stdlib records).

### Completion Notes List

- All 2 ACs verified: SQLite WAL + foreign keys enabled, Alembic runs before any route, migration failure raises SystemExit(1), repository placeholder in place, baseline migration runs clean, structlog JSON output with all required fields (timestamp, level, event, component), stdlib logging bridged.
- 12 tests pass: 3 integration (alembic runs, idempotent, failure handling), 5 unit/storage (WAL, FK, lifecycle, RuntimeError, row_factory), 3 unit/logging (JSON format, required fields, level filtering), 1 baseline.
- `uv.lock` unchanged — no new dependencies added (alembic and structlog were already locked in Story 1.1).
- ruff, mypy strict, pytest all exit 0.

### File List

- `alembic.ini` (created)
- `migrations/__init__.py` (created)
- `migrations/env.py` (created)
- `migrations/script.py.mako` (created)
- `migrations/versions/__init__.py` (created)
- `migrations/versions/0001_initial_schema.py` (created)
- `src/open_ems/logging_config.py` (created)
- `src/open_ems/main.py` (created)
- `src/open_ems/settings.py` (created)
- `src/open_ems/storage/__init__.py` (created)
- `src/open_ems/storage/database.py` (created)
- `src/open_ems/storage/repositories/__init__.py` (created)
- `src/open_ems/web/__init__.py` (created)
- `src/open_ems/web/app.py` (created)
- `tests/conftest.py` (created)
- `tests/integration/__init__.py` (created)
- `tests/integration/test_migrations.py` (created)
- `tests/unit/__init__.py` (created)
- `tests/unit/storage/__init__.py` (created)
- `tests/unit/storage/test_database.py` (created)
- `tests/unit/test_logging.py` (created)

## Senior Developer Review (AI)

**Review Date:** 2026-05-01  
**Outcome:** Changes Requested  
**Reviewer:** claude-sonnet-4-6 (parallel 3-layer adversarial review)

### Summary

10 patch findings, 1 deferred, 8 dismissed as noise. 5 High severity issues require attention before this story can be marked done.

### Action Items

**High**
- [x] Migration failure test is self-referential — lifespan error path has no coverage [tests/integration/test_migrations.py:30]
- [x] `exc_info=True` without `format_exc_info` processor — traceback not JSON-serializable, error log crashes at runtime [src/open_ems/logging_config.py:37]
- [x] `init_database` silently leaks connection on double-call [src/open_ems/storage/database.py:12]
- [x] Partial `init_database` failure leaves broken `_connection` [src/open_ems/storage/database.py:14]
- [x] `Config("alembic.ini")` resolves relative to CWD — fails on systemd/Docker deployment [src/open_ems/web/app.py:22]

**Medium**
- [x] WAL PRAGMA result not validated — silent failure on FAT32/read-only filesystems [src/open_ems/storage/database.py:14]
- [x] `log_level` accepts any string, silently falls back to INFO on typos [src/open_ems/settings.py:13]
- [x] Idempotency test uses separate in-memory DBs — does not test same-DB idempotency [tests/integration/test_migrations.py:22]

**Low**
- [x] `tmp_db_path` fixture has wrong type annotation (`pytest.TempPathFactory` → `pathlib.Path`) [tests/conftest.py:10]
- [x] `initialized_db` fixture typed as `AsyncGenerator[None, None]` but yields `aiosqlite.Connection` [tests/conftest.py:15]

**Deferred**
- [x] Pre-lifespan uvicorn startup logs through stdlib before `configure_logging` — inherent FastAPI ordering constraint
