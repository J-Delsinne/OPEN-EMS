---
stepsCompleted: ["step-01-validate-prerequisites", "step-02-design-epics", "step-03-create-stories"]
inputDocuments:
  - "_bmad-output/planning-artifacts/prd.md"
  - "_bmad-output/planning-artifacts/architecture.md"
  - "_bmad-output/planning-artifacts/ux-design-specification.md"
---

# OPEN-EMS - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for OPEN-EMS, decomposing the requirements from the PRD, Architecture, and UX Design Specification into implementable stories organized by user value.

## Requirements Inventory

### Functional Requirements

**Device Integration and Management**

- FR1: The installer can discover and connect to energy devices on the local network via Modbus TCP and OCPP 1.6, and connect to DSMR P1 smart meters as a real-time grid data source
- FR2: The installer can assign an energy role (inverter, battery, EV charger, grid meter) to each discovered device
- FR3: The system can detect whether a discovered device supports full or partial integration capability
- FR4: The system can operate in reduced-capability mode when a device's integration is partial, while continuing to enforce safety constraints using available data
- FR5: The system maintains a versioned capability profile per supported device model and firmware version, validated through read and write/control testing before listing as supported
- FR6: The installer can view the integration capability status and any active limitations for each connected device
- FR6b: The system does not expose unsupported or unvalidated device capabilities to the decision engine

**Energy Control and Decision Engine**

- FR7: The system continuously evaluates energy state on a fixed cycle and issues control actions within installer-defined safety boundaries
- FR8: The system enforces a configurable peak consumption limit by controlling battery discharge and EV charge rate
- FR9: The system controls battery charge and discharge state in accordance with installer-defined constraints and the active energy strategy
- FR10: The system manages EV charging session start, stop, and — where supported by the charger — dynamic charge rate adjustment
- FR11: The system applies a homeowner-selected energy strategy (Minimize Cost / Maximize Self-Consumption / Prioritize EV) to determine control priorities
- FR11b: The system resolves conflicting energy demands using a deterministic priority model derived from the active strategy and installer-defined constraints
- FR12: The system respects a configurable EV charging window preference while permitting homeowner overrides bounded by active safety constraints
- FR13: The system transitions to a conservative fallback mode when device communication is degraded or energy data is incomplete, while maintaining safety constraints
- FR14: The system verifies command acknowledgment before assuming a control action has taken effect
- FR15: The system never silently ignores a constraint violation, command failure, or degraded state — all such conditions are logged and surfaced to the installer

**Installer Configuration and Management**

- FR16: The installer can configure per-site safety constraints (peak consumption limit, battery reserve floor) and the preferred EV charging window
- FR17: The installer can validate device communication and constraint configuration before completing deployment
- FR18: The installer can view a system health indicator showing overall system status (healthy / degraded / error) and per-device connectivity state
- FR19: The installer can view current-month peak consumption data (highest 15-minute interval recorded) against the configured limit
- FR20: The installer can access a timestamped event log of control decisions, constraint enforcement events, command failures, degraded-mode transitions, and recovery events
- FR21: The installer can add documentation notes to the event log
- FR22: The installer can check for available platform updates and trigger an update with explicit approval, with release notes visible before confirming
- FR23: The installer can create and reset homeowner credentials
- FR24: The installer can export a site configuration backup and restore it to the same or a replacement system without exposing plaintext credentials

**Homeowner Energy Interface**

- FR25: The homeowner can select an energy strategy from three options: Minimize Cost, Maximize Self-Consumption, or Prioritize EV
- FR26: The homeowner can view the current system state: battery charge level, solar production, and grid draw
- FR27: The homeowner can view the next scheduled EV charging session
- FR28: The homeowner can trigger an immediate EV charging session that overrides scheduled timing, while remaining subject to active safety constraints and dynamic charge rate adjustments if required
- FR29: The homeowner can view a simple system status indicator providing a plain-language indication when the system is operating with reduced capability, with no technical detail exposed
- FR30 *(nice-to-have)*: The homeowner can view a weekly energy summary: peaks avoided, self-consumption ratio, and estimated cost savings

**System Observability and Reliability**

- FR31: The system automatically recovers to normal operation after a reboot or temporary network interruption without manual intervention
- FR32: The system detects stalled control loops or unresponsive subsystems and triggers automatic recovery (service restart or transition to fail-safe mode) depending on failure type
- FR33: The system logs all automatic recovery and watchdog-triggered events with timestamps, visible to the installer
- FR34: The system transitions to fail-safe mode (ceases issuing control commands; relies on device-level safety) when device communication is lost, and logs the degraded state

**Access Control and Security**

- FR35: Users authenticate with role-based local credentials before accessing any system functionality — Installer/Admin and Homeowner/User roles are distinct
- FR36: The Installer/Admin role has access to device configuration, constraint management, system logs, update management, and homeowner account management
- FR37: The Homeowner/User role has access only to strategy selection, system status, EV override, and degraded-mode indication
- FR38: The system enforces session expiry for both roles, with Installer/Admin sessions requiring re-authentication after extended inactivity
- FR39: The system serves the web interface over HTTPS; any HTTP access during development is explicitly marked as development-only

**External Data Integration**

- FR40 *(nice-to-have)*: The system can retrieve solar production forecasts from a configurable external API and use them to improve optimization decisions
- FR41 *(nice-to-have)*: The system can consume dynamic tariff data from a configurable external feed and use it for cost-optimization decisions
- FR42: The installer can view when the optimization layer is operating in degraded mode due to unavailability of external forecast or tariff data

### NonFunctional Requirements

**Performance**

- NFR-P1: Decision engine evaluation cycle executes at 5–15 seconds under normal conditions; under degraded connectivity, must complete within 60 seconds before triggering watchdog recovery
- NFR-P2: Control actions applied within 30 seconds from decision to observable device state change under normal conditions
- NFR-P3: Web interface pages load and are interactive within 3 seconds on Raspberry Pi 4 under normal local network conditions
- NFR-P4: Device state data in interfaces reflects actual device state within 30 seconds of a confirmed device state change
- NFR-P5: Event log queries covering the past 30 days return results within 5 seconds

**Reliability**

- NFR-R1: System operates without unhandled crashes or forced restarts for a minimum of 30 consecutive days under normal operating conditions
- NFR-R2: System must never violate configured safety constraints due to internal logic errors
- NFR-R3: After reboot or power loss, system returns to full operational state within 2 minutes (all device connections re-established, decision engine running)
- NFR-R4: After temporary network interruption, system reconnects to devices and resumes control within 60 seconds of network restoration
- NFR-R5: Stalled control loops detected and watchdog recovery triggered within 2 consecutive missed evaluation cycles

**Security**

- NFR-S1: Credentials stored using bcrypt; no plaintext credentials stored at rest
- NFR-S2: Configuration backups must not contain plaintext credentials or secrets
- NFR-S3: All web interface traffic via HTTPS in production; self-signed certificates acceptable for v1
- NFR-S4: Session tokens use minimum 128 bits of entropy via cryptographically secure random generator
- NFR-S5: Installer/Admin sessions expire after configurable inactivity (recommended default: 4 hours)
- NFR-S6: Homeowner/User sessions expire after configurable inactivity (recommended default: 7–30 days)
- NFR-S7: Local energy data not transmitted externally except for explicitly configured optional API calls (HTTPS only)

**Integration**

- NFR-I1: Modbus TCP and OCPP connection attempts timeout within 10 seconds; timeouts handled without blocking the control loop
- NFR-I2: Failed control commands may be retried up to 2 times, provided retries do not risk safety constraint violations or oscillating behavior
- NFR-I3: DSMR P1 data older than 60 seconds treated as stale; triggers conservative control assumptions in the decision engine
- NFR-I4: External API calls (solar forecast, dynamic tariff) timeout within 10 seconds; no impact on core control operation

**Data and Storage**

- NFR-D1: Energy state history retained locally for a minimum of 12 months
- NFR-D2: Event log entries retained locally for a minimum of 90 days
- NFR-D3: Event log entries for failures, constraint enforcement, and recovery events never pruned below minimum retention
- NFR-D4: Configuration and event log data written crash-safely (atomic writes or journaling via SQLite WAL)
- NFR-D5: Platform updates preserve all site configuration and energy history without data loss or silent migration failure
- NFR-D6: No energy or personal data transmitted externally outside explicitly configured optional API integrations

**Maintainability and Deployability**

- NFR-M1: Fresh installation on Raspberry Pi 4 completable in under 30 minutes following documented procedure
- NFR-M2: Site configuration export and restore completable in under 10 minutes following documented procedure
- NFR-M3: Installation process reproducible across sites using the same documented steps and package versions; no site-specific system configuration required beyond device onboarding and constraint setup

### Additional Requirements

Architecture-derived requirements that directly affect epic and story scope:

- AR1: Project initialized with `uv` and the specified dependency set (fastapi, uvicorn, pydantic, pydantic-settings, aiosqlite, alembic, pymodbus, ocpp, dsmr-parser, structlog, cryptography, pytest, pytest-asyncio, ruff, mypy)
- AR2: Dual deployment paths required: Docker Compose (primary) and systemd native (secondary) — both fully documented and functional
- AR3: GitHub Actions CI pipeline: ruff (lint/format), mypy (type check), pytest (unit + integration), docker buildx (multi-arch amd64+arm64) on every push and tagged release
- AR4: Self-signed TLS certificate generated at install time via `scripts/generate-tls.sh` (CN/SAN matching local hostname); installer documentation for browser trust warning
- AR5: Alembic migrations run automatically on every service startup before adapters or control loop initialize; migration failure is fatal
- AR6: Staged constraint validation flow (BAD-3): installer submits draft → system validates (schema, capability, safety impact) → installer confirms → atomic activation with config audit record; failed validation blocks activation
- AR7: 15-minute peak tracking uses two distinct mechanisms: (1) fixed clock-aligned intervals written to `peak_intervals` table (authoritative billing record); (2) in-memory partial-window projection used as real-time control signal in peak_limiting.py — these are separate and must never be conflated
- AR8: Degradation matrix (BAD-2) defines deterministic behavior per degraded device class; control_loop.py maps current DegradedDeviceState set to SystemOperatingMode (normal / degraded / conservative / fail_safe) before each evaluation cycle
- AR9: `services/validation.py` implements DeploymentValidationService with 6 check types (connectivity, role completeness, capability-strategy consistency, constraint completeness, constraint safety pre-check, control readiness); returns typed ValidationReport
- AR10: Configuration backup exports YAML artifact excluding all credentials; restore must be tested and documented; `storage/backup.py` owns this
- AR11: SSE state stream (`/api/stream/state`) is role-filtered at serialization: homeowner payload excludes installer diagnostics; installer payload may include degraded-mode details
- AR12: CSRF protection required on all state-changing routes; httpOnly Secure SameSite=Strict session cookies; CSRF token validated server-side before processing
- AR13: `/health/live` and `/health/ready` endpoints required; systemd sd_notify READY=1 / WATCHDOG=1 integration for native installs; Docker healthcheck uses `/health/ready`
- AR14: StateStore maintains immutable snapshots; all device state published through StateStore only; no component mutates snapshots
- AR15: PolicyGuard.authorize_and_dispatch() is the single required path for all device commands; no adapter may be called directly from any other component
- AR16: All adapter send_command() implementations must return a typed CommandResult (correlation_id, device_id, status, applied, reason, observed_state)
- AR17: Two logging sinks: structlog JSON to stdout (process/operational log) + SQLite event_log table with human-readable description (installer-visible audit log); device errors causing degraded mode go to both
- AR18: Time synchronization check on startup; NTP drift detection; if clock is unavailable or suspect, tariff/peak-window optimization enters degraded mode and condition is surfaced to installer
- AR19: All constraint changes recorded in `config_audit_log` table with: actor, timestamp, field, previous_value, new_value, config_version
- AR20: All database access through repository classes in `storage/repositories/` only; no raw SQL outside that package

### UX Design Requirements

UX Design Specification: `_bmad-output/planning-artifacts/ux-design-specification.md`

The UX design specification is complete and covers both user personas (Installer "Lien" and Homeowner "Marc"), 5 journey flows (installer first-time setup, degraded mode setup, homeowner daily glance, EV override, post-deployment operations check), 17 custom components, the design system (Tailwind pre-compiled + CSS custom properties + Jinja2 macros), and an implementation roadmap in 4 phases. All UI requirements in this epic breakdown are derived from or consistent with the UX design specification.

### FR Coverage Map

| FR | Epic | Coverage |
|---|---|---|
| FR1 | 3 + 4 + 9 | Protocol-level discovery (3); device normalization and scan (4); discovery UI wizard step 1 (9) |
| FR2 | 9 | Role assignment wizard step 2 |
| FR3 | 4 + 9 | Capability detection and DegradedDeviceState (4); capability UI display with FULL/REDUCED badges (9) |
| FR4 | 4 + 9 | Reduced-capability adapter mode (4); reduced-capability UI with specific condition explanation (9) |
| FR5 | 4 | Versioned capability profile per device model/firmware in device abstraction layer |
| FR6 | 9 | Capability status and active limitation display in installer setup wizard |
| FR6b | 4 | Capability gate — unvalidated/unsupported capabilities not exposed to decision engine |
| FR7 | 7 | Decision engine evaluation cycle (decision logic only; runtime loop is Epic 8) |
| FR8 | 7 | Peak limiting rule + dual tracking mechanism (AR7): peak_intervals table + PartialIntervalTracker |
| FR9 | 7 | Battery control rule within installer-defined constraints and active strategy |
| FR10 | 7 | EV session management: start, stop, dynamic rate adjustment where supported |
| FR11 | 7 | Energy strategy application: Minimize Cost / Maximize Self-Consumption / Prioritize EV |
| FR11b | 7 | Deterministic conflict resolution: constraints > optimization > convenience |
| FR12 | 7 | EV charging window preference with homeowner override bounds |
| FR13 | 4 + 8 | DegradedDeviceState returned by adapters (4); conservative fallback SystemOperatingMode enforcement (8) |
| FR14 | 8 | Command acknowledgment verification via CommandResult before assuming effect |
| FR15 | 6 | No-silent-failures contract enforced at event log write layer — every failure is logged |
| FR16 | 9 | Constraint configuration wizard step 3 with inline validation and staged flow (AR6) |
| FR17 | 9 | Deployment validation wizard step 4: 6 check types, per-check PASS/WARN/FAIL/TIMEOUT (AR9) |
| FR18 | 11 | System health indicator reads SystemOperatingMode from StateStore — display only |
| FR19 | 11 | Current-month peak consumption display against configured limit |
| FR20 | 6 + 11 | Event log schema and storage (6); event log UI with filtering (11) |
| FR21 | 6 + 11 | Installer note storage layer (6); note entry UI (11) |
| FR22 | 12 | Platform update management: check, view release notes, confirm, apply |
| FR23 | 11 | Homeowner credential creation and reset — installer settings UI |
| FR24 | 12 | Site configuration export (YAML, no credentials) and restore (AR10) |
| FR25 | 10 | Strategy selector: inline expansion, no modal, immediate POST, HTMX partial update |
| FR26 | 10 | Current system state display: battery, solar, grid metric cards |
| FR27 | 10 | Next scheduled EV session display on EV card |
| FR28 | 10 | EV override: single tap, 4-state optimistic UI (Idle/Optimistic/Confirmed/Fallback) |
| FR29 | 10 + 11 | Homeowner degraded display: calm slate-600 headline + inline explanation (10); installer degraded view with WARN badges (11) |
| FR30 | 10 | Weekly energy summary (nice-to-have — must not block homeowner dashboard MVP) |
| FR31 | 4 + 12 | Adapter automatic reconnection after network interruption (4); system restart recovery validation (12) |
| FR32 | 8 | Watchdog: missed-cycle counter, stalled loop detection, automatic recovery trigger |
| FR33 | 6 + 8 | Recovery event storage (6); watchdog triggers and logs recovery events (8) |
| FR34 | 8 | Fail-safe mode on device communication loss: ceases commands, logs degraded state |
| FR35 | 2 | Role-based authentication: distinct Installer/Admin and Homeowner/User credentials |
| FR36 | 2 | Installer/Admin role access scope enforcement |
| FR37 | 2 | Homeowner/User role access scope enforcement |
| FR38 | 2 | Session expiry: configurable inactivity timeout for both roles |
| FR39 | 2 | HTTPS enforcement; HTTP marked dev-only |
| FR40 | 13 | Solar forecast API integration (nice-to-have) |
| FR41 | 13 | Dynamic tariff data feed integration (nice-to-have) |
| FR42 | 13 | Degraded optimization mode surfaced to installer when external sources unavailable |

## Epic List

### Epic 1: Platform Foundation

A runnable, health-checked OPEN-EMS instance deploys on Raspberry Pi 4 via Docker Compose or systemd, with database auto-initialized, TLS generated, CI pipeline passing, and the repository pattern in place as the foundation for all subsequent data access.

**User outcome:** The system exists, deploys repeatably, and passes health checks. Every subsequent epic builds on this without rework.

**FRs/ARs covered:** AR1, AR2, AR3, AR4, AR5, AR13, AR18, AR20, NFR-M1, NFR-M3, NFR-D4, NFR-S3, NFR-D6 (default — no external data transmission), NFR-R3 (partial — startup sequence established here)

**Scope:**
- `uv` project initialization with full specified dependency set (AR1)
- Docker Compose deployment with multi-arch build (amd64 + arm64) (AR2, AR3)
- systemd native deployment with service unit files (AR2)
- GitHub Actions CI: ruff lint/format, mypy type check, pytest unit + integration, docker buildx (AR3)
- TLS certificate generation: `scripts/generate-tls.sh` with CN/SAN matching local hostname (AR4)
- Alembic migration framework: auto-runs on startup before any adapter or control loop initializes; migration failure is fatal (AR5)
- `/health/live` and `/health/ready` endpoints; systemd sd_notify READY=1; Docker healthcheck on `/health/ready` (AR13)
- NTP drift detection on startup; clock suspect condition surfaced (AR18)
- Repository pattern: all DB access via `storage/repositories/` only; no raw SQL outside that package (AR20)
- SQLite WAL mode for crash-safe writes (NFR-D4)
- FastAPI app skeleton: `web/app.py`, lifespan management, startup sequence ordering

---

### Epic 2: Authentication and Access Control

Installers and homeowners log in with role-based credentials, are routed directly to their role's interface, and sessions expire safely according to configured inactivity timeouts with CSRF protection on all state-changing routes.

**User outcome:** Lien logs in and lands on the installer interface. Marc logs in and lands on the homeowner dashboard. Neither can access the other's interface. Sessions expire automatically.

**FRs covered:** FR35, FR36, FR37, FR38, FR39, AR12, NFR-S1, NFR-S3, NFR-S4, NFR-S5, NFR-S6

**Scope:**
- Installer/Admin and Homeowner/User role definitions with distinct access scopes (FR35, FR36, FR37)
- bcrypt credential storage — no plaintext credentials at rest (NFR-S1)
- Login form with return URL: after login, user returns to originally requested URL (not generic home screen)
- Role-based routing: successful login routes directly to role's home view
- Server-side sessions: SQLite `sessions` table, opaque token with 128-bit entropy via `secrets` (NFR-S4)
- httpOnly Secure SameSite=Strict session cookies (AR12)
- CSRF protection on all state-changing routes; CSRF token validated server-side (AR12)
- Session expiry: Installer/Admin configurable inactivity (default 4h, NFR-S5); Homeowner/User configurable (default 7–30d, NFR-S6) (FR38)
- 401 redirect to `/login?next=[originally requested URL]` on session expiry during active interaction
- HTTPS enforcement: HTTP marked dev-only (FR39, NFR-S3)
- FastAPI `Depends(require_installer)` and `Depends(require_homeowner)` as primary role boundary

---

### Epic 3: Protocol Adapters

Raw communication adapters for Modbus TCP, OCPP 1.6, and DSMR P1 — protocol framing, connection management, and timeout enforcement only. No domain logic, no device capability modeling.

**User outcome:** The system can send bytes to and receive bytes from inverters, batteries, EV chargers, and DSMR smart meters within timeout bounds. Adapter failures are represented, not raised.

**FRs covered:** FR1 (protocol level), NFR-I1, NFR-I3 (DSMR data timestamping), AR16 (CommandResult type defined here)

**Scope:**
- Modbus TCP adapter: `pymodbus` async client, raw register read/write, connection management per device
- OCPP 1.6 Central System: WebSocket server, chargers connect in (not out), OCPP message framing and dispatch
- DSMR P1 adapter: `dsmr-parser`, serial/USB read, telegram timestamping for staleness detection
- Connection timeout enforcement: 10 seconds for all protocol connection attempts (NFR-I1)
- DSMR data timestamp recorded for staleness evaluation in Epic 4 (NFR-I3 precondition)
- `CommandResult` type defined: `correlation_id`, `device_id`, `status`, `applied`, `reason`, `observed_state` (AR16)
- `DeviceAdapter` Protocol (structural subtyping): interface contract all adapters implement
- No domain logic — pure protocol translation

---

### Epic 4: Device Abstraction Layer

Normalizes raw protocol data into typed DeviceState models, detects and classifies capability limitations per device model and firmware version, gates unvalidated capabilities from the decision engine, and reconnects automatically after network interruptions.

**User outcome:** The system has typed, normalized state from every device. Capability limitations are detected and represented as `DegradedDeviceState`. Reconnection is automatic and does not require manual intervention.

