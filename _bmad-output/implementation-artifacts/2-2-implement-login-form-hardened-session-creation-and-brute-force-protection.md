# Story 2.2: Implement login form, hardened session creation, and brute-force protection

Status: done

## Story

As an installer or homeowner,
I want to log in securely over HTTPS with a hardened session cookie and rate-limiting against brute-force attacks,
So that my credentials and session are protected even on shared or unattended local networks.

## Acceptance Criteria

**AC1 — Login page renders with return URL preserved**
**Given** an unauthenticated user navigates to any protected URL
**When** they are redirected to the login page
**Then** `GET /login` renders an HTML login form
**And** the original URL is preserved as `?next=` in the redirect

**AC2 — Successful login: session creation and cookie**
**Given** a user submits valid credentials via `POST /login` over HTTPS
**When** the form is processed
**Then** the password is verified against the stored hash using the algorithm embedded in the PHC hash string
**And** any existing sessions for the same user are invalidated before the new session is created (session fixation prevention)
**And** a new session is created in the `sessions` table with a token of minimum 128 bits of entropy generated via `secrets.token_hex(32)`
**And** the token stored in the database is a SHA-256 hash of the raw cookie value — the raw token exists only in the cookie
**And** the session cookie is set with: `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`
**And** the user is redirected to the URL from `?next=` if valid (relative path, not open redirect), or to their role's default home screen if absent
**And** a structured event is written: `event="login_success"`, `component="auth"`, including role

**AC3 — HTTP login refusal**
**Given** a user submits valid credentials via `POST /login` over HTTP
**When** the form is processed
**Then** authentication is refused
**And** the user is redirected to the HTTPS equivalent URL
**And** no session cookie is ever set over HTTP

**AC4 — Invalid credentials**
**Given** a user submits invalid credentials
**When** the form is processed
**Then** the login form is re-rendered with a generic error: "Invalid username or password"
**And** no technical detail, role hint, or account-existence signal is included in the error
**And** a structured event is written: `event="login_failed"`, `component="auth"`, including source IP

**AC5 — Brute-force rate limiting**
**Given** failed login attempts from an IP address or for a username exceed the configured threshold (default: 5 failures within 5 minutes)
**When** a subsequent login attempt arrives
**Then** it is refused with the same generic error message
**And** rate-limit state is tracked in-memory per application process; counter resets on process restart
**And** a structured event is written: `event="auth_rate_limited"`, `component="auth"`, including source IP and username

**AC6 — Sessions table**
**And** the `sessions` table migration (0003) adds fields: `id`, `user_id`, `token_hash` (SHA-256 of cookie value), `created_at`, `last_active_at`, `expires_at`

## Tasks / Subtasks

- [x] **Task 0: Pre-story gate check** (AC: all)
  - [x] Run `python -m pytest tests/` — confirm 79 tests still pass, coverage ≥ 75%
  - [x] No additional pre-story gates from Epic 2 retro (retro not yet done)

- [x] **Task 1: Add dependencies** (AC: 1, 2)
  - [x] Add `"jinja2>=3.1.0"` to `dependencies` in `pyproject.toml`
  - [x] Add `"python-multipart>=0.0.12"` to `dependencies` in `pyproject.toml`
  - [x] Run `uv lock` to update lockfile
  - [x] Verify imports work: `from fastapi.templating import Jinja2Templates`

- [x] **Task 2: Settings additions** (AC: 2)
  - [x] Add `installer_session_timeout_hours: int = 4` to `Settings` in `settings.py`
  - [x] Add `homeowner_session_timeout_days: int = 30` to `Settings` in `settings.py`
  - [x] Add test for new settings fields in `tests/unit/test_settings.py`

- [x] **Task 3: Sessions table migration** (AC: 6)
  - [x] Create `migrations/versions/0003_add_sessions_table.py`
  - [x] `upgrade()`: `op.create_table("sessions", ...)` with all 6 columns + FK constraint + indexes
  - [x] `downgrade()`: `op.drop_table("sessions")`

- [x] **Task 4: session_repo.py** (AC: 2, 6)
  - [x] Create `src/open_ems/storage/repositories/session_repo.py`
  - [x] `SessionRepo` class: `create()`, `get_by_token_hash()`, `delete_by_id()`, `delete_all_for_user()`, `update_last_active()`

- [x] **Task 5: In-memory rate limiter** (AC: 5)
  - [x] Create `src/open_ems/services/rate_limiter.py`
  - [x] Module-level functions with `record_failure()`, `is_rate_limited()`, `reset_for_key()`, `_reset_all()`
  - [x] Sliding window (prune entries older than window on each call)
  - [x] Track by IP key and username key independently

- [x] **Task 6: Auth routes** (AC: 1–5)
  - [x] Create `src/open_ems/web/routes/auth.py`
  - [x] `GET /login` — render `login.html` template, pass `next` and optional `error`
  - [x] `POST /login` — HTTPS check → rate limit check → credential verify → session create → redirect
  - [x] Validate `?next=` to prevent open redirect
  - [x] Delete all existing user sessions before creating new session (AC2 session fixation)
  - [x] Set session cookie with correct attributes

- [x] **Task 7: Login template** (AC: 1, 4)
  - [x] Create `src/open_ems/web/templates/login.html`
  - [x] Apply design tokens from UX spec (CSS custom properties)
  - [x] Show `error` message if present
  - [x] Preserve `next` parameter in form action
  - [x] Pre-fill username on error (do NOT pre-fill password)

- [x] **Task 8: Wire auth router into app** (AC: 1–5)
  - [x] Add `from open_ems.web.routes.auth import router as auth_router` in `app.py`
  - [x] Add `app.include_router(auth_router)` in `create_app()`

