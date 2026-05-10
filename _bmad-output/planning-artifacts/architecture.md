---
stepsCompleted: ["step-01-init", "step-02-context", "step-03-starter", "step-04-decisions", "step-05-patterns", "step-06-structure", "step-07-validation", "step-08-complete"]
lastStep: 8
status: 'complete'
completedAt: '2026-05-01'
inputDocuments:
  - "_bmad-output/planning-artifacts/prfaq-OPEN-EMS.md"
  - "_bmad-output/planning-artifacts/prfaq-OPEN-EMS-distillate.md"
  - "_bmad-output/planning-artifacts/prd.md"
workflowType: 'architecture'
project_name: 'OPEN-EMS'
user_name: 'Jordan'
date: '2026-05-01'
---

# Architecture Decision Document

_This document builds collaboratively through step-by-step discovery. Sections are appended as we work through each architectural decision together._

---

## Project Context Analysis

### Requirements Overview

**Functional Requirements — 42 FRs across 7 categories:**

| Category | FRs | Architectural weight |
|---|---|---|
| Device Integration & Management | FR1–FR6b (7) | Protocol adapters, capability profiles, degraded mode |
| Energy Control & Decision Engine | FR7–FR15 (9) | Core real-time loop, safety enforcement, fallback logic |
| Installer Configuration & Management | FR16–FR24 (9) | Admin web interface, validation flows, backup/restore |
| Homeowner Energy Interface | FR25–FR30 (6) | User web interface, override mechanics |
| System Observability & Reliability | FR31–FR34 (4) | Watchdog, fail-safe mode, auto-recovery |
| Access Control & Security | FR35–FR39 (5) | Local auth, role-based sessions, HTTPS |
| External Data Integration | FR40–FR42 (3) | Optional enrichment, graceful unavailability |

**Non-Functional Requirements — critical drivers:**

| NFR | Constraint | Architectural implication |
|---|---|---|
| Control loop timing | 5–15s cycle; 60s max under degradation | Loop must be isolated from blocking I/O |
| Action latency | Decision → device state ≤ 30s | Command dispatch must be async with ACK verification |
| Page load | ≤ 3s on Raspberry Pi 4 | Lightweight frontend; no heavy JS framework |
| State freshness | ≤ 30s device data in UI | In-memory state cache; push or poll to UI |
| Uptime | 30 consecutive days no crash | Process supervision; structured restart policy |
| Reboot recovery | Full operational state ≤ 2 min | Stateless or fast state-rebuild from persistent store |
| Network recovery | Resume control ≤ 60s after restoration | Auto-reconnect logic per protocol adapter |
| Watchdog trigger | ≤ 2 missed evaluation cycles | External watchdog thread or systemd watchdog |
| DSMR staleness | > 60s → conservative fallback | Timestamp validation before using meter data |
| Data retention | 12 months energy history, 90 days event log | Time-series store with configurable pruning |
| Crash-safe writes | Power-loss-safe config and log writes | Atomic writes; avoid microSD write-through issues |
| Install time | Fresh install ≤ 30 min | Scripted setup; minimal environment-specific config |

---

### Scale & Complexity Assessment

- **Complexity level: HIGH** — but intentionally scoped for a team of 3
- **Primary technical domain:** Edge IoT orchestration + local web application
- **Estimated architectural components:** 7 major subsystems

The complexity is driven not by scale (single site, single process) but by **correctness requirements**:
- Safety constraints must be mathematically impossible to bypass
- Three protocols behave fundamentally differently (polling vs. event-driven vs. serial)
- All failure modes must be explicitly handled with observable, logged transitions
- The system must be trusted to run autonomously for 30+ days on hardware the team never touches

This is a **reliability-critical embedded-style system** packaged as a Linux service with a web UI — not a typical web application.

---

### Technical Constraints & Dependencies

| Constraint | Source | Impact |
|---|---|---|
| Raspberry Pi 4 (4 GB RAM) minimum | PRD hardware spec | Prefer lean runtimes and modest memory footprint; avoid heavy runtime stacks unless there is a clear reliability or ecosystem benefit |
| Linux (Debian/Ubuntu preferred) | PRD OS spec | Systemd native; apt packaging; no Windows APIs |
| Modbus TCP — polling-based | Protocol | Adapter must poll on schedule; register maps per model |
| OCPP 1.6 — Central System role | Protocol | OPEN-EMS must run an OCPP Central System endpoint over WebSocket; chargers connect to this local endpoint when configured to do so |
| DSMR P1 — serial or TCP, read-only | Protocol | Adapter reads continuously; 60s staleness threshold |
| No cloud dependency for core | PRD mandate | All critical paths local; external APIs are optional enrichment only |
| SSD strongly recommended | PRD hardware | Storage I/O reliability; journaling or atomic writes required |
| No built-in remote access | PRD mandate | OPEN-EMS never opens outbound tunnels; VPN is installer's job |
| HTTPS in production | Security NFR | TLS termination at the edge runtime |
| Bcrypt credential storage | Security NFR | Local password management; no external identity provider |

---

### Cross-Cutting Concerns Identified

These concerns cut across **all** architectural components and must be designed in from the start — they cannot be retrofitted:

1. **Safety constraint enforcement** — The peak limit and battery reserve floor must be enforced at every control decision point. The EV charging window is a scheduling preference and must be evaluated by the decision engine, but may be overridden by homeowner actions within hard safety constraints. Safety constraint enforcement must be centralized in a dedicated policy/guard layer that every command path must pass through before dispatch. Safety cannot be spread across individual adapters or UI logic.

2. **Structured logging and observability** — Every control decision, constraint enforcement event, degraded-mode transition, and recovery event must be logged with timestamp and human-readable explanation. Logging is not optional or bolted-on; it is a system output.

3. **Fail-safe and degraded-mode cascade** — Every subsystem (protocol adapter, decision engine, watchdog) has a defined safe state. Loss of any input must trigger a conservative fallback, not a crash or silent stall. The fail-safe cascade must be explicit in the architecture.

4. **Idempotency and command ACK** — All device commands must be idempotent and verified via ACK before state is assumed updated. Retry logic must be bounded and must not create oscillating or unsafe behavior.

5. **Role-based access enforcement** — The two roles (Installer/Admin, Homeowner/User) must be enforced at both the API routing layer and the UI rendering layer. No installer-only data may leak into homeowner views.

6. **Crash-safe persistence** — Config, event log, and energy history writes must survive sudden power loss. This is a hardware constraint (microSD failure mode) as much as a software one.

7. **Auto-recovery** — Reboot and network interruption recovery are not edge cases; they are expected events the system must handle without operator intervention.

8. **Time synchronization** — Time correctness is required for correct event ordering, tariff interpretation, peak-window tracking, and log traceability. The system must detect significant clock drift and surface it as a degraded system condition.

9. **Configuration versioning** — Site configuration must be versioned and changes must be auditable, especially constraint changes that affect control behavior. Installer trust and liability require knowing when constraints changed.

10. **Testability** — Core decision logic must be testable independently of physical devices using simulated device state and recorded event sequences. The decision engine cannot be validated only against real hardware; this is essential for safe iteration.

---

## Technology Foundation

### Primary Technology Domain

Edge IoT orchestration daemon + local web application, running as a persistent Linux service.
No conventional web starter template applies. The foundation is the runtime stack selection.

### Stack Selection

**Runtime:** Python 3.12+, asyncio-based service architecture.

Rationale: strongest ecosystem for Modbus TCP, OCPP 1.6, DSMR P1, and energy data processing; fastest v1 integration iteration for a small team; lower barrier for community device integrations. Architecture is modular so performance-critical subsystems can be rewritten in Go or Rust independently in future if needed.

### Core Technology Decisions

**Language & typing:**
- Python 3.12+ with strict type hints throughout
- Pydantic v2 for all data models, configuration schemas, and API contracts
- Type enforcement via mypy or pyright in CI

**Web framework:**
- FastAPI — serves both REST API endpoints and static/template assets from a single process
- Jinja2 for server-rendered HTML pages (installer and homeowner views)
- HTMX + Alpine.js for lightweight interactivity (real-time state polling, single-tap actions)
- No separate frontend build pipeline; no React, Vue, or heavy SPA framework in v1

**Database:**
- SQLite with WAL mode — single file, crash-safe, simple backup/restore
- Covers: site configuration, user credentials, device registry, sessions, event log, energy state history, capability profiles
- aiosqlite for async access; SQLAlchemy Core or raw SQL for queries (no heavy ORM)
- Alembic or lightweight SQL migration mechanism required for schema evolution and safe updates
- v2 may introduce a dedicated time-series store if query volume warrants it

**Protocol libraries (behind adapter interfaces):**
- Modbus TCP: pymodbus (async client)
- OCPP 1.6: Python ocpp library (websockets-based Central System)
- DSMR P1: existing dsmr-parser library or lightweight custom parser if needed
- All protocol libraries isolated behind adapter interfaces; library substitution must not propagate changes into the decision engine

**Testing:**
- pytest for unit and integration tests
- pytest-asyncio for async control-loop and adapter tests
- Simulated device adapters for decision engine testing without physical hardware
- Recorded protocol fixtures for regression testing device integrations

