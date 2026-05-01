# Story 1.3: Implement health endpoints and startup sequence with NTP check

Status: done

## Story

As an installer,
I want the system to report its own readiness and detect clock issues on startup,
So that deployment tooling can confirm the system is operational and time-sensitive features degrade safely when the clock is unreliable.

## Acceptance Criteria

1. **Given** the FastAPI process has started at any point  
   **When** `GET /health/live` is requested  
   **Then** it returns HTTP 200 `{"status": "alive"}` — even before migrations complete

2. **Given** all migrations and initialization have completed successfully  
   **When** `GET /health/ready` is requested  
   **Then** it returns HTTP 200 `{"status": "ready"}`  
   **And** on systemd native deployment, `sd_notify("READY=1")` has been sent before this endpoint returns 200

3. **Given** migrations or initialization are still in progress  
   **When** `GET /health/ready` is requested  
   **Then** it returns HTTP 503

4. **Given** the startup NTP check runs  
   **When** it executes  
   **Then** it attempts to verify the system clock using the system clock plus an optional NTP query against a configurable NTP server  
   **And** if NTP is unreachable, the system falls back to the system clock only — NTP unavailability is not fatal and does not block startup  
   **And** the result is stored as `system_clock_status` with one of three values: `valid` (clock verified against NTP), `suspect` (drift exceeds configured threshold), or `unknown` (NTP unreachable or not configured, system clock assumed)  
   **And** this flag is stored in internal startup state for later exposure via StateStore (Epic 5)  
   **And** a structured log entry is written at `warn` level with `event="clock_suspect"` or `event="clock_unknown"` when the result is not `valid`  
   **And** startup is never blocked by the NTP check result

## Tasks / Subtasks

- [x] Task 1: Extend Settings with NTP configuration fields (AC: 4)
  - [x] Add `ntp_host: str | None = None` to `Settings` in `src/open_ems/settings.py`
  - [x] Add `ntp_drift_threshold_seconds: float = 2.0` to `Settings`
  - [x] Confirm `python -m uv run mypy src/` still exits 0

- [x] Task 2: Create services package and ReadinessService (AC: 2, 3)
  - [x] Create `src/open_ems/services/__init__.py` (empty)
  - [x] Create `src/open_ems/services/readiness.py` with module-level state:
    - [x] `_ready: bool = False` module global
    - [x] `is_ready() -> bool` — returns `_ready`
    - [x] `mark_ready() -> None` — calls `_sd_notify("READY=1")` then sets `_ready = True`
    - [x] `reset() -> None` — sets `_ready = False` (test isolation only)
    - [x] Private `_sd_notify(message: str) -> None` — writes to NOTIFY_SOCKET unix socket if env var is set; silently skips if absent or on socket error
  - [x] Confirm `python -m uv run mypy src/` still exits 0

- [x] Task 3: Create time_sync service (AC: 4)
  - [x] Create `src/open_ems/services/time_sync.py`:
    - [x] `ClockStatus = Literal["valid", "suspect", "unknown"]`
    - [x] `_clock_status: ClockStatus = "unknown"` module global
    - [x] `get_clock_status() -> ClockStatus`
    - [x] `check_clock(ntp_host: str | None, drift_threshold_seconds: float) -> ClockStatus` — stores and returns result
    - [x] Private `_query_ntp(host: str, timeout: float) -> float` — raw UDP socket NTP query (port 123); returns Unix timestamp; raises `OSError` on failure
    - [x] If `ntp_host` is None → return `"unknown"` immediately
    - [x] If NTP unreachable (any exception) → return `"unknown"`
    - [x] If `abs(ntp_time - system_time) > drift_threshold_seconds` → return `"suspect"`
    - [x] Otherwise → return `"valid"`
  - [x] Confirm `python -m uv run mypy src/` still exits 0

- [x] Task 4: Create health routes (AC: 1, 2, 3)
  - [x] Create `src/open_ems/web/routes/__init__.py` (empty)
  - [x] Create `src/open_ems/web/routes/health.py`:
    - [x] `router = APIRouter()`
    - [x] `GET /health/live` → returns `{"status": "alive"}` with 200 (plain dict — no readiness check)
    - [x] `GET /health/ready` → returns `JSONResponse({"status": "ready"}, 200)` if `is_ready()` else `JSONResponse({"status": "starting"}, 503)`
  - [x] Confirm `python -m uv run mypy src/` still exits 0

