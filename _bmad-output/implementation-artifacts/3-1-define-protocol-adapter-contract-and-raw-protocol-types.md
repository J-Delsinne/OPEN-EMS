# Story 3.1: Define protocol adapter contract and raw protocol types

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want a typed interface contract that all protocol adapters implement, using raw protocol types with no energy-domain interpretation,
so that the abstraction layer in Epic 4 can wrap any protocol adapter through a consistent, type-checked API.

## Acceptance Criteria

**AC1 - Protocol package and interface**
**Given** the adapters package is initialized
**When** the protocol contract types are imported
**Then** a `ProtocolAdapter` Protocol using `typing.Protocol` is defined with required async methods:
- `get_raw_state() -> RawProtocolState | ProtocolDegradedState`
- `send_raw_command(command: RawProtocolCommand) -> ProtocolCommandResult`

**AC2 - Protocol command result**
**And** `ProtocolCommandResult` is defined with fields:
- `correlation_id`
- `device_id`
- `protocol_status` with allowed values `sent`, `acked`, `timeout`, `error`
- `raw_response` as optional bytes or dict

**AC3 - Degraded protocol state**
**And** `ProtocolDegradedState` is defined with fields:
- `device_id`
- `reason` as `str`
- `occurred_at` as timezone-aware `datetime`

**AC4 - Raw protocol state containers**
**And** `RawProtocolState`, `RawModbusState`, `RawOCPPState`, and `RawDSMRState` are defined as typed containers for raw protocol data with no energy-domain interpretation.

**AC5 - Domain boundary**
**And** no domain types (`DeviceState`, `DegradedDeviceState`, `CommandResult`) are defined or imported in this package; those belong to Epic 4 and Epic 8 respectively.

**AC6 - Type-checkable concrete implementations**
**And** mypy validates all concrete adapter implementations against `ProtocolAdapter` with no type errors.

**AC7 - Import-time structural contract test**
**And** the contract is verified by a unit test asserting that all concrete adapter classes structurally satisfy `ProtocolAdapter` at import time.

## Tasks / Subtasks

- [x] **Task 0: Pre-story gate** (AC: all)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/`

- [x] **Task 1: Create the protocol adapter package skeleton** (AC: AC1, AC5)
  - [x] Create `src/open_ems/adapters/__init__.py`
  - [x] Create `src/open_ems/adapters/protocol.py` for raw protocol contracts
  - [x] Do not create normalized device adapters yet; Epic 4 owns `DeviceAdapter`, normalized `DeviceState`, and capability profiles
  - [x] Keep `adapters/` imports limited to standard library and package-local protocol types for this story

- [x] **Task 2: Define raw command and result models** (AC: AC2)
  - [x] Define `ProtocolStatus` as a narrow type (`Literal["sent", "acked", "timeout", "error"]` or enum with equivalent values)
  - [x] Define `RawProtocolCommand` with `correlation_id`, `device_id`, protocol command name/type, and raw payload
  - [x] Define `ProtocolCommandResult` with exact AC fields and no domain-level applied/effect semantics
  - [x] Make `raw_response` accept protocol bytes or JSON-like dict payloads without forcing OCPP/Modbus/DSMR interpretation

- [x] **Task 3: Define degraded and raw state containers** (AC: AC3, AC4)
  - [x] Define `ProtocolDegradedState(device_id, reason, occurred_at)`
  - [x] Define a base raw state contract with `device_id` and protocol-specific timestamps where needed
  - [x] Define `RawModbusState` for register addresses and 16-bit integer values plus read timestamp
  - [x] Define `RawOCPPState` for raw OCPP connection/message fields used by Story 3.3
  - [x] Define `RawDSMRState` for raw parsed telegram fields plus `received_at`
  - [x] Use timezone-aware UTC datetimes (`datetime.now(UTC)` in tests/examples); never naive datetimes

- [x] **Task 4: Define `ProtocolAdapter`** (AC: AC1, AC6)
  - [x] Use `typing.Protocol` with async method signatures
  - [x] Ensure method names are exactly `get_raw_state` and `send_raw_command`
  - [x] Ensure return unions are raw protocol types only: no `DeviceState`, no `CommandResult`, no persistence or web response types
  - [x] Add package exports so future stories can import from one stable location without reaching into implementation modules

- [x] **Task 5: Add structural typing tests** (AC: AC6, AC7)
  - [x] Create `tests/unit/adapters/test_protocol_contract.py`
  - [x] Include a minimal fake adapter class in the test file that structurally satisfies `ProtocolAdapter`
  - [x] Assert import-time structural satisfaction using assignment to a `ProtocolAdapter`-typed variable or helper function
  - [x] Add negative-boundary tests where practical for invalid `protocol_status` and timezone expectations

- [x] **Task 6: Final validation** (AC: all)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/`

