# Story 4.5: Implement device discovery and connection management

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want the abstraction layer to discover devices using configured endpoints and reconnect automatically after failures,
so that the installer setup wizard has a reliable device list and the system self-heals after network interruptions without manual intervention.

## Acceptance Criteria

**AC1 — Modbus TCP device discovery via configured endpoints**
**Given** a device discovery scan is triggered
**When** the scan runs
**Then** Modbus TCP devices are probed at configured IP addresses or IP ranges — configured endpoints are first-class; blind subnet broadcast is not the default
**And** OCPP EV chargers are discovered by their WebSocket connection and `BootNotification` registration — they initiate the connection; they are not scanned
**And** DSMR meters are detected by probing the configured serial port(s) or TCP endpoint(s) — not by general network scanning
**And** each discovered device is returned with: protocol, address, detected model (if identifiable), and initial capability classification (FULL / REDUCED / UNKNOWN based on `get_profile()` lookup)

**AC2 — `DegradedDeviceState` on protocol failure during connection window**
**Given** a connected device becomes unreachable after a successful initial connection
**When** the device adapter's `get_state()` is called and the underlying `ProtocolAdapter.get_raw_state()` returns `ProtocolDegradedState`
**Then** the device abstraction layer returns `DegradedDeviceState(reason="reconnecting")` for the duration of the reconnection window
**And** the protocol adapter (Epic 3) handles low-level reconnection attempts with backoff — Epic 4 does not duplicate reconnect logic
**And** StateStore (Epic 5) owns the system-wide current view of device states — Epic 4 does not maintain global system state
**And** once the protocol adapter recovers and `get_raw_state()` returns valid data, `get_state()` returns the normalized domain state

**AC3 — Automatic reconnection on restart**
**Given** the system restarts after a reboot or power loss
**When** the startup sequence runs (Step 6: "Initialize protocol adapters")
**Then** device adapters re-establish all previously configured connections automatically using stored configuration
**And** Epic 4 contributes to reboot recovery by restoring configured device connections
**And** full system operational recovery within 2 minutes (NFR-R3) is a cross-epic guarantee — not Epic 4 alone

**AC4 — `DeviceDiscoveryResult` typed return from discovery scan**
**When** the discovery scan completes for any protocol
**Then** a `DeviceDiscoveryResult` dataclass (or Pydantic model) is returned per discovered device containing:
- `device_id: str` — caller-supplied or generated from address
- `protocol: Literal["modbus_tcp", "ocpp_1_6", "dsmr_p1"]`
- `address: str` — IP:port for Modbus/DSMR TCP, serial path for DSMR serial, charge_point_id for OCPP
- `model: str | None` — detected model string if identifiable, else `None`
- `capability_status: CapabilityStatus` — from `get_profile()` lookup; `CapabilityStatus.reduced` if model is `None`

**Tests:**
**And** integration tests using a simulated `ProtocolAdapter` returning `ProtocolDegradedState` verify that `DegradedDeviceState(reason="reconnecting")` is returned during the reconnection window
**And** integration tests verify that once the simulated adapter recovers, `get_state()` returns the normalized domain state
**And** tests verify that OCPP discovery correctly registers a newly connecting charger via `BootNotification`

## Tasks / Subtasks