- [x] Task 5: Wire lifespan and app factory (AC: 1, 2, 3, 4)
  - [x] Modify `src/open_ems/web/app.py`:
    - [x] Include health router in `create_app()` via `app.include_router(health_router)`
    - [x] Add NTP check step in lifespan (Step 3) between migrations and database init:
      ```
      clock_status = check_clock(settings.ntp_host, settings.ntp_drift_threshold_seconds)
      if clock_status != "valid":
          logger.warning("clock_" + clock_status, system_clock_status=clock_status, component="startup")
      ```
    - [x] After `init_database()`: call `mark_ready()` (which sends sd_notify internally)
    - [x] Log `system_ready` at info level after `mark_ready()`
  - [x] Confirm `python -m uv run mypy src/` still exits 0

- [x] Task 6: Write tests (AC: all)
  - [x] Create `tests/unit/services/__init__.py` (empty)
  - [x] Create `tests/unit/services/test_readiness.py`:
    - [x] `test_initial_not_ready` — `is_ready()` returns False before `mark_ready()`
    - [x] `test_mark_ready` — after `mark_ready()`, `is_ready()` returns True
    - [x] `test_reset` — after `reset()`, `is_ready()` returns False again
    - [x] `test_sd_notify_skipped_when_no_socket` — `mark_ready()` does not raise when NOTIFY_SOCKET not set
  - [x] Create `tests/unit/services/test_time_sync.py`:
    - [x] `test_unknown_when_no_ntp_host` — `check_clock(None, 2.0)` returns `"unknown"`
    - [x] `test_unknown_when_ntp_unreachable` — mock `_query_ntp` to raise `OSError`; expect `"unknown"`
    - [x] `test_valid_when_no_drift` — mock `_query_ntp` to return `time.time()`; expect `"valid"`
    - [x] `test_suspect_when_high_drift` — mock `_query_ntp` to return `time.time() + 60`; expect `"suspect"`
    - [x] `test_clock_status_stored` — `get_clock_status()` returns the last result from `check_clock()`
  - [x] Create `tests/unit/web/__init__.py` (empty)
  - [x] Create `tests/unit/web/test_health_routes.py`:
    - [x] `test_liveness_always_alive` — direct call to `liveness()` returns `{"status": "alive"}`
    - [x] `test_readiness_503_before_ready` — direct call to `readiness()` returns JSONResponse with status 503
    - [x] `test_readiness_200_when_ready` — after `mark_ready()`, call returns status 200
  - [x] Add `clean_readiness_state` fixture to `tests/conftest.py`
  - [x] Run `python -m uv run pytest` — all tests must pass

- [x] Task 7: Final validation
  - [x] Run `python -m uv run ruff check .` — must exit 0
  - [x] Run `python -m uv run mypy src/` — must exit 0
  - [x] Run `python -m uv run pytest` — all tests must pass
  - [x] Confirm `uv.lock` has not changed (no new dependencies)

### Review Findings (AI)