**FRs covered:** FR1 (device discovery scan), FR3, FR4, FR5, FR6b, FR13 (DegradedDeviceState), FR31 (reconnection), NFR-R4, NFR-I3 (stale treatment)

**Scope:**
- `DeviceState` typed models: `InverterState`, `BatteryState`, `EVChargerState`, `GridMeterState`
- `DegradedDeviceState`: returned (never raised) by adapters on recoverable failures; carries `device_id` and `reason`
- Device discovery scan: Modbus TCP network scan, OCPP Central System listen, DSMR port detection (FR1 at adapter level)
- Versioned capability profile per supported device model and firmware version (FR5)
- Capability gate: unvalidated or unsupported capabilities not exposed to engine; `FR6b` enforced here
- DSMR data older than 60 seconds treated as stale; triggers conservative control assumptions (NFR-I3)
- Automatic reconnection after network interruption: exponential backoff, reconnects within 60 seconds of network restoration (FR31, NFR-R4)
- `adapters/register_maps/` per device model for Modbus; OCPP profile definitions for EV chargers

---

### Epic 5: State Aggregation and Real-Time Distribution

StateStore maintains the canonical, immutable system state snapshot aggregating all device states. The system state model defines all valid global and per-component states. SSE and HTMX polling serve role-filtered live state to both UIs.

**User outcome:** Any component reads a consistent, immutable view of the system from a single source. The homeowner and installer UIs receive live state updates that reflect device reality within 30 seconds, via SSE or polling with no visible difference.

**FRs covered:** AR14, AR11, NFR-P4

**Scope:**
- **System State Model** (single authoritative definition):
  - Global system states: `NORMAL` / `DEGRADED` / `STALE` / `FAILED`
  - Per-component states: `IDLE` / `PENDING` / `ACTIVE` / `ERROR` / `UNAVAILABLE`
  - `SystemOperatingMode` enum: `normal` / `degraded` / `conservative` / `fail_safe` (consumed by Epic 7 + 8)
- `StateStore`: maintains immutable `SystemSnapshot`; all device state published through StateStore only; no component reads adapters directly (AR14)
- UI components must not infer system state from raw adapter data — they read StateStore snapshots only
- SSE stream (`/api/stream/state`): role-filtered at serialization; homeowner payload excludes installer diagnostics; installer payload may include degraded-mode details (AR11)
- HTMX polling endpoints: serve identical server-rendered fragments as SSE at same interval; silent fallback, no visible difference
- Stale detection logic: marks data age per device; triggers `STALE` per-component state flag
- NFR-P4: state data in interfaces reflects device state within 30 seconds of a confirmed device state change

---

### Epic 6: Observability and Event Logging

Dual logging infrastructure with a defined event contract: structured JSON to stdout (operational) and SQLite `event_log` table (installer audit). Event schema, append-only guarantees, plain-language summary requirement, actor attribution, and retention enforcement are established here. All subsequent epics write to this; Epic 11 reads from it.

**User outcome:** Every system decision, constraint enforcement, failure, and recovery event is permanently recorded with a plain-language description, timestamp, and actor attribution. No event is silently dropped. The no-silent-failures contract is enforced at the write layer.

**FRs covered:** FR15, FR20 (storage), FR21 (note storage), FR33 (recovery event storage), AR17, NFR-D2, NFR-D3

**Scope:**
- **Event Log Contract**:
  - `event_log_entry` schema: `id`, `timestamp`, `actor` (system/installer/homeowner), `event_type` (DECISION/DEVICE/SYSTEM/CONSTRAINT/INSTALLER), `summary` (plain language, required, never empty), `detail` (optional JSON), `device_id` (nullable), `config_version` (nullable)
  - Append-only guarantee: entries never updated or deleted; pruning only via retention policy
  - Plain-language `summary` required for all system-generated entries — no register names, no error codes
  - Actor/source attribution for every entry: `system` / `installer` / `homeowner`
- Dual logging sinks: `structlog` JSON to stdout (process/operational log) + SQLite `event_log` table (AR17)
- Device errors causing degraded mode go to both sinks
- Installer note storage (FR21 storage layer): `actor=installer`, `event_type=INSTALLER`, freetext summary
- No-silent-failures contract: enforced at event log write layer — constraint violations, command failures, degraded transitions are all required entries (FR15)
- Recovery event storage (FR33 storage layer): watchdog recovery and fail-safe transitions logged here
- Event log retention: 90-day minimum (NFR-D2); failure, constraint enforcement, and recovery entries never pruned below minimum (NFR-D3)
- **Config audit table**: `config_audit_log` schema introduced here — `actor`, `timestamp`, `field`, `previous_value`, `new_value`, `config_version` (AR19 table) — consumed by Epic 9 staged constraint validation

---

### Epic 7: Decision Engine

The decision engine evaluates StateStore snapshots and produces structured intents on each evaluation cycle. It does not dispatch commands. Pure decision logic — testable without hardware, without PolicyGuard, without the runtime loop.

**User outcome:** The system knows what it wants to do at every evaluation cycle. Strategy selection affects behavior. Peak windows are respected. Conflicts are resolved deterministically. All decisions are deterministic and reproducible from the same snapshot inputs.

**FRs covered:** FR7 (evaluation logic), FR8, FR9, FR10, FR11, FR11b, FR12, AR7, AR8, NFR-P1, NFR-R2 (constraint modeling in intent)

**Scope:**
- Evaluates immutable `SystemSnapshot` from StateStore — no direct adapter access
- Energy strategy evaluation: Minimize Cost / Maximize Self-Consumption / Prioritize EV (FR11)
- Deterministic conflict resolution: constraints > optimization > convenience preferences (FR11b)
- Peak limiting logic: reads in-memory `PartialIntervalTracker` (real-time control signal) and `peak_intervals` table (billing record) — two distinct mechanisms, never conflated (FR8, AR7)
- Battery control logic: charge/discharge decisions within installer-defined constraints (FR9)
- EV scheduling logic: respects charging window preference; homeowner override bounds enforced (FR10, FR12)
- Energy balancing: solar, battery, grid coordination to produce coherent intent
- `SystemOperatingMode` determination from current `DegradedDeviceState` set via degradation matrix (AR8) — runs before each evaluation cycle
- Produces typed structured intents (not `Command` objects) — runtime loop in Epic 8 consumes these
- NFR-P1: evaluation cycle 5–15 seconds under normal conditions; must complete within 60 seconds under degraded connectivity before triggering watchdog

---

### Epic 8: Control Execution and Safety Enforcement

The runtime control loop orchestrates evaluation cycles, translates decision engine intents into device commands, and dispatches all commands exclusively through PolicyGuard. Handles retries, timeouts, acknowledgment verification, watchdog heartbeat, and fail-safe transition.

**User outcome:** Device commands are issued safely, retried safely, and acknowledged before being considered applied. Safety constraints cannot be bypassed by any code path. Stalled loops are detected and recovered automatically. Failures degrade to fail-safe, not silence.

**FRs covered:** FR13 (conservative fallback enforcement), FR14, FR32, FR33 (watchdog logging), FR34, AR15, AR16 (CommandResult enforcement), NFR-I2, NFR-P2, NFR-R2, NFR-R5

**Scope:**
- **Runtime control loop** (orchestration):
  - Control loop scheduler: asyncio task managing evaluation cycle timing
  - Intent consumption: structured intents from Epic 7 decision engine consumed here
  - Intent-to-command translation: maps typed intents to typed `Command` objects
  - `PolicyGuard.authorize_and_dispatch()`: single required path for all device commands; no adapter called directly from any other component (AR15)
  - Watchdog heartbeat: `sd_notify WATCHDOG=1` (systemd native) / `/health/ready` 200 response (Docker); missed cycles counted
- `CommandResult` contract enforcement: all `send_command()` implementations must return typed `CommandResult`; raw exceptions not permitted to escape adapters (AR16)
- Idempotency: one command issued per intent per evaluation cycle; no duplicate dispatch
- Retry policy: up to 2 retries on failure; no retry if retry risks safety violation or oscillating behavior (NFR-I2)
- Timeout handling: command dispatch timeouts within bounds (NFR-I1)
- Command acknowledgment verification: `CommandResult.applied` checked before assuming effect (FR14)
- Fail-safe mode: ceases issuing all control commands on device communication loss; relies on device-level safety mechanisms; logs degraded state (FR34)
- Watchdog: missed-cycle counter; stalled loop detection; automatic recovery trigger within 2 consecutive missed evaluation cycles (FR32, NFR-R5)
- All watchdog recovery and fail-safe transitions logged via Epic 6 event log (FR33)
- NFR-P2: control actions applied within 30 seconds from decision to observable device state change

---

### Epic 9: Installer Site Setup and Deployment Validation

The installer completes the 4-step setup wizard against the live, running system — device discovery, role assignment, constraint configuration, and deployment validation — and receives an unambiguous PASS/WARN/FAIL result before handoff. All protocol adapters, device abstraction, state layer, decision engine, and control execution are in place before this epic.

**User outcome:** Lien configures a new site against real devices, runs deployment validation, reads "System is ready," and hands off with professional confidence. The system she validates is the system that will run.

**FRs covered:** FR1 (discovery UI), FR2, FR3 (UI), FR4 (UI), FR6 (UI), FR16, FR17, AR6, AR9, AR19

**Dependency note:** `config_audit_log` table is introduced in Epic 6 (Observability). Epic 9 consumes it for staged constraint validation audit records (AR19). Config persistence foundations exist before this UI epic.

**Scope:**
- 4-step setup wizard with persistent step indicator (Discovery / Roles / Constraints / Validation):
  - **Step 1 — Device Discovery**: auto-scan results with FULL/REDUCED/NOT FOUND capability flags; specific condition explanation inline (not modal); "Retry scan" and "Add manually" advanced path for NOT FOUND rows (FR1 UI, FR3 UI, FR6 UI)
  - **Step 2 — Role Assignment**: role picker per discovered device (inverter / battery / EV charger / grid meter / unassigned); inline conflict detection; role gap warning without modal (FR2)
  - **Step 3 — Constraint Configuration**: peak limit, battery reserve floor, preferred EV window; inline validation on blur; staged constraint validation flow — draft → validate → confirm → atomic activation (FR16, AR6); config audit log records written on activation (AR19)
  - **Step 4 — Deployment Validation**: concurrent check dispatch with progressive display (each check: pending → PASS/WARN/FAIL/TIMEOUT); 6 typed check types via `DeploymentValidationService` (AR9); overall status banner; handoff button activation state
- Validation state model: `never_run` / `running` / `complete-PASS` / `complete-WARN` / `complete-FAIL` / `outdated` (configuration change after completion marks validation outdated and disables handoff)
- REDUCED capability display: FULL/REDUCED badges with specific condition and inline two-path choice (continue in reduced mode / halt and investigate)
- TIMEOUT check result: WARN severity with explicit TIMEOUT label and connectivity-focused corrective action
- Re-run validation without losing other resolved check results
- **Language scope:** Multilingual support (nl/fr) is explicitly out of scope for v1 and planned for v2 (NFR-L1, NFR-L2).

---

### Epic 10: Homeowner Dashboard and Controls

The homeowner dashboard with card stack, real-time SSE updates with HTMX polling fallback, 4-state optimistic EV override, inline strategy selector, stale data handling, and calm degraded-mode display. FR30 weekly summary is nice-to-have and must not block MVP completion.

**User outcome:** Marc reads current system state in under 10 seconds, taps "Charge EV now" with one tap and sees immediate feedback, switches strategy without friction. Degraded-mode state is calm and visible without requiring action.

**FRs covered:** FR25, FR26, FR27, FR28, FR29, FR30 (nice-to-have — must not block MVP), NFR-P3, NFR-P4

**Scope:**
- Status headline card: "Your home is running on solar · Minimize Cost" (normal) / "Running with limited functionality" (degraded, slate-600, calm inline explanation, no amber) (FR29)
- Metric cards: battery, solar, grid — plain-language values; stale data caption "last updated X min ago" in slate-400 at stable position; disappears when updates resume (FR26)
- EV status card + override button — **4-state model** (FR28):
  - Idle: "Charge EV now" — accent background, enabled
  - Optimistic: "Starting charging…" — accent background, pulse indicator, disabled (immediate on tap)
  - Confirmed: "Charging now" — accent-hover background, checkmark icon, disabled; constraint notice in neutral styling below
  - Fallback: "Could not start charging" — degraded-bg, disabled; "Try again" link; calm explanation
- Button idempotency: disabled on first tap; one POST issued; never repositions or resizes across states
- Strategy selector: inline expansion (no modal), three option rows, immediate POST `/actions/set-strategy`, HTMX partial update of headline; failure preserves previous strategy (FR25)
- **Optimistic UI handling**: Alpine.js state machine manages transitions; server state is authoritative; Alpine state is the optimistic layer only
- **SSE-driven updates with HTMX polling fallback**: same server-rendered fragments for both paths; no visible difference; no loading indicators (AR11)
- **State synchronization rule**: UI reads `SystemOperatingMode` from StateStore snapshots only; does not infer state from raw adapter data
- Next scheduled EV session visible without scrolling on standard phone (FR27)
- FR30 weekly energy summary: nice-to-have, accessible via secondary link/collapsed section — must not affect primary dashboard layout or block MVP
- NFR-P3: pages interactive within 3 seconds on Raspberry Pi 4
- **Language scope:** Multilingual support (nl/fr) is explicitly out of scope for v1 and planned for v2 (NFR-L1, NFR-L2).

---

### Epic 11: Installer Operational Monitoring

Installer dashboard for remote site health monitoring, structured event log with filtering, anomaly surfacing, homeowner credential management. All state is read from StateStore and event log — no degradation logic lives here.

**User outcome:** Three weeks post-deployment, Lien opens the dashboard, reads 47 decision entries in plain language, confirms no anomalies, checks peak tracker, closes the dashboard. No support call required.

**FRs covered:** FR18, FR19, FR20 (UI), FR21 (UI), FR23, FR29 (installer-side degraded view), NFR-P5

**Scope:**
- Installer dashboard: system health indicator reading `SystemOperatingMode` from StateStore (NORMAL/DEGRADED/FAILED — display only, no logic here) (FR18); per-device connectivity rows; peak tracker (current month highest 15-min vs. configured limit) (FR19); recent event log preview
- Anomaly notice: inline above stat cards (not modal, not fixed bar); WARN/FAIL badge; one-line description; "View in event log" link pre-applying relevant type filter; dismissible (FR18)
- Event log UI: chronological list newest-first; DECISION/DEVICE/SYSTEM/CONSTRAINT/INSTALLER badge filter (toggle pills); keyword search (substring, debounced 300ms, HTMX partial); date range filter; filter state carried in URL query params (FR20 UI)
- Installer note entry UI: textarea + "Add note"; HTMX prepends new INSTALLER row to log top; textarea clears on success (FR21 UI)
- Homeowner credential management: create and reset homeowner credentials from installer settings (FR23)
- Degraded-mode display: device WARN badges, REDUCED capability indicators — reads from StateStore and event log; display only
- NFR-P5: event log queries for past 30 days return within 5 seconds

---

### Epic 12: Platform Reliability and Configuration Management

Platform update management, site configuration backup and restore, data retention enforcement, and system restart recovery validation. Watchdog and fail-safe runtime behavior are implemented in Epic 8 — this epic covers operational hardening.

**User outcome:** Lien applies a platform update with explicit approval after reading release notes. Configuration survives a hardware swap. Data is retained and never silently pruned. The system returns to full operation within 2 minutes of a reboot.

**FRs covered:** FR22, FR24, FR31 (system restart recovery validation), NFR-R1, NFR-R3, NFR-D1, NFR-D5, NFR-S2, AR10, NFR-M2

**Scope note:** Watchdog and fail-safe runtime behavior (FR32, FR34, NFR-R5) are implemented in Epic 8. Epic 12 covers operational hardening only.

**Scope:**
- Platform update management: check for available updates, display release notes, explicit installer confirmation before applying (FR22)
- Site configuration export: YAML artifact excluding all credentials and secrets (FR24, AR10, NFR-S2)
- Site configuration restore: validates before applying; tested and documented procedure (FR24, AR10)
- Data retention enforcement: 12-month energy state history (NFR-D1); retention policy applied without silent data loss
- Platform updates preserve all site configuration and energy history without data loss or silent migration failure (NFR-D5)
- System restart recovery: NFR-R3 — full operational state (all device connections re-established, decision engine running) within 2 minutes of reboot; startup sequence validates this
- NFR-R1: 30-day uptime target — validated through test scenarios and operational monitoring
- NFR-M2: backup/restore procedure completable in under 10 minutes

---

### Epic 13: External Optimization Data *(nice-to-have)*

Solar production forecast and dynamic tariff data as configurable optional optimization inputs. External data influences decisions only within PolicyGuard-enforced safety constraints. The system continues full local deterministic operation when external sources are unavailable.

**User outcome:** When configured, the decision engine uses forecast and tariff data to improve optimization. When these sources are unavailable, the system continues without interruption and the installer sees the specific reason.

**FRs covered:** FR40, FR41, FR42, NFR-I4, NFR-S7

**Safety constraint:** External forecast and tariff data cannot override PolicyGuard-enforced hard safety constraints. External data influences optimization layer only within the established constraint boundaries. If external data is unavailable, the system must continue full local deterministic control operation — no degradation of core control capability.

**Scope:**
- Solar production forecast integration from configurable external API (FR40)
- Dynamic tariff data feed from configurable external source (FR41)
- External API timeout: 10 seconds; no impact on core control loop timing or reliability (NFR-I4)
- All external API calls over HTTPS only; local energy data not transmitted (NFR-S7)
- Degraded optimization mode: when external sources unavailable, system operates on local data; installer sees specific unavailability reason (FR42)
- External data unavailability never degrades core control — only optimization quality is affected

---

## Epic 1: Platform Foundation

A runnable, health-checked OPEN-EMS instance deploys on Raspberry Pi 4 via Docker Compose or systemd, with database auto-initialized, TLS generated, CI pipeline passing, central configuration in place, and structured logging standardized.

**Requirements covered:** AR1, AR2, AR3, AR4, AR5, AR13, AR18, AR20, NFR-M1, NFR-M3, NFR-D4, NFR-S3

---

### Story 1.1: Initialize project repository with uv and dependency lockfile

As an installer,
I want to deploy OPEN-EMS from a locked, reproducible Python project,
So that every site runs the identical dependency set with no version drift between installations.

**Acceptance Criteria:**

**Given** a fresh Linux host with Python 3.12+ and uv installed
**When** the developer runs `uv sync`
**Then** all specified dependencies install from the lockfile: fastapi, uvicorn, pydantic, pydantic-settings, aiosqlite, alembic, pymodbus, ocpp, dsmr-parser, structlog, cryptography, pytest, pytest-asyncio, ruff, mypy
**And** `uv run ruff check .` exits 0 with no violations
**And** `uv run mypy src/` exits 0 with no type errors on the initial skeleton
**And** `uv run pytest` discovers and exits 0 with an empty test suite (baseline)
**And** `uv.lock` is committed to the repository

---

### Story 1.2: Bootstrap FastAPI application with SQLite, Alembic migrations, and structured logging

As a developer,
I want a running FastAPI app with crash-safe SQLite storage, auto-running migrations, and standardized structured logging,
So that subsequent features have a stable data layer and consistent observability without rework.

**Acceptance Criteria:**

**Given** the project is initialized per Story 1.1
**When** the FastAPI application starts
**Then** an SQLite database file is created or opened at the path from configuration
**And** SQLite WAL mode is enabled on the connection
**And** Alembic runs all pending migrations automatically before any route handler or adapter initializes
**And** a migration failure terminates startup with a non-zero exit code and a structured error log entry
**And** raw SQL is permitted only inside Alembic migration files; all application runtime database access uses repository classes in `storage/repositories/` exclusively
**And** an initial empty-schema migration exists as the versioned baseline

**And** structlog is configured as the sole logging backend with JSON output to stdout
**And** every log entry includes at minimum: `timestamp`, `level`, `event`, `component`
**And** no plain-text log output exists anywhere in the codebase — all logging goes through structlog

---

### Story 1.3: Implement health endpoints and startup sequence with NTP check

As an installer,
I want the system to report its own readiness and detect clock issues on startup,
So that deployment tooling can confirm the system is operational and time-sensitive features degrade safely when the clock is unreliable.

**Acceptance Criteria:**

**Given** the FastAPI process has started at any point
**When** `GET /health/live` is requested
**Then** it returns HTTP 200 `{"status": "alive"}` — even before migrations complete

**Given** all migrations and initialization have completed successfully
**When** `GET /health/ready` is requested
**Then** it returns HTTP 200 `{"status": "ready"}`
**And** on systemd native deployment, `sd_notify("READY=1")` has been sent before this endpoint returns 200

**Given** migrations or initialization are still in progress
**When** `GET /health/ready` is requested
**Then** it returns HTTP 503

**Given** the startup NTP check runs
**When** it executes
**Then** it attempts to verify the system clock using the system clock plus an optional NTP query against a configurable NTP server
**And** if NTP is unreachable, the system falls back to the system clock only — NTP unavailability is not fatal and does not block startup
**And** the result is stored as `system_clock_status` with one of three values: `valid` (clock verified against NTP), `suspect` (drift exceeds configured threshold), or `unknown` (NTP unreachable, system clock assumed)
**And** this flag is stored in internal startup state for later exposure via StateStore (Epic 5)
**And** a structured log entry is written at `warn` level with `event="clock_suspect"` or `event="clock_unknown"` when the result is not `valid`
**And** startup is never blocked by the NTP check result