- [x] **Task 0: Pre-story quality gate** (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — confirm 426 tests pass
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m mypy src/`

- [x] **Task 1: Define `DeviceDiscoveryResult` in `core/devices.py`** (AC: AC4)
  - [x] Add a frozen Pydantic `DeviceDiscoveryResult` model **after** `DeviceCapabilityProfile`
  - [x] Update module docstring to document `DeviceDiscoveryResult` purpose

- [x] **Task 2: Implement `DiscoveryService` in `adapters/discovery.py`** (AC: AC1, AC4)
  - [x] Create `src/open_ems/adapters/discovery.py`
  - [x] Define `DiscoveryService` class with `probe_modbus_endpoint()`, `probe_dsmr_endpoint()`, `register_ocpp_discovery()`
  - [x] For Modbus probe: attempt a `ModbusTcpAdapter` `get_raw_state()` call; degraded → raises `DeviceProbeError`
  - [x] For DSMR probe: start adapter and wait for first telegram within timeout; timeout → raises `DeviceProbeError`
  - [x] Capability classification: call `get_profile()` if model provided, else `CapabilityStatus.reduced`
  - [x] Log structured events: `event="device_discovered"` and `event="device_not_found"`

- [x] **Task 3: Verify `DegradedDeviceState(reason="reconnecting")` path in existing adapters** (AC: AC2)
  - [x] Add `_RECONNECTING_REASONS` frozensets and update `_to_degraded()` in all four adapters
  - [x] `InverterAdapter` — maps `"modbus_timeout"`, `"modbus_error"` → `"reconnecting"`
  - [x] `BatteryAdapter` — same mapping
  - [x] `EVChargerAdapter` — maps `"ocpp_disconnected"` → `"reconnecting"`
  - [x] `GridMeterAdapter` — maps `"dsmr_unavailable"`, `"dsmr_stale"` → `"reconnecting"`

- [x] **Task 4: Verify startup adapter re-connection pattern** (AC: AC3)
  - [x] Confirmed `GridMeterAdapter.connect()` calls `DSMRAdapter.start()` — auto-reconnect handled
  - [x] Confirmed `InverterAdapter.connect()` and `BatteryAdapter.connect()` are no-ops (lazy Modbus connect)
  - [x] `DeviceAdapter` Protocol still satisfied by all four adapters

- [x] **Task 5: Write integration tests — degraded state and recovery** (AC: AC2 tests)
  - [x] Created `tests/integration/adapters/__init__.py`
  - [x] Created `tests/integration/adapters/test_discovery_and_recovery.py`
  - [x] All four adapters tested for `reconnecting` reason and recovery

- [x] **Task 6: Write unit tests for `DiscoveryService`** (AC: AC1, AC4 tests)
  - [x] Created `tests/unit/adapters/test_discovery.py`
  - [x] `probe_modbus_endpoint()` success and failure cases
  - [x] `register_ocpp_discovery()` success cases
  - [x] `probe_dsmr_endpoint()` success and timeout cases
  - [x] structlog event assertions

- [x] **Task 7: Write unit tests for `DeviceDiscoveryResult` model** (AC: AC4)
  - [x] Added `DeviceDiscoveryResult` tests to `tests/unit/core/test_devices.py`
  - [x] Immutability, extra fields forbidden, protocol validation, defaults

- [x] **Task 8: Final validation** (AC: all)
  - [x] `uv run python -m ruff check .` — passed
  - [x] `uv run python -m ruff format --check .` — passed
  - [x] `uv run python -m mypy src/` — passed
  - [x] `uv run python -m pytest tests/ --no-cov -q` — 465 tests pass (39 new tests added)

### Review Findings

- [x] [Review][Patch] DSMR probe hardcodes `capability_status=CapabilityStatus.full`, bypassing `_resolve_capability_status()` — route through `_resolve_capability_status("dsmr_p1")` for consistency with Modbus/OCPP paths [`discovery.py:176-182`]
- [x] [Review][Patch] OCPP BootNotification chain test missing — add integration test wiring `OCPPCentralSystem` to `DiscoveryService` via a simulated `BootNotification` to satisfy AC1 test requirement
- [x] [Review][Defer] AC3 automated reconnection test — `connect()` behavior is covered by earlier adapter unit tests; the startup orchestrator that calls Step 6 belongs to Epic 8 — deferred, pre-existing
- [x] [Review][Patch] `probe_dsmr_endpoint`: address resolves to `"None:None"` when both `serial_port` and `tcp_host`/`tcp_port` are `None` — no guard clause; `DSMRAdapterConfig` raises `ValidationError` rather than `DeviceProbeError` [`discovery.py:147`]
- [x] [Review][Patch] `_wait_for_dsmr_telegram`: `asyncio.get_event_loop()` is deprecated; replace with `asyncio.get_running_loop()` (Python 3.10+ emits `DeprecationWarning`; 3.12+ may raise) [`discovery.py:194,199`]
- [x] [Review][Patch] `probe_dsmr_endpoint`: `except (TimeoutError, DeviceProbeError)` swallows `DeviceProbeError` from `_wait_for_dsmr_telegram` and re-raises with `from None`, losing the original message; handle `TimeoutError` and `DeviceProbeError` separately [`discovery.py:163-171`]
- [x] [Review][Patch] `probe_dsmr_endpoint`: `adapter.start()` exceptions outside `(TimeoutError, DeviceProbeError)` (e.g. `OSError` from transport) escape the method as non-`DeviceProbeError`; callers expect `DeviceProbeError` on all probe failures [`discovery.py:160-162`]
- [x] [Review][Patch] `_wait_for_dsmr_telegram`: `adapter.get_raw_state()` exceptions propagate uncaught out of the polling loop and bypass `probe_dsmr_endpoint`'s handler and `device_not_found` log [`discovery.py:196`]
- [x] [Review][Patch] `_resolve_capability_status`: `get_profile()` never raises (returns reduced profile for unknown models per docstring) — dismissed as false positive [`discovery.py:235-240`]
- [x] [Review][Patch] `DeviceDiscoveryResult.model`: empty string `""` is not rejected by `str | None`; add `min_length=1` constraint via `Annotated` to prevent a silent no-op registry lookup [`core/devices.py:249`]
- [x] [Review][Defer] `_RECONNECTING_REASONS` duplicated across 4 adapter modules — code smell, not a runtime bug; sets legitimately differ per protocol; refactor to shared utility if the pattern grows [`all adapter files`] — deferred, pre-existing
- [x] [Review][Defer] `probe_modbus_endpoint`: `ModbusTcpAdapterConfig` `ValidationError` propagates on bad caller args — caller-error boundary, not a network failure; `DeviceProbeError` is reserved for unreachable devices [`discovery.py:76-101`] — deferred, pre-existing
- [x] [Review][Defer] `probe_dsmr_endpoint`: invalid `dsmr_version` literal raises `ValidationError` — caller error; validated by `DSMRAdapterConfig` at construction [`discovery.py:148-154`] — deferred, pre-existing
- [x] [Review][Defer] `register_ocpp_discovery`: empty `device_id`/`address` raises Pydantic `ValidationError` — caller error; `NonEmptyStr` constraint on `DeviceDiscoveryResult` is the correct guard [`discovery.py:204-232`] — deferred, pre-existing
- [x] [Review][Defer] `_to_degraded` empty reason string raises `ValidationError` in all 4 adapters — Epic 3 contract violation, not Epic 4's responsibility; `ProtocolDegradedState` should already enforce non-empty reason [`all adapter files`] — deferred, pre-existing
- [x] [Review][Defer] Unit tests couple to live capability registry (`fronius_gen24_v1`, `ocpp_1_6`) — intentional; registry is in-process and stable [`test_discovery.py`] — deferred, pre-existing
- [x] [Review][Defer] `probe_modbus_endpoint`: `adapter.close()` in `finally` may hang if Modbus TCP connection is wedged — pre-existing `ModbusTcpAdapter` behavior, not introduced by this story [`discovery.py:100`] — deferred, pre-existing
- [x] [Review][Defer] Unit test `test_probe_dsmr_success`: `patch("DSMRAdapter")` returns fake without validating `DSMRAdapterConfig` construction — inherent patch limitation; config validation is covered by `DSMRAdapter` unit tests [`test_discovery.py`] — deferred, pre-existing

## Dev Notes

### Context and Scope

This is the **final story of Epic 4**. Stories 4.1–4.4 established all domain types, adapter normalization, and the capability registry. This story adds two things:

1. A **`DeviceDiscoveryResult` domain type** in `core/devices.py` — a typed result for the installer wizard's device scan (Epic 9 Step 1)
2. A **`DiscoveryService`** in `adapters/discovery.py` — the active-probing and OCPP-registration mechanism that feeds those results
3. **Explicit `reason="reconnecting"`** mapping in existing adapters — making the reconnection-window semantics testable and contractually correct

**What this story does NOT do:**
- Implement the full startup adapter lifecycle (that's Epic 8's `control_loop.py`)
- Implement StateStore publishing (Epic 5)
- Build the installer wizard UI (Epic 9)

### Current Codebase State (at story start)

**Existing adapter implementations — understand before modifying:**

- **`src/open_ems/adapters/modbus/inverter_adapter.py`** — `InverterAdapter`. `connect()` is a no-op (Modbus is lazy). `disconnect()` calls `self._protocol_adapter.close()`. `get_state()` already maps `ProtocolDegradedState` → `DegradedDeviceState` but uses the raw protocol reason, not `"reconnecting"`. `_to_degraded()` logs `device_degraded` with `component="adapters"`.

- **`src/open_ems/adapters/modbus/battery_adapter.py`** — `BatteryAdapter`. Same pattern as `InverterAdapter`. Existing `_to_degraded()` must be updated to map connection-class reasons to `"reconnecting"`.

- **`src/open_ems/adapters/ocpp/charger_adapter.py`** — `EVChargerAdapter`. `get_state()` currently raises `NotImplementedError` or returns a stub — **check and implement** the `ProtocolDegradedState` → `DegradedDeviceState(reason="reconnecting")` path. OCPP discovery happens via `BootNotification` in `OCPPCentralSystem.handle_charger()` — **no active scanning needed**.

- **`src/open_ems/adapters/dsmr/meter_adapter.py`** — `GridMeterAdapter`. `connect()` calls `self._protocol_adapter.start()`. `disconnect()` calls `self._protocol_adapter.stop()`. `get_state()` already maps `ProtocolDegradedState` → `DegradedDeviceState`. Same `_to_degraded()` mapping update needed for connection-class reasons.

- **`src/open_ems/adapters/ocpp/central_system.py`** — `OCPPCentralSystem`. Already has `register()` and `handle_charger()` for OCPP connection lifecycle. OCPP EV charger "discovery" = charger connects inbound and sends `BootNotification` → `OCPPCentralSystem.handle_charger()` dispatches to the pre-registered `OCPPChargerAdapter`.

- **`src/open_ems/adapters/modbus/tcp.py`** — `ModbusTcpAdapter`. Has `reconnect_delay_s` and `reconnect_delay_max_s` in `ModbusTcpAdapterConfig`. Uses lazy `_ensure_connected()`. Already handles exponential backoff via pymodbus client config. Returns `ProtocolDegradedState` on all failures — **Epic 4 domain layer must translate this to `DegradedDeviceState(reason="reconnecting")`**.

- **`src/open_ems/adapters/dsmr/p1.py`** — `DSMRAdapter`. Has `_read_loop()` with automatic reconnect on `OSError`. `reconnect_delay_s = 5.0` default. Returns `ProtocolDegradedState("dsmr_unavailable")` when not yet connected, `ProtocolDegradedState("dsmr_stale")` on stale data.

- **`src/open_ems/core/devices.py`** — Contains all domain types. `DeviceAdapter` Protocol with `connect()`, `disconnect()`, `get_state()`, `get_capabilities()`. `DegradedDeviceState` has `device_id`, `role`, `reason`, `occurred_at`. **Add `DeviceDiscoveryResult` here.**

**Protocol reason → domain reason mapping (for Task 3):**

| `ProtocolDegradedState.reason` | Domain `DegradedDeviceState.reason` |
|---|---|
| `"modbus_timeout"` | `"reconnecting"` |
| `"modbus_error"` | `"reconnecting"` |
| `"ocpp_disconnected"` | `"reconnecting"` |
| `"dsmr_unavailable"` | `"reconnecting"` |
| `"dsmr_stale"` | `"reconnecting"` |
| `"device_id_mismatch"` | `"device_id_mismatch"` (keep original — not a connection failure) |
| `"unexpected_raw_state_type"` | `"unexpected_raw_state_type"` (keep original) |
| `"validation_error:..."` | keep original |
| `"missing_dsmr_field:..."` | keep original |

**New files to create:**
- `src/open_ems/adapters/discovery.py`
- `tests/integration/adapters/test_discovery_and_recovery.py`
- `tests/unit/adapters/test_discovery.py`

**Existing files to modify:**
- `src/open_ems/core/devices.py` — add `DeviceDiscoveryResult`
- `src/open_ems/adapters/modbus/inverter_adapter.py` — update `_to_degraded()` reason mapping
- `src/open_ems/adapters/modbus/battery_adapter.py` — update `_to_degraded()` reason mapping
- `src/open_ems/adapters/ocpp/charger_adapter.py` — add `get_state()` `ProtocolDegradedState` path + `reason="reconnecting"`
- `src/open_ems/adapters/dsmr/meter_adapter.py` — update `_to_degraded()` reason mapping
- `tests/unit/core/test_devices.py` — add `DeviceDiscoveryResult` tests

**Baseline:** 426 tests pass (from Story 4.4).

### Architecture Constraints

**Import boundary (strictly enforced, never violate):**
```
core/     ← zero external imports (no adapters, no storage, no services)
adapters/ ← may import from core/ only
```
- `adapters/discovery.py` → may import `open_ems.core.devices` (DeviceDiscoveryResult, CapabilityStatus) and `open_ems.adapters.capabilities` (get_profile) — ✅
- `adapters/discovery.py` → may import `open_ems.adapters.modbus.tcp` and `open_ems.adapters.dsmr.p1` for probing — ✅
- **`core/devices.py` must NOT import from `adapters/`** — ✅ no change needed here

**Startup sequence (from architecture):**
```
1. Load pydantic-settings configuration
2. Run Alembic migrations — fatal if they fail
3. Check time synchronization
4. Initialize StateStore
5. Initialize PolicyGuard
6. Initialize protocol adapters (connect to devices)  ← Epic 4's connect() is called here
7. Start control loop
8. Start watchdog task
9. Mark system readiness
```

Epic 4 only owns Step 6 behavior: `adapter.connect()` must work correctly. The orchestrator that calls Step 6 belongs to Epic 8.

**OCPP discovery model:** OCPP chargers are NOT actively scanned. Discovery = charger connects WebSocket and sends `BootNotification` → `OCPPCentralSystem.handle_charger()` fires → `DiscoveryService.register_ocpp_discovery()` can be called at that point with the `charge_point_id` and model extracted from `BootNotification`. This is a passive/callback model, not a polling model.

**Degradation behavior (architecture BAD-2):**
- Grid meter degraded → enter conservative mode (stop all load-increase commands)
- Battery degraded → stop battery charge/discharge
- EV charger degraded → stop all EV session commands
- PV inverter degraded → drop PV production from optimization layer
- Recovery is **automatic** — on next poll cycle where `get_state()` returns healthy `DeviceState`, control loop exits fail-safe

### Testing Standards

- **pytest + pytest-asyncio** for all async tests
- **No real hardware required** — use simulated adapters and fake transports following existing patterns in `tests/unit/adapters/`
- All simulated adapters implement the same `DeviceAdapter` Protocol as real ones
- Look at `tests/unit/adapters/modbus/test_inverter_adapter.py` for fake `ModbusTcpAdapter` patterns
- Look at `tests/unit/adapters/dsmr/test_meter_adapter.py` for fake `DSMRAdapter` patterns
- Look at `tests/unit/adapters/ocpp/test_charger_adapter.py` for OCPP fake patterns
- Integration tests go in `tests/integration/adapters/` — use existing `tests/integration/` folder structure

### Protocol-Specific Discovery Details

**Modbus TCP discovery:**
- Use `ModbusTcpAdapterConfig(device_id=..., host=..., port=..., registers=..., timeout_s=5.0)` for probe
- A single probe with short timeout (5s) is sufficient
- Do not leave connections open after the probe — call `close()` on the adapter
- Return `DeviceDiscoveryResult(protocol="modbus_tcp", address=f"{host}:{port}", model=model, capability_status=...)`

**OCPP EV charger discovery:**
- Passive: charger connects → `BootNotification` → `OCPPCentralSystem` fires → call `register_ocpp_discovery(charge_point_id, model)`
- Model comes from `BootNotification.charge_point_model` field (already captured in `_ChargerState.last_status_notification`)
- The `OCPPChargerAdapter` must be pre-registered via `OCPPCentralSystem.register()` before the charger can connect
- `address = charge_point_id` for the discovery result

**DSMR P1 discovery:**
- Probe configured serial port or TCP endpoint with a bounded timeout (e.g., 30s to receive first telegram)
- Use the `_TelegramSource`/`_DSMRTransport` abstraction — inject a fake factory in tests
- Return `DeviceDiscoveryResult(protocol="dsmr_p1", address=serial_port or f"{tcp_host}:{tcp_port}", model="dsmr_p1", capability_status=CapabilityStatus.full)`

### Project Structure Notes

- `DeviceDiscoveryResult` → `src/open_ems/core/devices.py` (domain type, no I/O)
- `DiscoveryService` → `src/open_ems/adapters/discovery.py` (adapter layer, may do I/O)
- Tests → `tests/unit/adapters/test_discovery.py` and `tests/integration/adapters/test_discovery_and_recovery.py`
- No new `__init__.py` exports required unless `DiscoveryService` needs to be part of `adapters/__init__.py` (optional)

### Previous Story Intelligence (from Story 4.4)

- `get_profile()` in `adapters/capabilities/__init__.py` is the canonical capability lookup — use it in `DiscoveryService` for capability classification
- `DeviceCapabilityProfile` is frozen Pydantic with `model_config = ConfigDict(frozen=True, extra="forbid")` — follow same pattern for `DeviceDiscoveryResult`
- All adapters' `get_capabilities()` now delegate to `get_profile()` — the registry is stable and comprehensive
- Review finding from 4.4: `_ALL_PROFILES` collision guard via `assert len(_ALL_PROFILES) == sum(...)` — do NOT add a new model key that would collide
- Capability profiles use `device_id="__placeholder__"` — never expose the placeholder to callers; always substitute `device_id` at lookup time

### References

- Story AC text: [Source: `_bmad-output/planning-artifacts/epics.md`#Story 4.5]
- Epic 4 scope and FRs: [Source: `_bmad-output/planning-artifacts/epics.md`#Epic 4: Device Abstraction Layer]
- Architecture startup sequence: [Source: `_bmad-output/planning-artifacts/architecture.md`#Startup sequence]
- Architecture BAD-2 degradation matrix: [Source: `_bmad-output/planning-artifacts/architecture.md`#BAD-2]
- Architecture import boundary: [Source: `_bmad-output/planning-artifacts/architecture.md`#Import rules]
- OCPP WebSocket integration: [Source: `_bmad-output/planning-artifacts/architecture.md`#Section 3.2]
- DeviceAdapter Protocol: [Source: `src/open_ems/core/devices.py`]
- ProtocolAdapter Protocol: [Source: `src/open_ems/adapters/protocol.py`]
- ModbusTcpAdapter: [Source: `src/open_ems/adapters/modbus/tcp.py`]
- DSMRAdapter: [Source: `src/open_ems/adapters/dsmr/p1.py`]
- OCPPCentralSystem: [Source: `src/open_ems/adapters/ocpp/central_system.py`]
- Capability registry: [Source: `src/open_ems/adapters/capabilities/__init__.py`]
- Previous story dev notes: [Source: `_bmad-output/implementation-artifacts/4-4-implement-device-capability-profile-system-and-capability-gate.md`]

## Dev Agent Record

### Agent Model Used

claude-sonnet-4.6

### Debug Log References

### Completion Notes List

Story 4-5 implementation complete. Key decisions:

- `DeviceDiscoveryResult` added to `core/devices.py` as a frozen Pydantic model with `Literal` protocol field, exported from `core/__init__.py`
- `DiscoveryService` in `adapters/discovery.py` with `DeviceProbeError` exception; injected factories for testability; uses `asyncio.wait_for` for timeouts
- `_RECONNECTING_REASONS` frozensets added as module constants in all four adapters; `_to_degraded()` updated to map transient connection reasons to `"reconnecting"` while preserving data-mapping error reasons unchanged
- DSMR recovery test uses `datetime.now(UTC)` for fresh timestamps to avoid stale data detection
- `get_profile()` called with `"_discovery_probe"` placeholder device_id in `_resolve_capability_status()`
- 39 new tests added (10 DeviceDiscoveryResult model tests, 18 DiscoveryService unit tests, 11 integration tests for degraded state and recovery)

### File List

- `src/open_ems/core/devices.py` — added `DeviceDiscoveryResult` model; updated docstring
- `src/open_ems/core/__init__.py` — exported `DeviceDiscoveryResult`
- `src/open_ems/adapters/discovery.py` — NEW: `DeviceProbeError`, `DiscoveryService`
- `src/open_ems/adapters/modbus/inverter_adapter.py` — added `_RECONNECTING_REASONS`; updated `_to_degraded()`
- `src/open_ems/adapters/modbus/battery_adapter.py` — added `_RECONNECTING_REASONS`; updated `_to_degraded()`
- `src/open_ems/adapters/ocpp/charger_adapter.py` — added `_RECONNECTING_REASONS`; updated `_to_degraded()`
- `src/open_ems/adapters/dsmr/meter_adapter.py` — added `_RECONNECTING_REASONS`; updated `_to_degraded()`
- `tests/unit/core/test_devices.py` — added 10 `DeviceDiscoveryResult` tests
- `tests/unit/adapters/test_discovery.py` — NEW: 18 unit tests for `DiscoveryService`
- `tests/integration/adapters/__init__.py` — NEW: empty init for test discovery
- `tests/integration/adapters/test_discovery_and_recovery.py` — NEW: 11 integration tests for degraded state and recovery
- `tests/unit/adapters/modbus/test_inverter_adapter.py` — updated 1 assertion: `"modbus_timeout"` → `"reconnecting"`
- `tests/unit/adapters/modbus/test_battery_adapter.py` — updated 1 assertion: `"modbus_timeout"` → `"reconnecting"`
- `tests/unit/adapters/dsmr/test_meter_adapter.py` — updated 4 assertions: dsmr reasons → `"reconnecting"`
- `tests/unit/adapters/ocpp/test_charger_adapter.py` — updated 3 assertions: `"ocpp_disconnected"` → `"reconnecting"`