- [x] **Task 9: Tests** (AC: all)
  - [x] Add `CREATE_SESSIONS_TABLE_DDL` to `tests/conftest.py`
  - [x] Add `session_repo` fixture to `tests/conftest.py`
  - [x] Create `tests/unit/storage/repositories/test_session_repo.py`
  - [x] Create `tests/unit/services/test_rate_limiter.py`
  - [x] Create `tests/unit/web/test_auth_routes.py`

- [x] **Task 10: Final validation** (AC: all)
  - [x] `python -m ruff check .` — exit 0
  - [x] `python -m ruff format --check .` — exit 0
  - [x] `python -m mypy src/` — 0 errors
  - [x] `python -m pytest tests/` — 126 passed, 83% coverage

### Review Findings

- [x] [Review][Patch] Add `TRUSTED_PROXY_IPS` setting and only trust `X-Forwarded-Proto` when `request.client.host` is in the list; also normalize multi-value header to first comma-separated segment — OPEN-EMS is directly reachable on LAN, so unconditional header trust is unsafe [`src/open_ems/settings.py`, `src/open_ems/web/routes/auth.py:81-84`]
- [x] [Review][Patch] Drop per-IP rate limiting — remove `"ip:{ip}"` key from `record_failure`, `is_rate_limited`, and `reset_for_key`; rely on username-based limiting only — behind a proxy the IP key is the proxy IP, causing shared lockouts without meaningful protection [`src/open_ems/services/rate_limiter.py`, `src/open_ems/web/routes/auth.py:86-97`]
- [x] [Review][Patch] HTTP→HTTPS redirect uses naive `str.replace("http://", "https://", 1)` — corrupts URLs containing `http://` in the query string; use Starlette's `request.url.replace(scheme="https")` [`src/open_ems/web/routes/auth.py:83`]
- [x] [Review][Patch] `_safe_next()` passes `/\evil.com` backslash-relative URLs — several browsers interpret `/\` as scheme-relative and redirect off-origin; block any path where the second character is `\` or where URL-decoded form starts with `/\` [`src/open_ems/web/routes/auth.py:39`]
- [x] [Review][Patch] `asyncio.get_event_loop().run_until_complete()` in two sync test functions — deprecated in Python 3.10+, raises `DeprecationWarning` in 3.12+; convert `test_post_login_stores_hash_not_raw_token` and `test_post_login_clears_old_sessions` to `async def` with `await` [`tests/unit/web/test_auth_routes.py:101-108, 196-211`]
- [x] [Review][Patch] `reset_rate_limiter` autouse fixture has no `yield` — `_reset_all()` runs only as setup, never as teardown; rate limiter state is left dirty after each test [`tests/unit/web/test_auth_routes.py:46-48`]
- [x] [Review][Patch] `max_age` recomputes `datetime.now(UTC)` after session DB write — under clock skew or slow writes `expires_at - now` can be zero or negative, producing a cookie that expires immediately; compute `max_age` from the original `timedelta` used to derive `expires_at` [`src/open_ems/web/routes/auth.py:145`]
- [x] [Review][Patch] Rate limiter retains empty list entries after pruning — `_failures` dict accumulates `"ip:*"` / `"user:*"` keys with empty lists indefinitely; delete the key after pruning when the list becomes empty [`src/open_ems/services/rate_limiter.py:14-16`]
- [x] [Review][Patch] Session timeout settings allow `0` or negative via env var — no `gt=0` constraint; a misconfigured `INSTALLER_SESSION_TIMEOUT_HOURS=0` makes all sessions expire on creation; add `Field(gt=0)` to both fields [`src/open_ems/settings.py:26-27`]
- [x] [Review][Patch] `login_success` event omits `user_id` — successful auth events cannot be correlated to a specific user in the audit log; add `user_id=user_id` to the structlog call [`src/open_ems/web/routes/auth.py:137-141`]
- [x] [Review][Defer] `update_last_active` raises `ValueError` on missing session — no caller exists yet; becomes relevant when Story 2.5 calls this on every authenticated request; caller must handle or it produces a 500 [`src/open_ems/storage/repositories/session_repo.py:78-83`] — deferred, Story 2.5 scope
- [x] [Review][Defer] `get_by_token_hash` does not filter by `expires_at` — expired tokens remain returnable; session validation must check `expires_at` separately; deferred to Story 2.5 expiry enforcement [`src/open_ems/storage/repositories/session_repo.py:46-53`] — deferred, Story 2.5 scope
- [x] [Review][Defer] No expired session cleanup from `sessions` table — rows accumulate indefinitely; Story 2.5 will add the bulk prune query using the `ix_sessions_expires_at` index [`migrations/versions/0003_add_sessions_table.py`] — deferred, Story 2.5 scope
- [x] [Review][Defer] No rollback in `session_repo.create()` if `commit()` fails after `execute()` — partial write inconsistency; pre-existing aiosqlite singleton architecture; no Story 2.x scope to address [`src/open_ems/storage/repositories/session_repo.py:38-43`] — deferred, pre-existing architecture
- [x] [Review][Defer] Single shared aiosqlite connection — no pooling, all concurrent requests serialise on one connection; pre-existing architecture decision; not introduced by this story — deferred, pre-existing

## Dev Notes

### Task 1 — Exact pyproject.toml changes

`fastapi` is declared as `"fastapi>=0.136.1"` (NOT `fastapi[standard]`). Neither `jinja2` nor `python-multipart` is a transitive dependency — both must be added explicitly.

**Add to `[project] dependencies`:**
```toml
"jinja2>=3.1.0",
"python-multipart>=0.0.12",
```

No new mypy overrides needed — both packages ship with type stubs.

---

### Task 2 — Exact settings.py changes

**Current last field:** `tls_key_path: str | None = None`

**Add after `tls_key_path`:**
```python
installer_session_timeout_hours: int = 4
homeowner_session_timeout_days: int = 30
```

These fields are used in Task 4 (`session_repo.create()`) to compute `expires_at`. They are also needed by Story 2.5 (session expiry enforcement) without requiring a schema change.

Env var names (pydantic-settings auto-derives): `INSTALLER_SESSION_TIMEOUT_HOURS`, `HOMEOWNER_SESSION_TIMEOUT_DAYS`.

---

### Task 3 — Migration: exact schema

**File:** `migrations/versions/0003_add_sessions_table.py`

Follow the header format from `0002_add_users_table.py` exactly (`revision`, `down_revision`, `branch_labels`, `depends_on`).

```python
"""Add sessions table

Revision ID: 0003
Revises: 0002
Create Date: <today>

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_active_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
```

Key decisions:
- `id` is TEXT (UUID4 string) — consistent with `users` table
- `token_hash` is TEXT (SHA-256 hex digest, 64 chars) — NOT the raw token
- All timestamps are TEXT (ISO 8601 UTC) — consistent with architecture convention
- FK `user_id` → `users.id` with `CASCADE` so sessions are cleaned up if a user is deleted
- Index on `user_id` — for `DELETE FROM sessions WHERE user_id = ?` (O(n_sessions) → O(log n))
- Index on `expires_at` — for Story 2.5 bulk prune: `DELETE FROM sessions WHERE expires_at < ?`

---

### Task 4 — session_repo.py: full interface

**File:** `src/open_ems/storage/repositories/session_repo.py`

Follow `user_repo.py` patterns exactly: `from __future__ import annotations`, `get_connection()` singleton, `async with self._conn.execute(...) as cursor:` (never bare cursor — learned from 2.1 review).

```python
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite

from open_ems.storage.database import get_connection

SESSION_TOKEN_BYTES = 32  # 256 bits entropy; architecture requires >= 128 bits


def generate_session_token() -> str:
    """Generate a cryptographically random session token. Returns raw hex string."""
    return secrets.token_hex(SESSION_TOKEN_BYTES)


def hash_token(raw_token: str) -> str:
    """SHA-256 hash of raw token. Only the hash is stored in the database."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


class SessionRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    async def create(
        self,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
    ) -> str:
        """Create a session. Returns the new session ID (UUID4 string)."""
        session_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            "INSERT INTO sessions (id, user_id, token_hash, created_at, last_active_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, user_id, token_hash, now, now, expires_at.isoformat()),
        )
        await self._conn.commit()
        return session_id

    async def get_by_token_hash(self, token_hash: str) -> aiosqlite.Row | None:
        """Fetch session by token hash. Returns None if not found."""
        async with self._conn.execute(
            "SELECT id, user_id, token_hash, created_at, last_active_at, expires_at"
            " FROM sessions WHERE token_hash = ?",
            (token_hash,),
        ) as cursor:
            return await cursor.fetchone()

    async def delete_by_id(self, session_id: str) -> None:
        """Delete a session by ID."""
        await self._conn.execute(
            "DELETE FROM sessions WHERE id = ?", (session_id,)
        )
        await self._conn.commit()

    async def delete_all_for_user(self, user_id: str) -> None:
        """Delete all sessions for a given user (session fixation prevention on re-login)."""
        await self._conn.execute(
            "DELETE FROM sessions WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    async def update_last_active(self, session_id: str) -> None:
        """Update last_active_at to now (called on every authenticated request — Story 2.5)."""
        now = datetime.now(UTC).isoformat()
        async with self._conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE id = ?",
            (now, session_id),
        ) as cursor:
            if cursor.rowcount == 0:
                raise ValueError(f"No session found with id={session_id!r}")
        await self._conn.commit()
```

Required imports at top: `from __future__ import annotations`, `import hashlib`, `import secrets`, `import uuid`, `from datetime import UTC, datetime, timedelta`, `import aiosqlite`, `from open_ems.storage.database import get_connection`.

`timedelta` is imported but not used in SessionRepo directly — it is used by the auth route when computing `expires_at`. You may omit it from `session_repo.py` and import it only in `auth.py`.

---

### Task 5 — Rate limiter: full implementation

**File:** `src/open_ems/services/rate_limiter.py`

In-memory, per-process, sliding window. Does NOT use asyncio.Lock — in asyncio's cooperative scheduling, dict read-modify-write is safe as long as there is no `await` between read and write. Keep the lock-free design to avoid deadlock risk.

```python
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

_WINDOW_SECONDS = 300       # 5-minute sliding window
_MAX_FAILURES = 5           # threshold before rate limiting

# {key: [failure_timestamps]}
# key is either "ip:<addr>" or "user:<username>"
_failures: dict[str, list[datetime]] = defaultdict(list)


def _prune(key: str, now: datetime) -> None:
    """Remove timestamps older than the window."""
    cutoff = now - timedelta(seconds=_WINDOW_SECONDS)
    _failures[key] = [t for t in _failures[key] if t > cutoff]


def record_failure(ip: str, username: str) -> None:
    """Record a failed login attempt for the given IP and username."""
    now = datetime.now(UTC)
    for key in (f"ip:{ip}", f"user:{username}"):
        _prune(key, now)
        _failures[key].append(now)


def is_rate_limited(ip: str, username: str) -> bool:
    """Return True if either the IP or username has exceeded the failure threshold."""
    now = datetime.now(UTC)
    for key in (f"ip:{ip}", f"user:{username}"):
        _prune(key, now)
        if len(_failures[key]) >= _MAX_FAILURES:
            return True
    return False