---

### Story 1.4: Add Docker Compose deployment with offline TLS certificate generation

As an installer,
I want to deploy OPEN-EMS with a single Docker Compose command over HTTPS accessible by both hostname and LAN IP,
So that the system is reachable securely on local networks where hostname resolution is unreliable, without internet access or external certificate authorities.

**Acceptance Criteria:**

**Given** the host has Docker and Docker Compose installed
**When** the installer runs `bash scripts/generate-tls.sh && docker compose up -d`
**Then** a self-signed TLS certificate is generated with CN and SAN entries covering both the local hostname and the host's LAN IP address
**And** TLS generation runs fully offline — no ACME, Let's Encrypt, or any external HTTP call during certificate generation or application startup
**And** no external calls are made during application startup for any reason

**And** the service starts and `GET /health/ready` returns HTTP 200 over HTTPS
**And** HTTPS is the default and primary exposed endpoint
**And** if HTTP is enabled (optional, for first-time access or local LAN recovery), HTTP requests either redirect to HTTPS or the server serves an explicit insecure-mode response; HTTP is never silently offered as an equivalent to HTTPS

**And** the Docker healthcheck uses `GET /health/ready` and reports healthy when the endpoint returns 200
**And** `docker compose logs` shows structured JSON application output
**And** the Docker image builds for both `linux/amd64` and `linux/arm64` via `docker buildx build`
**And** `docker buildx build` succeeds when run locally without CI — installers can build on-site without pipeline access

---

### Story 1.5: Add systemd native deployment with watchdog integration

As an installer,
I want to run OPEN-EMS as a managed systemd service on bare Linux,
So that the system integrates with standard service management — auto-restart on failure, boot-time start, journald logging — without requiring Docker.

**Acceptance Criteria:**

**Given** a Raspberry Pi 4 with Python 3.12+ and uv installed
**When** the installer runs `sudo systemctl enable --now open-ems`
**Then** the service starts, `sd_notify("READY=1")` is sent within the configured startup timeout, and `systemctl status open-ems` shows `active (running)`
**And** `GET /health/ready` returns HTTP 200 after the service reports ready

**Given** the service is running with `WatchdogSec` configured in the unit file
**When** the application sends `sd_notify("WATCHDOG=1")` on schedule
**Then** systemd resets the watchdog timer and does not kill the process

**Given** the application fails to send `sd_notify("WATCHDOG=1")` within the `WatchdogSec` interval
**When** `WatchdogSec` elapses without a watchdog signal
**Then** systemd kills and restarts the process
**And** the application logs a structured entry with `event="watchdog_missed"` if it detects internally that the watchdog heartbeat was not sent within the expected interval (precursor detection; full watchdog cycle implementation in Epic 8)

**Given** the service process exits unexpectedly for any reason
**When** systemd detects the exit
**Then** the service restarts automatically per `Restart=on-failure`

**And** all application log output is captured by journald as structured JSON lines
**And** the systemd unit file uses `Type=notify` so systemd waits for `READY=1` before reporting the service as started

---

### Story 1.6: Configure GitHub Actions CI pipeline with migration validation

As a developer,
I want automated quality gates, migration validation, and multi-arch Docker image builds on every push,
So that code quality is enforced continuously, schema migrations are validated before merge, and release images are produced without manual steps.

**Acceptance Criteria:**

**Given** code is pushed to any branch
**When** the CI pipeline runs
**Then** `ruff check` and `ruff format --check` pass with zero violations
**And** `mypy src/` passes with zero type errors
**And** `pytest tests/` passes with all tests collected and passing
**And** `alembic upgrade head` runs successfully against a temporary SQLite database — the pipeline fails if any migration fails
**And** the pipeline marks the push as failed if any of the above checks fail

**Given** a tagged release commit is pushed (e.g., `v1.0.0`)
**When** the release pipeline runs
**Then** `docker buildx build` produces a multi-arch image for `linux/amd64` and `linux/arm64`
**And** the image is tagged with the release version and published to GitHub Container Registry using `GITHUB_TOKEN`

**And** the pipeline configuration lives in `.github/workflows/` and is committed to the repository

---

### Story 1.7: Establish central configuration system with pydantic-settings

As a developer,
I want all application configuration managed through a central pydantic-settings config loaded from environment variables or a .env file,
So that deployments can be customized per site with no hardcoded values in the codebase and no full redeployment for config changes.

**Acceptance Criteria:**

**Given** the application starts
**When** configuration is loaded
**Then** a `Settings` class (pydantic-settings) is the single source for all configuration values
**And** config is loaded in priority order: environment variables override .env file values, which override hardcoded defaults
**And** a `.env` file at the project root is loaded if present; its absence is not an error
**And** no hardcoded values (ports, file paths, timeouts, secrets) appear in application runtime code — all are read from `Settings`

**And** a `.env.example` file is committed to the repository showing all available configuration keys with example values and descriptions
**And** `.env` is listed in `.gitignore` and never committed
**And** the `Settings` instance is loaded once at application startup and injected via FastAPI dependency where needed
**And** required fields with no default (e.g., `SECRET_KEY`) cause startup to fail immediately with a structured error log identifying the missing field by name

---

## Epic 2: Authentication and Access Control

Installers and homeowners log in with role-based credentials, are routed to their respective interfaces, and sessions expire safely. Authentication events are written to the structured logging system and will flow to the installer-visible audit log once Epic 6 is complete.

**Requirements covered:** FR35, FR36, FR37, FR38, FR39, AR12, NFR-S1, NFR-S3, NFR-S4, NFR-S5, NFR-S6

---

### Story 2.1: Define user model, upgradeable credential storage, and safe admin bootstrap

As an installer,
I want the system to store credentials using a self-describing, algorithm-agnostic hash format and require a safe first-run bootstrap,
So that accounts are protected from the first deployment and can be migrated to stronger hashing algorithms in the future without breaking existing users.

**Acceptance Criteria:**

**Given** the application starts for the first time with no users in the database
**When** `INITIAL_ADMIN_PASSWORD` is not set
**Then** the application fails startup immediately with a structured error: `event="startup_failed"`, `reason="INITIAL_ADMIN_PASSWORD required when no users exist"`

**Given** the application starts for the first time with no users in the database
**When** `INITIAL_ADMIN_PASSWORD` is set
**Then** an installer/admin account is created with username "admin" and `must_change_password = true`
**And** the password is stored using the configured hashing algorithm (default: bcrypt) in a self-describing format (PHC string format: algorithm identifier + parameters + salt + hash embedded together)
**And** `INITIAL_ADMIN_PASSWORD` is consumed at startup only; its value is never stored or logged in plaintext

**Given** an admin user has `must_change_password = true`
**When** they authenticate successfully
**Then** they are redirected to a mandatory password change form before accessing any other route
**And** `must_change_password` is cleared to `false` only after a successful password change submission

**Given** any password is stored or updated
**When** the hash is written
**Then** it uses PHC string format so the algorithm, parameters, and salt are self-describing in the stored value
**And** the system can verify passwords against hashes using a different future algorithm (e.g., argon2id) without invalidating existing hashes — existing hashes remain verifiable; new hashes use the updated algorithm

**Given** the `users` table is accessed
**When** user data is read or written
**Then** all access is via `storage/repositories/user_repo.py`; no raw SQL outside the repository
**And** the `users` table migration adds fields: `id`, `username`, `role` (installer/homeowner), `hashed_password`, `must_change_password`, `created_at`

---

### Story 2.2: Implement login form, hardened session creation, and brute-force protection

As an installer or homeowner,
I want to log in securely over HTTPS with a hardened session cookie and rate-limiting against brute-force attacks,
So that my credentials and session are protected even on shared or unattended local networks.

**Acceptance Criteria:**

**Given** an unauthenticated user navigates to any protected URL
**When** they are redirected to the login page
**Then** `GET /login` renders an HTML login form; the original URL is preserved as `?next=` in the redirect

**Given** a user submits valid credentials via `POST /login` over HTTPS
**When** the form is processed
**Then** the password is verified against the stored hash using the algorithm embedded in the PHC hash string
**And** any existing sessions for the same user are invalidated before the new session is created (token rotation — session fixation prevention)
**And** a new session is created in the `sessions` table with a token of minimum 128 bits of entropy generated via `secrets.token_hex()`
**And** the token stored in the database is a SHA-256 hash of the raw cookie value — the raw token exists only in the cookie
**And** the session cookie is set with: `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`
**And** if a password change occurs during an active session, that session is invalidated and the user must re-authenticate
**And** the user is redirected to the URL from `?next=` if valid, or to their role's home screen if absent
**And** a structured event is written: `event="login_success"`, `component="auth"`, including role

**Given** a user submits valid credentials via `POST /login` over HTTP (when HTTP mode is enabled per Epic 1)
**When** the form is processed
**Then** authentication is refused; the user is redirected to the HTTPS equivalent URL
**And** no session cookie is ever set over HTTP

**Given** a user submits invalid credentials
**When** the form is processed
**Then** the login form is re-rendered with a generic error: "Invalid username or password"
**And** no technical detail, role hint, or account-existence signal is included in the error
**And** a structured event is written: `event="login_failed"`, `component="auth"`, including source IP

**Given** failed login attempts from an IP address or for a username exceed the configured threshold (default: 5 failures within 5 minutes)
**When** a subsequent login attempt arrives
**Then** it is subject to a configurable delay or temporary lockout before processing
**And** rate-limit state is tracked in-memory per application process; counter resets on process restart (acceptable for local-first deployment)
**And** a structured event is written: `event="auth_rate_limited"`, `component="auth"`, including source IP and username

**And** the `sessions` table migration adds fields: `id`, `user_id`, `token_hash` (SHA-256 of cookie value), `created_at`, `last_active_at`, `expires_at`

---

### Story 2.3: Implement role-based routing with differentiated browser and API enforcement

As an installer,
I want the system to route me to my role's interface after login and return consistent responses for browser and HTMX/API cross-role access attempts,
So that the role boundary is enforced without creating HTMX redirect loops or exposing route structure.

**Acceptance Criteria:**

**Given** a user logs in successfully as installer/admin
**When** the session is established
**Then** they are routed to `/installer/dashboard`

**Given** a user logs in successfully as homeowner/user
**When** the session is established
**Then** they are routed to `/homeowner/dashboard`

**Given** a browser request (identified by `Accept: text/html` and absence of `HX-Request` header) reaches a route protected by `Depends(require_installer)` with a valid homeowner session
**When** the server processes the request
**Then** the server redirects to the homeowner's role home view — no error page revealing route structure

**Given** an HTMX or API request (identified by `HX-Request` header or `Accept: application/json`) reaches a route protected by `Depends(require_installer)` with a valid homeowner session
**When** the server processes the request
**Then** the server returns HTTP 403 with no redirect
**And** a structured event is written: `event="role_access_denied"`, `component="auth"`, including role, user ID, and attempted route

**Given** the same role-mismatch scenario for homeowner routes accessed by an installer session
**When** the server processes the request
**Then** the same browser/HTMX differentiated response behavior applies

**Given** any protected route is accessed with no valid session
**When** the server processes the request
**Then** it returns HTTP 401 and redirects to `/login?next=[originally requested URL]`

**And** `Depends(require_installer)` and `Depends(require_homeowner)` are the sole role enforcement mechanism; no inline role checks inside route handlers

---

### Story 2.4: Implement CSRF protection on state-changing routes

As an installer or homeowner,
I want all state-changing requests to carry a server-side validated CSRF token,
So that cross-site request forgery attacks cannot trigger actions using my session.

**Acceptance Criteria:**

**Given** a user has an active session
**When** any server-rendered form is returned
**Then** it includes a hidden CSRF token field populated from the per-session CSRF token stored server-side

**Given** an HTMX request is dispatched to a state-changing endpoint
**When** the request is sent
**Then** it includes the per-session CSRF token in an `X-CSRF-Token` request header

**Given** any state-changing request (POST, PUT, DELETE, PATCH) is received
**When** the server validates the CSRF token
**Then** it compares the submitted token (form field or `X-CSRF-Token` header) against the server-side session token
**And** a missing or invalid token is rejected with HTTP 403 before any handler logic executes
**And** a valid token allows the request to proceed

**Given** a GET request or SSE endpoint (`/api/stream/state`) is accessed
**When** the server processes the request
**Then** CSRF validation is not applied — CSRF protection is scoped to state-changing methods only

**And** CSRF tokens are per-session, generated at session creation, stored server-side, and remain valid for the session lifetime
**And** CSRF validation runs in middleware before route handlers on all applicable routes

---

### Story 2.5: Implement session expiry, multi-device concurrency policy, and logout

As an installer or homeowner,
I want my session to expire automatically after inactivity, to log out explicitly, and to use the system from multiple devices without interference,
So that unattended devices cannot retain access indefinitely while normal multi-device use is unaffected.

**Acceptance Criteria:**

**Given** an installer/admin session is active
**When** no request is made within the configured inactivity window (default: 4 hours via `INSTALLER_SESSION_TIMEOUT_HOURS`)
**Then** the next request with that session returns HTTP 401 and redirects to `/login?next=[URL]`
**And** a structured event is written: `event="session_expired"`, `component="auth"`, including role and user ID

**Given** a homeowner/user session is active
**When** no request is made within the configured inactivity window (default: 30 days via `HOMEOWNER_SESSION_TIMEOUT_DAYS`)
**Then** the same expiry and redirect behavior applies with the same event written

**Given** a request arrives with an expired session
**When** the server checks the session
**Then** the expired session is deleted from the `sessions` table immediately (lazy cleanup)
**And** the session cookie is cleared in the response
**And** the user is redirected to `/login?next=[originally requested URL]`

**Given** a user submits `POST /logout`
**When** the request is processed
**Then** the current session is deleted from the `sessions` table, the cookie is cleared, and the user is redirected to `/login`
**And** a structured event is written: `event="logout"`, `component="auth"`, including role and user ID

**Session concurrency policy — multiple sessions per user (Option A):**
**Given** a user logs in from a second device or browser
**When** the new session is created
**Then** existing sessions for the same user remain active — multiple concurrent sessions are permitted
**And** each session has its own independent token, expiry timestamp, and `last_active_at` value

**Given** expired sessions accumulate in the `sessions` table
**When** they are encountered
**Then** they are pruned lazily — any authenticated request triggers deletion of that user's expired sessions
**And** a background task runs every 24 hours to prune all expired sessions across all users regardless of access activity

**And** `last_active_at` is updated on every authenticated request to reset the inactivity window

---

## Epic 3: Protocol Adapters

Raw communication adapters for Modbus TCP, OCPP 1.6, and DSMR P1. Epic 3 proves raw protocol communication and failure handling only. It does not expose normalized energy states to the decision engine or UI — domain normalization belongs to Epic 4.

**Requirements covered:** FR1 (protocol level), NFR-I1, NFR-I3 (DSMR timestamping), AR16 (ProtocolCommandResult defined here)

---

### Story 3.1: Define protocol adapter contract and raw protocol types

As a developer,
I want a typed interface contract that all protocol adapters implement, using raw protocol types with no energy-domain interpretation,
So that the abstraction layer in Epic 4 can wrap any protocol adapter through a consistent, type-checked API.

**Acceptance Criteria:**

**Given** the adapters package is initialized
**When** the protocol contract types are imported
**Then** a `ProtocolAdapter` Protocol (structural subtyping via `typing.Protocol`) is defined with required methods: `get_raw_state() -> RawProtocolState | ProtocolDegradedState` and `send_raw_command(command: RawProtocolCommand) -> ProtocolCommandResult`
**And** `ProtocolCommandResult` is defined with fields: `correlation_id`, `device_id`, `protocol_status` (sent/acked/timeout/error), `raw_response` (optional bytes or dict)
**And** `ProtocolDegradedState` is defined with fields: `device_id`, `reason` (str), `occurred_at` (datetime)
**And** `RawProtocolState`, `RawModbusState`, `RawOCPPState`, and `RawDSMRState` are defined as typed containers for raw protocol data with no energy-domain interpretation
**And** no domain types (`DeviceState`, `DegradedDeviceState`, `CommandResult`) are defined or imported in this package — those belong to Epic 4 and Epic 8 respectively
**And** mypy validates all concrete adapter implementations against `ProtocolAdapter` with no type errors

**And** the contract is verified by a unit test asserting that all concrete adapter classes structurally satisfy `ProtocolAdapter` at import time

---

### Story 3.2: Implement Modbus TCP adapter with connection management and timeout enforcement

As a developer,
I want a Modbus TCP adapter that reads raw register values and writes register commands with enforced timeouts and typed failure representation,
So that the system can communicate with inverters and batteries over Modbus without the control loop blocking on unresponsive hardware.

**Acceptance Criteria:**

**Given** a device is reachable at the configured Modbus TCP host and port
**When** `get_raw_state()` is called
**Then** it opens (or reuses) a `pymodbus` async TCP connection and reads the configured register set
**And** raw Modbus register values are returned as a typed `RawModbusState` containing register addresses and their 16-bit integer values with a read timestamp — no interpretation as inverter, battery, or energy values at this layer

**Given** a Modbus TCP connection or read operation exceeds 10 seconds
**When** the timeout elapses
**Then** the adapter returns `ProtocolDegradedState(device_id=..., reason="modbus_timeout", occurred_at=...)`
**And** no Modbus or asyncio exception propagates beyond the adapter boundary
**And** a structured log entry is written via structlog: `event="adapter_timeout"`, `component="modbus"`, `device_id=...`

**Given** `send_raw_command()` is called with a register write operation
**When** the write is executed
**Then** a `ProtocolCommandResult` is returned with `protocol_status="acked"` if the Modbus write acknowledgement is received, or `"timeout"` / `"error"` otherwise
**And** protocol-level write acknowledgement is the only confirmation at this layer — domain-level applied/effect confirmation (did the device respond to the energy intent?) happens in Epic 8
**And** optional read-back of written registers may be performed as a protocol verification, not as an energy-effect confirmation

**Given** runtime communication failures occur (connection refused, broken pipe, Modbus exception code)
**When** the failure is encountered
**Then** it is translated to `ProtocolDegradedState` or `ProtocolCommandResult(protocol_status="error")` — no `ModbusException` or asyncio exception propagates
**And** startup configuration errors (unparseable host, invalid port format) may raise at initialization — they are not silently swallowed

**Given** a connection failure occurs for one device
**When** the adapter handles it
**Then** other device adapters using separate connections are unaffected — each device has its own independent connection

**Tests:**
**And** a simulated Modbus TCP server is used in tests to validate: successful register read, write ACK, read timeout, write timeout, connection refused, and unexpected register value responses
**And** timeout tests verify that `get_raw_state()` and `send_raw_command()` return within 10 seconds + a small epsilon under all conditions — no hang
**And** reconnect tests verify the adapter re-establishes its connection after a simulated mid-session disconnect

---

### Story 3.3: Implement OCPP 1.6 Central System adapter

As a developer,
I want an OCPP 1.6 Central System that EV chargers connect to, with raw OCPP message data stored and accessible,
So that the system can communicate with EV chargers using the charger-initiates-connection model without polling.

**Acceptance Criteria:**

**Given** the OCPP Central System WebSocket server is running
**When** an EV charger connects via WebSocket
**Then** the connection is accepted, the charger is registered by its `charge_point_id`, and OCPP 1.6 message framing (CALL/CALLRESULT/CALLERROR) is handled per specification
**And** incoming `BootNotification`, `Heartbeat`, and `StatusNotification` messages are processed and their raw payloads stored

**Given** `get_raw_state()` is called for a connected charger
**When** the call executes
**Then** it returns a `RawOCPPState` containing:
- `charge_point_id`
- `last_status_notification`: raw payload dict from the most recent StatusNotification
- `last_heartbeat_at`: datetime of the most recent Heartbeat
- `connection_status`: `connected` or `disconnected`
- `last_call_result`: raw CALLRESULT payload from the most recent dispatched command (nullable)
- `last_call_error`: raw CALLERROR payload if the most recent command was rejected (nullable)
**And** no normalization (session_active, current_power_kw, etc.) is applied — raw OCPP message data only

**Given** a charger disconnects or no Heartbeat is received within the configured timeout
**When** the timeout elapses
**Then** `get_raw_state()` returns `ProtocolDegradedState(device_id=..., reason="ocpp_disconnected", occurred_at=...)`
**And** a structured log entry is written via structlog: `event="adapter_disconnected"`, `component="ocpp"`, `device_id=...`

**Given** `send_raw_command()` is called with a raw OCPP action name and payload
**When** the command is dispatched
**Then** the adapter sends the OCPP CALL message and awaits CALLRESULT within 10 seconds
**And** a `ProtocolCommandResult` is returned: `protocol_status="acked"` on CALLRESULT, `"error"` on CALLERROR, `"timeout"` if no response arrives within 10 seconds
**And** mapping of OPEN-EMS control intents to specific OCPP action names and payloads is not performed at this layer — that belongs to Epic 4 and Epic 8