### Review Findings

- [x] [Review][Patch] Timestamp validators accept non-UTC aware datetimes [src/open_ems/adapters/protocol.py:20]
- [x] [Review][Patch] Structural contract test is not enforced by runtime pytest or the configured mypy gate [tests/unit/adapters/test_protocol_contract.py:40]
- [x] [Review][Patch] String protocol payloads are coerced to bytes instead of rejected [src/open_ems/adapters/protocol.py:16]
- [x] [Review][Patch] Modbus register addresses do not enforce the 16-bit upper bound [src/open_ems/adapters/protocol.py:77]
- [x] [Review][Patch] Modbus registers accept coerced string keys and values [src/open_ems/adapters/protocol.py:74]
- [x] [Review][Patch] Empty protocol identifiers are accepted [src/open_ems/adapters/protocol.py:31]

## Dev Notes

### Architecture Mandates

- Epic 3 is raw protocol communication only. It proves Modbus TCP, OCPP 1.6, and DSMR P1 framing/state/failure contracts; it must not expose normalized energy state to the decision engine or UI. Domain normalization starts in Epic 4. [Source: _bmad-output/planning-artifacts/epics.md#Epic-3-Protocol-Adapters]
- `ProtocolAdapter` is the low-level raw protocol contract for Epic 3. It is distinct from the future Epic 4 `DeviceAdapter` Protocol and must not define `DeviceState`, `DegradedDeviceState`, capability profiles, or energy sign conventions. [Source: _bmad-output/planning-artifacts/epics.md#Story-3.1-Define-protocol-adapter-contract-and-raw-protocol-types]
- Runtime communication failures in later Epic 3 adapters must be returned as `ProtocolDegradedState` or `ProtocolCommandResult(protocol_status="error"|"timeout")`; recoverable device failures must not escape adapter boundaries as raw library exceptions. [Source: _bmad-output/planning-artifacts/epics.md#Cross-story-constraints-applying-to-all-Epic-3-stories]
- Startup/configuration errors may fail fast; programming errors should propagate naturally. Do not hide implementation bugs behind broad `except Exception` blocks. [Source: _bmad-output/planning-artifacts/epics.md#Cross-story-constraints-applying-to-all-Epic-3-stories]
- All persisted/runtime timestamps in contracts and tests should be timezone-aware UTC. Use `datetime.now(UTC)` or explicit UTC datetimes; avoid naive `datetime.now()`. [Source: _bmad-output/planning-artifacts/architecture.md#Naming-Conventions]
- `adapters/` may import from `core/` only in later stories and may publish only through StateStore interfaces. For Story 3.1, prefer no `core/` dependency at all because the raw protocol contract stands below the domain layer. [Source: _bmad-output/planning-artifacts/architecture.md#Import-boundary]

### File Placement

| File | Status | Notes |
|------|--------|-------|
| `src/open_ems/adapters/__init__.py` | NEW | Export the stable raw protocol contract symbols. |
| `src/open_ems/adapters/protocol.py` | NEW | Owns `ProtocolAdapter`, `RawProtocolCommand`, `ProtocolCommandResult`, `ProtocolDegradedState`, and raw state containers. |
| `tests/unit/adapters/__init__.py` | NEW | Mirrors source package structure. |
| `tests/unit/adapters/test_protocol_contract.py` | NEW | Structural Protocol and model validation tests. |

No migrations, routes, templates, settings, repositories, or external protocol clients are required in this story.

### Suggested Type Shape

Prefer Pydantic v2 models for the data containers because architecture standardizes Pydantic for data contracts and pyproject already includes it. Keep the command/status Protocol itself in plain typing:

```python
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

ProtocolStatus = Literal["sent", "acked", "timeout", "error"]
RawPayload = bytes | dict[str, Any]


class RawProtocolCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    correlation_id: str
    device_id: str
    command_name: str
    payload: RawPayload


class ProtocolCommandResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    correlation_id: str
    device_id: str
    protocol_status: ProtocolStatus
    raw_response: RawPayload | None = None


class ProtocolDegradedState(BaseModel):
    model_config = ConfigDict(frozen=True)

    device_id: str
    reason: str
    occurred_at: datetime


class ProtocolAdapter(Protocol):
    async def get_raw_state(self) -> RawProtocolState | ProtocolDegradedState: ...
    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult: ...
```

This sketch is guidance, not a required exact implementation. Add timezone validation if the project has an established Pydantic validator pattern; otherwise add direct tests that constructed timestamps are timezone-aware and document the expectation.

### Raw State Boundaries

- `RawModbusState` should represent register-level data only, such as register address to 16-bit integer value and a `read_at` timestamp. It must not name fields such as `soc_percent`, `power_kw`, `grid_power_kw`, or inverter/battery roles. [Source: _bmad-output/planning-artifacts/epics.md#Story-3.2-Implement-Modbus-TCP-adapter-with-connection-management-and-timeout-enforcement]
- `RawOCPPState` should represent charger identity, raw recent OCPP payloads, connection status, and last CALLRESULT/CALLERROR payloads. It must not infer `session_active`, `current_power_kw`, or OPEN-EMS control intents. [Source: _bmad-output/planning-artifacts/epics.md#Story-3.3-Implement-OCPP-1.6-Central-System-adapter]
- `RawDSMRState` should represent raw parsed telegram fields and `received_at`. It must not apply DSMR sign convention, unit conversions, or map to `GridMeterState`; that starts in Epic 4. [Source: _bmad-output/planning-artifacts/epics.md#Story-3.4-Implement-DSMR-P1-adapter-with-raw-telegram-parsing-and-staleness-timestamping]

### Current Codebase Context

- `src/open_ems/adapters/` does not exist yet. Story 3.1 initializes the package.
- Existing tests mirror source structure under `tests/unit/...`; follow that layout for `tests/unit/adapters/`.
- Current quality gates from `pyproject.toml`: Python `>=3.12`, strict mypy, ruff line length 100, pytest coverage fail-under 75.
- Current dependencies already include the future protocol libraries: `pymodbus>=3.13.0`, `ocpp>=2.1.0`, and `dsmr-parser>=1.6.0`. Do not add dependencies for this contract-only story unless implementation proves one is truly required. [Source: pyproject.toml]

### Previous Story Intelligence

- Story 2.5 showed that concrete, explicit edge-case instructions reduce review churn. Keep failure states and boundaries testable instead of relying on prose-only intent. [Source: _bmad-output/implementation-artifacts/2-5-implement-session-expiry-multi-device-concurrency-policy-and-logout.md#Review-Findings]
- Epic 2 retrospective action B1 specifically asks Story 3.1 to carry a protocol-adapter checklist covering timeout, disconnect, malformed payload, stale data, and no blocking I/O. This story establishes those concepts in the contract notes; implementation stories 3.2-3.4 must turn them into fake-transport tests. [Source: _bmad-output/implementation-artifacts/epic-2-retro-2026-05-02.md#Action-Items]
- Epic 2 retrospective action B3 asks to confirm the raw/domain boundary. The explicit AC5 and raw state notes above are the handoff guardrail. [Source: _bmad-output/implementation-artifacts/epic-2-retro-2026-05-02.md#Action-Items]

### Latest Technical Notes

- PyModbus 3.13.0 is the current PyPI release as of 2026-05-02 and pyproject already matches `pymodbus>=3.13.0`. PyModbus documents async and sync APIs and warns that minor-version updates may include API changes, so concrete adapter stories should consult changelog/API changes before using client calls. [Source: https://pypi.org/project/pymodbus/]
- The Python `ocpp` package current PyPI release is 2.1.0 as of 2026-05-02, requires Python >=3.11, and supports OCPP 1.6 and 2.0.1. The project uses only OCPP 1.6 in v1; do not widen scope to OCPP 2.x behavior. [Source: https://pypi.org/project/ocpp/]
- `dsmr-parser` current PyPI release is 1.6.0 as of 2026-05-02 and pyproject already matches `dsmr-parser>=1.6.0`. Story 3.1 should not import it; Story 3.4 owns parser integration. [Source: https://pypi.org/project/dsmr-parser/]

### Testing Requirements

- Use `pytest` and existing strict mypy workflow.
- Unit tests should avoid real hardware, sockets, serial ports, or WebSockets in Story 3.1.
- A minimal fake adapter in tests is enough to prove structural typing:
  - It implements `async get_raw_state(...)`
  - It implements `async send_raw_command(...)`
  - It can be assigned to a `ProtocolAdapter` variable under mypy
- Include model tests for the allowed `protocol_status` values and representative raw payloads (`bytes` and `dict[str, Any]`).
- Keep future adapter failure-mode checklist visible for Stories 3.2-3.4: success, timeout, malformed payload, disconnect/reconnect, stale data, and no blocking I/O.

### Anti-Patterns To Reject

- Importing or defining `DeviceState`, `DegradedDeviceState`, `CommandResult`, `DeviceCommand`, `PolicyGuard`, `StateStore`, route dependencies, repositories, or web response models in `src/open_ems/adapters/protocol.py`.
- Naming raw fields with normalized energy semantics such as `grid_power_kw`, `soc_percent`, `battery_state`, `charger_session_active`, or `energy_kwh`.
- Using mutable default dict/list values in Pydantic models.
- Using `datetime.now()` without UTC.
- Adding real protocol client code, network timeouts, serial reads, or WebSocket handlers in this story.
- Swallowing programming errors with broad catch-all exception handling.

### References

- Story requirements: [Source: _bmad-output/planning-artifacts/epics.md#Story-3.1-Define-protocol-adapter-contract-and-raw-protocol-types]
- Epic 3 boundary: [Source: _bmad-output/planning-artifacts/epics.md#Epic-3-Protocol-Adapters]
- Cross-story adapter constraints: [Source: _bmad-output/planning-artifacts/epics.md#Cross-story-constraints-applying-to-all-Epic-3-stories]
- Stack and library choices: [Source: _bmad-output/planning-artifacts/architecture.md#Stack-Selection]
- Project structure: [Source: _bmad-output/planning-artifacts/architecture.md#Complete-Project-Directory-Structure]
- Import boundaries: [Source: _bmad-output/planning-artifacts/architecture.md#Import-boundary]
- Epic 2 lessons/action items: [Source: _bmad-output/implementation-artifacts/epic-2-retro-2026-05-02.md#Next-Epic-Preparation]

## Dev Agent Record

### Agent Model Used

Codex (GPT-5)

### Debug Log References

- `uv run python -m ruff check .` - passed before implementation
- `uv run python -m ruff format --check .` - passed before implementation
- `uv run python -m mypy src/` - passed before implementation
- `uv run python -m pytest tests/` - passed before implementation: 186 passed, 85.19% coverage
- `uv run python -m pytest tests/unit/adapters/test_protocol_contract.py` - expected red phase failure before package existed
- `uv run python -m pytest tests/unit/adapters/test_protocol_contract.py --no-cov` - passed after implementation: 12 passed
- `uv run python -m ruff check src/open_ems/adapters tests/unit/adapters` - passed
- `uv run python -m ruff format --check src/open_ems/adapters tests/unit/adapters` - passed
- `uv run python -m mypy src/` - passed after implementation
- `uv run python -m ruff check .` - final validation passed
- `uv run python -m ruff format --check .` - final validation passed: 63 files already formatted
- `uv run python -m mypy src/` - final validation passed: no issues in 27 source files
- `uv run python -m pytest tests/` - final validation passed: 198 passed, 86.37% coverage
- `uv run python -m pytest tests/unit/adapters/test_protocol_contract.py --no-cov` - review patch validation passed: 18 passed
- `uv run python -m ruff check .` - post-review final validation passed
- `uv run python -m ruff format --check .` - post-review final validation passed: 63 files already formatted
- `uv run python -m mypy src/` - post-review final validation passed: no issues in 27 source files
- `uv run python -m pytest tests/` - post-review final validation passed: 204 passed, 86.35% coverage

### Implementation Plan

- Add the `open_ems.adapters` package as a raw protocol boundary only; no normalized device/domain imports.
- Use frozen Pydantic v2 models with `extra="forbid"` so domain-shaped fields are rejected rather than silently ignored.
- Keep protocol status and connection status narrow through `Literal` types.
- Validate protocol timestamps as timezone-aware datetimes at model boundaries.
- Verify structural `ProtocolAdapter` compatibility through a fake adapter unit test.

### Completion Notes List

Ultimate context engine analysis completed - comprehensive developer guide created.
- Added the raw protocol adapter package skeleton and stable exports.
- Implemented `RawProtocolCommand`, `ProtocolCommandResult`, `ProtocolDegradedState`, `RawProtocolState`, `RawModbusState`, `RawOCPPState`, `RawDSMRState`, and `ProtocolAdapter`.
- Enforced raw/domain separation by rejecting extra fields on protocol models and keeping adapters free of `core`, `engine`, `web`, `storage`, or domain command imports.
- Added contract tests covering structural adapter typing, allowed protocol statuses, raw bytes/dict payloads, timezone-aware timestamp validation, Modbus 16-bit register bounds, optional OCPP message payloads, and raw state domain-field exclusions.
- Final validation passed with ruff, format check, mypy, and the full pytest suite.
- Resolved all six code review patch findings: UTC-only timestamp validation, runtime-checkable protocol test, strict protocol payload bytes, 16-bit Modbus address bounds, strict Modbus register key/value types, and non-empty protocol identifiers.
- Post-review validation passed with ruff, format check, mypy, and the full pytest suite.

### File List

- `src/open_ems/adapters/__init__.py`
- `src/open_ems/adapters/protocol.py`
- `tests/unit/adapters/__init__.py`
- `tests/unit/adapters/test_protocol_contract.py`
- `_bmad-output/implementation-artifacts/3-1-define-protocol-adapter-contract-and-raw-protocol-types.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`

## Change Log

- Created Story 3.1 context package and marked ready for development. (Date: 2026-05-02)
- Implemented Story 3.1 raw protocol adapter contract package, structural contract tests, and final validation. (Date: 2026-05-02)
- Addressed all code review patch findings and marked Story 3.1 done. (Date: 2026-05-02)