def reset_for_key(ip: str, username: str) -> None:
    """Clear rate limit counters on successful login."""
    _failures.pop(f"ip:{ip}", None)
    _failures.pop(f"user:{username}", None)
```

**No class needed** — module-level functions with module-level state. This matches the pattern of other service modules in the project (`readiness.py` uses module-level `_ready` flag). The state is per-process (resets on restart), which matches the AC.

For testability: tests can import `_failures` and clear it between tests, or call `reset_for_key()` to clear state. Add a `_reset_all()` function for tests:

```python
def _reset_all() -> None:
    """Clear all rate limit state. For testing only."""
    _failures.clear()
```

---

### Task 6 — Auth routes: full implementation

**File:** `src/open_ems/web/routes/auth.py`

```python
from __future__ import annotations

import pathlib
from typing import Annotated

import structlog
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from open_ems.services import rate_limiter
from open_ems.settings import get_settings
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, verify_password

logger = structlog.get_logger(__name__)

router = APIRouter()

_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_COOKIE_NAME = "session"
_ROLE_HOME: dict[str, str] = {
    "installer": "/installer/dashboard",
    "homeowner": "/homeowner/dashboard",
}


def _safe_next(next_url: str | None) -> str | None:
    """Validate next URL to prevent open redirect. Returns None if unsafe."""
    if next_url is None:
        return None
    # Must be a relative path, not protocol-relative (//) or absolute URL
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return None


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next, "error": error, "username": ""},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: str | None = None,
) -> Response:
    settings = get_settings()

    # AC3: Refuse login over HTTP
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    if scheme != "https":
        https_url = str(request.url).replace("http://", "https://", 1)
        return RedirectResponse(url=https_url, status_code=302)

    ip = request.client.host if request.client else "unknown"

    # AC5: Rate limit check BEFORE credential verification
    if rate_limiter.is_rate_limited(ip=ip, username=username):
        logger.warning(
            "auth_rate_limited",
            component="auth",
            source_ip=ip,
            username=username,
        )
        return _render_login(request, next=next, username=username,
                             error="Invalid username or password")

    # AC2 / AC4: Credential verification
    user_row = await UserRepo().get_by_username(username)
    if user_row is None or not verify_password(password, user_row["hashed_password"]):
        rate_limiter.record_failure(ip=ip, username=username)
        logger.info(
            "login_failed",
            component="auth",
            source_ip=ip,
        )
        return _render_login(request, next=next, username=username,
                             error="Invalid username or password")

    # Successful authentication — create session
    user_id: str = user_row["id"]
    role: str = user_row["role"]

    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    if role == "installer":
        expires_at = datetime.now(UTC) + timedelta(hours=settings.installer_session_timeout_hours)
    else:
        expires_at = datetime.now(UTC) + timedelta(days=settings.homeowner_session_timeout_days)

    session_repo = SessionRepo()

    # AC2: Delete all existing sessions for this user (session fixation prevention)
    # NOTE: Story 2.5 will relax this to allow concurrent sessions per user.
    await session_repo.delete_all_for_user(user_id)

    raw_token = generate_session_token()
    token_hash = hash_token(raw_token)
    await session_repo.create(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )

    rate_limiter.reset_for_key(ip=ip, username=username)

    logger.info(
        "login_success",
        component="auth",
        role=role,
    )

    redirect_to = _safe_next(next) or _ROLE_HOME.get(role, "/")
    response = RedirectResponse(url=redirect_to, status_code=303)
    max_age = int((expires_at - datetime.now(UTC)).total_seconds())
    response.set_cookie(
        key=_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
        max_age=max_age,
    )
    return response


def _render_login(
    request: Request,
    *,
    next: str | None,
    username: str,
    error: str,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next, "error": error, "username": username},
        status_code=200,
    )