**Dependency management:**
- uv — fast, modern Python package manager; lockfile-based reproducibility
- pyproject.toml as the single project manifest

**Deployment:**
- Primary: Docker Compose (one compose file, one service or tightly coupled set)
- Secondary: direct host installation with systemd service unit
- Both paths must be documented and reproducible; Docker is not a prerequisite for operation
- systemd watchdog integration for direct host installs; Docker healthchecks / restart policies for containerized installs

**Project initialization:**
```
uv init open-ems && uv add fastapi uvicorn pydantic aiosqlite pymodbus ocpp dsmr-parser
```

### Architectural Decisions Established by This Stack

| Decision area | Choice | Implication |
|---|---|---|
| Concurrency model | asyncio-first single-service | All critical I/O must be non-blocking; blocking adapters must be isolated behind timeouts and execution boundaries |
| Data contracts | Pydantic models | Validates device data, config, and API payloads at boundaries |
| UI delivery | Server-rendered + HTMX | No frontend build step; no client-side state management needed |
| Persistence | SQLite WAL | Single-file backup; WAL ensures crash-safe concurrent reads |
| Protocol isolation | Adapter interfaces | Protocol library changes are contained; engine consumes normalized data |
| Process supervision | systemd / Docker restart | Automatic restart on crash; watchdog signaling for stall detection |

### Trade-offs

- **asyncio-first single-service architecture:** a poorly written protocol adapter can block the control loop if it performs synchronous blocking work. Protocol adapters must be disciplined about timeouts and must never perform synchronous blocking I/O in the event loop. Blocking adapters must be isolated behind timeouts and execution boundaries (e.g. `asyncio.wait_for`, executor offloading). This is a design invariant enforced in code review. The architecture does not forbid carefully isolated worker threads or subprocesses for libraries that require it.
- **SQLite for time-series:** adequate for v1 single-site with 5–15 second samples over 12 months (order of a few million rows depending on sampling interval and retention strategy). Query performance is acceptable with proper indexing. If v2 moves to fleet-level aggregation, this decision gets revisited.
- **HTMX + server-rendering:** simpler to build and maintain; real-time UI state updates use Server-Sent Events or periodic polling. WebSocket remains reserved for OCPP and is not needed for the homeowner/admin UI in v1. This is well within the 30-second state-freshness NFR.
- **uv over poetry:** more modern, faster, better lockfile behavior — but less mature. Acceptable risk for v1 greenfield.

---

## Core Architectural Decisions

### Decision Priority Analysis

**Critical — block implementation:**
- SystemState StateStore model and asyncio command queue (1.1)
- Server-side session mechanism + CSRF protection (2.1)
- OCPP WebSocket integration pattern (3.2)
- Structlog dual-sink logging (5.1)
- Watchdog asyncio task design (5.2)

**Important — shape architecture significantly:**
- SQLite-as-authoritative-store + YAML export for backup (1.2)
- Energy history schema and peak tracking structure (1.3)
- TLS self-signed at install with SAN (2.2)
- `/actions/` API namespace (3.1)
- Single bounded command queue (3.3)
- Role-aware SSE state stream (4.1)
- Alembic auto-run before adapters or control loop (5.5)
- Time synchronization detection and degraded mode (5.6)
- Configuration versioning and audit log (5.7)

**Deferred — post-v1:**
- Dedicated time-series store (if fleet/query volume warrants in v2)
- Automated TLS certificate renewal
- Deployment automation / remote update push

---

### Data Architecture

**Decision 1.1 — In-memory system state model**

A shared in-memory SystemState model updated through a dedicated StateStore service. Protocol adapters publish normalized device state updates to the StateStore; the decision engine and web layer read snapshots from it. State snapshots should be immutable or copy-on-read to avoid accidental mutation outside the StateStore boundary.

User-initiated commands flow from the web layer to the control loop via a bounded asyncio command queue. Device control commands generated by the decision engine must still pass through the centralized safety/policy guard before reaching adapters.

No inter-process IPC is required in v1. The runtime is asyncio-first, with blocking or long-running work isolated behind explicit execution boundaries where necessary.

**Decision 1.2 — Configuration storage**

SQLite is the single authoritative store for all configuration, state, events, and credentials. A configuration export command serializes the site config tables to a human-readable YAML file for backup and disaster recovery — this is the "YAML or equivalent" the PRD refers to. The YAML is a backup artifact, not the live store. Secrets (hashed passwords, session secrets) are excluded from the YAML export. This satisfies both crash-safety and human-readability requirements without splitting stores.

**Decision 1.3 — Energy history and peak tracking**

Dedicated `energy_readings` table optimized for common dashboard queries. A narrow metric/value schema is acceptable for v1, but the architecture phase should confirm indexes, retention policy, and whether site-level aggregate readings should be stored separately for peak tracking. Peak tracking is core functionality (capaciteitstarief compliance); aggregate 15-minute interval readings may deserve explicit structure rather than being computed on-demand from raw samples. Retention enforced by a nightly cleanup asyncio task (12-month rolling window for energy readings, 90 days for event log).

---

### Authentication & Security

**Decision 2.1 — Session mechanism**

Server-side sessions stored in a `sessions` table in SQLite. Session ID is a cryptographically random opaque token with at least 128 bits of entropy generated via Python's `secrets` module. Delivered as an httpOnly, Secure, SameSite=Strict cookie. No JWT — stateless tokens add complexity and make revocation impossible, which matters for an installer-managed system where credential resets must take effect immediately.

FastAPI dependency injection enforces role per-route: `Depends(require_installer)` / `Depends(require_homeowner)`.

All state-changing web actions must include CSRF protection, especially because authentication is cookie-based.

**Decision 2.2 — TLS certificate**

