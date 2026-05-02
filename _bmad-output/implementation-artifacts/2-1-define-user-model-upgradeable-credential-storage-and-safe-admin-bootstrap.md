# Story 2.1: Define user model, upgradeable credential storage, and safe admin bootstrap

Status: done

## Story

As an installer,
I want the system to store credentials using a self-describing, algorithm-agnostic hash format and require a safe first-run bootstrap,
So that accounts are protected from the first deployment and can be migrated to stronger hashing algorithms in the future without breaking existing users.

## Acceptance Criteria

**AC1 — First-run guard: no password env var**
**Given** the application starts with no users in the database
**When** `INITIAL_ADMIN_PASSWORD` is not set in the environment
**Then** startup fails immediately with: `event="startup_failed"`, `reason="INITIAL_ADMIN_PASSWORD required when no users exist"`, `component="startup"`

**AC2 — First-run bootstrap**
**Given** the application starts with no users in the database
**When** `INITIAL_ADMIN_PASSWORD` is set
**Then** a user is created: `username="admin"`, `role="installer"`, `must_change_password=true`
**And** the password is stored as a bcrypt PHC string (self-describing: algorithm + cost + salt + hash)
**And** `INITIAL_ADMIN_PASSWORD` is never stored or emitted in any log

**AC3 — must_change_password enforcement (data layer)**
**Given** a user has `must_change_password=true`
**When** `user_repo.update_password()` is called with a new password
**Then** `must_change_password` is cleared to `false`
**And** the new hash is stored in PHC format
*(Redirect behavior at auth time is Story 2.2/2.3 scope — this AC covers the data model side)*

**AC4 — Algorithm-agnostic verification**
**Given** any stored password hash
**When** verified via `user_repo.verify_password()`
**Then** it works correctly regardless of the hash algorithm embedded in the PHC string
*(bcrypt is the default; future stories can add argon2 to CryptContext without invalidating existing hashes)*

**AC5 — Repository boundary**
**Given** the `users` table is accessed
**When** any read or write occurs
**Then** all access is via `src/open_ems/storage/repositories/user_repo.py`; no raw SQL outside the repository
**And** the Alembic migration `0002` creates the `users` table with: `id`, `username`, `role`, `hashed_password`, `must_change_password`, `created_at`

## Tasks / Subtasks