**Given** runtime OCPP failures occur (malformed message, WebSocket error)
**When** the failure is encountered
**Then** it is translated to `ProtocolDegradedState` or `ProtocolCommandResult(protocol_status="error")` — no unhandled exception propagates
**And** startup configuration errors (invalid WebSocket bind address) may raise at initialization
**And** multiple concurrent charger connections are supported — each charger has independent state

**Tests:**
**And** a simulated OCPP charger client is used in tests to validate: BootNotification, StatusNotification, Heartbeat, CALLRESULT for a dispatched command, CALLERROR response, and charger disconnect/reconnect
**And** timeout tests verify that `send_raw_command()` returns within 10 seconds + small epsilon when no CALLRESULT arrives
**And** malformed OCPP message tests verify that parse errors are translated to `ProtocolDegradedState` and do not crash the message loop

---

### Story 3.4: Implement DSMR P1 adapter with raw telegram parsing and staleness timestamping

As a developer,
I want a DSMR P1 adapter that continuously parses smart meter telegrams and timestamps each reading,
So that the system has access to raw grid meter data and can detect when that data has gone stale.

**Acceptance Criteria:**

**Given** a DSMR P1 smart meter is connected via serial/USB (primary v1 path)
**When** the adapter is running
**Then** it reads telegrams continuously using `dsmr-parser` and updates internal state on each successful parse

**Given** a TCP-connected P1 reader is available (supported secondary path)
**When** the adapter is configured for TCP mode
**Then** it reads telegrams from the TCP stream using the same `dsmr-parser` pipeline and the same `ProtocolAdapter` interface as the serial path

**Given** `get_raw_state()` is called and a valid telegram has been received
**When** the call executes
**Then** it returns a `RawDSMRState` containing the raw parsed DSMR telegram fields (e.g., `electricity_delivered_tariff1`, `electricity_returned_tariff1`, `current_electricity_usage`, `current_electricity_delivery`) and `received_at` (datetime of the most recent telegram)
**And** no normalization to `GridMeterState`, energy-sign conventions, or unit conversions is applied — raw DSMR field values as parsed

**Given** `get_raw_state()` is called and the `received_at` of the last telegram is more than 60 seconds ago
**When** the staleness check runs
**Then** it returns `ProtocolDegradedState(device_id=..., reason="dsmr_stale", occurred_at=...)`
**And** a structured log entry is written via structlog: `event="adapter_stale"`, `component="dsmr"`, `seconds_since_last_telegram=...`

**Given** the serial port or TCP connection becomes unavailable
**When** a read attempt fails
**Then** the adapter returns `ProtocolDegradedState(device_id=..., reason="dsmr_unavailable", occurred_at=...)` — no exception propagates
**And** the adapter attempts to reopen the port or reconnect on the next read cycle

**Tests:**
**And** a simulated DSMR telegram source (serial loopback or TCP loopback) is used in tests to validate: successful parse, stale detection (>60 seconds without telegram), serial disconnect/reconnect, and malformed telegram (parse error → `ProtocolDegradedState`)
**And** timeout tests verify that the 60-second stale threshold triggers `ProtocolDegradedState` correctly
**And** a malformed telegram test verifies that parse failures do not crash the reader loop

**Cross-story constraints applying to all Epic 3 stories:**
- Runtime communication failures must not escape adapter boundaries — they are translated to `ProtocolDegradedState` or `ProtocolCommandResult(protocol_status="error")`
- Startup and configuration errors (invalid host, bad port format, missing device) may fail fast with a structured log error — they are not silently swallowed
- Programming errors (bugs, assertion failures) are not swallowed — they propagate naturally for fail-fast behavior
- All logging in Epic 3 uses structlog to stdout only; no installer-facing event_log entries are written at this layer — those are created by Epic 4, Epic 6, and Epic 8 when protocol failures affect system state

---

## Epic 4: Device Abstraction Layer

Normalizes raw protocol data into typed domain DeviceState models, exposes validated capability profiles, and handles device discovery and reconnection. After Epic 4, the system can read normalized energy state and query validated device capabilities. It does not yet issue domain control commands — that belongs to Epic 8.

**Requirements covered:** FR1 (discovery), FR3, FR4, FR5, FR6b, FR13 (DegradedDeviceState), FR31 (reconnection), NFR-R4, NFR-I3 (stale treatment)

---

### Story 4.1: Define domain device state model, DeviceAdapter protocol, and energy sign conventions

As a developer,
I want a typed domain model for energy device states with a DeviceAdapter protocol exposing state and capabilities,
So that all higher layers — StateStore, decision engine, UI — work with normalized, energy-meaningful values through a consistent, type-checked interface.

**Acceptance Criteria:**

**Given** the device abstraction package is initialized
**When** domain types are imported
**Then** `DeviceAdapter` Protocol is defined with: `get_state() -> DeviceState | DegradedDeviceState` and `get_capabilities() -> DeviceCapabilityProfile`
**And** no `send_command()` method exists on `DeviceAdapter` in Epic 4 — domain command dispatch belongs to Epic 8 via PolicyGuard
**And** typed `DeviceState` subtypes are defined: `InverterState`, `BatteryState`, `EVChargerState`, `GridMeterState`
**And** domain-level `DegradedDeviceState` is defined with fields: `device_id`, `role`, `reason` (str), `occurred_at` — distinct from `ProtocolDegradedState` defined in Epic 3
**And** `DeviceRole` enum is defined: `inverter` / `battery` / `ev_charger` / `grid_meter`

**Energy sign conventions — explicitly declared and enforced throughout the system:**
**And** grid power: `grid_power_kw > 0` = import from grid; `grid_power_kw < 0` = export to grid
**And** battery power: `battery_power_kw > 0` = charging (consuming power); `battery_power_kw < 0` = discharging (providing power)
**And** PV power: `pv_power_kw >= 0` always (production only, never negative)
**And** these conventions are documented in the module docstring and enforced by type-level validation

**And** no raw protocol types (`RawModbusState`, `RawOCPPState`, etc.) are exposed to consumers of this package
**And** mypy validates all concrete device adapter implementations against `DeviceAdapter` with no type errors

**Tests:**
**And** unit tests verify that all concrete device adapter implementations satisfy `DeviceAdapter` at import time
**And** unit tests verify sign convention enforcement: a battery reading indicating charge produces `power_kw > 0`; a reading indicating discharge produces `power_kw < 0`

---

### Story 4.2: Implement Modbus device normalization with register maps

As a developer,
I want Modbus-based device adapters that translate raw register values into typed domain states using per-model register maps,
So that the decision engine receives normalized, energy-meaningful values regardless of which supported Modbus device model is installed.

**Acceptance Criteria:**

**Given** a supported inverter model (Fronius Gen24, Huawei SUN2000, or Growatt hybrid) is connected via Modbus TCP
**When** `get_state()` is called on its device adapter
**Then** it calls the underlying `ProtocolAdapter.get_raw_state()` and maps raw `RawModbusState` register values to a typed `InverterState` using the model's register map
**And** `InverterState` includes: `pv_power_kw` (>= 0 always), `ac_power_kw`, `operating_mode`, `fault_code` (nullable)

**Given** a supported battery model (BYD HVS or BYD HVM) is connected via Modbus TCP
**When** `get_state()` is called on its device adapter
**Then** raw registers are mapped to `BatteryState` including: `soc_percent`, `power_kw`, `capacity_kwh`, `operating_mode`
**And** battery power sign convention is applied and explicitly documented: `power_kw > 0` = charging (consuming power from grid or PV); `power_kw < 0` = discharging (providing power to loads or grid)
**And** this sign convention is consistent with the system-wide convention declared in Story 4.1

**Given** the underlying `ProtocolAdapter` returns `ProtocolDegradedState`
**When** the device adapter processes it
**Then** it translates to domain-level `DegradedDeviceState` with role context added
**And** a structured log entry is written via structlog: `event="device_degraded"`, `component="adapters"`, `device_id=...`, `role=...`

**And** register maps live in `adapters/modbus/register_maps/` as per-model definitions
**And** an unsupported model fails at initialization with a structured error — it does not silently default to a wrong register layout

**Tests:**
**And** unit tests for each supported device model verify that raw register values are correctly mapped to typed fields (at least one passing and one boundary/error case per field)
**And** unit tests verify battery sign convention: a register value indicating charging produces `power_kw > 0`; a value indicating discharging produces `power_kw < 0`
**And** unit tests for `ProtocolDegradedState` → `DegradedDeviceState` translation verify that role context is correctly added

---

### Story 4.3: Implement OCPP and DSMR device normalization

As a developer,
I want OCPP and DSMR device adapters that translate raw protocol states into typed domain states with sign conventions and data-quality metadata applied,
So that the EV charger and grid meter are available to the decision engine through the same DeviceAdapter interface as Modbus devices, with honest representation of data availability.

**Acceptance Criteria:**

**Given** an OCPP EV charger has sent a `StatusNotification`
**When** `get_state()` is called on the EV charger adapter
**Then** it reads `RawOCPPState` and maps it to `EVChargerState`
**And** `EVChargerState` includes: `status` (available/charging/faulted/unavailable), `session_active` (bool)
**And** `current_power_kw` is nullable — it is null when the charger does not provide it and must never be fabricated or inferred from status alone
**And** if `current_power_kw` is derived from an OCPP `MeterValues` message, the field includes `power_source = "meter_values"` and `power_measured_at` (datetime of the MeterValues message) so consumers know the data quality and age

**Given** a DSMR telegram has been parsed and `RawDSMRState` is available
**When** `get_state()` is called on the grid meter adapter
**Then** it reads `RawDSMRState` and maps it to `GridMeterState`
**And** `GridMeterState` includes: `grid_power_kw` (sign convention applied), `energy_delivered_kwh`, `energy_returned_kwh`, `received_at`
**And** DSMR sign convention is applied: `current_electricity_usage` maps to positive `grid_power_kw` (import); `current_electricity_delivery` maps to negative `grid_power_kw` (export)
**And** DSMR data with `received_at` older than 60 seconds causes `get_state()` to return `DegradedDeviceState(reason="dsmr_stale")` — the `received_at` from `RawDSMRState` drives this evaluation

**Given** the underlying `ProtocolAdapter` returns `ProtocolDegradedState` for either device type
**When** the device adapter processes it
**Then** it translates to domain-level `DegradedDeviceState` with role and device context, logged via structlog

**Tests:**
**And** unit tests verify DSMR sign convention: `current_electricity_usage` produces positive `grid_power_kw`; `current_electricity_delivery` produces negative `grid_power_kw`
**And** unit tests verify `current_power_kw` is null when `MeterValues` data is absent from `RawOCPPState`
**And** unit tests verify that `current_power_kw` derived from `MeterValues` includes correct `power_source` and `power_measured_at` fields
**And** unit tests verify the 60-second DSMR staleness threshold triggers `DegradedDeviceState`

---

### Story 4.4: Implement device capability profile system and capability gate

As a developer,
I want each supported device model to have a versioned capability profile that gates unvalidated capabilities from being queried by higher layers,
So that the decision engine and PolicyGuard in Epic 8 are never offered capabilities that have not been validated for that model and firmware version.

**Acceptance Criteria:**

**Given** a device adapter is initialized for a specific device model and firmware version
**When** `get_capabilities()` is called
**Then** it returns the matching `DeviceCapabilityProfile` from the capability registry (keyed by model + firmware version range)
**And** `DeviceCapabilityProfile` declares: supported read capabilities, supported write/control capabilities (as a typed enumeration), and known limitations for that firmware version
**And** unsupported capabilities are absent from the profile or explicitly marked as `CapabilityStatus.unsupported` — they are never exposed as available to higher layers

**Given** a higher layer (Epic 7 decision engine or Epic 8 PolicyGuard) queries a device capability profile
**When** the query resolves
**Then** the profile accurately reflects only validated capabilities for that model/firmware
**And** no control command is dispatched from Epic 4 — dispatching based on capability is PolicyGuard's responsibility in Epic 8

**Given** a device firmware version does not match any known capability profile entry
**When** the adapter initializes
**Then** read-only state (`get_state()`) is still exposed if safe — unknown firmware does not block visibility
**And** all write/control capabilities are blocked by default for unknown firmware
**And** the device is marked REDUCED capability with reason: `"capability_profile_unknown: model={model} firmware={firmware}"`
**And** a structured log entry is written: `event="capability_profile_unknown"`, `component="adapters"`, including model and firmware

**And** capability profiles live in `adapters/capabilities/` as per-model definitions
**And** FR6b is enforced here: unvalidated or unsupported capabilities are never present in the profile exposed to higher layers

**Tests:**
**And** unit tests verify that known firmware versions return a profile with the expected capabilities
**And** unit tests verify that unknown firmware versions return a REDUCED profile with all write/control capabilities absent
**And** unit tests verify that a device with unknown firmware still exposes read state without error
**And** unit tests verify the limitation reason string correctly identifies the model and firmware

---

### Story 4.5: Implement device discovery and connection management

As a developer,
I want the abstraction layer to discover devices using configured endpoints and reconnect automatically after failures,
So that the installer setup wizard has a reliable device list and the system self-heals after network interruptions without manual intervention.

**Acceptance Criteria:**

**Given** a device discovery scan is triggered
**When** the scan runs
**Then** Modbus TCP devices are probed at configured IP addresses or IP ranges — configured endpoints are first-class; blind subnet broadcast is not the default
**And** OCPP EV chargers are discovered by their WebSocket connection and `BootNotification` registration — they initiate the connection; they are not scanned
**And** DSMR meters are detected by probing the configured serial port(s) or TCP endpoint(s) — not by general network scanning
**And** each discovered device is returned with: protocol, address, detected model (if identifiable), and initial capability classification (FULL / REDUCED / UNKNOWN based on capability profile lookup)

**Given** a connected device becomes unreachable after a successful initial connection
**When** the device adapter detects the failure (via `ProtocolDegradedState` from the underlying protocol adapter)
**Then** the protocol adapter (Epic 3) handles low-level reconnection attempts with backoff
**And** the device abstraction layer translates the reconnecting/unavailable protocol state into domain `DegradedDeviceState(reason="reconnecting")` for the duration of the reconnection window
**And** StateStore (Epic 5) owns the system-wide current view of device states — Epic 4 does not maintain global system state
**And** once the protocol adapter recovers and `get_raw_state()` returns valid data, `get_state()` returns the normalized domain state

**Given** the system restarts after a reboot or power loss
**When** the startup sequence runs
**Then** device adapters re-establish all previously configured connections automatically using stored configuration
**And** Epic 4 contributes to reboot recovery by restoring configured device connections; full system operational recovery within 2 minutes (NFR-R3) is validated across StateStore initialization, control loop startup, and health readiness — not Epic 4 alone

**Tests:**
**And** integration tests using a simulated `ProtocolAdapter` returning `ProtocolDegradedState` verify that `DegradedDeviceState(reason="reconnecting")` is returned during the reconnection window
**And** integration tests verify that once the simulated adapter recovers, `get_state()` returns the normalized domain state
**And** tests verify that OCPP discovery correctly registers a newly connecting charger via `BootNotification`

---

## Epic 5: State Aggregation and Real-Time Distribution

Epic 5 establishes the single source of truth for system state and distribution. It does not contain decision logic or device communication.

**Requirements covered:** AR14, AR11, NFR-P4

---

### Story 5.1: Define system state model and implement StateStore with immutable versioned snapshots

As a developer,
I want a StateStore that holds an immutable, versioned SystemSnapshot and defines all global and per-component state types,
So that every higher layer reads a consistent, point-in-time system view from a single source without mutating shared state.

**Acceptance Criteria:**

**Given** the state package is initialized
**When** state types are imported
**Then** global system states are defined: `NORMAL` / `DEGRADED` / `STALE` / `FAILED`
**And** per-component states are defined: `IDLE` / `PENDING` / `ACTIVE` / `ERROR` / `UNAVAILABLE` / `STALE`
**And** `SystemOperatingMode` enum is defined: `normal` / `degraded` / `conservative` / `fail_safe` — consumed by Epic 7 and Epic 8
**And** the authoritative `SystemOperatingMode` → UI state mapping contract is:
| `SystemOperatingMode` | UI global state | Notes |
|---|---|---|
| `normal` | `NORMAL` | All devices healthy |
| `degraded` | `DEGRADED` | One or more devices in error/unavailable |
| `conservative` | `DEGRADED` | Reduced-capability mode; visually identical to degraded |
| `fail_safe` | `FAILED` | System halted; no control actions permitted |
**And** `STALE` is a per-device UI state derived from `data_age_seconds > 60`, not a `SystemOperatingMode` value — it overlays device cards independently of global state
**And** `SystemSnapshot` is a frozen dataclass or Pydantic model containing:
- `sequence_id`: monotonically increasing integer — increments by 1 with every new snapshot
- `captured_at`: datetime
- `global_state`: derived from per-device states (see derivation rule below)
- `operating_mode`: `SystemOperatingMode`
- per-device state slots: `inverter`, `battery`, `ev_charger`, `grid_meter` — each typed as `DeviceState | DegradedDeviceState | None`
- per-device `data_age_seconds`: float per slot, computed at snapshot construction time
- per-device component state: one of the per-component state enum values, including `STALE` and `UNAVAILABLE`
- `system_clock_status`: forwarded from startup state recorded in Story 1.3

**Global state derivation (evaluated in priority order at snapshot construction time):**
**And** `global_state = FAILED` if any critical device slot contains `DegradedDeviceState` with an ERROR-class reason (unrecoverable failure)
**And** `global_state = STALE` if any required device slot has component state `STALE`
**And** `global_state = DEGRADED` if any device slot contains `DegradedDeviceState` (including reconnecting states)
**And** `global_state = NORMAL` if all required device slots are healthy and no higher-priority condition applies
**And** the derivation priority is strict: FAILED > STALE > DEGRADED > NORMAL — a FAILED device cannot yield NORMAL or DEGRADED

**StateStore construction contract:**
**Given** the StateStore is running and a publisher calls `StateStore.publish(device_states: dict[DeviceRole, DeviceState | DegradedDeviceState])`
**When** the publish executes
**Then** StateStore constructs a new immutable `SystemSnapshot` from the provided device states — it does NOT pull from adapters directly
**And** the publisher is the control loop (Epic 8) or the startup sequence — no other component calls `publish()`
**And** `sequence_id` is incremented by exactly 1 from the previous snapshot on every publish call
**And** the new snapshot atomically replaces the previous one using an asyncio-safe single-writer lock — no concurrent reader sees a partially-constructed snapshot

**Given** `StateStore.get_snapshot()` is called from any component
**When** it executes
**Then** it returns the latest `SystemSnapshot` without acquiring locks — snapshots are immutable and safe for concurrent reads
**And** no component reads device adapters directly; all device state flows through StateStore (AR14)

**Thread-safety guarantee:**
**And** StateStore supports concurrent readers and a single writer — the write lock is held only during snapshot construction and atomic replacement, not during reads
**And** no shared mutable state exists between concurrent readers — each reader receives the same immutable snapshot reference

**Tests:**
**And** unit tests verify that publishing a new snapshot does not mutate any previous snapshot held by other references
**And** unit tests verify that `sequence_id` increments by exactly 1 on every publish, including under rapid successive calls
**And** unit tests verify the global state derivation rule at each priority level: FAILED, STALE, DEGRADED, NORMAL — one scenario per level
**And** unit tests verify that `system_clock_status` from startup is preserved correctly in every snapshot
**And** concurrency tests verify that concurrent readers see consistent, non-torn snapshots during a concurrent publish

---

### Story 5.2: Implement SSE state stream with role-filtered serialization and event metadata

As a developer,
I want an SSE endpoint that streams role-filtered, versioned system state snapshots as server-sent events with standard metadata,
So that the homeowner and installer UIs receive live state in real time, each seeing only the data appropriate for their role, with SSE event IDs for ordering and reconnection.

**Acceptance Criteria:**

**Given** an authenticated homeowner opens `/api/stream/state`
**When** StateStore publishes a new snapshot
**Then** an SSE event is delivered to the homeowner connection with:
- `event: state_update`
- `id: <snapshot.sequence_id>`
- data payload containing: global state, `operating_mode`, and the four device state values (battery soc, pv power, grid power, ev status) in normalized form
**And** installer-diagnostic fields (per-component state enums, `DegradedDeviceState` details, device error reasons) are absent from the homeowner payload — stripped at serialization, not at the query layer

**Given** an authenticated installer opens `/api/stream/state`
**When** StateStore publishes a new snapshot
**Then** the SSE event contains the full snapshot including per-component state flags, `DegradedDeviceState` details, and degraded-mode context
**And** the installer payload is a strict superset of the homeowner payload — no homeowner-visible field is absent from the installer stream
**And** the `id` field in the SSE event carries the same `sequence_id` value regardless of role

**Given** an SSE client reconnects after a disconnect and sends a `Last-Event-ID` header
**When** the server receives the reconnection
**Then** the server sends the current snapshot immediately on reconnect — full event replay is not required, but the current state is never withheld on reconnect

**Given** multiple concurrent clients connect to `/api/stream/state` (installer + homeowner + multiple browser tabs)
**When** StateStore publishes a snapshot
**Then** all active connections receive the event independently — each connection maintains its own write buffer
**And** no shared mutable state exists between connections — one slow or blocked client does not delay or block delivery to others

**Given** an unauthenticated client attempts to connect
**When** the connection request is evaluated
**Then** it is rejected with HTTP 401 before any SSE frames are sent

**Given** a connected client's session expires
**When** expiry is detected on the next snapshot push
**Then** the SSE connection is closed with no further events sent after the expiry point