```

**Important implementation notes:**

1. The `from datetime import ...` inside the function body — this is fine for readability, but move imports to the top of the file to satisfy ruff's `PLC0415` (import not at top) if ruff is configured with `C` rules. Check `pyproject.toml` — current ruff rules are `["E", "W", "F", "I", "B", "C4", "UP"]`. `C4` is flake8-comprehensions, NOT pylint. So the inline import will NOT be flagged. Keep it at the top of the file for clarity.

2. `structlog.get_logger(__name__)` — standard pattern across all modules.

3. `request.client` can be `None` in tests — always guard: `request.client.host if request.client else "unknown"`.

4. Rate limit check happens **before** DB lookup to prevent timing oracle.

5. `verify_password` from `user_repo.py` already handles malformed hashes by returning `False` (never raises) — this was patched in 2.1 review.

6. The response is `303 See Other` for POST→GET redirect — this is the correct HTTP semantics for form submission redirects.

7. `_ROLE_HOME` maps role → default destination. Destinations don't exist yet (Story 2.3); that is fine — the redirect will 404 until routes are added. The login itself is complete.

8. Cookie `max_age` is set to session timeout duration (in seconds). This means the browser cookie expires when the server-side session expires. Story 2.5 adds server-side expiry enforcement via `expires_at`.

---

### Task 7 — Login template

**File:** `src/open_ems/web/templates/login.html`

Create the `templates/` directory: `src/open_ems/web/templates/`

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Log in — OPEN-EMS</title>
  <style>
    :root {
      --color-surface: #f8fafc;
      --color-card: #ffffff;
      --color-border: #e2e8f0;
      --color-text-primary: #1e293b;
      --color-text-secondary: #64748b;
      --color-error: #dc2626;
      --font-sans: system-ui, -apple-system, sans-serif;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--color-surface);
      color: var(--color-text-primary);
      font-family: var(--font-sans);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .card {
      background: var(--color-card);
      border: 1px solid var(--color-border);
      border-radius: 8px;
      padding: 2rem;
      width: 100%;
      max-width: 360px;
    }
    h1 { font-size: 1.25rem; margin-bottom: 1.5rem; }
    label { display: block; margin-bottom: 1rem; }
    label span { display: block; font-size: 0.875rem; color: var(--color-text-secondary); margin-bottom: 0.375rem; }
    input[type="text"], input[type="password"] {
      width: 100%;
      padding: 0.5rem 0.75rem;
      border: 1px solid var(--color-border);
      border-radius: 4px;
      font-size: 1rem;
      font-family: inherit;
    }
    input:focus { outline: 2px solid #3b82f6; outline-offset: 1px; }
    .error {
      color: var(--color-error);
      font-size: 0.875rem;
      margin-bottom: 1rem;
      padding: 0.5rem 0.75rem;
      background: #fef2f2;
      border: 1px solid #fecaca;
      border-radius: 4px;
    }
    button[type="submit"] {
      width: 100%;
      padding: 0.625rem;
      background: #1e293b;
      color: #fff;
      border: none;
      border-radius: 4px;
      font-size: 1rem;
      font-family: inherit;
      cursor: pointer;
      margin-top: 0.5rem;
    }
    button[type="submit"]:hover { background: #334155; }
  </style>
</head>
<body>
  <div class="card">
    <h1>OPEN-EMS</h1>
    <form method="POST" action="/login{% if next %}?next={{ next | urlencode }}{% endif %}">
      {% if error %}
      <div class="error" role="alert">{{ error }}</div>
      {% endif %}
      <label>
        <span>Username</span>
        <input type="text" name="username" value="{{ username }}" required autofocus autocomplete="username">
      </label>
      <label>
        <span>Password</span>
        <input type="password" name="password" required autocomplete="current-password">
      </label>
      <!-- CSRF token: Story 2.4 -->
      <button type="submit">Log in</button>
    </form>
  </div>
</body>
</html>
```

**Notes:**
- `{{ username }}` pre-fills the username field on error (UX spec: preserve form state on failure)
- Password is never pre-filled
- `role="alert"` on the error div for screen reader accessibility
- `{{ next | urlencode }}` uses Jinja2's built-in `urlencode` filter — no custom filter needed
- CSRF placeholder comment — Story 2.4 will insert `<input type="hidden" name="csrf_token" value="...">` here

---

### Task 8 — App wiring: exact change to create_app()

**Current `create_app()` in `app.py`:**
```python
def create_app() -> FastAPI:
    app = FastAPI(title="OPEN-EMS", lifespan=lifespan)
    app.include_router(health_router)
    return app
```

**After change:**
```python
from open_ems.web.routes.auth import router as auth_router

def create_app() -> FastAPI:
    app = FastAPI(title="OPEN-EMS", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(auth_router)
    return app
```

Add the import to the top-level imports in `app.py` alongside `from open_ems.web.routes.health import router as health_router`.

---

### Task 9 — Tests: complete specification

#### conftest.py additions

Add after `CREATE_USERS_TABLE_DDL`:

```python
CREATE_SESSIONS_TABLE_DDL = """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY NOT NULL,
        user_id TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        last_active_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )
"""
```

Add `session_repo` fixture after the `user_repo` fixture:

```python
@pytest_asyncio.fixture
async def session_repo(tmp_db_path: str) -> AsyncGenerator[SessionRepo, None]:
    """Empty SessionRepo backed by a fresh SQLite DB with users + sessions tables."""
    await init_database(tmp_db_path)
    conn = get_connection()
    await conn.execute(CREATE_USERS_TABLE_DDL)
    await conn.execute(CREATE_SESSIONS_TABLE_DDL)
    await conn.commit()
    yield SessionRepo()
    await close_database()
```

Add import: `from open_ems.storage.repositories.session_repo import SessionRepo`

#### test_session_repo.py

**File:** `tests/unit/storage/repositories/test_session_repo.py`

Use the `session_repo` fixture (from conftest). The `user_repo` fixture is also available for creating a user to reference.

**Fixture for a test user (define locally):**
```python
@pytest_asyncio.fixture
async def user_id(session_repo: SessionRepo) -> str:
    user_repo = UserRepo(get_connection())
    return await user_repo.create(
        username="testuser",
        hashed_password=hash_password("testpass"),
        role="installer",
    )
```

**Test cases:**
- `test_create_returns_id` — `create()` returns a non-empty string
- `test_get_by_token_hash_found` — `get_by_token_hash(token_hash)` returns row with correct `user_id`
- `test_get_by_token_hash_not_found` — returns `None` for unknown hash
- `test_token_hash_unique_constraint` — duplicate `token_hash` raises `aiosqlite.IntegrityError`
- `test_delete_by_id` — session gone after `delete_by_id()`
- `test_delete_all_for_user` — all sessions for user deleted; sessions for other users unaffected
- `test_update_last_active` — `last_active_at` changes after `update_last_active()`
- `test_update_last_active_raises_on_missing_id` — raises `ValueError` if session_id not found

Also test helper functions:
- `test_generate_session_token_length` — token is 64 hex chars (32 bytes = 64 hex)
- `test_hash_token_deterministic` — same input → same output
- `test_hash_token_is_hex` — output is 64-char hex string (SHA-256 hex digest)
- `test_raw_token_not_equal_to_hash` — raw token != hash_token(raw_token)

#### test_rate_limiter.py

**File:** `tests/unit/services/test_rate_limiter.py`

**Setup:** import `rate_limiter` module; call `rate_limiter._reset_all()` in a fixture to isolate test state.

