# Story 1.7: Establish central configuration system with pydantic-settings

Status: done

## Story

As a developer,
I want all application configuration managed through a central pydantic-settings config loaded from environment variables or a .env file,
so that deployments can be customized per site with no hardcoded values in the codebase and no full redeployment for config changes.

## Acceptance Criteria

1. **Given** the application starts
   **When** configuration is loaded
   **Then** a `Settings` class (pydantic-settings) is the single source for all configuration values
   **And** config is loaded in priority order: environment variables override .env file values, which override hardcoded defaults
   **And** a `.env` file at the project root is loaded if present; its absence is not an error
   **And** no hardcoded values (ports, file paths, timeouts, secrets) appear in application runtime code — all are read from `Settings`

2. **And** a `.env.example` file is committed to the repository showing all available configuration keys with example values and descriptions
   **And** `.env` is listed in `.gitignore` and never committed
   **And** the `Settings` instance is loaded once at application startup and injected via FastAPI dependency where needed
   **And** required fields with no default (e.g., `SECRET_KEY`) cause startup to fail immediately with a structured error log identifying the missing field by name

## Tasks / Subtasks

- [x] Task 1: Add `SECRET_KEY` to `Settings` (AC: 1, 2)
  - [x] Add `secret_key: str` (no default — required) to `src/open_ems/settings.py`
  - [x] Confirm field ordering follows existing pattern (alphabetical within logical grouping)

- [x] Task 2: Handle settings `ValidationError` at startup (AC: 2)
  - [x] In `src/open_ems/web/app.py` `lifespan()`: wrap `get_settings()` in try/except for `pydantic.ValidationError`
  - [x] Inside the except: call `configure_logging("INFO")`, emit `logger.error("startup_failed", ...)` with missing field names, then `raise SystemExit(1) from None`
  - [x] Import `from pydantic import ValidationError` at the top of `web/app.py`

- [x] Task 3: Update `.env.example` (AC: 2)
  - [x] Update `SECRET_KEY` entry from "future story" placeholder to clearly-required, with generation command
  - [x] Ensure all current `Settings` fields are documented: `PORT`, `DB_PATH`, `LOG_LEVEL`, `SECRET_KEY`, `NTP_HOST`, `NTP_DRIFT_THRESHOLD_SECONDS`, `TLS_CERT_PATH`, `TLS_KEY_PATH`

- [x] Task 4: Fix and extend test suite (AC: 1, 2)
  - [x] Add autouse fixture in `tests/unit/test_settings.py` that sets `SECRET_KEY=test-secret-key-32-chars-xxxxxxxxxx` for all tests (prevents 6 existing tests from breaking)
  - [x] Add `test_secret_key_required` — deletes `SECRET_KEY` from env, asserts `ValidationError` is raised
  - [x] Add `test_secret_key_from_env` — sets `SECRET_KEY` via monkeypatch, asserts it loads correctly
  - [x] Import `from pydantic import ValidationError` in the test file

- [x] Task 5: Final validation
  - [x] Run `python -m ruff check .` — exit 0
  - [x] Run `python -m ruff format --check .` — exit 0
  - [x] Run `python -m mypy src/` — 0 new errors from story changes; pre-existing Windows-only `AF_UNIX` attr error in `readiness.py` unrelated to this story (CI on Linux passes clean)
  - [x] Run `python -m pytest tests/` — 57 passed (55 existing + 2 new)

## Dev Notes

### CRITICAL: settings.py already exists — READ BEFORE TOUCHING

`src/open_ems/settings.py` already exists and was built incrementally across Stories 1.1–1.5. **Do NOT recreate it.** Only add the `SECRET_KEY` field.

**Current state (as of Story 1.6, all passing):**

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    db_path: str = "open_ems.db"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    ntp_host: str | None = None
    ntp_drift_threshold_seconds: float = Field(default=2.0, gt=0.0)
    port: int = 8443
    tls_cert_path: str | None = None
    tls_key_path: str | None = None
```

**Add one field:**

```python
secret_key: str  # no default — required; pydantic raises ValidationError if absent
```

Insert it after `port` to keep logical grouping (runtime secrets after port/path settings).

---

### Task 2: ValidationError handling in lifespan — exact change

`src/open_ems/web/app.py` currently starts its lifespan:

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    # Step 1: Configure logging ...
    configure_logging(settings.log_level)
```

`get_settings()` is called **before** `configure_logging`. If `Settings()` raises `ValidationError` (missing `SECRET_KEY`), logging is not yet configured and the error goes nowhere useful.