**Given** no new snapshot has been published in the last 25 seconds
**When** the keepalive timer fires
**Then** an SSE comment line (`: keepalive`) is sent to all active connections — preventing proxy and browser connection timeouts

**Tests:**
**And** unit tests verify that homeowner-serialized payloads do not contain installer-diagnostic keys — verified against an explicit field allowlist, not an exclusion list
**And** unit tests verify that installer-serialized payloads contain all homeowner-visible fields plus installer-specific fields
**And** unit tests verify that SSE events include the correct `event: state_update` and `id: <sequence_id>` metadata fields
**And** integration tests verify that a new StateStore snapshot triggers an SSE event on all active connections within 30 seconds (NFR-P4)
**And** integration tests verify that multiple concurrent connections each receive events independently without one blocking another

---

### Story 5.3: Implement HTMX polling endpoints and per-device stale and unavailable detection

As a developer,
I want HTMX polling endpoints that serve the same state as SSE using server-rendered fragments, and configurable per-device stale detection that overlays freshness metadata without discarding last-known device state,
So that UI components degrade gracefully when SSE is unavailable and users always see honest data-freshness indicators rather than stale values silently presented as current.

**Acceptance Criteria:**

**Given** a UI component polls a state fragment endpoint (e.g., `/fragments/battery-card`, `/fragments/ev-card`)
**When** the request arrives with a valid session
**Then** the server renders an HTMX-compatible HTML fragment from the current `SystemSnapshot`
**And** the rendered fragment is indistinguishable from a fragment produced from an SSE event update — same markup, same data, no loading state difference

**Stale detection — overlay model (not destructive):**
**Given** a device's last state update is older than `STALE_THRESHOLD_SECONDS` (default: 30, configurable via `pydantic-settings` environment variable) at snapshot construction time
**When** StateStore constructs the next snapshot
**Then** the per-component state for that device slot is set to `STALE`
**And** the last known `DeviceState` is retained in the snapshot — STALE is a derived overlay on top of the last known state, not a replacement or zeroing
**And** `data_age_seconds` for that slot is set to the elapsed time since the last received state
**And** the decision engine (Epic 7) may read the last known `DeviceState` from a STALE slot — it is not nulled or removed

**Given** a device's component state is `STALE`
**When** the HTMX fragment for that device is rendered
**Then** the fragment renders a "last updated X min ago" caption in slate-400 at a stable layout position — layout does not shift between fresh and stale states
**And** when a fresh update arrives and the component state returns to a non-STALE value, the caption disappears without layout shift

**UNAVAILABLE vs STALE distinction:**
**Given** the system has just started and no state has ever been received for a device
**When** any state fragment is requested for that device
**Then** the device slot has component state `UNAVAILABLE` (never received data)
**And** the fragment renders an explicit unavailable state — not zero values (0 kW, 0%) and not a stale caption
**And** `UNAVAILABLE` (never received any data) and `STALE` (data was received but is now too old) are distinct values in both the snapshot model and the rendered fragment — the distinction is preserved for installer debugging

**Given** `system_clock_status` in the snapshot is `suspect` or `unknown`
**When** any state fragment is rendered
**Then** the clock-status value is available to fragment templates for conditional rendering — the fragment rendering layer does not suppress or modify it

**Tests:**
**And** unit tests verify that a device slot with last state older than `STALE_THRESHOLD_SECONDS` has component state `STALE` in the snapshot, and that the last known `DeviceState` is still present (not replaced)
**And** unit tests verify that a device that has never sent any state has component state `UNAVAILABLE` — not `STALE`
**And** unit tests verify that `STALE_THRESHOLD_SECONDS` is read from configuration and applied correctly at snapshot construction
**And** integration tests verify that HTMX polling endpoints return the same normalized values as the SSE stream for the same snapshot
**And** unit tests verify that the stale caption is present in the rendered fragment when component state is `STALE`, and absent when the device is fresh
**And** concurrency tests verify that rapid snapshot updates do not cause `sequence_id` to be observed out of order by concurrent SSE readers

**Cross-story constraints applying to all Epic 5 stories:**
- StateStore is the single publish interface — no component reads from device adapters directly (AR14 enforced here)
- Snapshot replacement is atomic; concurrent reads require no lock (asyncio-safe single-writer pattern)
- `sequence_id` monotonically increases and is the authoritative event ID for SSE ordering and client reconnection
- `STALE` and `UNAVAILABLE` are always distinct states in both snapshot model and UI rendering — never conflated
- `STALE_THRESHOLD_SECONDS` is configuration-controlled — not hardcoded anywhere in Epic 5
- No decision logic or device communication lives in Epic 5

---

## Epic 6: Observability and Event Logging

Dual logging infrastructure with a defined event contract: structured JSON to stdout (operational) and SQLite `event_log` table (installer audit). Event schema, append-only guarantees, plain-language summary requirement, actor attribution, schema versioning, retention enforcement, and the `config_audit_log` table are established here. All subsequent epics write to this via `ObservabilityService`; Epic 11 reads from it.

**Requirements covered:** FR15, FR20 (storage), FR21 (note storage), FR33 (recovery event storage), AR17, NFR-D2, NFR-D3, AR19

---

### Story 6.1: Define event log schema and implement ObservabilityService with dual-sink logging

As a developer,
I want a central ObservabilityService that routes audit-worthy events to both structlog stdout and the SQLite event_log table, while allowing operational-only logs to use structlog directly,
So that the installer audit log contains only meaningful, human-readable events and operational process logs are not polluted with audit noise.

**Acceptance Criteria:**

**Given** the observability package is initialized
**When** a component calls `ObservabilityService.audit(...)` to log an audit-worthy event
**Then** the call writes to both sinks:
- A structlog JSON line to stdout with `audit=true` to distinguish audit events from operational-only log lines
- A row to the `event_log` SQLite table
**And** operational-only debug and process logs (e.g., adapter polling cycles, cache operations, connection heartbeats) call `structlog` directly — they do NOT go through `ObservabilityService.audit()` and do NOT create `event_log` rows

**Given** an audit event is written
**When** the `event_log` row is constructed
**Then** the `event_log_entry` schema is: `id` (autoincrement), `schema_version` (integer — the current schema version constant), `timestamp` (timezone-aware UTC — DB storage format is not over-constrained; external API and UI representation must always be ISO 8601 UTC), `actor` (`system` / `installer` / `homeowner`), `event_type` (`DECISION` / `DEVICE` / `SYSTEM` / `CONSTRAINT` / `INSTALLER`), `summary` (plain-language str, required, non-empty), `detail` (optional JSON blob), `device_id` (nullable str), `config_version` (nullable str)
**And** `schema_version` is present in every entry — future event schema changes must remain backward-compatible for UI rendering; the UI must handle older schema versions without errors
**And** the `summary` field is validated non-empty at write time — a missing or blank summary is a programming error that raises immediately, not a silent truncation
**And** `actor` and `event_type` values outside the defined enums are rejected at write time

**Given** the Epic 6 test utilities are imported in a subsequent epic's test suite
**When** a test needs to verify that an audit event was emitted
**Then** `FakeAuditLog` (a test double provided by Epic 6) is used to capture and assert audit events by type and actor — without requiring a real SQLite database in that test

**Tests:**
**And** unit tests verify that `ObservabilityService.audit()` produces both a structlog output line with `audit=true` and a database row with matching fields
**And** unit tests verify that a blank or null `summary` raises at write time
**And** unit tests verify that out-of-enum `actor` or `event_type` values are rejected at write time
**And** unit tests verify that `schema_version` is present and correct in every written row
**And** unit tests verify that `FakeAuditLog` correctly captures emitted events and supports assertion by `event_type` and `actor`

---

### Story 6.2: Enforce append-only semantics and event retention policy

As a developer,
I want the event log repository to enforce append-only storage and a retention policy that protects critical event classes from normal pruning,
So that the installer audit log is a reliable, tamper-resistant record where no system event is ever silently dropped or overwritten.

**Acceptance Criteria:**

**Given** an entry has been written to the `event_log` table
**When** any code path attempts to update or delete it outside of the retention pruning job
**Then** the repository layer rejects the operation — no `UPDATE` or `DELETE` is permitted on `event_log` rows except by the designated retention pruning job

**Given** the retention pruning job runs
**When** it evaluates entries for deletion
**Then** entries older than 90 days are eligible for deletion (NFR-D2) — with the exception of critical event classes
**And** critical event classes (CONSTRAINT enforcement, command failure, fail-safe transitions, watchdog recovery events) are protected from normal pruning and are never deleted before 90 days have elapsed (NFR-D3)
**And** after the 90-day minimum has elapsed, long-term retention of critical events may be configurable or deferred to a future decision — but the pruning job must never delete critical entries before the minimum window

**No-silent-failures contract:**
**Given** a constraint violation, command failure, or degraded-mode transition occurs anywhere in the system
**When** the condition is handled
**Then** an audit event must be emitted via `ObservabilityService.audit()` — this requirement is enforced by:
- mandatory call-site patterns established in each subsequent epic that handles these conditions
- integration tests (added per epic) that verify each failure path emits an audit event using the `FakeAuditLog` helper from Story 6.1
**And** the event log write layer enforces valid event structure, append-only storage, and required summaries — it cannot detect events that were never sent to it; call-site discipline and integration tests are the mechanism for coverage

**Tests:**
**And** unit tests verify that calling an update or delete repository method on an existing log entry raises
**And** unit tests verify that the retention pruning job deletes entries older than 90 days — except entries with critical event classes
**And** unit tests verify that critical entries exactly at the 90-day boundary are not deleted by the pruning job

---

### Story 6.3: Implement installer note storage and config audit log

As a developer,
I want installer note entries writable to the event log and a config audit log table that records all safety-relevant configuration changes with structured before/after values,
So that installers can annotate the audit trail with context and every configuration change affecting system behavior is durably traceable with full unit and structure preserved.

**Acceptance Criteria:**

**Given** an installer submits a freetext note
**When** the note is processed
**Then** the note body is sanitized for safe HTML rendering before storage
**And** notes exceeding 2,000 characters are rejected with a validation error — not silently truncated
**And** empty or whitespace-only notes are rejected with a validation error
**And** the note is written as an `event_log` entry with `actor=installer`, `event_type=INSTALLER`, and the sanitized text as `summary`
**And** installer notes are subject to the same append-only guarantee as all other event log entries

**Given** a safety-relevant configuration change is activated
**When** the activation is committed
**Then** a row is written to the `config_audit_log` table with: `actor`, `timestamp` (UTC), `field` (the specific configuration field changed), `previous_value` (JSON), `new_value` (JSON), `config_version`
**And** the following configuration fields are considered safety-relevant and produce audit rows when changed: peak consumption limit, battery reserve floor, EV charging window preference, energy strategy default, device role assignment, capability override or acceptance (where applicable)
**And** `previous_value` and `new_value` are stored as JSON — not plain strings — to preserve unit context and structured values (e.g., `{"value": 3.5, "unit": "kW"}`)
**And** the `config_audit_log` schema is introduced in Epic 6 and consumed by Epic 9's staged constraint validation flow (AR19)
**And** `config_version` is a monotonically increasing identifier that links the audit record to the specific configuration state that produced it

**Tests:**
**And** unit tests verify that an installer note produces a correctly typed `event_log` row with `event_type=INSTALLER` and sanitized content
**And** unit tests verify that notes exceeding 2,000 characters are rejected with a validation error
**And** unit tests verify that empty or whitespace-only notes are rejected with a validation error
**And** unit tests verify that a config change produces a `config_audit_log` row with correct JSON `previous_value` and `new_value` and actor attribution
**And** unit tests verify that `config_version` increments on each activation
**And** unit tests verify that all defined safety-relevant config fields produce audit rows when changed

**Cross-story constraints applying to all Epic 6 stories:**
- `ObservabilityService.audit()` is the sole entry point for all audit-worthy events — no direct `INSERT` into `event_log` from outside the observability package
- Operational-only structlog calls (adapter polling, connection heartbeats, cache operations) do not go through `ObservabilityService.audit()` and do not create `event_log` rows
- All timestamps are timezone-aware UTC; DB storage format is not over-constrained but external API/UI representation is always ISO 8601 UTC
- `schema_version` is present in every `event_log` row; UI rendering must handle older schema versions without error
- `FakeAuditLog` test helper is provided by Epic 6 for use in all subsequent epics to assert audit event emission without a real database

---

## Epic 7: Decision Engine

The decision engine evaluates typed evaluation input objects built from `StateStore` snapshots and produces a typed `EvaluationResult` with structured intents, decision reasons, and cycle timing. Pure decision logic — no adapter calls, no database writes, no web or session access. Fully testable in isolation without hardware.

**Requirements covered:** FR7, FR8, FR9, FR10, FR11, FR11b, FR12, AR7, AR8, NFR-P1, NFR-R2

---

### Story 7.1: Implement SystemOperatingMode derivation and degradation matrix

As a developer,
I want the decision engine to derive a recommended `SystemOperatingMode` from the evaluation input before each cycle,
So that all energy decisions are conditioned on an explicit, deterministic mode rather than ad hoc per-device checks scattered through the decision logic.

**Acceptance Criteria:**

**Given** the decision engine begins an evaluation cycle
**When** `SystemOperatingMode` is derived from the evaluation input
**Then** the degradation matrix (AR8) maps the set of `DegradedDeviceState` entries in the input to exactly one of: `normal` / `degraded` / `conservative` / `fail_safe`
**And** the mapping is deterministic — the same set of degraded devices always produces the same mode
**And** `fail_safe` is produced when loss of critical device state prevents safe control decisions; other degraded combinations produce `degraded` or `conservative` per the matrix rules
**And** the derived mode is the first input consumed by all subsequent decision logic in the same cycle

**Operating mode ownership boundary:**
**And** the decision engine derives a *recommended* operating mode for its own decision purposes — it does not set `SystemOperatingMode` on `StateStore` or apply fail-safe transitions to the runtime
**And** `StateStore` remains the authoritative source for the UI-readable current operating mode
**And** Epic 8 (runtime loop) is responsible for applying fail-safe transitions based on the `recommended_operating_mode` in `EvaluationResult`

**Tests:**
**And** unit tests cover each operating mode outcome with the specific `DegradedDeviceState` combinations that produce it
**And** unit tests verify determinism: the same inputs always produce the same mode

---

### Story 7.2: Implement peak limiting logic consuming PeakContext

As a developer,
I want the decision engine to enforce peak consumption limits using a `PeakContext` object supplied as part of the evaluation input,
So that peak limiting logic is testable without a database and the authoritative billing record remains outside the pure decision engine.

**Acceptance Criteria:**

**Given** the decision engine receives an evaluation input containing a `PeakContext`
**When** peak limiting logic runs
**Then** it reads peak data exclusively from `PeakContext` — it does NOT access the `peak_intervals` table or the `PartialIntervalTracker` directly
**And** `PeakContext` contains: `current_partial_window_projection_kw` (real-time control signal), `configured_peak_limit_kw`, `current_monthly_recorded_peak_kw`, `current_interval_start` (clock-aligned datetime), `current_interval_elapsed_seconds`
**And** if `current_partial_window_projection_kw` would exceed `configured_peak_limit_kw`, the engine produces a load-reduction intent (battery discharge, EV charge rate reduction)
**And** the 15-minute interval boundaries are clock-aligned (e.g., :00, :15, :30, :45) — not rolling from first observation

**Dual tracking contract (AR7):**
**And** the `PartialIntervalTracker` (in-memory real-time signal) and `peak_intervals` table (authoritative billing record) remain distinct mechanisms — the decision engine consumes neither directly; it consumes `PeakContext` only
**And** the decision engine never writes to `peak_intervals` — completed interval aggregation and persistence belong to the runtime infrastructure (Epic 8 or storage layer), not the decision engine

**Tests:**
**And** unit tests verify that a projected overshoot produces a load-reduction intent
**And** unit tests verify that the engine reads only from `PeakContext` fields and performs no database access
**And** unit tests verify clock-aligned interval boundary detection using `current_interval_start` from `PeakContext`

---

### Story 7.3: Implement energy strategy evaluation and deterministic conflict resolution

As a developer,
I want the decision engine to apply the active strategy using available local data and resolve conflicting energy demands with an explicit deterministic tiebreaker,
So that strategy selection measurably changes system behavior, v1 operates without optional external data, and equal-priority conflicts always resolve identically.

**Acceptance Criteria:**

**Given** the active strategy is `Minimize Cost`
**When** the engine evaluates a cycle in v1
**Then** it uses peak avoidance and local or default tariff assumptions as primary cost drivers — it does not require dynamic tariff data (FR40/FR41 are nice-to-have; Epic 13 enriches behavior if configured)
**And** battery charging is timed to minimize grid draw during expected high-cost periods based on available local signals

**Given** the active strategy is `Maximize Self-Consumption`
**When** the engine evaluates a cycle in v1
**Then** it uses current PV production from the evaluation input and recent consumption trends to maximize use of locally generated energy before drawing from the grid
**And** solar forecast data may improve optimization if configured via Epic 13 but is not required — the strategy functions fully on local data

**Given** the active strategy is `Prioritize EV`
**When** the engine evaluates a cycle
**Then** it prioritizes available power toward the EV charge session within the configured charging window preference

**Given** conflicting energy demands exist (e.g., EV charging vs. battery reserve vs. peak limit)
**When** conflict resolution runs
**Then** it applies the deterministic priority model: safety constraints > optimization goals > convenience preferences (FR11b)
**And** if two intents remain at equal priority after the hierarchy is applied, the engine resolves the tie using a documented, stable tiebreaker — either a fixed device role priority order or an explicit numeric priority weight — never wall-clock time, random ordering, or unordered collection iteration

**Tests:**
**And** unit tests verify each strategy produces meaningfully different intents from the same evaluation input
**And** unit tests verify the deterministic conflict resolution order with scenarios exercising each priority level
**And** unit tests verify that the equal-priority tiebreaker produces identical output across repeated calls with the same input

---

### Story 7.4: Implement battery control and EV scheduling logic

As a developer,
I want the decision engine to produce battery charge/discharge intents and EV session intents using capability summaries and a homeowner override flag supplied in the evaluation input,
So that battery and EV decisions respect configured constraints and homeowner preferences without the decision engine accessing adapters or web session state.

**Acceptance Criteria:**

**Given** the decision engine evaluates battery control
**When** it produces a battery intent
**Then** it reads battery capabilities from a capability summary included in the evaluation input — it does NOT call `DeviceAdapter.get_capabilities()` during evaluation
**And** the intent never violates the configured battery reserve floor — a discharge intent that would bring SoC below the floor is blocked and replaced by a hold intent
**And** the intent is bounded by the supplied capability summary — capabilities not present in the summary are not included in the intent

**Given** the EV is connected and an active charging window preference is configured
**When** the engine evaluates EV scheduling
**Then** it produces a charge intent only within the configured window, unless the homeowner override flag is set in the evaluation input
**And** the homeowner EV override is a boolean flag in the evaluation input — the decision engine does NOT read web/session state or the HTTP request
**And** if the charger's capability summary includes dynamic rate adjustment, the intent may include a target charge rate; otherwise it is a binary start/stop intent

**Given** the homeowner override flag is set in the evaluation input
**When** the engine evaluates
**Then** it produces a charge intent regardless of the configured window — bounded by active safety constraints (peak limit, battery reserve floor)

**Tests:**
**And** unit tests verify that a battery discharge intent reaching the reserve floor is blocked and replaced by a hold intent
**And** unit tests verify that an EV charge intent outside the window is suppressed unless the override flag is set
**And** unit tests verify that a dynamic rate intent is only produced when the capability summary supports it
**And** unit tests verify that no adapter objects are accessed and no web/session state is read during evaluation

---

### Story 7.5: Define EvaluationResult contract with decision reasons and cycle timing

As a developer,
I want the decision engine to return a typed `EvaluationResult` with one resolved intent per device role, plain-language decision reasons per intent, and cycle timing,
So that the runtime loop in Epic 8 has an unambiguous, fully annotated output to act on, and the event log can explain every decision in human-readable form.

**Acceptance Criteria:**

**Given** an evaluation cycle completes
**When** the engine returns its output
**Then** it returns a typed `EvaluationResult` containing: `cycle_id` (UUID), `evaluated_at` (datetime), `recommended_operating_mode` (`SystemOperatingMode`), `intents` (list of typed intent objects — one final resolved intent per device role), `decision_reasons` (list of reason annotations, one per intent), `cycle_duration_ms` (int)
**And** each intent is a typed object (e.g., `BatteryIntent`, `EVChargerIntent`) — not a raw dict or string
**And** no `Command` object is produced by the decision engine — intent-to-command translation happens in Epic 8

**Intent resolution contract:**
**And** multiple candidate intents per role may be considered internally during conflict resolution — only the final resolved intent per role is included in `EvaluationResult.intents`
**And** the output contains no conflicting or redundant intents for the same device role

**Decision reasons:**
**And** each intent in `EvaluationResult.intents` has a corresponding entry in `decision_reasons` — a plain-language or structured reason-code explanation specific enough to produce a meaningful installer-visible audit log summary (e.g., `"Battery discharge blocked: SoC 22% is below configured reserve floor of 25%"`)
**And** `decision_reasons` entries must never be generic placeholders — they must reference the specific values and constraints that drove the decision