```python
import pytest
from open_ems.services import rate_limiter as rl

@pytest.fixture(autouse=True)
def reset() -> None:
    rl._reset_all()
```

**Test cases:**
- `test_not_limited_initially` — `is_rate_limited("1.2.3.4", "admin")` is `False`
- `test_limited_after_threshold_by_ip` — 5 `record_failure("1.2.3.4", "admin")` → `is_rate_limited` True
- `test_limited_after_threshold_by_username` — same IP but different usernames still limits on username key
- `test_not_limited_below_threshold` — 4 failures → still `False`
- `test_reset_clears_limit` — after `reset_for_key()`, `is_rate_limited` returns `False`
- `test_window_pruning` — failures older than window don't count (mock `datetime.now` to advance time, or use `_failures` dict directly to inject stale timestamps)

#### test_auth_routes.py

**File:** `tests/unit/web/test_auth_routes.py`

Use `TestClient` with `base_url="https://test"` for all HTTPS tests.

**Key fixtures needed:**
```python
import pytest
from fastapi.testclient import TestClient
from open_ems.web.app import create_app

@pytest.fixture
def client(initialized_db: ...) -> TestClient:
    # Need DB + users table + sessions table
    # Use a fixture that creates both tables
    ...
```

Wait — `TestClient` wraps the ASGI app which has a `lifespan`. You cannot easily use `TestClient` with the real lifespan in unit tests (it would try to run Alembic migrations). Use the standard FastAPI TestClient approach with `lifespan="off"` or use a pre-configured app that doesn't trigger lifespan, OR mock the auth route dependencies.

**Recommended approach:** Create a minimal test app that includes ONLY the auth router (no lifespan) and provide a pre-initialized DB via the `session_repo` fixture. Use `Depends` override if needed.

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient
from open_ems.web.routes.auth import router as auth_router

@pytest.fixture
def auth_app(session_repo, user_repo):
    # session_repo fixture initializes DB with both tables
    app = FastAPI()
    app.include_router(auth_router)
    return app

@pytest.fixture
def https_client(auth_app) -> TestClient:
    return TestClient(auth_app, base_url="https://test")

@pytest.fixture
def http_client(auth_app) -> TestClient:
    return TestClient(auth_app, base_url="http://test")
```

Note: `session_repo` fixture already calls `init_database()` and creates both tables (users + sessions). The `user_repo` fixture calls `init_database()` separately — these two conflict. Use a combined fixture or the `session_repo` fixture which sets up both tables.

**Actually:** the `session_repo` fixture (defined above) only creates `users` and `sessions` tables. But it also gives us `SessionRepo`. The `auth.py` handler calls `UserRepo()` and `SessionRepo()` directly — both use `get_connection()` which is initialized by `session_repo` fixture. So `session_repo` fixture is sufficient.

You'll also need a test user in the `users` table. Add a helper fixture:

```python
@pytest_asyncio.fixture
async def test_user(session_repo):
    from open_ems.storage.repositories.user_repo import UserRepo, hash_password
    from open_ems.storage.database import get_connection
    repo = UserRepo(get_connection())
    return await repo.create(
        username="admin",
        hashed_password=hash_password("correcthorse"),
        role="installer",
    )
```

**Test cases:**
- `test_get_login_returns_200` — `GET /login` → 200 with HTML
- `test_get_login_preserves_next_param` — `GET /login?next=/installer/dashboard` → response body contains `next`
- `test_post_login_valid_credentials_redirects` — `POST /login` with correct creds → 303 + `Location` header
- `test_post_login_sets_session_cookie` — response has `Set-Cookie: session=...` with `HttpOnly`, `Secure`, `SameSite=Strict`
- `test_post_login_stores_hash_not_raw_token` — raw token in cookie ≠ value in `sessions.token_hash`
- `test_post_login_invalid_password_returns_200` — wrong password → 200 (not redirect), error in HTML
- `test_post_login_unknown_user_returns_200` — unknown username → 200 with same generic error
- `test_post_login_error_message_is_generic` — error text == "Invalid username or password" exactly
- `test_post_login_username_preserved_on_error` — response HTML contains submitted username in input value
- `test_post_login_over_http_redirects_to_https` — `http_client` POST → 302 redirect to `https://`
- `test_post_login_no_cookie_over_http` — http POST response has no `Set-Cookie`
- `test_post_login_rate_limited_after_5_failures` — 5 failed POSTs → 6th returns 200 (not redirect) with generic error
- `test_post_login_success_clears_old_sessions` — after login, old session for same user is deleted
- `test_post_login_redirects_to_next_if_valid` — `next=/installer/dashboard` → 303 to `/installer/dashboard`
- `test_post_login_ignores_unsafe_next` — `next=https://evil.com` → 303 to role home (not external URL)
- `test_login_success_event_logged` (optional but valuable) — use `structlog.testing.capture_logs()` to assert `event="login_success"` in log output
- `test_login_failed_event_logged` — assert `event="login_failed"` in log output on bad creds
- `test_rate_limited_event_logged` — assert `event="auth_rate_limited"` after threshold

**Important test isolation:** `rate_limiter._reset_all()` must be called between tests that exercise rate limiting, or failures in one test will bleed into the next. Add to a fixture or call explicitly in rate-limiting tests.

**structlog test capture:** FastAPI TestClient uses the real app synchronously. To capture structlog output, use:
```python
import structlog.testing
with structlog.testing.capture_logs() as logs:
    response = https_client.post("/login", data={"username": "admin", "password": "wrong"})
assert any(log["event"] == "login_failed" for log in logs)
```

---

### Architecture Compliance