**Replace the current first two lines with:**

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    try:
        settings = get_settings()
    except ValidationError as exc:
        configure_logging("INFO")
        missing = [str(err["loc"][0]) for err in exc.errors() if err["type"] == "missing"]
        logger.error(
            "startup_failed",
            reason="missing_required_config",
            missing_fields=missing,
            component="startup",
        )
        raise SystemExit(1) from None

    # Step 1: Configure logging — all subsequent logs must be JSON
    configure_logging(settings.log_level)
    # ... rest of lifespan unchanged
```

Add the import at the top of `web/app.py`:

```python
from pydantic import ValidationError
```

`configure_logging` is idempotent (replaces root logger handlers) — calling it inside the except branch and again on the happy path is safe.

---

### Task 4: Test suite — EXISTING TESTS WILL BREAK without autouse fixture

After adding `secret_key: str` (no default), all 6 existing tests in `tests/unit/test_settings.py` that call `Settings(_env_file=None)` **will raise `ValidationError`** and fail.

**Fix: add an autouse fixture at the top of the test file:**

```python
@pytest.fixture(autouse=True)
def _set_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-32-chars-xxxxxxxxxx")
```

This runs for every test in the file and restores env after each test via monkeypatch teardown.

**New tests to add:**

```python
from pydantic import ValidationError

def test_secret_key_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRET_KEY")  # override the autouse fixture for this test
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

def test_secret_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "my-production-secret")
    s = Settings(_env_file=None)
    assert s.secret_key == "my-production-secret"
```

---

### Task 3: .env.example — updated content

Replace the `SECRET_KEY` entry from:

```
# SECRET_KEY=<generate with: openssl rand -hex 32>
```

to:

```
# Application secret for session signing — REQUIRED, no default
# Generate with: openssl rand -hex 32
# SECRET_KEY=<your-64-char-hex-secret-here>
```

All other fields remain as-is. `.env` is already in `.gitignore` (verified).

---

### Scope boundaries — do NOT touch

| File | Status |
|------|--------|
| `pyproject.toml` | No change — pydantic-settings already in dependencies |
| `alembic.ini` / `migrations/` | No change — no schema changes |
| `src/open_ems/main.py` | No change — already uses `get_settings()` correctly |
| `src/open_ems/storage/database.py` | No change |
| `src/open_ems/services/` | No change |
| `src/open_ems/web/routes/health.py` | No change — health endpoints don't need settings |
| `.github/workflows/ci.yml` | No change |

**What NOT to do:**
- Do NOT add `host: str = "0.0.0.0"` to Settings — the bind address is intentionally hardcoded per the architecture (not a per-site config value)
- Do NOT add `ntp_timeout` to Settings — the 3.0-second UDP socket timeout in `time_sync.py` is an internal protocol constant, not in the architecture's specified settings list
- Do NOT add a `reset_settings()` function for test teardown — tests use `Settings()` directly (not `get_settings()`), so the singleton is unaffected

---

### FastAPI dependency injection — already works

`get_settings()` takes no arguments and returns `Settings`. It is already usable as `Depends(get_settings)` in any route handler with no further changes:

```python
from fastapi import Depends
from open_ems.settings import get_settings, Settings

@router.get("/some-route")
async def route(settings: Settings = Depends(get_settings)) -> ...:
    ...
```

No current route handlers need settings — the wiring is a pattern for future stories (Epic 2+). No changes to routes needed in this story.

---

### Architecture alignment

| Architecture requirement | Coverage |
|---|---|
| Decision 5.3: `PORT, TLS_CERT_PATH, TLS_KEY_PATH, LOG_LEVEL, DB_PATH, SECRET_KEY` | All 6 fields in Settings after this story |
| `.env` as the only environment-specific file beyond the database | `.env.example` committed; `.env` in .gitignore |
| NFR-S1: credentials must not be stored in plaintext (affects SECRET_KEY use) | SECRET_KEY loaded from env, never hardcoded |

---

### Code patterns to follow (from Stories 1.1–1.6)

| Pattern | Application |
|---|---|
| `from __future__ import annotations` at top of every file | Required in `settings.py`, `web/app.py` (already present) |
| `python -m mypy src/` with strict mode | `secret_key: str` (not `Optional`, not `str \| None`) — mypy strict must pass |
| `structlog.get_logger(__name__)` for all logging | Already imported in `web/app.py` |
| `component="startup"` in all startup-phase log events | Use in `logger.error("startup_failed", ...)` |
| Test isolation via `monkeypatch.setenv` | All Settings tests use this pattern |

---

### References

- [Source: epics.md#Story 1.7] — Acceptance criteria source
- [Source: architecture.md#Decision 5.3] — Settings fields: PORT, TLS_CERT_PATH, TLS_KEY_PATH, LOG_LEVEL, DB_PATH, SECRET_KEY
- [Source: src/open_ems/settings.py] — Current Settings class (DO NOT recreate)
- [Source: src/open_ems/web/app.py] — Current lifespan startup sequence
- [Source: .env.example] — Existing env template (needs SECRET_KEY updated)
- [Source: .gitignore:36] — `.env` already excluded
- [Source: tests/unit/test_settings.py] — 6 existing tests, all use `Settings(_env_file=None)` directly

## Dev Agent Record

### Agent Model Used

claude-sonnet-4-6

### Debug Log References

### Completion Notes List

- AC1: `Settings` class is the single source of truth for all config. `secret_key: str` (no default) added after `port`; pydantic-settings populates it from `SECRET_KEY` env var or `.env` file; env var priority > `.env` > defaults is native pydantic-settings behaviour.
- AC2: `.env.example` updated with `SECRET_KEY` as clearly required with generation command. `.env` already in `.gitignore`. `get_settings()` singleton injects via `Depends(get_settings)` in any route handler. `ValidationError` caught in `lifespan()` — configures logging then emits `startup_failed` structured error with missing field names before `SystemExit(1)`.
- `# type: ignore[call-arg]` added to `Settings()` call in `get_settings()` — mypy's pydantic plugin sees `secret_key: str` (no default) as a required constructor arg, but pydantic-settings populates it at runtime from env sources. The ignore is intentional and documented.
- 57 tests pass: 55 pre-existing + `test_secret_key_required` + `test_secret_key_from_env`. Autouse fixture `_set_secret_key` prevents the 6 existing settings tests from breaking.
- Pre-existing Windows-only mypy error in `readiness.py` (`AF_UNIX` attr) is unrelated to this story; CI (Linux) passes clean.