- [x] [Review][Defer] `/health/live` unreachable during uvicorn lifespan — uvicorn does not serve any HTTP request until the lifespan `yield` is reached; Alembic migration + NTP check block serving, so `/health/live` is inaccessible while startup runs (violates AC1: "even before migrations complete") [src/open_ems/web/app.py]
- [x] [Review][Patch][High] `_sd_notify` non-OSError can escape, permanently preventing `_ready = True` — if `_sd_notify` raises any non-OSError (e.g. future code path exception), `mark_ready()` leaves `_ready = False` forever; fix: broaden `except OSError` to `except Exception` inside `_sd_notify` [src/open_ems/services/readiness.py:30]
- [x] [Review][Patch][High] NTP check blocks asyncio event loop — `check_clock()` calls blocking `_query_ntp` (3 s timeout) directly on the async event loop; wrap the call in `asyncio.to_thread()` like the Alembic migration [src/open_ems/web/app.py:50]
- [x] [Review][Patch][High] NTP response not length-validated before `struct.unpack` — `msg[40:44]` assumes `len(msg) >= 48`; a short/spoofed datagram raises `struct.error` (caught by broad except) or unpacks garbage; add `if len(msg) < 48: return "unknown"` guard before unpacking [src/open_ems/services/time_sync.py:40]
- [x] [Review][Patch][Med] `_sd_notify` uses `connect` + `sendall` on `SOCK_DGRAM` — portable sd_notify convention is `sendto(msg, path)` without `connect`; `sendall` on a connected datagram socket is non-standard on some platforms [src/open_ems/services/readiness.py:31]
- [x] [Review][Patch][Low] Fixture return type `-> None` with `yield` — `clean_readiness` fixtures annotated `-> None` but contain a `yield`; correct to `Generator[None, None, None]` and remove `# type: ignore[misc]` [tests/unit/services/test_readiness.py:7, tests/unit/web/test_health_routes.py:8]
- [x] [Review][Patch][Low] `ntp_drift_threshold_seconds` accepts zero and negative values — zero makes every NTP check return `"suspect"`; negative makes it always return `"valid"`; add `Field(gt=0.0)` validator [src/open_ems/settings.py:18]
- [x] [Review][Patch][Low] NTP mode byte not validated — any 48+ byte UDP reply to port 123 accepted as a valid NTP response; check `msg[0] & 0x07 == 4` (server mode) before trusting the timestamp [src/open_ems/services/time_sync.py:38]
- [x] [Review][Defer] `_ready` not reset in lifespan teardown — module global persists across ASGI warm-reload cycles; not actionable without StateStore integration (Epic 5) [src/open_ems/services/readiness.py]
- [x] [Review][Defer] `get_settings()` singleton race — check-then-set has TOCTOU under threads; GIL protects in CPython; pre-existing from Story 1.2 [src/open_ems/settings.py:24]
- [x] [Review][Defer] `SystemExit(1)` in lifespan behavior is version-dependent — Starlette version may or may not propagate cleanly; pre-existing from Story 1.2, currently tested [src/open_ems/web/app.py:46]
- [x] [Review][Defer] `db_path` relative default problematic under systemd/Docker — Story 1.7 (central config) is the correct place to address deployment paths [src/open_ems/settings.py:15]
- [x] [Review][Defer] `_PROJECT_ROOT` parents[3] wrong under non-standard install — pre-existing from Story 1.2; address in deployment stories (1.4/1.5) [src/open_ems/web/app.py:20]
- [x] [Review][Defer] IPv4-only NTP (`AF_INET`) — no IPv6 fallback; low risk for home LAN deployment; revisit when IPv6 support is needed [src/open_ems/services/time_sync.py:35]
- [x] [Review][Defer] `configure_logging` multiple-call inconsistency — structlog caches processor chain on first use; pre-existing from Story 1.2 [src/open_ems/logging_config.py]
- [x] [Review][Defer] `alembic.ini` missing gives misleading `migration_failed` log — pre-existing from Story 1.2; address with pre-flight check in a later hardening story [src/open_ems/web/app.py:28]
- [x] [Review][Defer] `from None` discards migration exception context — pre-existing from Story 1.2; exception is logged before raise so diagnostic info is not lost [src/open_ems/web/app.py:46]

## Dev Notes

### Critical Context — Read First

**Scope boundary — Story 1.3 only creates:**
- NTP-related Settings fields (`ntp_host`, `ntp_drift_threshold_seconds`)
- `services/readiness.py` — module-level readiness flag + sd_notify for READY=1
- `services/time_sync.py` — clock check + raw socket NTP client
- `web/routes/health.py` — `/health/live` and `/health/ready` endpoints
- Lifespan update: NTP check step, readiness marking