**Cycle timing:**
**And** `cycle_duration_ms` reflects the actual elapsed evaluation time
**And** the runtime loop in Epic 8 is responsible for detecting slow cycles and triggering watchdog recovery — the decision engine reports timing in the result but does not self-recover

**Tests:**
**And** unit tests verify that `EvaluationResult` contains all required fields with correct types
**And** unit tests verify that `intents` contains at most one entry per device role
**And** unit tests verify that each intent has a corresponding non-empty, specific `decision_reason`
**And** unit tests verify that `cycle_duration_ms` is recorded and reflects elapsed time

**Cross-story constraints applying to all Epic 7 stories:**
- The decision engine performs no adapter calls, no database writes, and no web or session access during evaluation — all inputs are supplied through explicit evaluation input objects; unit tests verify this boundary without requiring real adapters, a real database, or a running web server
- `PeakContext` is the only mechanism through which peak data enters the engine — no direct table reads
- Device capabilities enter through capability summaries in the evaluation input — no `DeviceAdapter` calls during evaluation
- Homeowner override state enters as a flag in the evaluation input — no HTTP or session reads
- The decision engine derives `recommended_operating_mode` for its own logic; `StateStore` is the UI authority; Epic 8 applies fail-safe transitions

---

## Epic 8: Control Execution and Safety Enforcement

The runtime control loop orchestrates evaluation cycles, polls device adapters, constructs evaluation input, translates decision engine intents into device commands through a two-stage pipeline (`IntentExecutor` → `PolicyGuard`), and dispatches commands exclusively through `PolicyGuard`. Handles retries, timeouts, acknowledgment verification, peak interval persistence, watchdog heartbeat, and fail-safe transition and recovery.

**Requirements covered:** FR13, FR14, FR32, FR33, FR34, AR15, AR16, NFR-I1, NFR-I2, NFR-P2, NFR-R2, NFR-R5

---

### Story 8.1: Implement the runtime control loop with adapter polling and StateStore publishing

As a developer,
I want a runtime control loop that polls device adapters, converts failures to DegradedDeviceState, publishes fresh system state to StateStore, and constructs evaluation input for each decision cycle,
So that the system continuously re-evaluates energy state from real device data and the StateStore always reflects the latest honest adapter output — never invented or stale values carried forward silently.

**Acceptance Criteria:**

**Given** the control loop is running
**When** the evaluation interval elapses (5–15 seconds under normal, configurable)
**Then** the loop polls device adapters (or consumes scheduled adapter updates) before constructing the evaluation input — device state is always fresh for each cycle
**And** polling failures are translated to `DegradedDeviceState` before any state is published to `StateStore` — the loop does not carry forward raw device state from a prior cycle without a successful fresh read
**And** `StateStore.publish()` is called with: latest device states from adapters, derived `SystemOperatingMode`, runtime health indicators, and stale/degraded overlays — it must not overwrite device state with values older than the current polling cycle

**Given** the evaluation input is assembled
**When** it is constructed
**Then** it includes: the current `SystemSnapshot`, a `PeakContext` built from the `PartialIntervalTracker` and `current_monthly_recorded_peak`, capability summaries per device, and the current homeowner override flag
**And** the assembled input is passed to the decision engine, and the resulting `EvaluationResult` is forwarded to the intent execution layer (Story 8.2)

**Given** a completed 15-minute clock-aligned interval boundary is detected
**When** the loop processes the boundary
**Then** the completed interval's peak value is written to the `peak_intervals` table — this is the only write path for `peak_intervals`; no other component writes to this table
**And** the `PartialIntervalTracker` resets for the new interval
**And** if grid meter data is stale or unavailable at the interval boundary, the `peak_intervals` row is still written with `data_quality=incomplete` — it is never silently omitted
**And** `peak_intervals` persistence continues even when the system is in `degraded` or `fail_safe` operating mode, provided valid meter data is available

**Tests:**
**And** unit tests verify that the loop constructs a complete evaluation input from mocked adapter output and `PartialIntervalTracker` state
**And** unit tests verify that a polling failure from an adapter produces `DegradedDeviceState` in the `StateStore.publish()` call — not a retained stale value from a prior cycle
**And** unit tests verify that `peak_intervals` is written at interval boundary with `data_quality=incomplete` when meter data is stale
**And** unit tests verify that `peak_intervals` persistence is not skipped when the operating mode is `degraded` or `fail_safe`

---

### Story 8.2: Implement IntentExecutor, PolicyGuard, and command dispatch

As a developer,
I want a two-stage command pipeline where IntentExecutor translates typed intents to candidate commands and PolicyGuard validates and dispatches them as the final mandatory safety gate,
So that PolicyGuard remains focused on safety enforcement rather than translation logic, and no device command can ever bypass the safety check.

**Acceptance Criteria:**

**Given** an `EvaluationResult` containing typed intents arrives at the execution layer
**When** command preparation runs
**Then** `IntentExecutor` translates each typed intent to a candidate `DeviceCommand` object — mapping intent fields to adapter-level command parameters
**And** each candidate command is then passed to `PolicyGuard.authorize_and_dispatch()` — translation and safety gate are separate, sequential responsibilities

**Given** a candidate `DeviceCommand` arrives at `PolicyGuard.authorize_and_dispatch()`
**When** PolicyGuard evaluates it
**Then** it checks the command against: active safety constraints (peak limit, battery reserve floor), command bounds, current `SystemOperatingMode`, and the declared device capability profile
**And** if the device capability profile does not include the requested capability, dispatch is blocked before any protocol command is sent — capability is a dispatch precondition, not a physical safety constraint, but it gates dispatch regardless
**And** a command that fails any check is rejected with status `rejected`: dispatch is skipped and an audit event is emitted via `ObservabilityService.audit()` with `event_type=CONSTRAINT`
**And** a command that passes all checks is dispatched to the device adapter via `send_command()`

**Given** PolicyGuard dispatches a command
**When** the adapter responds
**Then** it returns a typed `CommandResult` with fields: `correlation_id`, `device_id`, `status` (`success` / `failed` / `timeout` / `rejected`), `applied` (bool), `reason` (str), `observed_state` (optional)
**And** `CommandResult.applied` is checked before the command is considered effective (FR14)
**And** raw adapter exceptions do not propagate — all failures are represented as `CommandResult(status="failed")` (AR16)

**Given** any component outside Epic 8 needs to issue a device command
**When** it attempts to do so
**Then** it must call `PolicyGuard.authorize_and_dispatch()` — no adapter `send_command()` may be called directly from any other component (AR15)

**Integration test:**
**And** an end-to-end integration test covers the full safety pipeline: typed intent → `IntentExecutor` → candidate command → `PolicyGuard` → simulated adapter → `CommandResult` → audit event; both the allowed path and the rejected path (constraint violation) are verified

**Tests:**
**And** unit tests verify that a constraint-violating candidate command is rejected with status `rejected` and an audit event is emitted
**And** unit tests verify that a capability-missing command is blocked before the adapter `send_command()` is called
**And** unit tests verify that `CommandResult.applied=False` does not mark the command as having taken effect
**And** unit tests verify that `CommandResult.status` uses only the defined enum values: `success`, `failed`, `timeout`, `rejected`

---

### Story 8.3: Implement retry policy, timeout handling, and idempotency enforcement

As a developer,
I want failed idempotent commands retried up to twice with safety re-checks before each retry, non-idempotent commands failed closed immediately, and all commands timed out within bounds,
So that transient device failures recover automatically without risking duplicate effects or safety violations from conflicting commands.

**Acceptance Criteria:**

**Given** a command returns `CommandResult(status="failed")` or `status="timeout"`
**When** the retry policy evaluates
**Then** only commands explicitly marked as idempotent or retry-safe may be retried automatically — non-idempotent commands (e.g., EV session start/stop) fail closed and wait for the next evaluation cycle to re-evaluate from fresh state
**And** for idempotent commands: up to 2 retries after the first attempt (3 total attempts maximum); retry count is configurable via environment variable
**And** before each retry, PolicyGuard re-checks the current constraints and operating mode — if conditions have changed such that re-issuing would be unsafe, the retry is blocked and treated as a final failure
**And** after all retries are exhausted, the failure is logged as an audit event with `event_type=DEVICE` and a plain-language summary, and the next evaluation cycle re-evaluates from fresh state

**Given** a command dispatch exceeds 10 seconds (NFR-I1)
**When** the timeout elapses
**Then** the adapter returns `CommandResult(status="timeout")` — the control loop does not block beyond the timeout
**And** the timeout is treated as a failure for retry purposes; idempotency and retry rules apply

**Given** `CommandResult.applied=False` after all attempts
**When** the result is processed
**Then** the command is treated as not applied — the system does not assume the device state has changed
**And** an audit event is emitted with `event_type=DEVICE` and a plain-language summary of the unacknowledged command

**Tests:**
**And** unit tests verify that retry logic fires at most the configured number of additional times
**And** unit tests verify that a non-idempotent command is not retried — it fails closed immediately on first failure
**And** unit tests verify that a retry is blocked when PolicyGuard re-check determines re-issuing is unsafe
**And** unit tests verify that `CommandResult.applied=False` produces an audit event and does not update assumed device state

---

### Story 8.4: Implement watchdog heartbeat, stalled loop detection, fail-safe transition, and recovery

As a developer,
I want the runtime loop to send watchdog heartbeats, recover stalled cycles via supervisor-managed restart, transition to fail-safe mode on unrecoverable failure, and exit fail-safe only when all required conditions are explicitly met,
So that stalled loops recover via the process supervisor rather than silent inaction, and fail-safe entry and exit are both logged, guarded against flapping, and unambiguous.

**Acceptance Criteria:**

**Given** an evaluation cycle completes successfully
**When** the cycle ends
**Then** a watchdog heartbeat is sent: `sd_notify("WATCHDOG=1")` for systemd deployments
**And** for Docker deployments, `/health/ready` returns HTTP 200 — Docker uses this for healthcheck-triggered restart policy

**Given** 2 consecutive evaluation cycles are missed or exceed the 60-second deadline (NFR-R5)
**When** the missed-cycle counter reaches threshold
**Then** an audit event is emitted with `event="watchdog_missed"`, `event_type=SYSTEM`, and the count of missed cycles
**And** the preferred recovery path is supervisor-managed process restart: systemd watchdog for native installs; Docker restart policy on failed healthcheck for container installs
**And** internal loop restart (without process restart) may be used only for isolated, recoverable subtask failures — it must emit a distinct audit event identifying it as a subtask restart, not a supervisor recovery

**Given** a stalled loop or an unrecovered fail-safe condition is active
**When** `/health/ready` is queried
**Then** it returns HTTP 503 — not 200 — so that Docker healthcheck can trigger a container restart
**And** this 503 response persists until the condition is fully resolved

**Given** device communication is lost and `EvaluationResult.recommended_operating_mode` is `fail_safe`
**When** Epic 8 applies the mode
**Then** the control loop ceases issuing all device commands immediately
**And** an audit event is emitted with `event_type=SYSTEM`, a plain-language summary identifying which devices triggered fail-safe entry, and the timestamp
**And** the system relies on device-level safety mechanisms for the duration of fail-safe mode (FR34)

**Fail-safe exit — all conditions required before resuming commands:**
**Given** the system is in fail-safe mode
**When** exit eligibility is evaluated
**Then** exit requires all of the following: required device communication restored (adapters returning valid `DeviceState`, not `DegradedDeviceState`), a fresh `StateStore` snapshot reflecting restored state, at least one successful full evaluation cycle with no errors, and no active critical failures
**And** a single healthy adapter read is insufficient to exit fail-safe — the full evaluation cycle must succeed first
**And** a recovery audit event is emitted with `event_type=SYSTEM` identifying the specific devices that recovered and which evaluation cycle cleared the condition

**Tests:**
**And** unit tests verify that 2 missed cycles emit a watchdog audit event
**And** unit tests verify that `/health/ready` returns 503 when the control loop is stalled or in unrecovered fail-safe
**And** unit tests verify that fail-safe mode suppresses all command dispatch — no `PolicyGuard.authorize_and_dispatch()` calls are made
**And** unit tests verify that fail-safe exit requires all defined conditions — a single recovered adapter read does not exit fail-safe
**And** unit tests verify that recovery from fail-safe emits an audit event with the specific exit reason

**Cross-story constraints applying to all Epic 8 stories:**
- `PolicyGuard.authorize_and_dispatch()` is the single required dispatch path — no adapter `send_command()` is called from any other component (AR15)
- `CommandResult.status` uses only: `success` / `failed` / `timeout` / `rejected` — no other values
- `IntentExecutor` handles translation; `PolicyGuard` handles safety validation and dispatch — these are separate responsibilities with no overlap
- `peak_intervals` is written only by the runtime loop at clock-aligned interval boundaries — no other component writes to it
- Non-idempotent commands are never retried automatically — they fail closed on first failure
- Supervisor-managed restart is the primary watchdog recovery path; internal subtask restart is the exception and must emit its own audit event

---

### Story 8.5: End-to-End Decision Engine Simulation

**As an** installer or developer validating a newly configured site,
**I want** to run a scripted end-to-end simulation of the decision engine against a synthetic energy scenario,
**So that** I can confirm the full control path — from grid reading through decision through PolicyGuard dispatch — behaves correctly before live operation begins.

**Acceptance Criteria:**

**Given** the decision engine, PolicyGuard, and adapter layer are running in integration test mode
**When** a synthetic scenario is loaded (configurable grid load, solar output, battery SoC, EV presence, capacity tariff window)
**Then** the engine produces decision events matching the expected strategy output for that scenario
**And** the `PolicyGuard.authorize_and_dispatch()` path is exercised — not bypassed — for every command in the scenario
**And** the simulated commands return `CommandResult` objects with correct `status` and `applied` fields
**And** all scenario events are written to the `event_log` via `ObservabilityService.audit()` (AR17)
**And** the simulation completes without touching real device adapters — a stub adapter is used for all protocol calls
**And** the test fixture is parameterisable: at minimum, one "peak tariff — curtail load" scenario and one "excess solar — charge battery" scenario are defined

**Tests:**
**And** integration test runs the curtail-load scenario and asserts `CommandResult.status == "success"` for the expected curtailment command
**And** integration test runs the excess-solar scenario and asserts the battery charge command is dispatched and logged
**And** test confirms that if PolicyGuard rejects a command (e.g. battery below reserve floor), `CommandResult.status == "rejected"` and no adapter call is made
**And** test confirms that all commands in both scenarios produce `event_log` rows with correct `event_type` and `correlation_id`

---

## Epic 9: Installer Site Setup and Deployment Validation

The installer completes the 4-step setup wizard against the live, running system — device discovery, role assignment, constraint configuration, and deployment validation — and receives an unambiguous PASS/WARN/FAIL result before handoff. All protocol adapters, device abstraction, state layer, decision engine, and control execution are in place before this epic.

**Requirements covered:** FR1 (UI), FR2, FR3 (UI), FR4 (UI), FR6 (UI), FR16, FR17, AR6, AR9, AR19

---

### Story 9.1: Implement device discovery step with live probing, capability classification, and manual entry

As an installer,
I want to trigger a live device discovery scan from the setup wizard and see each result with its capability classification, with the ability to add devices manually when discovery misses them,
So that I have an accurate, honest view of what the system can actually reach before I assign roles.

**Acceptance Criteria:**

**Given** the installer navigates to Step 1 of the setup wizard
**When** the discovery scan runs
**Then** the scan uses live probing through the device abstraction layer: Modbus TCP devices probed at configured endpoints, OCPP chargers registered by `BootNotification`, and DSMR meters detected via configured serial/TCP paths
**And** previously configured devices (from stored configuration) may be shown in a separate "known configured devices" section — they are visually distinct from live scan results and must never be presented as newly discovered
**And** each live-discovered device appears as a row with: protocol, address, detected model, and a capability badge — `FULL` or `REDUCED`
**And** REDUCED devices display the specific limitation condition inline below the row — not in a modal

**NOT FOUND — expected but undetected:**
**And** devices that were previously configured and expected but not found in the current scan appear with a `NOT FOUND` badge — they represent a missing expected device, not a discovered device
**And** NOT FOUND rows show a "Retry scan" action

**Manual device entry:**
**Given** the installer selects "Add manually" for a NOT FOUND or entirely new device
**When** they complete the manual entry form
**Then** they can specify: protocol, address or path, model (if known), and optional firmware version
**And** the manually entered device is validated through the same capability profile system as automatically discovered devices — the same `DeviceCapabilityProfile` lookup applies
**And** manually entered devices are clearly marked with a "Not yet validated" indicator until they have been successfully connected and their capability profile confirmed
**And** a manual entry that cannot be reached or validated does not progress to role assignment without explicit installer acknowledgment

**Tests:**
**And** integration tests verify that a simulated adapter returning `DegradedDeviceState` with a capability reason produces a REDUCED row with the correct inline limitation condition
**And** unit tests verify that a device present in stored configuration but absent from the live scan result appears as NOT FOUND — not as FULL or REDUCED
**And** unit tests verify that a manually entered device with unknown firmware produces a "Not yet validated" indicator, not a FULL badge
**And** automated accessibility scan (axe-core or equivalent) of the Discovery step reports zero WCAG 2.1 AA violations (NFR-A1)
**And** all interactive elements (scan trigger, row controls, "Add manually" path) are reachable and operable via keyboard alone
**And** FULL / REDUCED / NOT FOUND badge colour combinations meet a minimum 4.5:1 contrast ratio against their background

---

### Story 9.2: Implement role assignment with inline conflict detection and gap acknowledgment

As an installer,
I want to assign energy roles to discovered and manually added devices with inline conflict detection, with grid meter absence hard-blocked and other role gaps requiring explicit acknowledgment,
So that I complete role assignment with full visibility into configuration validity — any gap I proceed past is a deliberate, recorded decision, not an oversight.

**Acceptance Criteria:**

**Given** the installer is on Step 2 (Role Assignment)
**When** they assign a role to a device using the role picker (inverter / battery / EV charger / grid meter / unassigned)
**Then** inline conflict detection runs on each change: duplicate role assignments and incompatible role/device combinations are flagged inline — no modal, no page navigation

**Grid meter role — hard block:**
**And** if no grid meter role is assigned, Step 3 is blocked with a hard blocking error: "Grid meter is required for peak limiting and safety behavior — this cannot be bypassed in v1"
**And** the installer cannot access Step 3 without a grid meter assigned

**Other role gaps — non-blocking with required acknowledgment:**
**And** other role gaps (e.g., no battery assigned, no EV charger assigned) produce an inline WARN notice with a plain-language explanation of the impact on system behavior
**And** the installer may proceed past a non-blocking role gap only after explicitly acknowledging it — the acknowledgment is recorded in setup state
**And** acknowledged gaps are surfaced to Step 4 deployment validation as WARN-class check inputs

**Tests:**
**And** unit tests verify that assigning the same role to two devices triggers an inline conflict warning
**And** unit tests verify that missing grid meter role blocks Step 3 with a hard blocking message
**And** unit tests verify that a non-blocking role gap requires explicit acknowledgment before Step 3 is accessible
**And** unit tests verify that acknowledged gaps are recorded in setup state and accessible to the validation service

---

### Story 9.3: Implement constraint configuration with staged validation and single-increment config_version

As an installer,
I want to configure per-site safety constraints using a staged draft → validate → confirm → activate flow with layered validation,
So that no constraint takes effect until I have explicitly reviewed and confirmed it, each activation is atomically committed with a single version increment, and every change is audit-logged.

**Acceptance Criteria:**

**Given** the installer is on Step 3 (Constraint Configuration) entering values (peak consumption limit, battery reserve floor, EV charging window preference)
**When** they interact with a field and move focus away
**Then** basic field validation (type, format, range bounds) runs client-side immediately — no server round trip for these checks
**And** the server is always the authoritative validator; client-side validation is supplementary and may be more permissive than the server

**Given** the installer submits the constraint draft for server-side validation
**When** the staged validation runs (AR6)
**Then** the server checks: schema correctness, capability-strategy consistency (e.g., dynamic rate strategy vs. charger capabilities against live capability profiles), and safety pre-check (constraints do not produce an immediately unsafe state given current live system state)
**And** validation failures return a typed report with field-level messages — the installer corrects and resubmits; validation does not activate anything
**And** a passing validation presents a confirmation summary displaying all constraint values before any activation occurs

**Given** the installer confirms the validated draft
**When** activation commits
**Then** the constraints are atomically activated — no partial application
**And** `config_version` is incremented exactly once for this activation, regardless of how many constraint fields changed
**And** a `config_audit_log` row is written for each changed field, all referencing the same new `config_version` (AR19)
**And** actor attribution on all audit rows reflects the authenticated installer identity
**And** the wizard marks Step 3 complete and enables Step 4

**Tests:**
**And** unit tests verify that a single activation with multiple changed fields writes multiple `config_audit_log` rows all sharing the same new `config_version`
**And** unit tests verify that `config_version` increments exactly once per activation — not once per changed field
**And** unit tests verify that a failed server-side validation blocks activation and returns field-level messages
**And** unit tests verify that atomic activation either commits all fields or none — no partial state persists on failure

---

### Story 9.4: Implement deployment validation with safe readiness probing and persisted results

As an installer,
I want Step 4 to run all 6 deployment checks concurrently with live progress, require acknowledgment before handoff on warnings, persist the validation result, and mark it outdated when configuration changes,
So that I receive an honest readiness signal before handoff — warnings are never invisible, and I always know whether the validation result reflects the current configuration.

