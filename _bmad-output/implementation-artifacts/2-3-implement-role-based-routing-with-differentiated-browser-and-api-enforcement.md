# Story 2.3: Implement role-based routing with differentiated browser and API enforcement

Status: done

## Story

As an installer,
I want the system to route me to my role's interface after login and return consistent responses for browser and HTMX/API cross-role access attempts,
So that the role boundary is enforced without creating HTMX redirect loops or exposing route structure.

## Acceptance Criteria

**AC1 — Installer login routes to installer dashboard**
**Given** a user logs in successfully as installer/admin
**When** the session is established
**Then** they are routed to `/installer/dashboard`

**AC2 — Homeowner login routes to homeowner dashboard**
**Given** a user logs in successfully as homeowner/user
**When** the session is established
**Then** they are routed to `/homeowner/dashboard`

**AC3 — Browser cross-role access: wrong session role redirects silently**
**Given** a browser request (identified by `Accept: text/html` and absence of `HX-Request` header) reaches a route protected by `Depends(require_installer)` with a valid homeowner session
**When** the server processes the request
**Then** the server redirects to the homeowner's role home view (`/homeowner/dashboard`) — no error page revealing route structure

**AC4 — HTMX/API cross-role access: returns 403**
**Given** an HTMX or API request (identified by `HX-Request` header or `Accept: application/json`) reaches a route protected by `Depends(require_installer)` with a valid homeowner session
**When** the server processes the request
**Then** the server returns HTTP 403 with no redirect
**And** a structured event is written: `event="role_access_denied"`, `component="auth"`, including role, user ID, and attempted route

**AC5 — Symmetric enforcement: installer accessing homeowner routes**
**Given** the same role-mismatch scenario for homeowner routes accessed by an installer session
**When** the server processes the request
**Then** the same browser/HTMX differentiated response behavior applies

**AC6 — No valid session: redirect to login**
**Given** any protected route is accessed with no valid session (missing cookie, or token hash not found in sessions table)
**When** the server processes the request
**Then** browser requests receive a 302 redirect to `/login?next=[originally requested URL]`
**And** HTMX/API requests receive HTTP 401 with no redirect

**AC7 — Dependency injection is sole enforcement mechanism**
**And** `Depends(require_installer)` and `Depends(require_homeowner)` are the sole role enforcement mechanism; no inline role checks inside route handlers

## Tasks / Subtasks

- [x] **Task 0: Pre-story gate check** (AC: all)
  - [x] Run `python -m pytest tests/` — confirm 126 tests still pass, coverage ≥ 75%
  - [x] Run `python -m ruff check .` — exit 0
  - [x] Run `python -m mypy src/` — 0 errors

- [x] **Task 1: Add `get_by_id()` to UserRepo** (AC: 3, 4, 5, 6)
  - [x] Add `async def get_by_id(self, user_id: str) -> aiosqlite.Row | None` to `UserRepo` in `src/open_ems/storage/repositories/user_repo.py`
  - [x] SELECT same columns as `get_by_username`: `id, username, role, hashed_password, must_change_password, created_at`
  - [x] Follow the `async with self._conn.execute(...) as cursor:` pattern (never bare cursor — learned from 2.1 review)
  - [x] Add unit test `test_get_by_id_returns_row` and `test_get_by_id_unknown_returns_none` to `tests/unit/storage/repositories/test_user_repo.py`