| Requirement | Implementation |
|---|---|
| `from __future__ import annotations` in all files | Required in all 3 new modules + migration |
| All DB access via repositories | `UserRepo` + `SessionRepo` only; no raw SQL in `auth.py` |
| `datetime.now(UTC)` — no naive datetimes | All timestamps use `datetime.now(UTC).isoformat()` |
| structlog for all logging | `logger = structlog.get_logger(__name__)` in `auth.py` |
| `component="auth"` on all auth events | Required in every structlog call in `auth.py` |
| Never `print()` | No print calls |
| `async with self._conn.execute(...) as cursor:` | Used in all `SELECT`/`UPDATE` in `session_repo.py` |
| `secrets.token_hex()` for session tokens | `generate_session_token()` uses `secrets.token_hex(32)` |
| Token hash stored (not raw) | `hash_token()` SHA-256 before storage |
| Cookie: `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/` | All set via `response.set_cookie()` |
| `asyncio_mode = "auto"` | No `@pytest.mark.asyncio` decorators in tests |
| `tmp_db_path` / `session_repo` fixture | Use conftest fixtures — do NOT hardcode DB paths |
| `close_database()` in fixture teardown | `session_repo` fixture calls `close_database()` on exit |
| mypy strict — 0 violations | `Annotated[str, Form()]` required for mypy on form params |

---

### Scope Boundaries — Do NOT Touch

| File / Area | Note |
|---|---|
| `require_installer` / `require_homeowner` deps | Story 2.3 scope |
| `dependencies.py` auth middleware | Story 2.3 scope |
| CSRF middleware | Story 2.4 scope |
| Session expiry enforcement (checking `expires_at`) | Story 2.5 scope — but `expires_at` must be stored now |
| `POST /logout` | Story 2.5 scope |
| Session `last_active_at` update on every request | Story 2.5 scope — `update_last_active()` exists but is not called yet |
| `must_change_password` redirect enforcement | Story 2.3 scope (enforced in auth dependency) |
| Password change form / `GET /change-password` | Story 11.3 scope |
| Any installer/homeowner dashboard routes | Story 2.3+ scope |

### Conflict Note: Session Concurrency (2.2 vs 2.5)

Story 2.2 AC2 states: "any existing sessions for the same user are invalidated before the new session is created." Story 2.5 AC states: "multiple concurrent sessions are permitted" (Option A). These are **contradictory**.

**Resolution for this story:** Follow Story 2.2's AC exactly — call `session_repo.delete_all_for_user(user_id)` before creating a new session. Story 2.5 will change this behavior when concurrent sessions are implemented. Add a code comment at the `delete_all_for_user` call as documented above.

---

### What NOT to Do

- Do NOT generate the Jinja2 `templates` object from `app.py` state — initialize it at module level in `auth.py` using `pathlib.Path(__file__).parent.parent / "templates"`
- Do NOT use `passlib` — it is NOT in the project (removed in Story 2.1). Use `bcrypt` directly (already done via `user_repo.verify_password`)
- Do NOT call `verify_password()` without first checking `user_row is not None` — avoid exception on None row
- Do NOT store the raw session token in the database — always store `hash_token(raw_token)`
- Do NOT skip the `_safe_next()` validation — open redirect is an OWASP top-10 vulnerability
- Do NOT raise exceptions in `verify_password()` — it already returns `False` on all error cases (patched in 2.1)
- Do NOT add `must_change_password` redirect — that is Story 2.3 scope
- Do NOT use `asyncio.Lock` in the rate limiter — cooperative asyncio scheduling makes it unnecessary and adds deadlock risk
- Do NOT call `session_repo.delete_all_for_user()` after creating the session — call it BEFORE

---

### Previous Story Intelligence (from 2.1)

| Learning | Application |
|---|---|
| `bcrypt` used directly (not passlib) — D1 | Do NOT add passlib. Use `user_repo.verify_password()` which already wraps bcrypt |
| Cursor leak fix — `async with conn.execute(...) as cursor:` required | Apply in ALL `SELECT`/`UPDATE` in `session_repo.py` |
| `update_password()` rowcount check pattern | Mirror in `update_last_active()` |
| `_HASHERS` PHC dispatch in `verify_password` | Already handles algorithm agnosticism — no changes needed |
| `TOCTOU` race guard with `IntegrityError` catch on bootstrap | If concurrent logins for same user: `delete_all_for_user` + `create` is NOT atomic. In the rare case of concurrent logins, one `create()` will succeed. Accept the race; don't add complex locking |
| DDL deduplication in conftest.py | `CREATE_SESSIONS_TABLE_DDL` goes in `conftest.py`, not in individual test files |
| `UserRepo()` DI pattern — constructed with `conn=None` → `get_connection()` | `SessionRepo()` follows same pattern |
| Review patches produced 16 fixes in 2.1 | Write the implementation correctly the first time using the exact code patterns given above |
| `component="startup"` pattern for structured logs | Use `component="auth"` for all events in `auth.py` |
| `hash_password()` raises `ValueError` if password > 72 bytes | Not relevant for login (we only call `verify_password`, not `hash_password`). But note: `generate_session_token()` does NOT go through `hash_password` — session tokens use SHA-256, not bcrypt |

---

### Project Structure Notes

New files follow the architecture structure exactly:

```
migrations/versions/0003_add_sessions_table.py   ← new
src/open_ems/
  services/
    rate_limiter.py                               ← new
  storage/repositories/
    session_repo.py                               ← new
  web/
    routes/
      auth.py                                     ← new
    templates/
      login.html                                  ← new (also create the templates/ dir)
tests/
  unit/
    services/
      test_rate_limiter.py                        ← new
    storage/repositories/
      test_session_repo.py                        ← new
    web/
      test_auth_routes.py                         ← new
```