**Do NOT create in this story:**
- `services/watchdog.py` — Story 1.5
- `services/audit_log.py` — Epic 6
- StateStore — Epic 5 (Story 1.3 stores `_clock_status` as a module-level variable; it's wired to StateStore in Epic 5)
- Any HTML templates or UI routes — Epic 2+
- Docker/systemd deployment files — Stories 1.4/1.5
- Full Settings schema — Story 1.7

---

### Startup Sequence After This Story

The updated lifespan in `web/app.py` must follow this exact order:
```
1. configure_logging(settings.log_level)
2. run Alembic migrations via asyncio.to_thread — fatal on failure
3. check_clock(settings.ntp_host, settings.ntp_drift_threshold_seconds) — log if not valid
4. await init_database(settings.db_path)
5. mark_ready()          ← sends sd_notify("READY=1") internally, then sets _ready = True
6. log startup_complete
yield   ← routes serve traffic; /health/ready now returns 200
shutdown: await close_database()
```

This aligns with the architecture startup sequence (steps 1–3 in architecture, deferred steps 4–9 to later epics).

---

### Liveness vs Readiness — Key Distinction

`/health/live` **must** return 200 before the lifespan runs. FastAPI registers routes at app-creation time (`create_app()`), so `include_router(health_router)` makes both endpoints available immediately. The liveness handler is a plain dict return (FastAPI converts to JSON) and checks nothing:

```python
@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}
```

`/health/ready` checks the module-level `_ready` flag from `services/readiness.py`. It returns 503 until `mark_ready()` is called in the lifespan:

```python
@router.get("/health/ready")
async def readiness() -> JSONResponse:
    if is_ready():
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "starting"}, status_code=503)
```

---

### ReadinessService Template

```python
# src/open_ems/services/readiness.py
from __future__ import annotations

import os
import socket

_ready: bool = False


def is_ready() -> bool:
    return _ready


def mark_ready() -> None:
    global _ready
    _sd_notify("READY=1")
    _ready = True


def reset() -> None:
    global _ready
    _ready = False


def _sd_notify(message: str) -> None:
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return
    try:
        abstract = notify_socket.startswith("@")
        addr = ("\0" + notify_socket[1:]) if abstract else notify_socket
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
    except OSError:
        pass
```

**Why silent OSError?** A failed sd_notify on a systemd deployment is non-fatal — the service will simply not transition to "active" via sd_notify and will rely on the timeout, but startup should not crash.

---

### TimeSync Template

```python
# src/open_ems/services/time_sync.py
from __future__ import annotations

import socket
import struct
import time
from typing import Literal

ClockStatus = Literal["valid", "suspect", "unknown"]

_clock_status: ClockStatus = "unknown"


def get_clock_status() -> ClockStatus:
    return _clock_status


def check_clock(ntp_host: str | None, drift_threshold_seconds: float) -> ClockStatus:
    global _clock_status
    if ntp_host is None:
        _clock_status = "unknown"
        return _clock_status
    try:
        ntp_time = _query_ntp(ntp_host)
        drift = abs(ntp_time - time.time())
        _clock_status = "suspect" if drift > drift_threshold_seconds else "valid"
    except Exception:  # noqa: BLE001
        _clock_status = "unknown"
    return _clock_status


def _query_ntp(host: str, timeout: float = 3.0) -> float:
    """Query NTP server over UDP port 123; returns Unix timestamp."""
    data = b"\x1b" + 47 * b"\x00"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(data, (host, 123))
        msg, _ = sock.recvfrom(1024)
    # NTP timestamp starts at 1900; unpack transmit timestamp (bytes 40-47)
    ntp_epoch_offset = 2208988800
    seconds = struct.unpack("!I", msg[40:44])[0]
    fraction = struct.unpack("!I", msg[44:48])[0]
    return seconds + fraction / 2**32 - ntp_epoch_offset
```

**Why raw socket?** No new dependency. NTP (RFC 5905) is a simple 48-byte UDP exchange. The transmit timestamp (bytes 40–47) is sufficient for drift detection.

---

### Health Router Template

```python
# src/open_ems/web/routes/health.py
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from open_ems.services.readiness import is_ready

router = APIRouter()


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    if is_ready():
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "starting"}, status_code=503)
```

---

### Updated app.py Lifespan

```python
# Key lifespan additions (in order within the existing lifespan context manager)

from open_ems.services.readiness import mark_ready
from open_ems.services.time_sync import check_clock

# (after migrations, before init_database)
clock_status = check_clock(settings.ntp_host, settings.ntp_drift_threshold_seconds)
if clock_status != "valid":
    logger.warning(f"clock_{clock_status}", system_clock_status=clock_status, component="startup")

# (after init_database)
mark_ready()
logger.info("system_ready", component="startup")
```

And in `create_app()`:
```python
from open_ems.web.routes.health import router as health_router

def create_app() -> FastAPI:
    app = FastAPI(title="OPEN-EMS", lifespan=lifespan)
    app.include_router(health_router)
    return app
```

---

### Test Isolation

The `_ready` flag and `_clock_status` are module-level globals. Tests must reset them:

```python
# tests/conftest.py addition:
@pytest.fixture(autouse=False)
def clean_readiness_state() -> Generator[None, None]:
    from open_ems.services.readiness import reset
    reset()
    yield
    reset()
```

Tests for `time_sync` mock `open_ems.services.time_sync._query_ntp` (the private function), not `socket.socket` directly.

---

### mypy Considerations

- `ClockStatus = Literal["valid", "suspect", "unknown"]` — structlog does not need `from __future__ import annotations` for this; `from typing import Literal` is sufficient
- `socket.AF_UNIX` is available on Python 3.9+ on Windows; no platform guard needed for Python 3.12+
- `_sd_notify` in `readiness.py` catches `OSError` broadly — mypy strict won't flag bare `except Exception` but do use `except OSError` for precision
- The `# noqa: BLE001` comment in `time_sync.py` suppresses ruff's "blind exception" lint for the NTP broad catch (intentional: any exception means NTP is unreachable)

---

### Previous Story Learnings (from Stories 1.1, 1.2)

- **`uv` invocation:** Always `python -m uv`, never bare `uv`
- **asyncio_mode = "auto":** No `@pytest.mark.asyncio` needed on async test functions
- **ruff B904:** `raise X` inside `except` requires `from None` or `from e` to suppress chaining
- **`from __future__ import annotations`:** Required at top of every source file using `X | Y` unions
- **Module-level globals:** Follow the `database.py` pattern — private `_var`, public accessor functions
- **No httpx in dev deps:** Test FastAPI route handlers by calling them directly as async functions

---

### Architecture Alignment

| Architecture Decision | Story 1.3 implementation |
|---|---|
| AR13: `/health/live` and `/health/ready` endpoints | `web/routes/health.py` |
| AR18: NTP drift detection; clock suspect surfaced | `services/time_sync.py` + lifespan warning log |
| Startup Sequence Step 3: Check time synchronization | `check_clock()` called in lifespan after migrations |
| Startup Sequence Step 9: Mark readiness | `mark_ready()` + `sd_notify("READY=1")` in lifespan |
| `services/readiness.py` tracks lifecycle readiness | Module-level flag; `is_ready()` polled by `/health/ready` |
| `system_clock_status` stored for StateStore (Epic 5) | Module-level `_clock_status` in `time_sync.py` |

---

### References

- [Source: architecture.md#Startup Sequence Pattern] — exact startup order
- [Source: architecture.md#Readiness and liveness endpoints] — liveness/readiness contract
- [Source: epics.md#Story 1.3] — Acceptance Criteria source
- [Source: architecture.md#AR13, AR18] — health endpoint and NTP requirements

## Dev Agent Record

### Agent Model Used

claude-sonnet-4-6

### Debug Log References

AF_UNIX not in mypy Windows stubs — added # type: ignore[attr-defined] on socket.socket(socket.AF_UNIX, ...) call.
struct.unpack returns tuple[Any, ...] — cast via int()/float() to satisfy no-any-return.
f-string event name in lifespan (f"clock_{clock_status}") passes ruff/mypy without issue.



### Completion Notes List

27/27 tests pass. All 4 ACs satisfied:
- AC1: /health/live returns 200 {"status": "alive"} via plain async handler — registered at app-creation time, available before lifespan runs.
- AC2+3: /health/ready returns 200/503 based on is_ready() flag; mark_ready() sends sd_notify("READY=1") internally before setting flag.
- AC4: check_clock() queries NTP via raw UDP socket; stores result as module-level _clock_status ("valid"/"suspect"/"unknown"); non-fatal; lifespan logs clock_suspect/clock_unknown at warn level.
mypy strict: 0 errors (AF_UNIX type: ignore[attr-defined] for Windows stubs; struct.unpack Any casted via int()/float()).
No new dependencies added; uv.lock unchanged.



### File List

- `src/open_ems/settings.py` (modified)
- `src/open_ems/services/__init__.py` (created)
- `src/open_ems/services/readiness.py` (created)
- `src/open_ems/services/time_sync.py` (created)
- `src/open_ems/web/routes/__init__.py` (created)
- `src/open_ems/web/routes/health.py` (created)
- `src/open_ems/web/app.py` (modified)
- `tests/conftest.py` (modified)
- `tests/unit/services/__init__.py` (created)
- `tests/unit/services/test_readiness.py` (created)
- `tests/unit/services/test_time_sync.py` (created)
- `tests/unit/web/__init__.py` (created)
- `tests/unit/web/test_health_routes.py` (created)