**Acceptance Criteria:**

**Given** the installer triggers the validation run in Step 4
**When** the 6 checks run via `DeploymentValidationService` (AR9)
**Then** the checks run concurrently: connectivity, role completeness, capability-strategy consistency, constraint completeness, constraint safety pre-check, control readiness
**And** each check transitions progressively from `pending` → `PASS` / `WARN` / `FAIL` / `TIMEOUT` as it completes
**And** a TIMEOUT result displays with a WARN badge and a connectivity-focused corrective action

**Control readiness probing — safe only:**
**And** the control readiness check must never issue a disruptive control action to a real device
**And** any device probe within the readiness check that could affect device state must pass through `PolicyGuard` using a safe, no-op, or minimal-impact operation — the check confirms the control pipeline is reachable and responsive, not that devices respond to real commands

**Given** all checks complete
**When** the overall result is determined
**Then** if any check is `FAIL`, the overall banner shows FAIL and the handoff button is disabled
**And** if any check is `WARN` (and none are `FAIL`), the overall banner shows "Ready with limitations" — the handoff button is enabled only after the installer explicitly acknowledges each active warning; the confirmation UI indicates "proceeding with known limitations"
**And** if all checks are `PASS`, the handoff button is enabled immediately with no additional acknowledgment required

**Validation result persistence:**
**And** the last validation result is persisted: timestamp, `config_version` at time of run, per-check outcome, and overall status
**And** if the current site `config_version` differs from the `config_version` recorded in the persisted result, Step 4 displays the result as `outdated` and disables the handoff button until the validation is re-run
**And** a constraint activation in Step 3 (which increments `config_version`) immediately marks any existing validation result as `outdated`

**Tests:**
**And** unit tests verify that each of the 6 check types returns the correct typed result
**And** unit tests verify that a FAIL result disables the handoff button regardless of other check outcomes
**And** unit tests verify that a WARN-only result enables the handoff button only after explicit per-warning acknowledgment
**And** unit tests verify that a post-validation `config_version` change marks the persisted result as `outdated`
**And** unit tests verify that the control readiness check does not invoke any adapter write path — only PolicyGuard-mediated safe probes

**Cross-story constraints applying to all Epic 9 stories:**
- Discovery uses live probing only — "known configured devices" may be displayed separately but never presented as live-discovered results
- NOT FOUND represents expected-but-undetected devices only — a newly probed device is either FULL or REDUCED, never NOT FOUND
- Manual entries are not promoted to FULL capability until successfully connected and capability profile confirmed
- Grid meter absence is a hard block at Step 2 — no bypass exists for v1
- Non-blocking role gaps require explicit installer acknowledgment before Step 3 is accessible; acknowledgment is recorded in setup state
- `config_version` increments exactly once per constraint activation, regardless of changed field count
- Validation result `config_version` is compared to current site `config_version` to determine outdated status
- Control readiness probes are PolicyGuard-mediated and safe — no disruptive device actions during validation

---

### Story 9.5: Installer Handoff Guide

**As an** installer completing the setup wizard,
**I want** to access a concise, printable handoff guide at the end of Step 4 (after a PASS result),
**So that** I can leave the homeowner with clear written instructions for reading the dashboard, requesting strategy changes, and knowing when to contact me.

**Acceptance Criteria:**

**Given** the deployment validation has completed with a PASS or PASS-with-WARN result
**When** the installer reaches the handoff screen
**Then** a "Download handoff guide" button is visible and accessible on the validation result screen
**And** triggering the button produces a static, pre-rendered PDF or printable HTML page — no dynamic content generation required
**And** the handoff guide contains: dashboard overview (what the cards mean), how to request a strategy change, what degraded-mode messages mean and when to call the installer, and installer contact placeholder fields
**And** the guide is English-only in v1 (NFR-L1)
**And** the handoff button is only shown when validation status is `complete-PASS` or `complete-WARN` — it is hidden when status is `complete-FAIL`, `running`, or `never_run`
**And** the guide download does not trigger any device action or state change

**Tests:**
**And** unit tests verify that the handoff button renders only when validation state is `complete-PASS` or `complete-WARN`
**And** unit tests verify that the handoff button is absent when validation state is `complete-FAIL`
**And** integration test confirms the guide endpoint returns a 200 response with correct content-type (`application/pdf` or `text/html`) when called after a PASS validation
**And** automated accessibility scan (axe-core) of the handoff screen reports zero WCAG 2.1 AA violations (NFR-A1)

---

## Epic 10: Homeowner Dashboard and Controls

The homeowner dashboard with card stack, real-time SSE updates with HTMX polling fallback, 4-state optimistic EV override, inline strategy selector, stale data handling, and calm degraded-mode display. FR30 weekly summary is nice-to-have and must not block the MVP dashboard.

**Requirements covered:** FR25, FR26, FR27, FR28, FR29, FR30 (nice-to-have), NFR-P3, NFR-P4

---

### Story 10.1: Implement homeowner dashboard layout with SystemOperatingMode-driven headline and metric cards

As a homeowner,
I want a dashboard that shows me the system status in plain language and current energy values with correct units,
So that I can understand the system state in under 10 seconds without any technical knowledge.

**Acceptance Criteria:**

**Given** the homeowner opens the dashboard
**When** the page loads
**Then** the status headline is derived exclusively from `SystemOperatingMode` + the active energy strategy — no component independently infers degraded state from individual device values or card data (FR26, FR29)
**And** when `SystemOperatingMode` is `normal`, the headline renders in plain language e.g., "Your home is running on solar · Minimize Cost"
**And** when `SystemOperatingMode` is `degraded` or `conservative`, the headline reads "Running with limited functionality" in slate-600 — no amber, no alarming styling; an inline explanation derived from the operating mode describes the limitation in plain language without technical detail (FR29)
**And** three metric cards display: battery charge level (%), solar production (kW), and current grid draw (kW) — labels are plain-language; numeric values include correct units (kW, %) always
**And** all values are read from `StateStore` snapshots via SSE stream or HTMX polling — never from direct adapter access
**And** the page is fully interactive within 3 seconds on Raspberry Pi 4 under normal local network conditions (NFR-P3)

**Partial device availability:**
**And** if one device slot is `UNAVAILABLE` or `STALE`, only that device's metric card shows the unavailable or stale indicator — the other cards continue rendering live values; no cascading empty dashboard

**Stale data display:**
**And** when a device's component state is `STALE`, the corresponding metric card displays a "last updated X min ago" caption in slate-400 at a stable layout position — layout does not shift between fresh and stale states
**And** when a device's component state is `UNAVAILABLE` (never received data), the metric card renders an explicit unavailable state — not 0 kW or 0%

**SSE disconnection fallback:**
**And** if the SSE connection drops, HTMX polling activates automatically — no visible "disconnected" state is shown to the homeowner
**And** if both SSE and polling fail simultaneously, the dashboard continues displaying last known values with stale indicators — not an error screen

**Tests:**
**And** unit tests verify that the status headline template reads only from `SystemOperatingMode` and active strategy — no device state fields are accessed in the headline rendering path
**And** unit tests verify that `STALE` renders a "last updated" caption and `UNAVAILABLE` renders an unavailable state (not zero values)
**And** unit tests verify that one `UNAVAILABLE` device card does not affect rendering of other cards
**And** unit tests verify that the degraded headline uses slate-600 and contains no amber CSS class
**And** automated accessibility scan (axe-core or equivalent) of the dashboard page reports zero WCAG 2.1 AA violations (NFR-A1)
**And** all interactive elements (override button, strategy selector, secondary links) are reachable and operable via keyboard alone
**And** metric card values and status text meet a minimum 4.5:1 contrast ratio against their card background in both normal and degraded states

---

### Story 10.2: Implement EV status card with 4-state optimistic override and StateStore-backed persistence

As a homeowner,
I want to see the next scheduled EV charging session and trigger an immediate override with a single tap that gives me instant feedback confirmed by actual device state,
So that I am confident the system received and applied my request — not just that an API call succeeded.

**Acceptance Criteria:**

**Idle state:**
**Given** the homeowner views the EV card with no active override
**When** the card renders
**Then** "Charge EV now" is displayed: accent background, button enabled, `aria-disabled="false"`; next scheduled session is visible without scrolling on a standard phone (FR27)

**Optimistic state — immediate on tap:**
**Given** the homeowner taps "Charge EV now"
**When** the button is first pressed
**Then** the button immediately transitions to Optimistic state: label "Starting charging…", pulse indicator, button disabled, `aria-disabled="true"`, `aria-live="polite"` announces the state change — this happens before the server responds
**And** exactly one POST is issued to `/actions/ev-override` — the button is disabled on the first tap and does not re-issue on subsequent taps

**Server-side idempotency:**
**And** the `/actions/ev-override` server endpoint enforces idempotency — duplicate requests received within an active override window are treated as success without re-triggering the override

**Confirmed state — requires StateStore confirmation:**
**Given** the server accepted the override
**When** SSE or polling delivers a new snapshot
**Then** Confirmed state is entered only when `EVChargerState.session_active = true` is observed in the `StateStore` snapshot — API success response alone is not sufficient
**And** Confirmed state renders: label "Charging now", checkmark icon, accent-hover background, button disabled, `aria-disabled="true"`, `aria-live="polite"` announces the state change
**And** if safety constraints require a reduced charge rate, a plain-language constraint notice appears in neutral styling below the button — no technical values exposed

**Fallback state — controlled retry:**
**Given** the server returns a failure or the override cannot be confirmed from StateStore
**When** the failure arrives or confirmation times out
**Then** the button transitions to Fallback state: label "Could not start charging", degraded-bg, button disabled, `aria-live="polite"` announces the state change; calm plain-language explanation shown
**And** the "Try again" link re-enables the button only after: a configurable cooldown has elapsed OR a new `StateStore` snapshot confirms the system can accept a new override attempt — preventing rapid retry loops

**Override lifecycle — StateStore-backed:**
**And** the active EV override state is tracked in `StateStore` and distributed via SSE — not stored only in Alpine.js client state
**And** the EV card correctly reflects override state after a page refresh (reads from `StateStore`) — not only from in-memory Alpine state
**And** override expiration or natural session completion is reflected automatically via SSE/polling without requiring user action; `aria-live="polite"` announces the return to Idle

**Button stability:**
**And** the button never repositions, resizes, or shifts surrounding layout across any of the 4 states
**And** Alpine.js manages the optimistic layer (Idle → Optimistic transition only); SSE state is authoritative and overrides Alpine optimistic state on receipt

**Tests:**
**And** unit tests verify state transitions: Idle → Optimistic on first tap; Optimistic → Confirmed only when `session_active=true` from StateStore; Optimistic → Fallback on server failure
**And** unit tests verify that Confirmed is NOT entered on API success response alone
**And** unit tests verify that the button is disabled after the first tap and no second POST is issued
**And** unit tests verify that the server endpoint returns success (not error) for a duplicate request within the active override window
**And** unit tests verify that Fallback retry re-enables only after cooldown or StateStore confirmation — not immediately
**And** unit tests verify that each state renders the correct `aria-live` and `aria-disabled` attributes
**And** automated accessibility scan (axe-core or equivalent) of the EV override card reports zero WCAG 2.1 AA violations in all four states (Idle / Optimistic / Confirmed / Fallback) (NFR-A1)
**And** the override button and "Try again" link are operable via keyboard alone in all states
**And** override button colour combinations (accent, degraded-bg) meet a minimum 3:1 contrast ratio for non-text UI elements and 4.5:1 for button label text

---

### Story 10.3: Implement inline strategy selector with StateStore-authoritative update and specific failure messaging

As a homeowner,
I want to switch my energy strategy with a single tap and see the headline update immediately with a specific failure message if the switch cannot be applied,
So that changing strategy feels instant and I always know definitively whether the change took effect.

**Acceptance Criteria:**

**Given** the homeowner taps the strategy display area
**When** the selector expands
**Then** it expands inline — no modal, no page navigation; three option rows are displayed (Minimize Cost / Maximize Self-Consumption / Prioritize EV)

**Given** the homeowner selects a strategy
**When** the selection is made
**Then** an immediate POST is sent to `/actions/set-strategy`
**And** the strategy change flows: POST → backend updates active strategy → `StateStore` publishes updated snapshot → SSE delivers update → headline re-renders with the new strategy
**And** HTMX may optimistically update the headline before the SSE response arrives; if SSE delivers a contradicting state, SSE wins and the headline corrects to the authoritative value

**Given** the POST fails or the strategy is not confirmed in StateStore within a timeout
**When** the failure is detected
**Then** the headline reverts to the previous strategy — no silent failure
**And** a specific plain-language message is displayed inline (e.g., "Could not change strategy — system temporarily unavailable") — not a generic indicator or spinner left in a hung state

**Tests:**
**And** unit tests verify that the headline reflects the strategy from the StateStore SSE update — not only the HTMX optimistic response
**And** unit tests verify that POST failure reverts the headline to the previous strategy and renders the specific failure message
**And** unit tests verify that no modal is opened — selector operates inline without navigation

---

### Story 10.4: Implement FR30 weekly energy summary as non-blocking, pre-aggregated secondary feature

As a homeowner,
I want to optionally see a weekly energy summary accessible via a secondary link,
So that I can review the system's impact over time without it affecting the primary dashboard performance or layout.

**Acceptance Criteria:**

**Given** FR30 weekly summary is implemented
**When** the homeowner accesses it
**Then** it is accessible via a secondary link or collapsed section — it does not appear in the primary dashboard card render path and does not affect layout or load time of Stories 10.1–10.3
**And** it displays: peaks avoided this week, self-consumption ratio, and estimated cost savings (€)
**And** all values are served from pre-aggregated data (e.g., computed by a scheduled background job or cached result) — no heavy on-demand query executes at render time

**Performance constraint:**
**And** the summary endpoint must respond within 200ms on Raspberry Pi 4 — validated by a performance test against representative data volume; live computation that would exceed this is not acceptable

**Given** insufficient history exists (system running less than 7 days)
**When** the summary is accessed
**Then** it renders a plain-language message explaining data is still being collected — not zero values or empty cards

**Implementation constraint:**
**And** this story must not block delivery of Stories 10.1–10.3; it is implemented only after the MVP dashboard is complete and functionally verified

**Tests:**
**And** unit tests verify that insufficient history renders an explanation message — not zeros
**And** unit tests verify that the summary section is not part of the primary card render path
**And** performance tests verify the summary endpoint responds within 200ms at representative data volume

**Cross-story constraints applying to all Epic 10 stories:**
- Status headline is derived exclusively from `SystemOperatingMode` + active strategy — no component independently infers degraded state from device values
- All dashboard values come from `StateStore` snapshots via SSE or HTMX polling — no direct adapter reads in any Epic 10 component
- If SSE drops, HTMX polling activates automatically with no visible disconnected state shown to the homeowner
- If both SSE and polling fail, last known values with stale indicators are shown — no error screen
- EV override state is `StateStore`-backed and SSE-driven — not only Alpine.js client state; page refresh must reflect current override state
- Confirmed state requires `EVChargerState.session_active = true` from StateStore — API success response alone is insufficient
- All interactive button states include correct `aria-live="polite"` and `aria-disabled` attributes; screen readers must correctly announce state transitions
- Weekly summary must be pre-aggregated; must not delay primary dashboard load

---

## Epic 11: Installer Operational Monitoring

Installer dashboard for site health monitoring, structured event log with filtering and pagination, anomaly surfacing, and homeowner credential management. All state is read from `StateStore` and the event log — no degradation logic lives here.

**Requirements covered:** FR18, FR19, FR20 (UI), FR21 (UI), FR23, FR29 (installer-side degraded view), NFR-P5

---

### Story 11.1: Implement installer dashboard with health indicator, per-device rows, peak tracker, and deterministic anomaly notice

As an installer,
I want a monitoring dashboard showing overall system health, per-device connectivity, peak consumption against the configured limit, and a single deterministic anomaly notice when action is needed,
So that I can assess site health in a single view without noise, without false alarms, and without navigating multiple pages.

**Acceptance Criteria:**

**Given** the installer opens the monitoring dashboard
**When** the page loads
**Then** all data is read from `StateStore` snapshots and the `peak_intervals` table — no degradation logic runs in the dashboard layer (FR18)
**And** the dashboard renders within 2 seconds under normal load

**System health indicator — direct mapping, no interpretation layer:**
**And** the system health indicator displays exactly one value derived directly from `SystemOperatingMode`:
- `normal` → NORMAL
- `degraded` or `conservative` → DEGRADED
- `fail_safe` → FAILED

**Per-device rows — component state and capability badge visually independent:**
**And** per-device rows render component state and capability badge as visually distinct elements in separate positions with distinct styling — they must never be conflated in the UI:
- Component state (ACTIVE / DEGRADED / ERROR / UNAVAILABLE): reflects connectivity and communication health
- Capability badge (FULL / REDUCED): reflects capability profile classification — a REDUCED device may be ACTIVE; a DEGRADED device may be FULL

**Peak tracker:**
**And** the current-month peak tracker displays: highest recorded 15-minute interval this month (from `peak_intervals` table) vs. configured peak limit, in kW with a plain-language label (FR19)

**Recent event log preview:**
**And** the 5 most recent event log entries are shown with timestamp (browser-local timezone), badge type, and plain-language summary; a "View all" link opens the full event log with no pre-filter
**And** "View in event log" links from device rows and health indicators pre-filter the event log by `device_id` or `event_type` as appropriate — enabling direct traceability

**Anomaly notice — single, deterministic, severity-ordered:**
**And** at most one anomaly notice is shown at a time — highest severity wins (FAIL > WARN); when multiple issues are active, the summary text aggregates them (e.g., "2 devices degraded, 1 command failure detected") rather than showing multiple notices
**And** the anomaly notice appears inline above the stat cards when `SystemOperatingMode` is DEGRADED or FAILED, or when any device is in ERROR state — it shows: severity badge, one-line aggregated description, "View in event log" link pre-filtered by event type and time window
**And** anomaly detection reads exclusively from the current `StateStore` snapshot — no additional database queries are triggered for anomaly detection
**And** the notice is dismissible per session; a dismissed notice reappears automatically if severity increases (e.g., from WARN to FAIL) or a new anomaly type appears that was not present when dismissed

**Tests:**
**And** unit tests verify that `SystemOperatingMode` values map exactly to their display counterparts with no additional interpretation
**And** unit tests verify that component state and capability badge are rendered in distinct DOM positions with distinct CSS classes — they are never the same element
**And** unit tests verify that only one anomaly notice is shown when multiple issues are active, and the highest severity wins
**And** unit tests verify that a dismissed notice reappears when severity increases or a new anomaly type appears
**And** unit tests verify that anomaly detection triggers no database query — it reads from the StateStore snapshot only
**And** performance tests verify the dashboard renders within 2 seconds under normal load

---

### Story 11.2: Implement event log UI with pagination, scoped filtered search, timezone-correct display, and resilient note entry

As an installer,
I want to browse the event log with paginated results, badge and date filters, keyword search scoped to the filtered dataset, timezone-correct timestamps, and note entry that preserves my text on failure,
So that I can find specific events efficiently and annotate the log with confidence even during poor connectivity.

**Acceptance Criteria:**

**Given** the installer opens the event log view
**When** the page loads
**Then** an initial batch of 50–100 entries loads — not the full log; additional entries load via "Load more" or incremental scroll with no full-page reload
**And** event log queries covering the past 30 days return results within 5 seconds (NFR-P5)

**Entry display:**
**And** each entry displays: timestamp rendered in the browser's local timezone with UTC offset shown (e.g., "2026-05-01 14:32 CEST / UTC+2"), event_type badge, plain-language summary, and optional device context
**And** all timestamps are stored in UTC; the rendering layer converts to local timezone — server time and client time are never mixed in the same display

**Filtering:**
**And** event_type badge filter pills (DECISION / DEVICE / SYSTEM / CONSTRAINT / INSTALLER) are shown — toggling a pill includes/excludes that type; multiple types may be active simultaneously
**And** a date range filter limits results to the selected window
**And** filter state is carried in URL query params — the filtered view is bookmarkable and shareable

**Keyword search — scoped to filtered dataset:**
**And** a keyword search box filters by substring match on `summary` — debounced 300ms, HTMX partial update replaces the entry list with no full-page reload
**And** keyword search applies only to the currently active filtered dataset (after date range and type filters are applied) — not the full unfiltered log

**Note entry — resilient on failure:**
**And** a textarea + "Add note" button allows the installer to submit a freetext note — on success, HTMX prepends a new INSTALLER-type row at the top of the log and the textarea clears (FR21 UI)
**And** notes exceeding 2,000 characters are rejected client-side before submission; the server enforces this limit authoritatively
**And** on submission failure: the textarea content is preserved (not cleared) and a specific inline error message is shown — no data loss occurs

**Tests:**
**And** unit tests verify that badge filter correctly includes/excludes entries by type
**And** unit tests verify that keyword search applies to the post-filter dataset — not the full unfiltered log
**And** unit tests verify that filter state is reflected in URL query params
**And** unit tests verify that note submission failure preserves textarea content and shows a specific inline error message
**And** performance tests verify queries for the past 30 days return within 5 seconds at representative log volume (NFR-P5)

---

### Story 11.3: Implement homeowner credential management with complexity enforcement and middleware-level password change