Modified files:
- `pyproject.toml` — add `jinja2`, `python-multipart` dependencies
- `uv.lock` — updated by `uv lock`
- `src/open_ems/settings.py` — add session timeout settings
- `src/open_ems/web/app.py` — include `auth_router`
- `tests/conftest.py` — add `CREATE_SESSIONS_TABLE_DDL`, `session_repo` fixture, `SessionRepo` import

---

### References

- [Source: epics.md#Story 2.2] — Acceptance criteria, session table schema, brute-force threshold (5/5min), cookie requirements
- [Source: architecture.md#Decision 2.1] — Server-side sessions, `secrets` module, httpOnly/Secure/SameSite=Strict, `require_installer`/`require_homeowner` dependency pattern
- [Source: architecture.md#Decision 2.3] — bcrypt, no external IdP
- [Source: architecture.md#Project Structure] — `web/routes/auth.py`, `storage/repositories/session_repo.py`, `web/templates/login.html` locations
- [Source: ux-design-specification.md#Component Reference] — Login form anatomy, error message text, return URL behavior, form state preservation
- [Source: ux-design-specification.md#Visual Design Foundation] — CSS design tokens
- [Source: 2-1-define-user-model-upgradeable-credential-storage-and-safe-admin-bootstrap.md#Dev Agent Record] — D1 (bcrypt direct), cursor leak pattern, review patches, `_HASHERS` dispatch
- [Source: src/open_ems/storage/repositories/user_repo.py] — `verify_password()`, `UserRepo` pattern, `_VALID_ROLES` pattern
- [Source: src/open_ems/storage/database.py] — `get_connection()`, `init_database()` singleton
- [Source: tests/conftest.py] — `CREATE_USERS_TABLE_DDL`, `user_repo`, `session_repo` fixture patterns
- [Source: migrations/versions/0002_add_users_table.py] — Migration file format, header convention

## Dev Agent Record

### Agent Model Used

claude-sonnet-4-6

### Debug Log References

**D1 — Starlette 1.0.0 TemplateResponse API change:** The story spec documented the old starlette API `TemplateResponse(name, context)`. Starlette 1.0.0 changed the signature to `TemplateResponse(request, name, context)` — `request` is now the first positional argument. Passing `"login.html"` as `request` and the context dict as `name` caused a `TypeError: cannot use 'tuple' as a dict key (unhashable type: 'dict')` in Jinja2's LRU cache. Fixed by adopting the new API. The `request` key no longer needs to be included manually in the context dict — starlette adds it automatically via `context.setdefault("request", request)`.

**D2 — httpx missing from dev dependencies:** `fastapi.testclient.TestClient` (via starlette) requires `httpx` to be installed. It was not in the project's dev dependencies. Added `"httpx>=0.28.0"` to `[dependency-groups] dev` in `pyproject.toml`.

### Completion Notes List

All 10 tasks complete. 126 tests pass (47 new). 83% branch coverage (up from 78%). Ruff clean. Mypy strict — 0 errors. All 6 ACs satisfied:
- AC1: `GET /login` renders HTML login form; `?next=` preserved in template
- AC2: Valid HTTPS POST → session created with SHA-256 token hash, `Secure`/`HttpOnly`/`SameSite=Strict`/`Path=/` cookie, existing sessions purged, `login_success` event logged, `?next=` validated against open redirect
- AC3: HTTP POST → 302 redirect to HTTPS equivalent, no cookie set
- AC4: Invalid creds → 200 with generic "Invalid username or password", username pre-filled, `login_failed` event logged
- AC5: 5 failures within 5-minute window → rate limited with same generic error, `auth_rate_limited` event logged
- AC6: Migration 0003 creates `sessions` table with all 6 columns, FK constraint, and 2 indexes

Key deviations from story spec:
- Starlette 1.0.0 `TemplateResponse` API: `(request, name, context)` not `(name, context)` — see D1
- `httpx` added as dev dependency — required by `TestClient` — see D2
- Rate limiter implemented as module-level functions (not class) — simpler, matches `readiness.py` pattern, no API difference

### File List

- `pyproject.toml` (modified — added jinja2, python-multipart runtime deps; httpx dev dep)
- `uv.lock` (modified — added jinja2 3.1.6, python-multipart 0.0.27, httpx 0.28.1, certifi, httpcore)
- `src/open_ems/settings.py` (modified — added installer_session_timeout_hours, homeowner_session_timeout_days)
- `src/open_ems/web/app.py` (modified — added auth_router import and include_router)
- `migrations/versions/0003_add_sessions_table.py` (created — sessions table migration with FK + 2 indexes)
- `src/open_ems/storage/repositories/session_repo.py` (created — generate_session_token, hash_token, SessionRepo)
- `src/open_ems/services/rate_limiter.py` (created — sliding window rate limiter, module-level functions)
- `src/open_ems/web/routes/auth.py` (created — GET/POST /login, HTTPS enforcement, session creation)
- `src/open_ems/web/templates/login.html` (created — login form with UX design tokens)
- `tests/conftest.py` (modified — added CREATE_SESSIONS_TABLE_DDL, session_repo fixture, SessionRepo import)
- `tests/unit/test_settings.py` (modified — added 4 tests for new session timeout settings)
- `tests/unit/storage/repositories/test_session_repo.py` (created — 13 tests)
- `tests/unit/services/test_rate_limiter.py` (created — 8 tests)
- `tests/unit/web/test_auth_routes.py` (created — 22 tests)

## Change Log

| Date | Change |
|---|---|
| 2026-05-02 | Implemented Story 2.2: login form, session creation, brute-force protection. Starlette 1.0.0 TemplateResponse API fix applied. httpx added as dev dependency. 126 tests pass, 83% coverage. |