- [x] **Task 0: Pre-story gate check** (AC: all)
  - [x] Confirm A2 gate is done: CI has a coverage baseline lock before opening this story
  - [x] Include A1 gate in-story: migrate `secret_key: str` → `SecretStr` in `settings.py` (gate before Story 2.2; bundle here since we're touching settings.py)

- [x] **Task 1: Add bcrypt dependency** (AC: 2, 4)
  - [x] Add `"bcrypt>=4.0.0"` to `dependencies` in `pyproject.toml` (passlib replaced with direct bcrypt — see completion notes)
  - [x] Add `bcrypt.*` to mypy `ignore_missing_imports` overrides in `pyproject.toml`
  - [x] Run `uv lock` to update lockfile

- [x] **Task 2: Alembic migration — users table** (AC: 5)
  - [x] Create `migrations/versions/0002_add_users_table.py`
  - [x] `upgrade()`: `op.create_table("users", ...)` with all 6 columns
  - [x] `downgrade()`: `op.drop_table("users")`

- [x] **Task 3: Create user_repo.py** (AC: 2, 3, 4, 5)
  - [x] Module-level `hash_password(password: str) -> str` function (using bcrypt directly)
  - [x] Module-level `verify_password(plain: str, hashed: str) -> bool` function
  - [x] `UserRepo` class with `count()`, `create()`, `get_by_username()`, `update_password()`

- [x] **Task 4: Settings updates** (AC: 1, 2)
  - [x] Add `INITIAL_ADMIN_PASSWORD: SecretStr | None = None` to `Settings` (no default required; None = not provided)
  - [x] Migrate `secret_key: str` → `SecretStr` (A1 retro gate)
  - [x] Update `test_secret_key_from_env` in `tests/unit/test_settings.py` to use `.get_secret_value()`
  - [x] Update `.env.example` with `INITIAL_ADMIN_PASSWORD` entry

- [x] **Task 5: Fix `_PROJECT_ROOT` in app.py (retro gate A3)** (AC: all — startup reliability)
  - [x] Remove module-level `_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]`
  - [x] Add `_locate_project_root()` function using marker-file walk (finds `alembic.ini`)
  - [x] Call `_locate_project_root()` inside `_run_alembic_upgrade()` instead

- [x] **Task 6: Admin bootstrap in lifespan** (AC: 1, 2)
  - [x] After `init_database()`, check `await UserRepo().count() == 0`
  - [x] If empty + password absent → structured error + `SystemExit(1)`
  - [x] If empty + password set → hash + create admin + structured `admin_bootstrapped` log (no password)
  - [x] Import `UserRepo` and `hash_password` in `app.py`

- [x] **Task 7: Tests** (AC: all)
  - [x] `tests/unit/storage/repositories/test_user_repo.py` — all cases listed in Dev Notes
  - [x] Update `tests/unit/test_settings.py` for SecretStr changes
  - [x] `tests/unit/web/test_bootstrap.py` — bootstrap logic tests (added for coverage gate)

- [x] **Task 8: Final validation**
  - [x] `python -m ruff check .` — exit 0
  - [x] `python -m ruff format --check .` — exit 0
  - [x] `python -m mypy src/` — 0 errors (also fixed pre-existing unused type: ignore in readiness.py)
  - [x] `python -m pytest tests/` — 74 passed, 78% coverage (17 new tests added)

## Dev Notes

### Pre-story gates from Epic 1 retrospective

**A1 (include in this story):** `secret_key: str` → `SecretStr` in `settings.py`. Gate is "before Story 2.2 opens" — bundle it here since settings.py is already touched. Change requires: add `SecretStr` to pydantic import; update `test_secret_key_from_env` (assert `.get_secret_value()` instead of direct string compare). No other current code consumes `secret_key` — confirmed from Story 1.7 dev notes.

**A2 (must already be done):** Coverage baseline lock in CI (`--cov-fail-under` or test count guard). If CI does not yet enforce a baseline, stop and resolve before proceeding.

**A3 (in scope here):** Fix `_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]` — fragile under non-standard installs. Fix detailed in Task 5.

---

### Task 1 — passlib: exact change to pyproject.toml

**Add to `dependencies`:**
```toml
"passlib[bcrypt]>=1.7.4",
```

**Add a new mypy override block** (append after the existing `[[tool.mypy.overrides]]` block):
```toml
[[tool.mypy.overrides]]
module = ["passlib.*"]
ignore_missing_imports = true
```

`passlib` has no type stubs. With `ignore_missing_imports = true`, mypy treats all passlib return values as `Any`. Wrap them in explicit types in the implementation (see Task 3).

**Run after adding:**
```
uv lock
```

`bcrypt` has pre-built wheels for Python 3.12+ on Windows x64, Linux x64, and aarch64 (Raspberry Pi 64-bit). No native build tools needed.

---

### Task 2 — Migration: exact schema

**File:** `migrations/versions/0002_add_users_table.py`

```python
"""Add users table

Revision ID: 0002
Revises: 0001
Create Date: <today>

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("hashed_password", sa.Text(), nullable=False),
        sa.Column("must_change_password", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
        sa.CheckConstraint("role IN ('installer', 'homeowner')", name="ck_users_role"),
    )


def downgrade() -> None:
    op.drop_table("users")
```

Key decisions:
- `id` is TEXT (UUID4 string) — consistent with audit log traceability pattern
- `must_change_password` is INTEGER (SQLite has no boolean) — 0/1 — `server_default="1"` (True)
- `created_at` is TEXT (ISO 8601 UTC string) — consistent with architecture timestamp convention
- Role constraint at DB level — belt-and-suspenders on top of Pydantic validation

---

### Task 3 — user_repo.py: full interface

**File:** `src/open_ems/storage/repositories/user_repo.py`

Follow the `from __future__ import annotations` convention. Use `get_connection()` from `open_ems.storage.database` (the module-level singleton — same pattern as all Epic 2+ repos).

**Module-level constants (before the class):**

```python
from passlib.context import CryptContext  # type: ignore[import-untyped]

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Hash a password. Returns a bcrypt PHC self-describing string."""
    return str(_pwd_context.hash(password))


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plain text against a stored hash. Algorithm is resolved from the hash string."""
    return bool(_pwd_context.verify(plain_password, hashed_password))
```

`str()` and `bool()` casts are required for mypy strict — passlib returns `Any` without stubs.

**`UserRepo` class:**

```python
class UserRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    async def count(self) -> int:
        """Return total number of users."""
        cursor = await self._conn.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def create(
        self,
        username: str,
        hashed_password: str,
        role: str,
        must_change_password: bool = True,
    ) -> str:
        """Create a user. Returns the new user ID (UUID4 string)."""
        user_id = str(uuid.uuid4())
        created_at = datetime.now(UTC).isoformat()
        await self._conn.execute(
            "INSERT INTO users (id, username, role, hashed_password, must_change_password, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, role, hashed_password, int(must_change_password), created_at),
        )
        await self._conn.commit()
        return user_id

    async def get_by_username(self, username: str) -> aiosqlite.Row | None:
        """Fetch user row by username. Returns None if not found."""
        cursor = await self._conn.execute(
            "SELECT id, username, role, hashed_password, must_change_password, created_at"
            " FROM users WHERE username = ?",
            (username,),
        )
        return await cursor.fetchone()

    async def update_password(self, user_id: str, hashed_password: str) -> None:
        """Replace hashed_password and clear must_change_password flag."""
        await self._conn.execute(
            "UPDATE users SET hashed_password = ?, must_change_password = 0 WHERE id = ?",
            (hashed_password, user_id),
        )
        await self._conn.commit()
```

Required imports: `from __future__ import annotations`, `import uuid`, `from datetime import UTC, datetime`, `import aiosqlite`, `import structlog`, `from open_ems.storage.database import get_connection`.

No logging in `create()` — the bootstrap caller in `app.py` logs the event so that it has the right context. If you want logging in the repo, limit it to DEBUG.

---

### Task 4 — Settings: exact changes to settings.py

**Current settings.py:**
```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
```

**After change:**
```python
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
```

**Current fields (in order):** `db_path`, `log_level`, `ntp_host`, `ntp_drift_threshold_seconds`, `port`, `secret_key: str`.

**After change — replace `secret_key` and add `initial_admin_password`:**
```python
port: int = 8443
secret_key: SecretStr          # no default — required; pydantic raises ValidationError if absent
initial_admin_password: SecretStr | None = None  # only required on first run (no users)
tls_cert_path: str | None = None
tls_key_path: str | None = None
```

Keep `# type: ignore[call-arg]` on `Settings()` in `get_settings()` — still needed.

**Test update** in `tests/unit/test_settings.py`: change
```python
assert s.secret_key == "my-production-secret"
```
to:
```python
assert s.secret_key.get_secret_value() == "my-production-secret"
```

**`.env.example` — add after SECRET_KEY block:**
```
# Initial admin password — REQUIRED on first run (no users in database)
# Consumed once at startup to create the "admin" installer account.
# On subsequent starts this env var is ignored.
# Generate with: openssl rand -hex 16
# INITIAL_ADMIN_PASSWORD=<your-password-here>
```

---

### Task 5 — Fix `_PROJECT_ROOT` (retro gate A3)

**Current app.py (module level):**
```python
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
```

**Remove** this line entirely.

**Add** this function near the top of `app.py` (before `_run_alembic_upgrade`):
```python
def _locate_project_root() -> pathlib.Path:
    """Walk parent directories to find alembic.ini (project root marker)."""
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "alembic.ini").exists():
            return parent
    raise RuntimeError(
        "Cannot locate project root: alembic.ini not found in any parent directory. "
        "Verify the package is installed from the correct source tree."
    )
```

**Update `_run_alembic_upgrade`** to call it:
```python
def _run_alembic_upgrade(db_url: str) -> None:
    from alembic import command as alembic_command
    from alembic.config import Config

    project_root = _locate_project_root()   # ← replaces _PROJECT_ROOT
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(cfg, "head")
```

`import pathlib` stays at the top — it's still used in `_locate_project_root`.

---

### Task 6 — Bootstrap in lifespan: exact insertion point

**Current lifespan order (app.py):**
1. settings load + ValidationError handler
2. configure_logging
3. Alembic migrations (thread)
4. check_clock (thread)
5. init_database
6. mark_ready + sd_notify
7. watchdog task
8. yield

**Insert bootstrap between step 5 and 6:**

```python
    # Step 5b: Admin bootstrap — create initial admin if no users exist
    from open_ems.storage.repositories.user_repo import UserRepo, hash_password
    user_repo = UserRepo()
    user_count = await user_repo.count()
    if user_count == 0:
        initial_password = settings.initial_admin_password
        if initial_password is None:
            logger.error(
                "startup_failed",
                reason="INITIAL_ADMIN_PASSWORD required when no users exist",
                component="startup",
            )
            raise SystemExit(1) from None
        hashed = hash_password(initial_password.get_secret_value())
        await user_repo.create(
            username="admin",
            hashed_password=hashed,
            role="installer",
            must_change_password=True,
        )
        logger.info(
            "admin_bootstrapped",
            username="admin",
            must_change_password=True,
            component="startup",
        )
```

**Critical:** `initial_password.get_secret_value()` is called exactly once, only in the bootstrap path. The raw password is never assigned to a variable that persists — it is consumed inline. `SecretStr` prevents accidental repr/log leakage.

The local import `from open_ems.storage.repositories.user_repo import UserRepo, hash_password` is placed inside the lifespan to keep module-level imports clean. This is consistent with how `alembic` is imported inside `_run_alembic_upgrade`.

---

### Task 7 — Tests: complete test list

**File:** `tests/unit/storage/repositories/test_user_repo.py`

Tests require a real SQLite DB with the `users` table created. Use this fixture (do NOT run Alembic in unit tests — create the table directly):

```python
@pytest_asyncio.fixture
async def user_repo(tmp_db_path: str) -> AsyncGenerator[UserRepo, None]:
    await init_database(tmp_db_path)
    conn = get_connection()
    await conn.execute("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY NOT NULL,
            username TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL CHECK (role IN ('installer', 'homeowner')),
            hashed_password TEXT NOT NULL,
            must_change_password INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    await conn.commit()
    yield UserRepo()
    await close_database()
```

**Test cases:**
- `test_count_empty` — `count()` on empty table returns 0
- `test_create_returns_uuid` — `create()` returns a non-empty string
- `test_create_increments_count` — `count()` = 1 after `create()`
- `test_username_unique_constraint` — duplicate username raises `aiosqlite.IntegrityError`
- `test_hash_not_plaintext` — `get_by_username()["hashed_password"]` does not equal the raw password
- `test_hash_is_phc_format` — stored hash starts with `$2b$` (bcrypt PHC marker)
- `test_verify_password_correct` — `verify_password(raw, hash)` returns `True`
- `test_verify_password_wrong` — `verify_password("wrong", hash)` returns `False`
- `test_get_by_username_found` — returns `aiosqlite.Row` with correct username
- `test_get_by_username_not_found` — returns `None`
- `test_must_change_password_default_true` — `row["must_change_password"]` == 1 after `create()`
- `test_update_password_changes_hash` — new hash differs from old
- `test_update_password_clears_flag` — `must_change_password` == 0 after `update_password()`

**Settings test update:** In `tests/unit/test_settings.py`:
```python
# Change this:
assert s.secret_key == "my-production-secret"
# To this:
assert s.secret_key.get_secret_value() == "my-production-secret"
```

---

### Architecture compliance

| Requirement | How met |
|---|---|
| All DB access via repositories (arch pattern) | `user_repo.py` — no raw SQL in `app.py` or elsewhere |
| `from __future__ import annotations` in all files | Required in all new/modified files |
| `datetime.now(UTC)` — no naive datetimes | `created_at` in `create()` uses `datetime.now(UTC).isoformat()` |
| structlog for all logging | `logger.info("admin_bootstrapped", ...)` in lifespan |
| Never print() | No print() calls |
| SecretStr for secrets (arch Decision 2.3 + retro A1) | `secret_key: SecretStr`, `initial_admin_password: SecretStr | None` |
| mypy strict — 0 violations | passlib in mypy overrides; explicit `str()` / `bool()` casts on passlib calls |
| bcrypt for credential hashing (arch Decision 2.3) | `CryptContext(schemes=["bcrypt"])` |
| PHC string format + algorithm agnosticism | `deprecated="auto"` on CryptContext; future: add `argon2` as primary scheme |

---

### Scope boundaries — do NOT touch

| File / Area | Note |
|---|---|
| Sessions table | Story 2.2 scope |
| Login form / auth routes | Story 2.2 scope |
| `must_change_password` redirect | Story 2.2/2.3 — data model is ready; middleware enforces it |
| Role-based dependencies (`require_installer`) | Story 2.3 scope |
| CSRF middleware | Story 2.4 scope |
| `tests/integration/` | Not needed for this story; unit tests with temp SQLite are sufficient |
| `src/open_ems/web/routes/auth.py` | Does not exist yet — do NOT create it in this story |

### What NOT to do

- Do NOT add `homeowner` role users in the bootstrap — only "admin" installer account
- Do NOT store `INITIAL_ADMIN_PASSWORD` value anywhere after bootstrap (don't cache it in `_settings` or any module variable)
- Do NOT add password complexity validation here — that is Story 11.3
- Do NOT add `must_change_password` middleware enforcement — that is Story 2.2/2.3
- Do NOT add `passlib` to `dev` dependencies — it is a runtime dependency

---

### Patterns from Epic 1 to carry forward

| Pattern | Application |
|---|---|
| `from __future__ import annotations` top of every file | Required in `user_repo.py`, `0002_add_users_table.py` |
| `asyncio_mode = "auto"` in pytest | All test functions are `async def`; no need for `@pytest.mark.asyncio` |
| `tmp_db_path` fixture from conftest | Use for all `user_repo` tests — do not hardcode paths |
| `close_database()` in fixture teardown | Required — module-level `_connection` singleton must be reset between tests |
| `component="..."` field in all structlog calls | Use `component="startup"` for bootstrap log, `component="storage.user_repo"` if adding debug logs |
| mypy `type: ignore` with comment | `# type: ignore[import-untyped]` on passlib import OR handled via pyproject.toml override |

---

### References

- [Source: epics.md#Story 2.1] — Acceptance criteria, schema fields, AC3 scope note
- [Source: architecture.md#Decision 2.3] — bcrypt, first-run flow, no external IdP
- [Source: architecture.md#Project Structure] — `storage/repositories/user_repo.py` location
- [Source: architecture.md#Naming Conventions] — `users` table name, snake_case columns
- [Source: epic-1-retro-2026-05-02.md#Action Items] — A1 (SecretStr), A2 (coverage gate), A3 (_PROJECT_ROOT fix)
- [Source: src/open_ems/settings.py] — Current Settings; add SecretStr + INITIAL_ADMIN_PASSWORD
- [Source: src/open_ems/web/app.py] — Current lifespan; insert bootstrap at step 5b
- [Source: src/open_ems/storage/database.py] — `get_connection()` / `init_database()` pattern
- [Source: migrations/versions/0001_initial_schema.py] — Migration file format to follow
- [Source: tests/conftest.py] — `_set_secret_key` autouse fixture; `tmp_db_path`; `initialized_db`

## Dev Agent Record

### Agent Model Used

claude-sonnet-4-6

### Debug Log References

**D1 — passlib+bcrypt 5.0 incompatibility:** The story spec called for `passlib[bcrypt]>=1.7.4` with `CryptContext`. However, `passlib 1.7.4` is incompatible with `bcrypt>=4.0.0`: passlib's `detect_wrap_bug` backend init check hashes a 256-byte secret, which bcrypt 4.0+ rejects with `ValueError` (strict 72-byte enforcement). Solution: replaced passlib with a direct `bcrypt>=4.0.0` dependency. The same `$2b$...` PHC format is produced. Future stories can swap in passlib (or another wrapper) if multi-algorithm CryptContext becomes necessary.

**D2 — readiness.py type: ignore:** mypy now correctly narrows `socket.AF_UNIX` as existing within the `hasattr` guard (Python 3.14 stubs improvement). Removed the now-redundant `# type: ignore[attr-defined]` comment from `readiness.py:31`.

**D3 — bootstrap extraction for coverage:** Bootstrap logic extracted into `_bootstrap_admin_if_needed()` in `app.py` so it could be unit-tested directly (via `test_bootstrap.py`), restoring coverage to 78%.

### Completion Notes List

All 8 tasks complete. 74 tests pass (17 new). 78% branch coverage. Mypy clean (0 errors). All ACs satisfied:
- AC1: SystemExit(1) + structured log when no users + no INITIAL_ADMIN_PASSWORD
- AC2: admin user created with bcrypt PHC hash, must_change_password=True, no password logged
- AC3: update_password() clears must_change_password flag
- AC4: verify_password() works via bcrypt checkpw on any stored $2b$ hash
- AC5: users table in migration 0002; all access via user_repo.py only

Key deviation from spec: `bcrypt` used directly instead of passlib CryptContext (see D1). Interface is identical; future algorithm upgrade path unchanged.

### File List

- `pyproject.toml` (modified — added bcrypt>=4.0.0, bcrypt mypy override, removed passlib)
- `uv.lock` (modified — added bcrypt 5.0.0, removed passlib 1.7.4)
- `migrations/versions/0002_add_users_table.py` (created — users table migration)
- `src/open_ems/storage/repositories/user_repo.py` (created — hash_password, verify_password, UserRepo)
- `src/open_ems/settings.py` (modified — SecretStr for secret_key + initial_admin_password)
- `src/open_ems/web/app.py` (modified — _bootstrap_admin_if_needed, _locate_project_root, imports)
- `src/open_ems/services/readiness.py` (modified — removed unused type: ignore comment)
- `.env.example` (modified — added INITIAL_ADMIN_PASSWORD block)
- `tests/unit/storage/repositories/__init__.py` (created)
- `tests/unit/storage/repositories/test_user_repo.py` (created — 13 tests)
- `tests/unit/web/test_bootstrap.py` (created — 4 bootstrap tests)
- `tests/unit/test_settings.py` (modified — SecretStr get_secret_value() assertion)

### Review Findings

**Decision-needed:**
- [x] [Review][Patch] Add `ALEMBIC_INI_PATH: str | None = None` to Settings; `_run_alembic_upgrade` uses it when set, falls back to `_locate_project_root()` for dev/editable installs — keeps auto-migration on startup while making wheel/package deploys production-safe [src/open_ems/web/app.py:52-61, src/open_ems/settings.py]
- [x] [Review][Patch] Add `_HASHERS` PHC-prefix dispatch to `verify_password` — `"$2b$"` maps to bcrypt verifier; unknown prefixes return `False`; preserves the algorithm upgrade path without a rewrite when argon2 is added [src/open_ems/storage/repositories/user_repo.py:17-19]

**Patch:**
- [x] [Review][Patch] Cursor leak in `count()` and `get_by_username()` — neither uses `async with self._conn.execute(...) as cursor:` [src/open_ems/storage/repositories/user_repo.py:27-30, 51-58]
- [x] [Review][Patch] `verify_password()` raises `ValueError` on malformed hash instead of returning `False` — future login endpoints get an unhandled 500 instead of a 401 [src/open_ems/storage/repositories/user_repo.py:17-19]
- [x] [Review][Patch] Bootstrap TOCTOU race — two concurrent startups both observe `count()==0` and one crashes with uncaught `IntegrityError` on duplicate `admin` insert; catch and swallow the unique-violation [src/open_ems/web/app.py:22-52]
- [x] [Review][Patch] `update_password()` silent no-op on non-existent `user_id` — 0 rows affected, no exception, caller cannot detect the failure [src/open_ems/storage/repositories/user_repo.py:53-59]
- [x] [Review][Patch] `create()` accepts invalid `role` string — DB CHECK raises opaque `IntegrityError` with no distinction from uniqueness violation; add Python-level validation [src/open_ems/storage/repositories/user_repo.py:32-49]
- [x] [Review][Patch] `_CREATE_USERS_TABLE` DDL duplicated verbatim in two test files — schema drift risk; move to `conftest.py` [tests/unit/storage/repositories/test_user_repo.py:12-21, tests/unit/web/test_bootstrap.py:13-22]
- [x] [Review][Patch] `SystemExit(1)` in bootstrap escapes the `lifespan` generator before `yield` — `close_database()` is never called on first-run failure; wrap init sequence in `try/finally` [src/open_ems/web/app.py:120-157]
- [x] [Review][Patch] bcrypt silently truncates passwords > 72 bytes — two passwords differing only after byte 72 verify as equal; add a max-length guard or prominent docstring [src/open_ems/storage/repositories/user_repo.py:11-13]
- [x] [Review][Patch] Test gap: `test_bootstrap_skips_when_users_exist` only checks `count()==1`; a regression that overwrites the existing hash would not be caught [tests/unit/web/test_bootstrap.py:57-61]
- [x] [Review][Patch] Test gap: `test_bootstrap_creates_admin_user` does not assert stored hash starts with `$2b$` (AC2 PHC format) [tests/unit/web/test_bootstrap.py:43-50]
- [x] [Review][Patch] Test gap: `test_update_password_clears_flag` does not assert the new hash is PHC format (AC3 partial) [tests/unit/storage/repositories/test_user_repo.py]
- [x] [Review][Patch] `update_password()` docstring missing caller-hashes contract — callers must pass a pre-hashed value; a future caller passing plaintext will store it unhashed silently [src/open_ems/storage/repositories/user_repo.py:53]
- [x] [Review][Patch] Test gap: no test captures log output to verify `INITIAL_ADMIN_PASSWORD` never appears in any log line (AC2 secret-never-logged) [tests/unit/web/test_bootstrap.py]
- [x] [Review][Patch] Test gap: `test_bootstrap_exits_when_no_password` does not assert structured log fields `reason` and `component` mandated by AC1 [tests/unit/web/test_bootstrap.py:33-36]

**Deferred:**
- [x] [Review][Defer] `UserRepo()` constructed directly in lifespan with no DI — works today but ties lifespan tests to the live `get_connection()` singleton [src/open_ems/web/app.py:124] — deferred, pre-existing pattern
- [x] [Review][Defer] `INITIAL_ADMIN_PASSWORD` remains in process memory after bootstrap — `SecretStr` prevents repr/log leakage; clearing pydantic field post-bootstrap is non-standard [src/open_ems/settings.py:27] — deferred, acceptable for embedded system
- [x] [Review][Defer] `server_default="1"` string DDL for INTEGER column — semantically correct in SQLite, diverges on other dialects [migrations/versions/0002_add_users_table.py:31] — deferred, SQLite-only project
- [x] [Review][Defer] AC5 raw-SQL boundary enforcement — no linting rule or grep-based CI check prevents future callers from bypassing the repo [src/open_ems/storage/repositories/user_repo.py] — deferred, enforcement tooling beyond story scope
- [x] [Review][Defer] CI migration step has no `SECRET_KEY` env var — pre-existing from 1.7; if `alembic/env.py` calls `get_settings()`, migration CI step will crash on secrets validation [.github/workflows/ci.yml:44] — deferred, pre-existing

## Change Log

| Date | Change |
|---|---|
| 2026-05-02 | Implemented Story 2.1: user model, bcrypt credential storage, safe admin bootstrap. bcrypt used directly (passlib incompatible with bcrypt 5.x). 74 tests pass, 78% coverage. |
| 2026-05-02 | Code review complete. 16 patches applied: ALEMBIC_INI_PATH settings override, _HASHERS dispatch for verify_password, cursor leak fixes, TOCTOU race guard, update_password rowcount check, role validation, DDL deduplication into conftest, SystemExit try/finally, bcrypt 72-byte guard, 5 test-gap fixes. 79 tests pass, 78% coverage. |