Self-signed certificate generated at install time by the setup script (via `openssl` or Python's `cryptography` library). Certificate uses CN/SAN matching the local hostname where possible (e.g. `open-ems.local`), with installer documentation for browser trust warnings in v1. Installer can replace with a proper cert at any time by dropping files at a documented path. No Let's Encrypt — it requires outbound internet and ongoing renewal automation, which is not appropriate for an airgapped local system.

**Decision 2.3 — Credential storage and initial setup**

bcrypt for all credential hashing. On first run, the system detects no users in the database and forces an "initial setup" flow where the installer creates the admin credential before any other access is permitted. Homeowner credentials are created by the installer post-deployment.

---

### API & Communication Patterns

**Decision 3.1 — API design pattern**

RESTful resource routes for configuration, device registry, event log, and state reads. An `/actions/` namespace for imperative operations where REST resource semantics do not apply:
- `POST /actions/charge-now` — homeowner EV override
- `POST /actions/validate-deployment` — installer deployment check
- `POST /actions/trigger-update` — installer update approval

FastAPI's automatic OpenAPI docs (`/docs`) are enabled in development mode only by default. If enabled in production for installer troubleshooting, they must require Installer/Admin authentication.

**Decision 3.2 — OCPP WebSocket integration with FastAPI**

A dedicated FastAPI WebSocket endpoint (`/ocpp/{charge_point_id}`) accepts charger connections and hands the WebSocket object to the OCPP adapter. Each charger connection runs as a separate asyncio task managed by the OCPP adapter pool. The adapter pool is part of the device integration layer — it exposes normalized `ChargerState` and accepts `SetChargingProfile` commands, hiding OCPP internals from the decision engine.

**Decision 3.3 — Internal command dispatch**

A single bounded asyncio.Queue carrying typed Command objects is the default v1 design. Web layer enqueues user-initiated commands; the control loop dequeues and dispatches before each evaluation cycle. Device control commands generated by the decision engine must still pass through the centralized safety/policy guard before reaching adapters. The queue is bounded (max depth: 10) to prevent unbounded accumulation. A single queue preserves ordering and avoids priority/starvation complexity that multiple queues would introduce.

**Decision 3.4 — Error handling**

All API errors return a structured JSON body `{error: str, code: str, detail: Optional[str]}`. FastAPI exception handlers catch both application-defined exceptions and unexpected errors. Unhandled exceptions are logged via structlog and return a generic 500 with no internal detail in production.

---

### Frontend Architecture

**Decision 4.1 — Real-time state updates**

A Server-Sent Events (SSE) endpoint (`GET /api/stream/state`) pushes current system state as a JSON payload on a configurable interval (default: 10 seconds). HTMX uses `hx-trigger="every 10s"` as a polling fallback for browsers with SSE issues. The 30-second state-freshness NFR is met comfortably.

The SSE state stream must be role-aware. Homeowner sessions receive only homeowner-safe state; installer sessions may receive diagnostics and degraded-mode details. Role filtering is applied server-side at the SSE serialization point.

**Decision 4.2 — Static asset delivery**

HTMX, Alpine.js, and all CSS are served from the Python package's `static/` directory (bundled at install time). No CDN dependency — the system is local-only and must work without internet access. Vendored JS files are committed to the repository at pinned versions. No npm/Node.js build step.

**Decision 4.3 — Template structure and role isolation**

Two separate Jinja2 template trees: `templates/installer/` and `templates/homeowner/`. Role is resolved server-side from the session; the router selects the appropriate template tree. No installer-only template or data is ever rendered into homeowner responses. This is enforced structurally, not by conditional rendering within shared templates.

---

### Infrastructure & Deployment

**Decision 5.1 — Structured logging**

Two separate log sinks:
1. **Process log** — structlog outputting structured JSON to stdout, captured by journald (native) or Docker logging driver (containerized). Used for operational monitoring and debugging.
2. **Decision audit log** — Control decisions, constraint enforcement events, degraded-mode transitions, and recovery events written as rows to the `event_log` SQLite table with human-readable `description` field. This is the log the installer reads in the UI.

Structlog is configured with `ProcessorFormatter` for both sinks. No external log aggregation in v1.

**Decision 5.2 — Watchdog implementation**

A dedicated asyncio task tracks the timestamp of the last completed control loop evaluation. If two consecutive evaluation cycles are missed (configurable threshold, default: 2× the cycle interval), the watchdog logs the stall event to both sinks and triggers recovery:
- For native installs: sends `sd_notify WATCHDOG=1` heartbeats during normal operation; failure to send triggers systemd's built-in watchdog restart.
- For Docker: the `/health` FastAPI endpoint returns 503 if the watchdog detects a stall; Docker's healthcheck policy triggers container restart.

**Decision 5.3 — Environment and deployment configuration**

`pydantic-settings` reads deployment-time parameters from environment variables and a `.env` file: `PORT`, `TLS_CERT_PATH`, `TLS_KEY_PATH`, `LOG_LEVEL`, `DB_PATH`, `SECRET_KEY`. Site configuration (device registry, constraints, strategy) lives entirely in SQLite. The `.env` file is the only environment-specific file needed beyond the database.

**Decision 5.4 — CI/CD pipeline**

GitHub Actions. On every push: `ruff` (linting + formatting), `mypy` (type checking), `pytest` (unit + integration tests with simulated adapters). No deployment automation in v1 — updates are installer-triggered. A `docker buildx` build step produces and pushes a multi-arch image (amd64 + arm64 for Raspberry Pi) on tagged releases.

**Decision 5.5 — Schema migration on startup**

Alembic migrations run automatically on service startup before protocol adapters or the control loop initialize. Migration failures are fatal and logged clearly — the service does not start with an inconsistent schema. This ensures updates preserve all data and never silently corrupt configuration or device state.

**Decision 5.6 — Time synchronization**

The runtime checks system clock availability and detects significant clock drift where possible (e.g. NTP sync status via `timedatectl` or equivalent, or timestamp plausibility checks). If time is unavailable or suspicious, the system enters degraded mode for tariff/peak-window logic and surfaces the condition to the installer via the system health indicator. Time synchronization state is a first-class component of system health.

**Decision 5.7 — Configuration versioning and audit**

All constraint changes and device configuration changes are stored with timestamp, actor (user ID/role), previous value, and new value in a dedicated `config_audit_log` table. The active configuration is versioned so control decisions can be traced back to the configuration in effect at the time. This is required for installer trust, liability traceability, and debugging constraint enforcement events.

---

### Decision Impact Analysis

**Implementation sequence (order matters):**
1. SQLite schema + Alembic migrations — everything depends on this
2. StateStore + SystemState model — decision engine and web layer depend on this
3. Safety/policy guard layer — all command paths depend on this
4. Protocol adapters (Modbus, OCPP, DSMR) publishing to StateStore
5. Decision engine reading from StateStore, dispatching via guard
6. Web layer: auth, sessions, CSRF protection, role-based routing
7. Web UI templates (installer + homeowner) + role-aware SSE state stream
8. Watchdog task + health endpoint
9. Logging pipeline (structlog process log + SQLite audit log)
10. CI/CD + Docker multi-arch build

**Cross-component dependencies:**
- StateStore is a dependency of: decision engine, web layer, watchdog
- Safety/policy guard is a dependency of: all device command paths (engine-initiated and web-initiated)
- SQLite schema is a dependency of: StateStore persistence, session store, event log, config audit log, energy history
- Alembic must complete before: any other subsystem initializes
- Time sync check must complete before: tariff/peak-window logic initializes

---

## Implementation Patterns & Consistency Rules

### Critical Conflict Points Identified

15 areas where agents could make incompatible choices: safety guard bypass, StateStore mutation, adapter interface drift, degraded-mode inconsistency, audit vs. process log misuse, CSRF omission, role enforcement bypass, command ACK skipping, naming inconsistency, async blocking, startup order violations, response format divergence, timestamp format, energy unit/sign ambiguity, and test isolation failures.

---

### Safety-Critical Patterns (Non-Negotiable)

These patterns must be followed by every agent implementing any code that touches device control, state, or configuration.

**Pattern: All device commands pass through the PolicyGuard**

Every command that results in a device action — regardless of whether it originates from the decision engine, the web layer, or a scheduled task — must be dispatched through `PolicyGuard.authorize_and_dispatch()` before reaching an adapter.

```python
# CORRECT
result = await policy_guard.authorize_and_dispatch(SetChargingRateCommand(rate_kw=7.0))

# WRONG — never call adapter directly from web handler or engine
await charger_adapter.set_charge_rate(7.0)
```

The PolicyGuard is the single point where safety constraints are evaluated. There is no bypass interface, not even in tests. Tests use a `SimulatedPolicyGuard` that enforces the same interface.

Every `DeviceCommand` must include: `device_id`, `command_type`, requested parameters, `origin` (one of `decision_engine` / `installer` / `homeowner` / `system`), and `correlation_id`. These fields are required for auditability, command tracing, and safe authorization.

PolicyGuard validates whether a command is allowed under current constraints. Adapters validate whether the target device can execute it and return a `CommandResult`. Both layers are required — neither replaces the other.

**Pattern: StateStore snapshots are immutable**

System state is updated only through StateStore methods. Protocol adapters publish normalized state updates; no component mutates state directly. The decision engine and web layer read exclusively via `state_store.get_snapshot()`. The returned snapshot must be treated as immutable — never mutated in place. Derived state must be computed from a copy.

```python
# CORRECT
snapshot = state_store.get_snapshot()
grid_draw = snapshot.grid_meter.power_kw  # read only

# WRONG — never mutate a snapshot
snapshot.battery.soc_percent = 80  # forbidden
```

Snapshots are only valid for the current evaluation cycle. Never store a snapshot reference across cycles.

**Pattern: Degraded state is returned, not raised**

Adapter methods must not raise exceptions for recoverable device communication failures (timeouts, transient disconnects). They return a typed degraded state or a failed `CommandResult`. Exceptions are reserved for programming errors, invalid adapter implementation, or startup/configuration failures. Runtime device communication failures must return degraded state or a failed `CommandResult` so the control loop remains stable.

```python
# CORRECT
async def get_state(self) -> BatteryState | DegradedDeviceState:
    try:
        return await asyncio.wait_for(self._read_registers(), timeout=10.0)
    except (asyncio.TimeoutError, ModbusException):
        return DegradedDeviceState(device_id=self.device_id, reason="timeout")

# WRONG — lets device errors propagate uncaught into the control loop
async def get_state(self) -> BatteryState:
    return await self._read_registers()  # may raise — blocks or crashes loop
```

**Pattern: Constraint enforcement events always go to the audit log**

Any time the PolicyGuard rejects or modifies a command due to a constraint, it must record a `ConstraintEnforcementEvent` to the audit log before returning the result. This is mandatory — not conditional on log level or configuration.

---

### Naming Conventions

**Python code:** snake_case for all variables, functions, methods, and modules. PascalCase for classes. UPPER_SNAKE_CASE for module-level constants.

**Database tables:** snake_case, plural nouns.

| Table | ✓ Correct | ✗ Wrong |
|---|---|---|
| Device registry | `device_registry` | `DeviceRegistry`, `devices` |
| Energy readings | `energy_readings` | `EnergyReading`, `energy_data` |
| Event log | `event_log` | `events`, `EventLog` |
| Config audit | `config_audit_log` | `config_history` |
| Sessions | `sessions` | `user_sessions` |

Database columns: snake_case. Foreign keys: `{table_singular}_id`. Timestamps always named `created_at`, `updated_at`, or `occurred_at`.

**API endpoints:** lowercase, hyphen-separated, plural nouns for resources. Verb-noun under `/actions/` for imperative operations.

```
GET  /api/devices                  ✓
GET  /api/devices/{device_id}      ✓
GET  /api/event-log                ✓
POST /actions/charge-now           ✓
POST /actions/validate-deployment  ✓

GET  /api/device/{id}              ✗  (singular)
POST /api/chargeNow                ✗  (camelCase, wrong namespace)
```

**Pydantic models:** PascalCase with suffix indicating role:

| Suffix | Use | Example |
|---|---|---|
| `State` | Current device/system state | `BatteryState`, `SystemState` |
| `Reading` | Raw data from device/meter | `GridMeterReading` |
| `Command` | Imperative action to dispatch | `SetChargingRateCommand` |
| `Response` | API response payload | `DeviceListResponse` |
| `Config` | Site/device configuration | `SiteConstraintConfig` |
| `Event` | Audit log entry | `ConstraintEnforcementEvent` |

**Energy field naming:** always encode unit in the field name.

```python
power_kw: float           # ✓ kilowatts
energy_kwh: float         # ✓ kilowatt-hours
soc_percent: float        # ✓ state of charge 0–100
peak_threshold_kw: float  # ✓ peak limit in kW

power: float              # ✗ ambiguous unit
soc: float                # ✗ ambiguous unit
```

**Energy sign convention:** positive/negative sign conventions must be explicitly documented per metric and consistently applied. Default convention for grid power:
- `grid_power_kw > 0` → importing from grid (consuming)
- `grid_power_kw < 0` → exporting to grid (producing surplus)

Any deviation from this convention for a specific field must be documented in the model definition. Inverted sign conventions in grid/peak logic are a critical failure mode.

**Timestamps:** all persisted timestamps must be timezone-aware UTC. Use `datetime.now(UTC)` or `datetime.now(timezone.utc)`; never naive datetimes. In JSON responses, format as ISO 8601 UTC (`"2026-05-01T12:00:00Z"`). Never use Unix timestamps in API responses.

---

### Project Structure Patterns

```
src/open_ems/
    core/           # Domain models, StateStore, SystemState, enums, base types
    engine/         # Decision engine, control loop, PolicyGuard
    adapters/       # Protocol adapters: modbus/, ocpp/, dsmr/
    storage/        # SQLite access layer, repositories, Alembic migrations
    web/            # FastAPI app, routes, templates, static assets
        routes/         # installer.py, homeowner.py, actions.py, api.py
        templates/
            installer/
            homeowner/
        static/         # Vendored JS/CSS at pinned versions
    services/       # Cross-cutting services: audit_log, time_sync, watchdog

tests/
    unit/           # Mirrors src/ structure; no I/O, no adapters
    integration/    # Uses simulated adapters; tests control loop behavior
    fixtures/       # Shared fixtures, simulated device states
    recorded/       # Recorded protocol sessions for regression testing

migrations/         # Alembic revision files
```

Tests mirror source structure: `src/open_ems/engine/policy_guard.py` ↔ `tests/unit/engine/test_policy_guard.py`.

---

### Repository Pattern

All database access goes through repository classes in `open_ems/storage/`. Route handlers, adapters, and the decision engine must not execute raw SQL directly.

Repositories are responsible for:
- transactions and connection lifecycle
- schema-aware reads and writes
- atomic configuration updates
- audit/event persistence
- backup and export queries

```python
# CORRECT
device = await device_repo.get_by_id(device_id)

# WRONG — raw SQL in a route handler or engine component
row = await db.execute("SELECT * FROM device_registry WHERE id = ?", [device_id])
```

---

### API Response Format Patterns

**Success responses:** direct Pydantic model serialization, no envelope wrapper.

```json
GET /api/devices/abc-123
→ 200 {"device_id": "abc-123", "role": "battery", "state": "connected"}
```

**Error responses:** always `{"error": "...", "code": "...", "detail": null | "..."}`.

```json
→ 409 {"error": "Constraint violation", "code": "PEAK_LIMIT_EXCEEDED",
        "detail": "Requested rate 11.0 kW would breach peak limit 5.0 kW"}
```

**JSON field naming:** snake_case throughout. Never camelCase in API responses.

**HTTP status codes:**

| Situation | Code |
|---|---|
| Success (read) | 200 |
| Success (created) | 201 |
| Validation error | 422 |
| Auth failure | 401 |
| Role insufficient | 403 |
| Not found | 404 |
| Constraint violation | 409 |
| Server error | 500 |

Do not use 200 for errors. Do not use 422 for business logic failures (constraint violations use 409).

---

### Logging Patterns

Two sinks — assignment depends on audience and operational impact:

| What | Where | How |
|---|---|---|
| Control decisions | `audit_log` SQLite table | `audit_log.record(DecisionEvent(...))` |
| Constraint enforcement | `audit_log` SQLite table | `audit_log.record(ConstraintEnforcementEvent(...))` |
| Degraded-mode transitions | `audit_log` SQLite table | `audit_log.record(DegradedModeEvent(...))` |
| Recovery events | `audit_log` SQLite table | `audit_log.record(RecoveryEvent(...))` |
| Device errors/timeouts causing degraded mode | **both** sinks | structlog for operational trace; audit_log for installer visibility |
| Pure protocol debug noise | structlog only | `logger.debug(...)` |
| Application errors | structlog only | `logger.error(..., exc_info=True)` |

Device errors that affect system state (cause degraded mode or affect control behavior) must appear in both the structlog process log and the audit log. Pure protocol-level debug noise stays in structlog only.

Structlog calls must include relevant context as keyword arguments:

```python
logger.warning("device_timeout", device_id="bat-01", adapter="modbus",
               timeout_s=10.0, retry_count=2)
```

Never use `print()` for any logging purpose.

---

### Async Patterns

All I/O-bound functions are `async def`. All external device calls are wrapped in `asyncio.wait_for` with explicit timeouts.

```python
# CORRECT
async def read_battery_state(self) -> BatteryState | DegradedDeviceState:
    try:
        return await asyncio.wait_for(self._read(), timeout=10.0)
    except asyncio.TimeoutError:
        return DegradedDeviceState(...)

# WRONG — blocks the event loop
def read_battery_state(self) -> BatteryState:
    return self._sync_client.read_registers(...)
```

Never use `time.sleep()`. Always `await asyncio.sleep()`. Blocking library calls, if unavoidable, must be offloaded via `asyncio.get_event_loop().run_in_executor(None, blocking_fn)`.

---

### Startup Sequence Pattern

Service startup must follow this exact order. No subsystem may initialize before its dependency completes successfully.

```
1. Load pydantic-settings configuration
2. Run Alembic migrations — fatal if they fail
3. Check time synchronization
4. Initialize StateStore
5. Initialize PolicyGuard (loads active constraints from DB)
6. Initialize protocol adapters (connect to devices)
7. Start control loop
8. Start watchdog task
9. Mark system readiness for web/API traffic after core services are initialized
```

The important invariant is not that the web server binary starts last, but that routes do not serve operational traffic before the system readiness flag is set. The `/health/ready` endpoint reflects this state.

**Readiness and liveness endpoints:**

- `GET /health/live` — returns 200 if the process is running (liveness)
- `GET /health/ready` — returns 200 only after migrations, StateStore, PolicyGuard, adapters, and control loop are all initialized; returns 503 otherwise (readiness)

Docker healthchecks and systemd `sd_notify READY=1` must use the readiness signal, not just liveness. The web UI must not be accessible to users before the readiness check passes.

---

### Role Enforcement Pattern

Route dependencies are the primary access-control boundary. Business logic should not duplicate route-level role checks. However, safety and policy checks must still be enforced in PolicyGuard regardless of request origin — route authentication alone is not sufficient for device safety.

```python
# CORRECT — route enforces role via dependency; PolicyGuard enforces safety
@router.post("/actions/charge-now")
async def charge_now(
    command: ChargeNowRequest,
    _user: HomeownerUser = Depends(require_homeowner),
    queue: CommandQueue = Depends(get_command_queue),
):
    await queue.put(ChargeNowCommand(...))

# WRONG — business logic doing role check instead of relying on dependency
async def charge_now(request, user):
    if user.role != "homeowner":  # should not be here
        raise HTTPException(403)
```

CSRF tokens must be included in all state-changing forms and validated server-side before processing. HTMX requests must include the CSRF token as a request header.

---

### Adapter Interface Pattern

All device adapters implement the `DeviceAdapter` protocol:

```python
class DeviceAdapter(Protocol):
    device_id: str
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def get_state(self) -> DeviceState | DegradedDeviceState: ...
    async def send_command(self, cmd: DeviceCommand) -> CommandResult: ...
```

Simulated adapters implement the same protocol and are used in all tests. Simulated adapters must support injecting specific states and failure modes for full scenario coverage.

---

### CommandResult Pattern

All `send_command` adapter implementations must return a `CommandResult` object containing:

- `command_id` / `correlation_id` — links result back to the originating command
- `device_id` — target device
- `status` — one of `accepted` / `rejected` / `failed` / `timed_out`
- `applied` — `True` / `False` / `None` (unknown) — whether the command took effect
- `reason` — optional human-readable explanation (required for rejected/failed)
- `observed_state` — optional post-command device state snapshot if available

The decision engine must not assume a command succeeded unless `CommandResult` confirms acceptance and either observed state confirms effect or the device protocol provides a reliable acknowledgment. Retries must be gated on `CommandResult.status` and must not retry if retrying would risk violating safety constraints.

**Status as of Story 9.0 (2026-05-10):** All three controllable domain adapters (`BatteryAdapter`, `InverterAdapter`, `EVChargerAdapter`) honor the cross-adapter command contract — see `src/open_ems/adapters/__init__.py` module docstring for the single authoritative reference (status mapping table, correlation_id semantics, cancellation/timeout rules, AR16 enforcement). The contract is versioned via `COMMAND_CONTRACT_VERSION`; bump that constant whenever any clause changes. `GridMeterAdapter` is intentionally excluded — DSMR P1 is read-only (FR6b) and never grows a write path.

---

### Enforcement Summary

**Every agent MUST:**
- Route all device commands through `PolicyGuard.authorize_and_dispatch()` — no exceptions
- Read system state exclusively via `StateStore.get_snapshot()`
- Never mutate a state snapshot
- Return `DegradedDeviceState` or failed `CommandResult` from adapters on recoverable failures — never raise
- Include all required `DeviceCommand` fields: `device_id`, `command_type`, parameters, `origin`, `correlation_id`
- Record control/constraint/degraded events to `audit_log`; device errors causing degraded mode to both sinks
- Wrap all external I/O in `asyncio.wait_for` with explicit timeout
- Use snake_case JSON field names in all API responses
- Use `datetime.now(UTC)` — never naive datetimes
- Apply the documented energy sign convention consistently; document any deviation
- Enforce role via FastAPI `Depends(require_*)` — not inside business logic functions
- Include CSRF token on all state-changing requests
- Include energy unit in field names (`_kw`, `_kwh`, `_percent`)
- Access all database state through repository classes — never raw SQL in handlers or engine
- Not serve operational traffic before `/health/ready` returns 200

**Every PolicyGuard rule must have unit tests** covering: allowed case, rejected case, and boundary/edge case. Every adapter must have tests covering: successful read, timeout, degraded state return, and failed `CommandResult`.

**Anti-patterns to reject in code review:**
- Direct adapter calls without `PolicyGuard.authorize_and_dispatch()`
- Snapshot mutation outside StateStore
- Bare `except Exception` without structured logging
- `time.sleep()` anywhere in the codebase
- Role checks inside service, repository, or engine functions
- `print()` for any purpose
- Ambiguous energy field names (no unit suffix)
- `datetime.now()` without timezone (naive datetime)
- Inverted or undocumented energy sign conventions
- Raw SQL outside repository classes

---

## Project Structure & Boundaries

### Requirements to Component Mapping

| FR Category | Primary Location | Secondary |
|---|---|---|
| Device Integration & Management (FR1–FR6b) | `adapters/`, `storage/repositories/device_repo.py` | `core/capabilities.py`, `web/routes/installer.py` |
| Energy Control & Decision Engine (FR7–FR15) | `engine/control_loop.py`, `engine/policy_guard.py`, `engine/rules/`, `engine/strategies/` | `core/state_store.py`, `services/audit_log.py` |
| Installer Configuration & Management (FR16–FR24) | `web/routes/installer.py`, `storage/repositories/`, `storage/backup.py` | `services/audit_log.py`, `web/routes/actions.py` |
| Homeowner Interface (FR25–FR30) | `web/routes/homeowner.py`, `web/routes/actions.py` | `web/routes/stream.py`, `core/models.py` |
| Observability & Reliability (FR31–FR34) | `services/watchdog.py`, `services/readiness.py`, `services/audit_log.py` | `engine/control_loop.py`, `web/routes/health.py` |
| Deployment Validation (FR17) | `services/validation.py` | `web/routes/actions.py`, adapters, config_repo |
| Access Control & Security (FR35–FR39) | `web/dependencies.py`, `web/csrf.py`, `web/routes/auth.py`, `storage/repositories/session_repo.py` | `web/app.py` (middleware) |
| External Data Integration (FR40–FR42) | `services/external/` | `engine/` (optimization inputs) |

---

### Complete Project Directory Structure

```
open-ems/
├── pyproject.toml                      # Project manifest, dependencies (uv)
├── uv.lock
├── .env.example                        # Documented env var template
├── .gitignore
├── README.md
├── Dockerfile                          # Multi-arch build (amd64 + arm64)
├── docker-compose.yml                  # Primary deployment path
├── systemd/
│   └── open-ems.service                # systemd unit for native install
├── scripts/
│   ├── install.sh                      # Fresh host installation
│   ├── generate-tls.sh                 # Self-signed cert generation (CN/SAN)
│   └── backup.sh                       # Config export helper
├── .github/
│   └── workflows/
│       └── ci.yml                      # ruff + mypy + pytest + docker buildx
├── migrations/
│   ├── env.py                          # Alembic environment config
│   ├── script.py.mako
│   └── versions/
│       └── 0001_initial_schema.py      # First migration (all v1 tables)
│
├── src/
│   └── open_ems/
│       ├── __init__.py
│       ├── main.py                     # Entrypoint; enforces startup sequence
│       ├── settings.py                 # pydantic-settings: PORT, DB_PATH, TLS, LOG_LEVEL, SECRET_KEY
│       │
│       ├── core/                       # Domain models, enums, base types — no I/O
│       │   ├── __init__.py
│       │   ├── models.py               # SystemState, DeviceState, DegradedDeviceState, snapshots
│       │   ├── capabilities.py         # DeviceCapabilityProfile models; only validated capabilities
│       │   │                           #   exposed to engine — no unsupported operations reach engine
│       │   ├── commands.py             # Command base + subtypes; CommandResult
│       │   ├── events.py               # Audit event types: DecisionEvent, ConstraintEnforcementEvent,
│       │   │                           #   DegradedModeEvent, RecoveryEvent, ConfigChangeEvent
│       │   ├── enums.py                # DeviceRole, SystemStatus, Strategy, CommandStatus, Origin
│       │   └── state_store.py          # StateStore service: publish + get_snapshot
│       │
│       ├── engine/                     # Decision engine and control loop
│       │   ├── __init__.py
│       │   ├── control_loop.py         # Main asyncio task; evaluation cycle; watchdog heartbeat
│       │   ├── policy_guard.py         # authorize_and_dispatch; constraint evaluation; audit recording
│       │   ├── rules/                  # One file per rule; each independently testable
│       │   │   ├── __init__.py
│       │   │   ├── peak_limiting.py    # FR8: 15-min peak window tracking + EV/battery dispatch
│       │   │   ├── battery_control.py  # FR9: charge/discharge within reserve floor
│       │   │   ├── ev_scheduling.py    # FR10, FR12: session scheduling + dynamic rate control
│       │   │   └── energy_balancing.py # FR11, FR11b: strategy-based priority resolution
│       │   └── strategies/             # Strategy parameter sets consumed by rules
│       │       ├── __init__.py
│       │       ├── minimize_cost.py
│       │       ├── maximize_self_consumption.py
│       │       └── prioritize_ev.py
│       │
│       ├── adapters/                   # Protocol adapters — all implement DeviceAdapter Protocol
│       │   ├── __init__.py
│       │   ├── base.py                 # DeviceAdapter Protocol definition
│       │   ├── modbus/
│       │   │   ├── __init__.py
│       │   │   ├── client.py           # pymodbus async client wrapper with timeout enforcement
│       │   │   ├── inverter_adapter.py # FR1, FR3, FR4: inverter read + capability detection
│       │   │   ├── battery_adapter.py  # FR1, FR3: battery read/write
│       │   │   └── register_maps/      # Versioned per-model; never shared between models
│       │   │       ├── __init__.py
│       │   │       ├── fronius_gen24_v1.py
│       │   │       ├── huawei_sun2000_v3.py
│       │   │       ├── growatt_hybrid_v1.py
│       │   │       ├── byd_hvs_v1.py
│       │   │       └── byd_hvm_v1.py
│       │   ├── ocpp/
│       │   │   ├── __init__.py
│       │   │   ├── central_system.py   # WebSocket endpoint handler; per-charger asyncio tasks
│       │   │   ├── charger_adapter.py  # FR1, FR10: normalized EV charger interface
│       │   │   └── profiles/           # Per-model OCPP compliance notes + command support matrix
│       │   │       ├── wallbox_pulsar.py
│       │   │       └── goe_charger.py
│       │   └── dsmr/
│       │       ├── __init__.py
│       │       └── meter_adapter.py    # FR1: DSMR P1 reader; staleness enforcement (60s)
│       │
│       ├── storage/                    # All DB access — repositories only, no raw SQL elsewhere
│       │   ├── __init__.py
│       │   ├── database.py             # aiosqlite pool; WAL mode; connection lifecycle
│       │   ├── repositories/
│       │   │   ├── __init__.py
│       │   │   ├── device_repo.py          # FR2, FR5, FR6: device registry, capability profiles
│       │   │   ├── config_repo.py          # FR16: site constraints, strategy, versioned config
│       │   │   ├── config_audit_repo.py    # Config change audit: timestamp, actor, previous/new values
│       │   │   ├── event_log_repo.py       # FR20, FR21: timestamped event log; installer note field
│       │   │   ├── energy_repo.py          # FR19, FR26: energy_readings (raw samples) + peak_intervals
│   │   │                           #   (completed 15-min clock-aligned aggregates, authoritative for billing)
│       │   │   ├── session_repo.py         # FR35, FR38: session CRUD; expiry enforcement
│       │   │   └── user_repo.py            # FR23, FR35: user CRUD; bcrypt credential storage
│       │   └── backup.py               # FR24: YAML config export/import; excludes secrets
│       │
│       ├── web/                        # FastAPI application
│       │   ├── __init__.py
│       │   ├── app.py                  # App factory; middleware (HTTPS redirect, session, CSRF)
│       │   ├── dependencies.py         # require_installer, require_homeowner, get_command_queue,
│       │   │                           #   get_state_store, get_audit_log, get_*_repo
│       │   ├── csrf.py                 # CSRF token generation and validation middleware
│       │   ├── routes/
│       │   │   ├── __init__.py
│       │   │   ├── auth.py             # GET/POST /login, POST /logout — FR35
│       │   │   ├── installer.py        # Installer pages: devices, setup, event log, settings — FR16–FR24
│       │   │   ├── homeowner.py        # Homeowner pages: dashboard, weekly summary — FR25–FR30
│       │   │   ├── actions.py          # POST /actions/charge-now, /validate-deployment,
│       │   │   │                       #   /trigger-update, /set-strategy — FR25, FR28
│       │   │   ├── api.py              # GET /api/devices, /api/event-log, /api/state — FR18–FR20, FR26
│       │   │   ├── stream.py           # GET /api/stream/state — SSE, role-filtered — FR26
│       │   │   └── health.py           # GET /health/live, GET /health/ready
│       │   ├── templates/
│       │   │   ├── base.html           # Shared layout: HTMX, Alpine.js, CSS includes
│       │   │   ├── login.html
│       │   │   ├── installer/
│       │   │   │   ├── dashboard.html  # System health, device status, peak tracker
│       │   │   │   ├── devices.html    # Device discovery, role assignment, capability status
│       │   │   │   ├── setup.html      # Constraint configuration + deployment validation
│       │   │   │   ├── event_log.html  # Timestamped log, installer note field
│       │   │   │   └── settings.html   # Update management, homeowner account, backup/restore
│       │   │   └── homeowner/
│       │   │       ├── dashboard.html  # Battery SOC, PV, grid draw, EV next session, charge-now
│       │   │       └── weekly_summary.html  # Nice-to-have: peaks avoided, self-consumption, estimated savings
│       │   └── static/
│       │       ├── htmx.min.js         # Pinned version — no CDN
│       │       ├── alpine.min.js       # Pinned version — no CDN
│       │       └── open-ems.css
│       │
│       └── services/                   # Cross-cutting runtime services
│           ├── __init__.py
│           ├── audit_log.py            # Writes to event_log table; called by engine + adapters
│           ├── watchdog.py             # Asyncio task; missed-cycle detection; sd_notify heartbeat
│           ├── readiness.py            # Tracks lifecycle readiness for /health/ready and startup gating
│           ├── time_sync.py            # Clock check; NTP status; degraded mode for tariff logic
│           ├── command_queue.py        # Bounded asyncio.Queue[Command] wrapper for user/system commands
│           ├── validation.py           # DeploymentValidationService: connectivity, role, capability,
│           │                           #   constraint, and control-readiness checks; returns ValidationReport
│           └── external/
│               ├── __init__.py
│               ├── solar_forecast.py   # FR40 (nice-to-have): HTTP client; graceful unavailability
│               └── tariff_feed.py      # FR41 (nice-to-have): configurable feed; fallback to ToU
│
├── tests/
│   ├── conftest.py                     # Shared fixtures; in-memory SQLite test DB
│   ├── unit/
│   │   ├── core/
│   │   │   ├── test_state_store.py     # Publish, snapshot immutability, copy-on-read
│   │   │   ├── test_commands.py        # Command field validation
│   │   │   ├── test_capabilities.py    # Capability profile validation; unsupported ops rejected
│   │   │   └── test_models.py          # DegradedDeviceState, energy sign conventions
│   │   ├── engine/
│   │   │   ├── test_policy_guard.py    # Allowed / rejected / boundary for every rule
│   │   │   ├── test_control_loop.py    # Cycle timing, missed-cycle detection
│   │   │   ├── rules/
│   │   │   │   ├── test_peak_limiting.py
│   │   │   │   ├── test_battery_control.py
│   │   │   │   └── test_ev_scheduling.py
│   │   │   └── strategies/
│   │   │       └── test_strategies.py
│   │   ├── storage/
│   │   │   └── repositories/
│   │   │       ├── test_device_repo.py
│   │   │       ├── test_config_audit_repo.py  # Audit record creation, actor, before/after values
│   │   │       ├── test_energy_repo.py         # Peak aggregate correctness
│   │   │       └── test_event_log_repo.py      # Retention enforcement
│   │   └── services/
│   │       ├── test_audit_log.py
│   │       ├── test_watchdog.py
│   │       ├── test_readiness.py
│   │       ├── test_time_sync.py
│   │       └── test_validation.py          # ValidationService: pass/warn/fail per check type
│   ├── integration/
│   │   ├── adapters/
│   │   │   ├── test_modbus_adapter.py      # Simulated Modbus server; timeout; degraded state
│   │   │   ├── test_ocpp_adapter.py        # Simulated OCPP charger; CommandResult verification
│   │   │   └── test_dsmr_adapter.py        # Staleness threshold enforcement
│   │   ├── engine/
│   │   │   ├── test_control_loop_degraded.py  # Degraded mode cascade; conservative fallback
│   │   │   └── test_failsafe.py               # Full communication loss → fail-safe transition
│   │   └── web/
│   │       ├── test_auth.py                # Login, logout, session expiry, role enforcement
│   │       ├── test_csrf.py                # CSRF rejection on state-changing routes
│   │       └── test_role_isolation.py      # No installer data in homeowner responses or SSE
│   ├── fixtures/
│   │   ├── simulated_adapters.py       # SimulatedBatteryAdapter, SimulatedChargerAdapter,
│   │   │                               #   SimulatedInverterAdapter, SimulatedMeterAdapter
│   │   ├── policy_scenarios.py         # Allowed/rejected/boundary scenarios for safety rules
│   │   ├── device_states.py            # healthy_battery_state, degraded_meter_state, etc.
│   │   ├── system_states.py            # healthy_system, peak_risk_system, comms_loss_system
│   │   └── degradation_scenarios.py    # Per-device and multi-device degradation scenarios
│   │                                   #   for control loop and fail-safe integration tests
│   └── recorded/                       # Protocol session recordings for regression
│       ├── modbus/
│       └── ocpp/
│
└── docs/
    ├── installation.md                 # Fresh install: Docker and native paths
    ├── configuration.md                # Constraint setup, device onboarding
    ├── device-integration.md           # Supported devices, firmware, Modbus register map notes
    └── backup-restore.md               # Config export/import procedure
```

---

### Architectural Boundaries

**State flow (data in):**

```
Physical devices
  → Protocol adapters (normalize to DeviceState / DegradedDeviceState)
  → StateStore.publish()
  → StateStore.get_snapshot()  ← Decision engine reads here
                               ← Web layer reads here (SSE serialization)
```

**Command flow (control out):**

```
Browser (homeowner/installer action)
  → Web route → Command queue (user/system Command)
                       ↓
Decision engine / control loop resolves high-level commands into
candidate DeviceCommands based on current state and constraints
  → PolicyGuard.authorize_and_dispatch()
                       ↓
              Adapter.send_command() → Physical device
                       ↓
                  CommandResult
```

Adapters never decide whether a command is safe; they only validate device capability and protocol execution and return `CommandResult`. Safety is the PolicyGuard's responsibility.

**Data boundary:**

SQLite is the only persistence boundary. No component writes to disk except through repository classes. No component reads from disk except through repository classes or pydantic-settings on startup.

**External boundary:**

`services/external/` → solar forecast / tariff API. Failures never propagate into the control loop. The control loop has no direct dependency on `services/external/`.

**Role boundary — enforced at two independent layers:**

1. FastAPI route dependencies (`web/dependencies.py`) — prevents unauthorized access
2. SSE stream serialization (`web/routes/stream.py`) — prevents unauthorized data exposure

These two layers are independent. A bug in one must not silently bypass the other.

**Time boundary:**

All persisted timestamps are timezone-aware UTC. Peak-window calculations, tariff interpretation, and event ordering use the same time source. If time sync is degraded (`services/time_sync.py`), tariff and peak-window optimization enter degraded mode and the condition is surfaced to the installer via the system health indicator.

**Configuration boundary:**

All site constraint changes go through `config_repo.py` and `config_audit_repo.py`. Every change creates an audit event recording timestamp, actor, previous value, and new value. The active configuration version is attached to relevant decision and audit events, enabling control decisions to be traced back to the constraint configuration in effect at the time.

**Import boundary:**

These rules prevent dependency cycles and unsafe bypass paths:

- `core/` may not import from `adapters/`, `storage/`, `web/`, or `services/`
- `engine/` may import from `core/` and service interfaces, but not from `web/`
- `adapters/` may import from `core/` only and publish through `StateStore` interfaces
- `web/` may call services and repositories and enqueue commands, but must not call `adapters/` directly

---

## Behavioral Architecture Decisions

_This section resolves behavioral specification gaps that affect safety, schema design, and implementation story correctness. These decisions must be read alongside the component definitions in Core Architectural Decisions and Project Structure._

---

### BAD-1: 15-Minute Peak Window Calculation

**Context:** The Belgian capaciteitstarief bills on the highest single 15-minute average consumption interval per calendar month. The architecture must distinguish between the billing record (authoritative, historical) and the operational control signal (real-time, predictive).

**Decision: Two distinct tracking mechanisms**

**1. Fixed clock-aligned 15-minute intervals — authoritative billing record**

Intervals are fixed and clock-aligned: 00:00–00:15, 00:15–00:30, etc. Each completed interval is stored as a `peak_intervals` row in `energy_repo`. This is the authoritative source for:
- Monthly peak reporting in the installer dashboard (FR19)
- Historical peak tracker (highest recorded interval vs. configured limit)

The `energy_repo` stores both:
- `energy_readings` — raw samples at poll interval (5–15 seconds per device per metric)
- `peak_intervals` — one row per completed 15-minute clock-aligned interval; calculated as the average power over all raw samples within that window

**2. Real-time partial-window projection — operational control signal**

The control loop maintains a `PartialIntervalTracker` in memory (within StateStore or control loop state). It tracks:
- The current interval start timestamp (last clock boundary)
- Running sum of power readings and sample count since that boundary
- Current projected average if draw continues at current rate to interval end

This projected average is the input to `peak_limiting.py`. It is **not** persisted as a billing record. It drives proactive decisions: if the projected average would exceed the configured peak limit before the interval closes, the engine acts now (reduce EV charge rate, stop battery discharge) rather than reacting after the fact.

**Invariant:** The billing record (peak_intervals) is never modified by control actions. The control signal (partial projection) is never used as a billing record. These two values have separate consumers and separate lifecycles.

**Schema addition:** `peak_intervals` table:
- `interval_start_utc` (PK) — clock-aligned interval boundary
- `avg_power_kw` — computed average over all samples in the interval
- `sample_count` — number of raw readings included
- `is_monthly_peak` — boolean flag, updated when a new monthly maximum is recorded

---

### BAD-2: Conservative Fallback Behavior Matrix

**Context:** FR13 requires the engine to transition to a conservative fallback mode when device communication is degraded or energy data is incomplete. The behavior must be deterministic and explicitly defined per degradation scenario.

**Decision: Explicit degradation matrix**

The control loop evaluates the current set of `DegradedDeviceState` objects in each cycle and applies the following rules in order:

| Degraded device | Immediate action | Continued operations |
|---|---|---|
| **Grid meter** | Enter conservative mode: stop issuing any command that increases controllable loads; no new EV charge commands; no battery discharge commands | Monitor for recovery; log `DegradedModeEvent`; basic battery maintenance may continue if battery SOC data is available |
| **Battery adapter** | Stop all battery charge/discharge commands immediately | Continue EV scheduling and peak management using grid meter data; peak limiting remains active without battery dispatch lever |
| **EV charger** | Stop all EV session commands; do not attempt reconnect mid-session | Continue battery control and peak management; grid meter peak tracking unaffected |
| **Inverter/PV adapter** | Drop PV production data from optimization layer | Continue peak/battery/EV control using grid meter data only; optimization quality reduced; log `DegradedModeEvent`; surface "Reduced capability" to installer and homeowner |
| **Forecast or tariff source** | Fall back to static time-of-use assumptions | Continue all core operations without change; log degraded optimization mode; do **not** enter fail-safe; surface to installer interface only |
| **Grid meter + any second critical adapter** | Enter full fail-safe | Stop issuing all new `DeviceCommand`s; abandon any pending queue commands; do not attempt to "undo" current device states (devices revert to their own safe defaults); log `RecoveryEvent` when healthy state is restored; surface error state to both installer and homeowner dashboards |
| **All adapters degraded** | Enter full fail-safe (as above) | — |

**Additional invariants:**

- Grid meter is the most critical device. Loss of real-time grid draw data removes the peak safety guarantee. Grid meter degradation always triggers conservative mode at minimum.
- Fail-safe is defined as: stop issuing new `DeviceCommand`s via `PolicyGuard.authorize_and_dispatch()`; no "reset to zero" commands are sent (devices handle their own safe defaults); all degraded state is logged to the audit log and exposed in the system health indicator.
- Recovery from fail-safe is **automatic**: on the next poll cycle where the previously-degraded device returns a healthy `DeviceState`, the control loop exits fail-safe and logs a `RecoveryEvent`. No manual intervention required.
- Recovery from conservative mode follows the same automatic pattern.
- The `services/readiness.py` readiness signal remains `ready` during degraded operation (the service is running); the system health indicator (`SystemStatus` field in `SystemState`) transitions to `degraded` or `fail_safe` and is visible in both UI roles.

**Implementation note:** The degradation matrix is implemented as a deterministic function in `control_loop.py` that maps the current set of `DegradedDeviceState` objects to a `SystemOperatingMode` enum (`normal` / `degraded` / `conservative` / `fail_safe`) before each evaluation cycle. Rules are applied in the order shown above.

---

### BAD-3: Constraint Change Validation Flow

**Context:** FR16 requires installer-defined safety constraints to be enforced unconditionally, and constraint changes to only become active after validation. This flow defines the exact sequence from change submission to activation.

**Decision: Staged validation with explicit installer confirmation**

```
1. Installer submits new constraint values (setup.html form)
         ↓
2. POST /api/constraints/draft
   → config_repo creates a DRAFT constraint record (not active)
   → draft does not affect PolicyGuard or control loop
         ↓
3. POST /actions/validate-constraints
   → ValidationService evaluates the DRAFT against:
       a. Schema / range validity (e.g. peak_limit_kw > 0)
       b. Device capability consistency (e.g. battery can achieve reserve floor)
       c. Immediate safety impact: would this constraint create an
          impossible or unsafe operating condition against current SystemState?
   → Returns ValidationReport (per-check pass / warn / fail + human-readable message)
         ↓
4. Installer reviews validation summary in setup.html
   ├── Any FAIL: change is rejected; activation blocked; installer must revise
   ├── Any WARN (no safety constraint violated): installer may proceed with explicit confirmation
   └── All PASS: installer confirms activation
         ↓
5. POST /actions/activate-constraints (requires prior validation pass or warn-with-confirm)
   → config_repo atomically swaps DRAFT to ACTIVE (versioned)
   → config_audit_repo records: actor, timestamp, field, previous_value, new_value, config_version
   → PolicyGuard reloads active constraint version on next evaluation cycle
   → DRAFT record cleared
         ↓
6. Control loop uses new constraints from next cycle onward
```

**Schema design:**
- `config_repo` holds: `active_constraints` (current, versioned) + `draft_constraints` (nullable, one at a time)
- `config_audit_repo` holds: one row per changed field per activation event (`config_version`, `changed_at`, `changed_by_user_id`, `field_name`, `previous_value`, `new_value`)
- `PolicyGuard` reads `active_constraints` at initialization and reloads on receipt of a `ConstraintsActivated` signal after step 5

**Constraint that cannot be activated:** if validation returns a FAIL result, the `POST /actions/activate-constraints` endpoint returns 409 with a structured error body. The draft remains staged but inactive. The installer must revise the draft and re-validate.

**Story 9.0b implementation note (2026-05-10):** Step 5's activation surface and step 6's "PolicyGuard reads active constraints" are implemented by `ActiveConstraintsProvider` (`open_ems.services.active_constraints`) and `ConfigRepo` (`open_ems.storage.repositories.config_repo`). `ConfigRepo.activate()` is a single `BEGIN IMMEDIATE` transaction that inserts a new row into `active_constraints` AND emits one `config_audit_log` row per changed field (delegating to `ConfigAuditRepo.append_activation` with `commit=False`), so the two tables can never disagree on `config_version`. The provider is hydrated once at lifespan startup (fail-loud `SystemExit(1)` on hydrate failure) and refreshed via `provider.reload()` after each activation commit — the reload is change-triggered, not TTL-based, because the activation endpoint is the only legitimate change source. `PolicyGuard` and `ControlLoop` share the same provider instance, eliminating the source-of-truth fragmentation the deferred Story 6.3 finding called out. The cold-start fallback (no DB row yet) seeds the in-memory snapshot from `Settings.peak_limit_kw` / `Settings.battery_reserve_floor_percent` with `config_version=0`; after the first installer activation those Settings fields are unread (enforced by `tests/unit/test_settings_seed_only.py`). Story 9.3 owns the draft / validate halves of the flow above.

---

### BAD-4: Deployment Validation Service

**Context:** FR17 requires a pre-handoff deployment validation that gives the installer an explicit pass/warn/fail result before walking away from a site. This must coordinate multiple subsystems without causing disruptive device behavior.

**Decision: Dedicated `ValidationService` in `services/validation.py`**

`ValidationService` is responsible exclusively for pre-deployment validation. It does not participate in the ongoing control loop.

**Validation checks performed (in order):**

| Check | Description | Failure mode |
|---|---|---|
| Device connectivity | Each registered adapter can communicate and return a `DeviceState` (not `DegradedDeviceState`) | FAIL if any required device is unreachable |
| Role assignment completeness | All required roles are assigned: at minimum `grid_meter` and one `energy_source` (inverter or battery) | FAIL if required role is missing |
| Capability-strategy consistency | Device capabilities support the configured strategy (e.g. "Maximize Self-Consumption" requires PV inverter data) | WARN if capability gap reduces optimization quality; FAIL if strategy is entirely unsupported |
| Constraint completeness | `peak_limit_kw` and `battery_reserve_floor_percent` are set, within valid ranges, and internally consistent | FAIL if missing or invalid |
| Constraint safety pre-check | Would active constraints create an impossible operating condition given current device states? | WARN if marginal; FAIL if unsafe |
| Control readiness | Adapters respond to a safe read-only capability probe (not a real command) | WARN if probe response is slow; FAIL if adapter rejects probe entirely |

**Coordination:**
- Reads current device states from the running adapters (via `StateStore.get_snapshot()` or direct adapter read where required)
- Reads active constraints from `ConfigRepository`
- Uses `PolicyGuard` for any check that would issue a command (control readiness check uses a no-op probe command type)
- Does **not** issue live device commands directly
- Does **not** modify `SystemState` or `ConfigRepository`

**Return type:** `ValidationReport`
- `overall_status`: `pass` / `warn` / `fail`
- `checks`: list of `ValidationCheckResult(name, status, message)`
- `human_summary`: one-paragraph plain-language explanation for installer

**Route:** `POST /actions/validate-deployment` → calls `ValidationService.run_deployment_validation()` → returns `ValidationReport` rendered in `installer/setup.html` with per-check status indicators.

---

## Architecture Validation Results

### Coherence Validation ✅

**Decision Compatibility:**

All technology choices are mutually compatible with no conflicts. Python 3.12+ asyncio + FastAPI + Jinja2 + HTMX + Alpine.js is an established combination. SQLite WAL + aiosqlite + Alembic is the standard async SQLite stack. All protocol libraries (pymodbus, Python ocpp, dsmr-parser) are asyncio-compatible. pydantic-settings and Pydantic v2 share the same ecosystem. uv + pyproject.toml has no dependency conflicts. Docker Compose and systemd are independent deployment paths around the same runtime.

**Pattern Consistency:**

snake_case naming propagates consistently across Python code, JSON API responses, and SQLite column names. StateStore boundary aligns with asyncio-first concurrency. PolicyGuard patterns align with centralized safety constraint decisions. Role enforcement via FastAPI Depends and role-separated Jinja2 template trees are structurally independent, consistent enforcement layers. SSE at 10-second intervals aligns with the 30-second state-freshness NFR. Import boundary rules enforce the component separation defined in decisions. No contradictions found.

**Structure Alignment:**

All 7 architectural subsystems map to concrete directories. Repository pattern enforces SQLite-as-single-store without exceptions. `core/` import boundary ensures domain models have no I/O dependencies. Versioned `register_maps/` per device model supports FR5. `services/validation.py` provides a clean, isolated home for deployment validation logic. `tests/fixtures/policy_scenarios.py` and `degradation_scenarios.py` together provide hardware-free testing coverage for all safety-critical paths.

---

### Requirements Coverage Validation ✅

**Functional Requirements — all 42 FRs covered:**

| FR Category | Status | Notes |
|---|---|---|
| Device Integration (FR1–FR6b) | ✅ | adapters/ + capabilities.py + device_repo |
| Energy Control / Engine (FR7–FR15) | ✅ | engine/ + policy_guard + CommandResult pattern |
| Installer Configuration (FR16–FR24) | ✅ | installer routes + config_repo + config_audit_repo; BAD-3 defines validation flow |
| Homeowner Interface (FR25–FR30) | ✅ | homeowner routes + SSE; FR30 explicitly nice-to-have |
| Observability & Reliability (FR31–FR34) | ✅ | watchdog + readiness + audit_log |
| Access Control & Security (FR35–FR39) | ✅ | auth + csrf + session_repo + HTTPS |
| External Data Integration (FR40–FR42) | ✅ | services/external/ + FR40/41 nice-to-have |
| Deployment Validation (FR17) | ✅ | services/validation.py; BAD-4 defines behavior |

**Non-Functional Requirements — all NFRs covered:**

All performance, reliability, security, integration, data, and maintainability NFRs have explicit architectural support. Key bindings:
- 5–15s control loop → asyncio task + non-blocking adapters + asyncio.wait_for
- Peak compliance → BAD-1 defines fixed-interval billing record + partial-window control signal
- Conservative fallback → BAD-2 defines degradation matrix per device
- Constraint enforcement → BAD-3 staged validation + PolicyGuard reload on activation
- 30-day uptime → systemd/Docker restart + watchdog recovery
- HTTPS, bcrypt, CSRF, session expiry → all defined and structurally enforced

---

### Gap Analysis Results

**All four previously identified important gaps are now resolved:**

| Gap | Resolution |
|---|---|
| 15-min peak window calculation | BAD-1: fixed clock-aligned intervals for billing; partial-window projection for control signal |
| Conservative fallback behavior | BAD-2: explicit degradation matrix per device; fail-safe defined with automatic recovery |
| Constraint change validation flow | BAD-3: staged draft → validate → explicit confirm → activate → audit |
| Deployment validation logic | BAD-4: dedicated `services/validation.py`; ValidationService with explicit check set |

**No critical or important gaps remain.**

Nice-to-have gaps (non-blocking, no architectural action required):
- Adapter auto-reconnect implementation detail (inferred from polling model; handled per-adapter)
- First-run initial-setup UI flow (handled by auth.py redirect on empty user table)
- Update mechanism implementation detail (appropriately deferred per PRD)

---

### Architecture Completeness Checklist

**Requirements Analysis**
- [x] Project context thoroughly analyzed
- [x] Scale and complexity assessed
- [x] Technical constraints identified
- [x] Cross-cutting concerns mapped

**Architectural Decisions**
- [x] Critical decisions documented with versions
- [x] Technology stack fully specified
- [x] Integration patterns defined
- [x] Performance considerations addressed

**Implementation Patterns**
- [x] Naming conventions established
- [x] Structure patterns defined
- [x] Communication patterns specified
- [x] Process patterns documented

**Project Structure**
- [x] Complete directory structure defined
- [x] Component boundaries established
- [x] Integration points mapped
- [x] Requirements to structure mapping complete

---

### Architecture Readiness Assessment

**Overall Status: READY FOR IMPLEMENTATION**

All 16 checklist items confirmed. All four behavioral gaps resolved and documented in the Behavioral Architecture Decisions section. No critical gaps remain.

**Confidence Level: High**

**Key Strengths:**
- Safety enforcement is centralized and structurally un-bypassable — no path reaches a device without passing through PolicyGuard
- All 42 FRs and all NFRs have explicit architectural homes
- Import boundary rules prevent unsafe dependency cycles before they form in code
- Testing strategy does not require physical hardware for any CI run
- Fail-safe and degraded-mode cascade is explicit, deterministic, and per-device
- Configuration audit trail is first-class: every constraint change is traceable to actor, time, version, and previous value
- 15-minute peak tracking correctly separates billing record from operational control signal — a correctness-critical distinction for Belgian tariff compliance

**Areas for Future Enhancement:**
- Dedicated time-series store if fleet/query volume grows in v2
- Automated TLS certificate renewal
- Remote update push and deployment automation (v2)
- Fleet management dashboard (v2 scope)
- Extended device compatibility library

---

### Implementation Handoff

**AI Agent Guidelines:**
- Follow all architectural decisions exactly as documented
- Read Behavioral Architecture Decisions (BAD-1 through BAD-4) before implementing any of: peak_limiting.py, control_loop.py, config_repo.py, or services/validation.py
- Use implementation patterns consistently across all components
- Respect import boundaries — violations create safety bypass risks
- Refer to this document for all architectural questions

**First Implementation Priority:**

```
uv init open-ems
uv add fastapi uvicorn pydantic pydantic-settings aiosqlite alembic \
       pymodbus ocpp dsmr-parser structlog cryptography
uv add --dev pytest pytest-asyncio ruff mypy
```

Then follow the implementation sequence:
1. SQLite schema + Alembic 0001 migration (all v1 tables including `peak_intervals`)
2. `core/` models, enums, commands, capabilities, StateStore
3. `engine/policy_guard.py` skeleton with unit tests (policy_scenarios.py)
4. Simulated adapters + degradation_scenarios.py fixtures
5. Protocol adapters (Modbus → Battery/Inverter → DSMR → OCPP)
6. Decision engine rules (peak_limiting → battery_control → ev_scheduling → energy_balancing)
7. Control loop + degradation matrix (BAD-2)
8. Web auth layer (auth.py + dependencies.py + csrf.py + session_repo)
9. Installer web routes + templates
10. Homeowner web routes + templates + SSE stream
11. services/validation.py (BAD-4) + constraint validation flow (BAD-3)
12. Watchdog + readiness + health endpoints
13. CI/CD + Docker multi-arch build