- [x] **Task 2: Create `src/open_ems/web/dependencies.py`** (AC: 3, 4, 5, 6, 7)
  - [x] Define `@dataclass class AuthenticatedUser` with fields: `user_id: str`, `username: str`, `role: str`, `session_id: str`
  - [x] Define type aliases: `InstallerUser = AuthenticatedUser`, `HomeownerUser = AuthenticatedUser`
  - [x] Define `_ROLE_HOME: dict[str, str]` matching auth.py (`"installer": "/installer/dashboard"`, `"homeowner": "/homeowner/dashboard"`)
  - [x] Define `_COOKIE_NAME = "session"` (same value as auth.py — NOT imported from there, both files own this constant independently)
  - [x] Implement `_is_htmx_or_api(request: Request) -> bool`: returns True if `HX-Request` header present OR `"application/json"` in `Accept` header
  - [x] Implement `async def _resolve_session(request: Request) -> AuthenticatedUser | None`: reads cookie, computes SHA-256 hash via `hash_token()`, calls `SessionRepo().get_by_token_hash()`, calls `UserRepo().get_by_id()` — returns None if any step fails
  - [x] Implement `async def require_installer(request: Request) -> InstallerUser`: calls `_resolve_session`; if None: browser→302 to `/login?next=`, HTMX/API→401; if role != "installer": browser→302 to `_ROLE_HOME[role]`, HTMX/API→403 + log `role_access_denied`; return `InstallerUser`
  - [x] Implement `async def require_homeowner(request: Request) -> HomeownerUser`: symmetric to `require_installer`
  - [x] Raise responses using `raise HTTPException(status_code=...)` or return `RedirectResponse` — use `raise` for HTTP exceptions so FastAPI handles them cleanly
  - [x] **CRITICAL**: For redirect responses from a dependency, use `raise` with a custom exception or return the response directly — see Dev Notes for the correct FastAPI pattern

- [x] **Task 3: Create stub installer and homeowner routes** (AC: 1, 2, 3, 4, 5)
  - [x] Create `src/open_ems/web/routes/installer.py`:
    - `router = APIRouter(prefix="/installer", tags=["installer"])`
    - `GET /installer/dashboard` with `_user: InstallerUser = Depends(require_installer)` — returns minimal HTML placeholder (no template needed yet; plain `HTMLResponse` is fine)
  - [x] Create `src/open_ems/web/routes/homeowner.py`:
    - `router = APIRouter(prefix="/homeowner", tags=["homeowner"])`
    - `GET /homeowner/dashboard` with `_user: HomeownerUser = Depends(require_homeowner)` — returns minimal HTML placeholder
  - [x] Both stub routes must use the dependency (enforces Story 2.3 requirement); Epics 10 and 11 will replace the body with real templates

- [x] **Task 4: Wire new routers into `app.py`** (AC: 1, 2)
  - [x] Import and include `installer_router` and `homeowner_router` in `create_app()` in `src/open_ems/web/app.py`
  - [x] Routers must be included AFTER `auth_router` (auth routes are foundational)

- [x] **Task 5: Tests for `dependencies.py`** (AC: 3, 4, 5, 6, 7)
  - [x] Create `tests/unit/web/test_dependencies.py`
  - [x] Test fixture: minimal FastAPI app with one installer-protected route and one homeowner-protected route (using `Depends(require_installer)` / `Depends(require_homeowner)`)
  - [x] Use `session_repo` fixture from `conftest.py` to initialise DB with users + sessions tables
  - [x] Helper function to create a test user and a valid session cookie in the DB
  - [x] **Tests to cover:**
    - `test_require_installer_no_cookie_browser_redirects_to_login` — browser GET with no cookie → 302 to /login?next=
    - `test_require_installer_no_cookie_htmx_returns_401` — HTMX GET with `HX-Request` header, no cookie → 401
    - `test_require_installer_invalid_token_browser_redirects_to_login` — browser GET with bad session cookie → 302 to /login
    - `test_require_installer_valid_installer_session_grants_access` — valid installer session on installer route → 200
    - `test_require_installer_homeowner_session_browser_redirects_to_homeowner_home` — homeowner session on installer route, browser → 302 to /homeowner/dashboard
    - `test_require_installer_homeowner_session_htmx_returns_403` — homeowner session on installer route, `HX-Request` header → 403
    - `test_require_installer_role_denied_event_logged` — role mismatch with HTMX → `role_access_denied` event in structlog
    - `test_require_homeowner_valid_homeowner_session_grants_access` — valid homeowner session on homeowner route → 200
    - `test_require_homeowner_installer_session_browser_redirects_to_installer_home` — installer session on homeowner route, browser → 302 to /installer/dashboard
    - `test_require_homeowner_installer_session_htmx_returns_403` — installer session on homeowner route, `HX-Request` → 403

- [x] **Task 6: Final validation** (AC: all)
  - [x] `python -m ruff check .` — exit 0
  - [x] `python -m ruff format --check .` — exit 0
  - [x] `python -m mypy src/` — 0 errors
  - [x] `python -m pytest tests/` — all pass, coverage ≥ 75%