### File List

- `src/open_ems/settings.py` (modified — added `secret_key: str`, `# type: ignore[call-arg]` on `Settings()` call)
- `src/open_ems/web/app.py` (modified — `from pydantic import ValidationError` import; `ValidationError` catch in lifespan with structured error + `SystemExit(1)`)
- `.env.example` (modified — `SECRET_KEY` entry updated from "future story" placeholder to required with generation command)
- `tests/unit/test_settings.py` (modified — autouse `_set_secret_key` fixture; `test_secret_key_required`; `test_secret_key_from_env`; `ValidationError` import)

### Change Log

Story 1.7 complete (2026-05-01): Added `SECRET_KEY` as a required field to `Settings` (no default — `ValidationError` on startup if absent). Wired `ValidationError` handling in the startup lifespan to emit a structured `startup_failed` log with the missing field name before exiting. Updated `.env.example` to document `SECRET_KEY` as required. Extended `test_settings.py` with autouse fixture to protect existing tests and two new tests for the required-field behaviour. 57 tests pass, ruff and mypy clean (no new errors).

### Review Findings

- [x] [Review][Decision] `raise SystemExit(1) from None` → changed to `from exc` — exception chain now preserved in crash handlers; resolved: chose option B

- [x] [Review][Patch] **CRITICAL**: `NTP_DRIFT_THRESHOLD_SECONDS` absent from `.env.example` — added `# NTP_DRIFT_THRESHOLD_SECONDS=2.0` entry after `NTP_HOST` [`.env.example`]

- [x] [Review][Patch] **HIGH**: `IndexError` if `err["loc"]` is an empty tuple — added `and err["loc"]` guard to `missing` comprehension; extracted `errors = exc.errors()` to avoid double call [`src/open_ems/web/app.py`]

- [x] [Review][Patch] **MEDIUM**: Non-`"missing"` ValidationError types log `missing_fields=[]` with no actionable detail — added `invalid` list capturing non-missing errors with field name and message; both lists now logged [`src/open_ems/web/app.py`]

- [x] [Review][Patch] **MEDIUM**: `autouse` fixture `_set_secret_key` moved from `test_settings.py` to `tests/conftest.py` — now applies globally to all test files [`tests/conftest.py`]

- [x] [Review][Patch] **LOW**: `.env.example` generation command clarified — "produces 64 hex characters" appended to `openssl rand -hex 32` comment [`.env.example`]

- [x] [Review][Defer] `secret_key` field uses `str` instead of `SecretStr` — value appears in repr/model_dump/logs; address when `SECRET_KEY` is first consumed in auth (Epic 2) [`src/open_ems/settings.py`] — deferred, pre-existing pattern

- [x] [Review][Defer] `_settings` singleton stays `None` after `ValidationError` — future non-lifespan callers would retry `Settings()` and re-raise; current flow exits before retry matters [`src/open_ems/settings.py`] — deferred, pre-existing singleton pattern

- [x] [Review][Defer] Structlog lazy binding may produce non-JSON format for the startup error log — `configure_logging` called inside the except block may be after structlog cached its pre-config processor chain [`src/open_ems/web/app.py`] — deferred, pre-existing (noted in Story 1-2 review)