As an installer,
I want to create and reset homeowner credentials with enforced password complexity, and have the first-login password change requirement enforced at the middleware level,
So that homeowner accounts are secure from the moment they are created and first-login enforcement cannot be bypassed by direct navigation.

**Acceptance Criteria:**

**Given** the installer creates homeowner credentials
**When** they submit the new credentials
**Then** the password is validated against minimum complexity: length ≥ 12 characters (or equivalent entropy rule); weak passwords are rejected client-side immediately and validated server-side authoritatively — the server is always the enforcing authority
**And** the credentials are stored using PHC string format (bcrypt default) — no plaintext credentials at rest (NFR-S1)
**And** `must_change_password=true` is set on the account
**And** an audit event is emitted: `actor=installer`, `event_type=SYSTEM`, `summary="Homeowner credentials created"`
**And** the confirmation screen includes a "View in event log" link pre-filtered to show the creation audit event

**Given** the installer resets homeowner credentials
**When** the reset is confirmed
**Then** a new temporary password is set; `must_change_password=true` is re-applied
**And** all active homeowner sessions are invalidated immediately on reset
**And** an audit event is emitted: `actor=installer`, `event_type=SYSTEM`, `summary="Homeowner credentials reset"`

**must_change_password — server-side middleware enforcement:**
**And** `must_change_password` is enforced at the authentication middleware level — a homeowner with this flag set is redirected to `/change-password` when accessing any route, regardless of the URL requested
**And** direct navigation to any dashboard route while `must_change_password=true` redirects to `/change-password` — there is no client-side-only enforcement path; middleware runs before any route handler

**Tests:**
**And** unit tests verify that `must_change_password=true` is set on both create and reset operations
**And** unit tests verify that a password shorter than 12 characters is rejected both client-side and server-side
**And** unit tests verify that all active homeowner sessions are invalidated on reset
**And** unit tests verify that a homeowner with `must_change_password=true` is redirected to `/change-password` when accessing any other route — including sub-pages, not only the root
**And** unit tests verify that audit events are emitted for both create and reset operations with correct fields

**Cross-story constraints applying to all Epic 11 stories:**
- All state reads come from `StateStore` snapshots or the event log repository — no degradation logic runs in the installer dashboard or event log UI
- `SystemOperatingMode` maps directly to display values (normal→NORMAL, degraded/conservative→DEGRADED, fail_safe→FAILED) — no additional interpretation layer
- Component state (ACTIVE/DEGRADED/ERROR/UNAVAILABLE) and capability badge (FULL/REDUCED) are always rendered as visually distinct elements in separate DOM positions
- Only one anomaly notice is shown at a time; anomaly detection reads `StateStore` only — no additional DB queries triggered
- Dismissed anomaly notices reappear on severity increase or new anomaly type appearing
- All event log timestamps stored in UTC; rendered in browser-local timezone with UTC offset explicitly shown
- "View in event log" links from dashboard and credential actions pre-filter by `device_id`, `event_type`, or time window
- `must_change_password` is enforced server-side at middleware — client-side enforcement alone is not acceptable

---

## Epic 12: Platform Reliability and Configuration Management

Platform update management with pre-update validation, atomic application, and rollback; site configuration backup and restore with integrity verification, device mismatch handling, and mandatory post-restore validation gate; non-blocking data retention; and staged startup recovery with graceful degraded handling. Watchdog and fail-safe runtime behavior are implemented in Epic 8.

**Requirements covered:** FR22, FR24, FR31 (restart recovery validation), NFR-R1, NFR-R3, NFR-D1, NFR-D5, NFR-S2, AR10, NFR-M2

---

### Story 12.1: Implement platform update management with pre-update validation, atomic application, and rollback

As an installer,
I want to check for updates, read release notes, have pre-conditions validated before applying, and have rollback guaranteed if the update fails,
So that I can apply updates with confidence that a failure will not leave the site in an unrecoverable state.

**Acceptance Criteria:**

**Given** the installer navigates to update management
**When** they check for available updates
**Then** the system displays: current version, available version, and release notes for the available version — release notes are always shown before any confirmation prompt
**And** the installer must take an explicit confirmation action before any update is applied — no automatic or one-click update proceeds without review

**Pre-update validation:**
**Given** the installer confirms an update
**When** pre-update validation runs
**Then** the system validates: sufficient disk space, database accessibility, and configuration integrity
**And** if any check fails, the update is blocked with a plain-language explanation — no partial update begins
**And** an audit event is emitted: `event_type=SYSTEM`, `summary="Update pre-validation failed: [specific reason]"`

**Update mechanism — deployment-mode specific and atomic:**
**And** the update mechanism is deterministic and defined per deployment mode:
- Docker: version-pinned image tag pulled, then container restarted — partial image states are never applied
- systemd: artifact or package update applied, then service restarted — the previous version artifact is retained before the update starts
**And** version switches are atomic — the system is on either the previous version or the new version, never a mixture of both
**And** an audit event is emitted when the update starts: `event_type=SYSTEM`, `summary="Platform update started: version X → Y"`

**Rollback on failure:**
**And** if the update fails after partial application, the system either:
- automatically rolls back to the previous working version using the retained previous-version reference, OR
- if automatic rollback is not possible, explicitly blocks startup with plain-language recovery instructions — the system never runs in an undefined mixed-version state
**And** an audit event is emitted for the failure and rollback outcome: `event_type=SYSTEM`, `summary="Update failed — rolled back to version X"` or `"Update failed — manual recovery required"`

**Successful update:**
**And** all site configuration and energy history are preserved after a successful update (NFR-D5)
**And** an audit event is emitted: `event_type=SYSTEM`, `summary="Platform update applied: version X → Y"`
**And** `config_version` is logged in the update audit event — version context is always traceable

**Tests:**
**And** unit tests verify that release notes are presented before the confirmation prompt
**And** unit tests verify that a pre-validation failure blocks the update and emits the correct audit event
**And** unit tests verify that a failed update emits a rollback or manual-recovery audit event
**And** integration tests verify that Alembic migrations run on the updated version without data loss on representative test data

---

### Story 12.2: Implement site configuration export with integrity hash, dry-run restore, and validation-gated full restore

As an installer,
I want to export site configuration with an integrity hash, validate a backup file before applying it, and have the full restore require re-running deployment validation before control resumes,
So that hardware swaps are safe and a restored configuration is never silently applied to a different or incomplete physical setup.

**Acceptance Criteria:**

**Export:**
**Given** the installer requests a configuration export
**When** the export generates
**Then** a YAML file is produced containing: device roles, constraint values, EV window preferences, energy strategy default, and `config_version` — explicitly no credentials, session tokens, or secrets (FR24, AR10, NFR-S2)
**And** the export includes a checksum or cryptographic hash of the YAML content, used to verify integrity on restore
**And** the file is human-readable

**Dry-run restore ("Validate backup file"):**
**Given** the installer uses the dry-run option
**When** the dry run executes
**Then** the file integrity hash is verified, the schema is validated, and device references are checked against currently connected devices — no changes are applied to the system
**And** the result is a plain-language report: valid / invalid, with specific issues listed (e.g., "3 devices in backup not found in current system")

**Full restore:**
**Given** the installer initiates a full restore
**When** the restore runs
**Then** the file integrity hash is verified before any parsing — a hash mismatch rejects the file with a plain-language error and no state is applied
**And** the schema is validated — malformed or version-incompatible files are rejected before any state is applied
**And** a `config_version` mismatch between the backup and current system triggers an explicit installer warning — not silent compatibility

**Device mismatch handling:**
**And** if the backup references devices (by ID or type) not currently present in the system:
- those device slots are restored as UNASSIGNED
- the installer sees a plain-language warning listing all missing devices
**And** the restore never assumes devices exist — it only applies configuration to confirmed-present or explicitly UNASSIGNED slots

**Post-restore — validation gate before control resumes:**
**And** after a successful restore, the system enters "configuration restored — validation required" state
**And** the control loop does NOT resume issuing commands until the installer re-runs Deployment Validation (Epic 9 Step 4) and receives a PASS or acknowledged WARN result
**And** `/health/ready` returns HTTP 503 while in "validation required" state
**And** a `config_audit_log` row is written for each restored field with `actor=installer` and the restored value
**And** audit events are emitted: `"Site configuration restore started"`, and on completion: `"Site configuration restored — validation required"` or the relevant failure summary
**And** the restore procedure is completable in under 10 minutes following documented procedure (NFR-M2)

**Tests:**
**And** unit tests verify the export contains no credential fields (hashes, tokens, secrets)
**And** unit tests verify that a hash mismatch rejects the file before any parsing occurs
**And** unit tests verify that missing devices produce UNASSIGNED slots and a warning — no silent assignment
**And** unit tests verify that the system enters "validation required" state after restore and `/health/ready` returns 503
**And** unit tests verify that the dry-run path applies no changes to system state

---

### Story 12.3: Implement non-blocking data retention and staged startup recovery with degraded handling

As a developer,
I want data retention to run asynchronously without impacting any critical path, and startup recovery to progress through explicit stages with graceful degraded mode if full recovery is not possible within 2 minutes,
So that energy history is never silently pruned, retention never affects site performance, and partial startup failures degrade gracefully rather than bricking the site.

**Acceptance Criteria:**

**Data retention — non-blocking:**
**Given** the retention enforcement job runs
**When** it evaluates energy state history
**Then** it executes as an asynchronous background task — it never blocks the control loop, API responses, or the startup sequence
**And** energy state entries older than 12 months are eligible for pruning (NFR-D1); entries within the 12-month window are never deleted
**And** an audit event is emitted when pruning runs: `event_type=SYSTEM`, `summary="Energy history pruned: X entries removed beyond 12-month retention"`

**Staged startup recovery:**
**Given** the system starts after a reboot or power loss
**When** the startup sequence runs
**Then** startup progresses through four explicit stages in order:
1. DB ready: migrations complete, WAL mode active
2. Adapters connected: all configured device connections established or in reconnecting state
3. StateStore active: initial snapshot published
4. Control loop running: first evaluation cycle complete
**And** `/health/ready` returns HTTP 200 only when all four stages have completed successfully
**And** `/health/status` exposes the current stage and any stage-specific failure reason — supporting field diagnostics without log access

**Degraded startup — graceful fallback:**
**And** if full recovery (all four stages) is not achieved within 2 minutes:
- if DB and StateStore are ready and at least one required device is connected, the system enters `DEGRADED` `SystemOperatingMode` — the control loop runs with available devices using the degradation matrix
- an audit event is emitted: `event_type=SYSTEM`, `summary="Startup degraded: [specific missing components listed]"`
**And** if the system cannot reach even DB-ready within 2 minutes, startup exits as FAILED — systemd or Docker restart policy handles the next attempt; an audit event is written if possible, otherwise the failure appears in the process log

**Full recovery:**
**And** when all four stages complete within 2 minutes, an audit event is emitted: `event_type=SYSTEM`, `summary="Startup complete: all components ready"`
**And** NFR-R3 is satisfied when this event is emitted within 2 minutes of service start

**Tests:**
**And** unit tests verify that the retention pruning job is asynchronous and does not block any synchronous code path
**And** unit tests verify that pruning removes entries older than 12 months and preserves entries within the window
**And** integration tests verify that the full startup sequence reaches `/health/ready` 200 within 2 minutes on representative hardware configuration
**And** unit tests verify that incomplete startup within 2 minutes produces `DEGRADED` mode (if minimum viable state reached) or exits as FAILED (if not)
**And** unit tests verify that each startup stage and degraded/failed startup emit the correct audit events
**And** unit tests verify that `/health/status` returns the current stage during startup without exposing raw log content

**Cross-story constraints applying to all Epic 12 stories:**
- All operations in this epic emit audit events: update start/success/failure/rollback, restore start/success/validation-required/failure, retention run, startup success/degraded/failed
- `config_version` is included in exports, validated on restore, and logged on update — version mismatch triggers an explicit warning, never silent compatibility
- Retention pruning is always asynchronous — it never blocks control loop, API responses, or startup
- Post-restore "validation required" state blocks control loop and returns `/health/ready` 503 until Deployment Validation passes
- Update failure must result in either automatic rollback to the previous version or an explicit startup block with recovery instructions — mixed-version state is never acceptable

---

## Epic 13: External Optimization Data *(nice-to-have)*

Solar production forecast and dynamic tariff data as configurable optional optimization inputs, fetched in isolated async schedulers, cached with TTL, rate-limited per source, and throttled in the event log. External data influences decisions only within PolicyGuard-enforced safety constraints. The system continues full local deterministic operation when external sources are unavailable.

**Requirements covered:** FR40, FR41, FR42, NFR-I4, NFR-S7

---

### Story 13.1: Implement external data service abstraction with isolated fetch, TTL caching, rate limiting, and event log throttling

As a developer,
I want external data fetches to run in isolated async schedulers with TTL-based caching, per-source rate limiting, and throttled audit logging,
So that the control loop reads from cache only — never blocking on an external API — and no external source can cause API bans or event log flooding.

**Acceptance Criteria:**

**Fetch isolation:**
**Given** an external data source is configured
**When** data needs to be refreshed
**Then** the fetch runs in a separate async task or scheduler — never inside the control loop execution path
**And** the decision engine reads the last-known `ExternalDataResult` from cache — it does NOT trigger a fetch during evaluation

**TTL and caching:**
**And** `ExternalDataAvailable` includes a `ttl_seconds` field indicating how long the data is valid
**And** cached data is used until the TTL expires; expired data is treated as `ExternalDataUnavailable(reason="stale")` — not as "never received"

**Rate limiting:**
**And** external calls are rate-limited per source — configurable minimum interval (default: 1 call per 5–15 minutes, tunable via environment variable)
**And** the scheduler enforces the minimum interval; burst or retry storms are prevented regardless of failure pattern

**Timeout and failure:**
**And** each fetch call times out within 10 seconds (NFR-I4) — the caller never blocks beyond this
**And** a typed `ExternalDataResult` is always returned: `ExternalDataAvailable` (data + fetched_at + ttl_seconds) or `ExternalDataUnavailable` (source name + reason)
**And** `ExternalDataUnavailable` is returned (never raised) on timeout, network failure, API error, or expired TTL

**Startup behavior:**
**And** on system startup, external data is considered `ExternalDataUnavailable` until the first successful fetch completes
**And** the system does NOT block on startup waiting for any external fetch — startup proceeds immediately on local data

**Audit event throttling:**
**And** repeated identical failures from the same source are rate-limited in the event log — a single audit entry is written when the failure begins, followed by periodic aggregated entries (e.g., "Forecast API timeout: 12 consecutive failures over 60 minutes") — not one entry per attempt
**And** recovery from unavailability always emits a distinct audit event: `event_type=SYSTEM`, `summary="External source [name] recovered after [duration]"`

**Multiple source independence:**
**And** if multiple external sources are configured, each has an independent status, TTL, rate limit, and event log stream
**And** installer-visible status shows per-source status — not a single aggregated flag

**Tests:**
**And** unit tests verify that a fetch timeout returns `ExternalDataUnavailable` within 10 seconds + epsilon — no hang
**And** unit tests verify that expired TTL produces `ExternalDataUnavailable(reason="stale")` — not `ExternalDataAvailable`
**And** unit tests verify that the rate limiter blocks calls scheduled sooner than the configured minimum interval
**And** unit tests verify that the decision engine reads from cache and does not trigger a fetch during evaluation
**And** unit tests verify that repeated identical failures produce one initial audit event and periodic aggregated entries — not one entry per failure

---

### Story 13.2: Implement solar forecast integration with sanity validation and bounded influence

As a developer,
I want the decision engine to use validated solar production forecasts to improve pre-charge and load-shift timing, with forecast influence strictly bounded so it cannot override real-time device state,
So that forecast data improves optimization without introducing "forecast hallucination" that contradicts what devices actually report.

**Acceptance Criteria:**

**Forecast validation:**
**Given** a solar forecast response is received
**When** the scheduler processes it before caching
**Then** all forecast values are validated:
- non-negative (negative solar production is physically impossible)
- within configured realistic bounds (`FORECAST_MAX_KW`, configurable, with a sensible site default)
**And** any value outside these bounds causes the entire response to be treated as `ExternalDataUnavailable(reason="invalid_data")` — partial use of an invalid response is not permitted

**Forecast influence — bounded:**
**Given** validated forecast data is in cache
**When** the decision engine evaluates a cycle with `Maximize Self-Consumption` or `Minimize Cost` strategy
**Then** forecast data influences only: pre-charging timing (when to charge the battery ahead of expected solar) and load-shift timing (when to defer loads)
**And** forecast data must NOT override real-time measurements — if current PV output contradicts the forecast, `InverterState.pv_power_kw` always takes precedence
**And** forecast data must NOT cause actions that contradict current device state — if the battery is already fully charged, a forecast-driven pre-charge intent is not produced
**And** all external calls are over HTTPS only; no local energy data (device IDs, site ID, consumption values) is transmitted (NFR-S7)

**Given** solar forecast is absent or `ExternalDataUnavailable`
**When** the decision engine evaluates
**Then** it falls back to current PV production from `InverterState` as defined in Story 7.3 — no control capability is lost

**Tests:**
**And** unit tests verify that negative or out-of-bounds forecast values produce `ExternalDataUnavailable(reason="invalid_data")`
**And** unit tests verify that strategy decisions are meaningfully different when valid forecast data is available vs. absent
**And** unit tests verify that a forecast-driven intent is suppressed when current device state contradicts it (e.g., battery already full)
**And** unit tests verify that no local energy data appears in the outgoing API request

---

### Story 13.3: Implement dynamic tariff integration with explicit fallback, oscillation prevention, and installer visibility

As a developer,
I want the decision engine to use dynamic tariff data with a documented deterministic fallback, a stability window preventing oscillation, and per-source installer visibility scoped as INFO/WARN,
So that `Minimize Cost` is richer with real tariff pricing, degrades gracefully with a predictable fallback, and external source issues never pollute the anomaly health banner.

**Acceptance Criteria:**

**Given** dynamic tariff data is configured and available
**When** the decision engine evaluates a cycle with `Minimize Cost` strategy
**Then** tariff data from `ExternalDataAvailable` influences charge/discharge timing — actual tariff prices used rather than default fallback assumptions

**Tariff fallback — explicit and deterministic:**
**Given** dynamic tariff data is absent or `ExternalDataUnavailable`
**When** the decision engine evaluates
**Then** it uses the documented tariff fallback: either a configurable fixed rate (`DEFAULT_TARIFF_RATE_EUR_KWH`) or a configurable day/night heuristic (`DAY_RATE_EUR_KWH` / `NIGHT_RATE_EUR_KWH` with configurable crossover hours)
**And** the fallback is deterministic, configuration-driven, and documented in the module — no undefined or ad hoc behavior

**Oscillation prevention:**
**And** tariff-driven decisions enforce a minimum stability window (configurable, default: no intent flip-flop within 10 minutes) — a tariff boundary transition does not cause rapid battery charge/discharge reversal or EV session thrashing
**And** once a charge or discharge intent is issued for a device role, a contradicting intent is suppressed until the window elapses — except when safety constraints require immediate override

**Installer visibility — INFO/WARN only, no anomaly banner by default:**
**Given** any external source is unavailable
**When** the installer views the monitoring dashboard
**Then** a specific per-source unavailability reason is shown inline in the external data section (e.g., "Solar forecast: API timeout" / "Tariff feed: invalid credentials") — not a generic message (FR42)
**And** external source unavailability is surfaced as INFO or WARN severity — it does NOT trigger the anomaly banner unless the installer has explicitly configured it to do so
**And** the reason string is sourced from `ExternalDataUnavailable.reason` — surfaced via `StateStore` and the event log

**Manual refresh:**
**And** the installer has a "Refresh now" action per external source — triggering an immediate out-of-schedule fetch (subject to the rate limiter minimum interval)
**And** the result of a manual refresh is shown inline and emits an audit event regardless of outcome

**Tests:**
**And** unit tests verify that tariff-influenced decisions differ from fallback-assumption decisions when tariff data is present
**And** unit tests verify that the fallback is the configured fixed rate or day/night heuristic — not an undefined default
**And** unit tests verify that the stability window suppresses a contradicting intent within the window — and does not suppress when safety constraints require override
**And** unit tests verify that external source unavailability does NOT appear in the anomaly banner unless explicitly configured
**And** unit tests verify that installer-visible messages include the specific source name and reason — not a generic string
**And** unit tests verify that all external API calls use HTTPS and transmit no local energy data (NFR-S7)

**Cross-story constraints applying to all Epic 13 stories:**
- External data fetches run in isolated async schedulers — never inside the control loop execution path; the decision engine reads from cache only
- Each external source has an independent TTL, rate limit, and status — multiple sources are never aggregated into a single status flag
- Repeated identical failures are rate-limited in the event log — one initial entry + periodic aggregated entries, not one entry per attempt
- External data unavailability is INFO/WARN severity only — it does not affect core `SystemOperatingMode` or trigger the anomaly banner by default
- All external calls are over HTTPS only; no local energy data is transmitted (NFR-S7)
- Forecast influence is bounded: pre-charge and load-shift timing only — never overrides real-time device state or contradicts current device state
- Tariff fallback is always explicitly defined, deterministic, and documented — no undefined behavior when tariff data disappears
- Startup never blocks waiting for any external fetch — external data starts as unavailable and populates asynchronously