## Dev Notes

### Architecture Mandate

**`Depends(require_installer)` / `Depends(require_homeowner)` are the SOLE role enforcement mechanism** — no inline `if user.role != "installer"` checks inside route handlers. This is a hard architecture rule [Source: architecture.md#Role Enforcement Pattern].

```python
# CORRECT
@router.post("/actions/charge-now")
async def charge_now(
    _user: HomeownerUser = Depends(require_homeowner),
    ...
): ...

# WRONG — never do this
async def charge_now(request, user):
    if user.role != "homeowner":  # must NOT be here
        raise HTTPException(403)
```

### Task 1 — `get_by_id()` exact implementation

**File:** `src/open_ems/storage/repositories/user_repo.py`

Add after `get_by_username()`:

```python
async def get_by_id(self, user_id: str) -> aiosqlite.Row | None:
    """Fetch user row by user ID. Returns None if not found."""
    async with self._conn.execute(
        "SELECT id, username, role, hashed_password, must_change_password, created_at"
        " FROM users WHERE id = ?",
        (user_id,),
    ) as cursor:
        return await cursor.fetchone()
```

Pattern: `async with self._conn.execute(...) as cursor:` — this is the established pattern in `user_repo.py` and `session_repo.py`. Never use bare cursor. Learned from Story 2.1 review.

### Task 2 — `dependencies.py` exact implementation

**File:** `src/open_ems/web/dependencies.py`

Key design decisions:

1. **`_is_htmx_or_api()`** — check `HX-Request` header (lowercase-safe via FastAPI headers) OR `application/json` in `Accept`. The ACs explicitly say "identified by `HX-Request` header or `Accept: application/json`".

2. **Redirect from a dependency** — FastAPI dependencies cannot `return` a `RedirectResponse` and have it used as the route response. The correct pattern is to `raise` an `HTTPException` with status 302 and a `Location` header, or to use a custom exception handler. The cleanest approach for redirect in a dependency:

```python
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

# Inside dependency — redirect must be raised, not returned:
raise HTTPException(
    status_code=302,
    headers={"Location": f"/login?next={request.url.path}"},
)
```

However, `HTTPException` with 302 may not set a proper redirect body. **The recommended FastAPI pattern** is to store the response on `request.state` and raise a custom exception. A simpler and correct alternative that works in practice:

```python
from starlette.responses import RedirectResponse

# Raise with status 307 or just use HTTP 401/403 exceptions
# For redirects from dependencies, the cleanest approach:
from fastapi import Request
from fastapi.exceptions import HTTPException

# Browser redirect: use a custom exception subclass resolved by an exception handler
# Or: raise HTTPException(302, headers={"Location": url})
```

**Practical working pattern** — use `HTTPException` with redirect headers:
```python
# No-session, browser:
raise HTTPException(
    status_code=302,
    headers={"Location": f"/login?next={request.url.path}"},
)
# No-session, HTMX/API:
raise HTTPException(status_code=401, detail="Authentication required")
# Wrong role, browser:
raise HTTPException(
    status_code=302,
    headers={"Location": _ROLE_HOME.get(user.role, "/")},
)
# Wrong role, HTMX/API:
raise HTTPException(status_code=403, detail="Forbidden")
```

3. **`_resolve_session()`** — does NOT check `expires_at`. That check is Story 2.5 scope (explicitly deferred in Story 2.2 review: "get_by_token_hash does not filter by expires_at — deferred to Story 2.5").

4. **`AuthenticatedUser` is a dataclass** — use `from dataclasses import dataclass` + `@dataclass`. Do NOT use TypedDict (no attribute access typing benefit for mypy in this context with strict mode).

Full skeleton:

```python
from __future__ import annotations

import structlog
from dataclasses import dataclass
from fastapi import Request
from fastapi.exceptions import HTTPException

from open_ems.storage.repositories.session_repo import SessionRepo, hash_token
from open_ems.storage.repositories.user_repo import UserRepo

logger = structlog.get_logger(__name__)

_COOKIE_NAME = "session"
_ROLE_HOME: dict[str, str] = {
    "installer": "/installer/dashboard",
    "homeowner": "/homeowner/dashboard",
}

@dataclass
class AuthenticatedUser:
    user_id: str
    username: str
    role: str
    session_id: str

InstallerUser = AuthenticatedUser
HomeownerUser = AuthenticatedUser


def _is_htmx_or_api(request: Request) -> bool:
    return bool(request.headers.get("hx-request")) or (
        "application/json" in request.headers.get("accept", "")
    )


async def _resolve_session(request: Request) -> AuthenticatedUser | None:
    raw_token = request.cookies.get(_COOKIE_NAME)
    if not raw_token:
        return None
    session_row = await SessionRepo().get_by_token_hash(hash_token(raw_token))
    if session_row is None:
        return None
    user_row = await UserRepo().get_by_id(session_row["user_id"])
    if user_row is None:
        return None
    return AuthenticatedUser(
        user_id=str(user_row["id"]),
        username=str(user_row["username"]),
        role=str(user_row["role"]),
        session_id=str(session_row["id"]),
    )


async def require_installer(request: Request) -> InstallerUser:
    user = await _resolve_session(request)
    if user is None:
        if _is_htmx_or_api(request):
            raise HTTPException(status_code=401, detail="Authentication required")
        raise HTTPException(
            status_code=302,
            headers={"Location": f"/login?next={request.url.path}"},
        )
    if user.role != "installer":
        if _is_htmx_or_api(request):
            logger.warning(
                "role_access_denied",
                component="auth",
                role=user.role,
                user_id=user.user_id,
                attempted_route=request.url.path,
            )
            raise HTTPException(status_code=403, detail="Forbidden")
        raise HTTPException(
            status_code=302,
            headers={"Location": _ROLE_HOME.get(user.role, "/")},
        )
    return user


async def require_homeowner(request: Request) -> HomeownerUser:
    user = await _resolve_session(request)
    if user is None:
        if _is_htmx_or_api(request):
            raise HTTPException(status_code=401, detail="Authentication required")
        raise HTTPException(
            status_code=302,
            headers={"Location": f"/login?next={request.url.path}"},
        )
    if user.role != "homeowner":
        if _is_htmx_or_api(request):
            logger.warning(
                "role_access_denied",
                component="auth",
                role=user.role,
                user_id=user.user_id,
                attempted_route=request.url.path,
            )
            raise HTTPException(status_code=403, detail="Forbidden")
        raise HTTPException(
            status_code=302,
            headers={"Location": _ROLE_HOME.get(user.role, "/")},
        )
    return user
```

**mypy strictness note**: `session_row["user_id"]` returns `Any` from `aiosqlite.Row`. Wrap with `str(...)` when assigning to `AuthenticatedUser` fields to satisfy strict mode. Same pattern as auth.py: `user_id: str = user_row["id"]` — see `auth.py:124-125`.

### Task 3 — Stub routes: exact shape

**`src/open_ems/web/routes/installer.py`:**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from open_ems.web.dependencies import InstallerUser, require_installer

router = APIRouter()


@router.get("/installer/dashboard", response_class=HTMLResponse)
async def installer_dashboard(
    _user: InstallerUser = Depends(require_installer),
) -> HTMLResponse:
    # Placeholder — replaced in Epic 10/11
    return HTMLResponse("<h1>Installer Dashboard</h1>")
```

**`src/open_ems/web/routes/homeowner.py`:**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from open_ems.web.dependencies import HomeownerUser, require_homeowner

router = APIRouter()


@router.get("/homeowner/dashboard", response_class=HTMLResponse)
async def homeowner_dashboard(
    _user: HomeownerUser = Depends(require_homeowner),
) -> HTMLResponse:
    # Placeholder — replaced in Epic 10/11
    return HTMLResponse("<h1>Homeowner Dashboard</h1>")
```

**No prefix on APIRouter** — routes include the full path (`/installer/dashboard`) to match `_ROLE_HOME` dict exactly.

### Task 4 — `app.py` changes

Add imports and router inclusion in `create_app()`:

```python
from open_ems.web.routes.installer import router as installer_router
from open_ems.web.routes.homeowner import router as homeowner_router

def create_app() -> FastAPI:
    app = FastAPI(title="OPEN-EMS", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(installer_router)  # ADD
    app.include_router(homeowner_router)  # ADD
    return app
```

### Task 5 — Test fixture pattern

The `session_repo` fixture from `conftest.py` creates both `users` and `sessions` tables. Use it as the DB fixture.

Test app fixture pattern (following `test_auth_routes.py` style):

```python
@pytest.fixture
def protected_app(session_repo: SessionRepo) -> FastAPI:
    app = FastAPI()
    
    @app.get("/installer/dashboard")
    async def installer_route(_user: InstallerUser = Depends(require_installer)) -> dict[str, str]:
        return {"role": _user.role}
    
    @app.get("/homeowner/dashboard")
    async def homeowner_route(_user: HomeownerUser = Depends(require_homeowner)) -> dict[str, str]:
        return {"role": _user.role}
    
    return app

@pytest.fixture
def client(protected_app: FastAPI) -> TestClient:
    return TestClient(protected_app, base_url="https://test", follow_redirects=False)
```

Helper to create a user + session in DB:

```python
async def _create_session(role: str) -> str:
    """Create user + session, return raw token for cookie."""
    user_repo = UserRepo(get_connection())
    user_id = await user_repo.create(
        username=f"{role}_user",
        hashed_password=hash_password("secret"),
        role=role,
    )
    raw_token = generate_session_token()
    expires_at = datetime.now(UTC) + timedelta(hours=4)
    repo = SessionRepo(get_connection())
    await repo.create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
    )
    return raw_token
```

Use `structlog.testing.capture_logs()` for event-logging tests (pattern from `test_auth_routes.py:222`).

**Browser request** — no special headers needed (default TestClient has no `HX-Request`)
**HTMX request** — add `headers={"HX-Request": "true"}`

### Deferred scope (DO NOT implement in this story)

Per Story 2.2 review findings (explicitly deferred):
- `expires_at` check in `get_by_token_hash` → Story 2.5 scope
- `update_last_active()` call on authenticated requests → Story 2.5 scope
- Expired session lazy cleanup → Story 2.5 scope

### No new migration needed

This story adds no new database tables or columns. All required tables (`users`, `sessions`) already exist from migrations 0002 and 0003.

### Project Structure Notes

- **`web/dependencies.py`** is the architecture-mandated location for `require_installer` / `require_homeowner` [Source: architecture.md#Project Structure, line `dependencies.py # require_installer, require_homeowner`]
- **`web/routes/installer.py`** and **`web/routes/homeowner.py`** are architecture-mandated files [Source: architecture.md#Project Structure]
- **`_COOKIE_NAME = "session"`** is intentionally duplicated (not imported from `auth.py`) — both modules own their own constants to avoid circular import risk and coupling between authentication routes and dependency module
- `_ROLE_HOME` dict: same values as in `auth.py` — this is intentional duplication for the same reason; a future refactor could extract to a shared `web/constants.py` but that is NOT in scope for this story
- Template directories `templates/installer/` and `templates/homeowner/` are NOT created in this story — stubs use inline HTML; templates are created in Epics 10 and 11

### Testing framework notes

- `asyncio_mode = "auto"` in `pyproject.toml` — all `async def` test functions work without `@pytest.mark.asyncio`
- `asyncio_default_fixture_loop_scope = "function"` — each test gets its own event loop; no shared state
- The `_set_secret_key` autouse fixture in `conftest.py` sets `SECRET_KEY` env var for all tests
- `follow_redirects=False` on TestClient is required to verify 302 redirects (not follow them)
- Coverage threshold is 75% (`--cov-fail-under=75`)

### References

- Role enforcement pattern: [Source: architecture.md#Role Enforcement Pattern]
- Dependencies file location: [Source: architecture.md#Project Structure, `web/dependencies.py`]
- `_COOKIE_NAME` and `_ROLE_HOME` values: [Source: `src/open_ems/web/routes/auth.py`]
- `hash_token()`, `generate_session_token()`: [Source: `src/open_ems/storage/repositories/session_repo.py`]
- `SessionRepo.get_by_token_hash()`: no `expires_at` filter — Story 2.5 scope [Source: Story 2.2 review findings]
- `aiosqlite.Row` cursor pattern: [Source: `src/open_ems/storage/repositories/user_repo.py#get_by_username`]
- Test fixture patterns: [Source: `tests/unit/web/test_auth_routes.py`, `tests/conftest.py`]
- Browser vs HTMX detection: [Source: epics.md#Story 2.3 AC3 and AC4]

## Dev Agent Record

### Agent Model Used

claude-sonnet-4.6

### Debug Log References

### Completion Notes List

- Added `get_by_id()` to `UserRepo` using `async with self._conn.execute(...) as cursor:` pattern (established from 2.1 review)
- Created `src/open_ems/web/dependencies.py` with `AuthenticatedUser` dataclass, `_is_htmx_or_api()`, `_resolve_session()`, `require_installer()`, and `require_homeowner()`
- Browser/HTMX detection: checks `HX-Request` header OR `application/json` in `Accept` header
- Redirect from dependency implemented via `raise HTTPException(status_code=302, headers={"Location": ...})` — FastAPI's exception handler turns this into a proper redirect response
- `_COOKIE_NAME` and `_ROLE_HOME` are intentionally duplicated from `auth.py` to avoid circular import
- Created stub routes `installer.py` and `homeowner.py` — both enforce dependency injection as sole role check (no inline role checks)
- Wired `installer_router` and `homeowner_router` into `create_app()` in `app.py` after `auth_router`
- Added 11 tests in `tests/unit/web/test_dependencies.py` covering all AC scenarios including structlog event capture for `role_access_denied`
- Added 2 tests in `tests/unit/storage/repositories/test_user_repo.py` for `get_by_id()` (found + not found)
- `expires_at` check NOT implemented — explicitly deferred to Story 2.5 (per 2.2 review findings)
- `update_last_active()` NOT called — also deferred to Story 2.5

### File List

- `src/open_ems/storage/repositories/user_repo.py` (modified — added `get_by_id()`)
- `src/open_ems/web/dependencies.py` (created)
- `src/open_ems/web/routes/installer.py` (created)
- `src/open_ems/web/routes/homeowner.py` (created)
- `src/open_ems/web/app.py` (modified — wired installer_router and homeowner_router)
- `tests/unit/storage/repositories/test_user_repo.py` (modified — added `test_get_by_id_returns_row`, `test_get_by_id_unknown_returns_none`)
- `tests/unit/web/test_dependencies.py` (created)

### Review Findings

- [x] [Review][Patch] `next` redirect loses query string and uses un-encoded path [`dependencies.py:63,89`] — Fixed: extracted `_next_url(request)` helper using `urllib.parse.quote` that preserves query string and encodes special characters; both `require_installer` and `require_homeowner` updated
- [x] [Review][Defer] Expired sessions authenticate indefinitely [`dependencies.py:42-53`] — deferred, pre-existing (Story 2.5 scope per Dev Notes)
- [x] [Review][Defer] No exception handling in `_resolve_session` for DB failures [`dependencies.py:42-53`] — deferred, pre-existing (legitimate 500 on genuine DB failure; `get_connection()` unreachable in normal app flow)
- [x] [Review][Defer] `_is_htmx_or_api` treats all non-HTMX as browser — minor deviation from AC3 strict wording [`dependencies.py:32-35`] — deferred, pre-existing (Dev Notes explicitly specify this logic)
- [x] [Review][Defer] `InstallerUser`/`HomeownerUser` type aliases provide no compile-time role enforcement [`dependencies.py:28-29`] — deferred, pre-existing (spec-mandated design; `NewType` improvement out of scope)

### Change Log

- Added `UserRepo.get_by_id()` method for session-to-user resolution (Date: 2026-05-02)
- Created `web/dependencies.py` with role enforcement dependencies: `require_installer`, `require_homeowner` (Date: 2026-05-02)
- Created stub routes `/installer/dashboard` and `/homeowner/dashboard` (Date: 2026-05-02)
- Wired new routers into `app.py` (Date: 2026-05-02)
- Added 11 new tests for dependency role enforcement (Date: 2026-05-02)
