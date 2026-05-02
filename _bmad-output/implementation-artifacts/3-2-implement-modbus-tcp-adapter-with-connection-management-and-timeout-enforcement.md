# Story 3.2: Implement Modbus TCP adapter with connection management and timeout enforcement

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want a Modbus TCP adapter that reads raw register values and writes register commands with enforced timeouts and typed failure representation,
so that the system can communicate with inverters and batteries over Modbus without the control loop blocking on unresponsive hardware.

## Acceptance Criteria

**AC1 - Raw register read**
**Given** a device is reachable at the configured Modbus TCP host and port
**When** `get_raw_state()` is called
**Then** it opens or reuses a `pymodbus` async TCP connection and reads the configured register set
**And** raw Modbus register values are returned as `RawModbusState(device_id=..., registers=..., read_at=...)`
**And** register values remain raw unsigned 16-bit values with no inverter, battery, role, unit, sign, or energy interpretation at this layer

**AC2 - Read and connection timeout**
**Given** a Modbus TCP connection or read operation exceeds 10 seconds
**When** the timeout elapses
**Then** the adapter returns `ProtocolDegradedState(device_id=..., reason="modbus_timeout", occurred_at=...)`
**And** no Modbus, socket, or asyncio timeout exception propagates beyond the adapter boundary
**And** a structlog warning is written with `event="adapter_timeout"`, `component="modbus"`, `device_id=...`, and `timeout_s=10.0`

**AC3 - Raw register write**
**Given** `send_raw_command()` is called with a supported Modbus register write command
**When** the write is executed
**Then** the adapter returns `ProtocolCommandResult(protocol_status="acked")` if the Modbus write acknowledgement is received
**And** it returns `protocol_status="timeout"` on timeout and `protocol_status="error"` on protocol/runtime communication errors
**And** protocol write acknowledgement is the only confirmation at this layer; domain-level applied/effect confirmation belongs to Epic 8
**And** optional read-back of written registers may verify protocol echo, but must not be treated as an energy-effect confirmation

**AC4 - Runtime failure translation**
**Given** runtime communication failures occur, including connection refused, broken pipe, unexpected disconnect, Modbus exception response, malformed command payload, or missing response registers
**When** the failure is encountered
**Then** it is translated to `ProtocolDegradedState` or `ProtocolCommandResult(protocol_status="error")`
**And** startup configuration errors such as invalid host, invalid port, invalid timeout, or invalid register plan may fail fast at initialization with a structured log error
**And** programming errors are not swallowed by broad catch-all handlers

**AC5 - Per-device connection isolation**
**Given** one Modbus device connection fails
**When** the adapter handles that failure
**Then** other device adapters using separate adapter instances and separate TCP clients are unaffected
**And** a failed adapter attempts to reconnect on a later operation without requiring process restart

**AC6 - Tests**
**And** tests validate successful register read, write acknowledgement, read timeout, write timeout, connection refused, Modbus exception response, malformed command payload, unexpected register response, and reconnect after mid-session disconnect
**And** timeout tests verify `get_raw_state()` and `send_raw_command()` return within 10 seconds plus a small epsilon under all simulated failure conditions
**And** tests use simulated/fake Modbus transports; they do not require physical devices or an installer LAN

## Tasks / Subtasks

- [x] **Task 0: Pre-story quality gate** (AC: all)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/`

- [x] **Task 1: Add Modbus adapter package and configuration model** (AC: AC1, AC4, AC5)
  - [x] Create `src/open_ems/adapters/modbus/__init__.py`
  - [x] Create `src/open_ems/adapters/modbus/tcp.py`
  - [x] Define a frozen Pydantic config model for one adapter instance, including `device_id` (OPEN-EMS identity), `host`, `port`, `modbus_device_id`, `registers`, `timeout_s`, and reconnect parameters
  - [x] Model each register range with an explicit Modbus table such as `holding` or `input`, start address, and count; reject duplicate output addresses that would collide in `RawModbusState.registers`
  - [x] Validate `port` as `1..65535`, `modbus_device_id` as valid non-broadcast target for v1, `timeout_s <= 10.0`, and register addresses/counts as unsigned 16-bit bounded values
  - [x] Keep device registry persistence out of this story; Epic 4 and installer discovery/config stories own stored device configuration

- [x] **Task 2: Implement async connection lifecycle** (AC: AC1, AC2, AC5)
  - [x] Use `pymodbus.client.AsyncModbusTcpClient`; do not use synchronous `ModbusTcpClient` in the event loop
  - [x] Lazily connect on first operation, reuse the client while healthy, and close/recreate it after failed connection states
  - [x] Serialize calls per adapter instance with an `asyncio.Lock`; PyModbus docs state async clients do not prevent semi-parallel calls at the application level
  - [x] Wrap connect/read/write operations in `asyncio.wait_for(..., timeout=timeout_s)` so OPEN-EMS enforces the 10-second NFR independent of library defaults
  - [x] Provide an explicit `async close()` helper for tests and future runtime shutdown, while keeping `ProtocolAdapter` methods unchanged

- [x] **Task 3: Implement `get_raw_state()`** (AC: AC1, AC2, AC4)
  - [x] Read the configured register plan and return `RawModbusState` with `read_at=datetime.now(UTC)`
  - [x] Use `read_holding_registers` for `holding` ranges and `read_input_registers` for `input` ranges; keep `RawModbusState.registers` keyed by the configured raw address only after collision validation
  - [x] Use PyModbus `device_id=` for the Modbus unit id, mapped from config `modbus_device_id`; never confuse it with the OPEN-EMS `device_id`
  - [x] For Modbus exception responses or missing `registers`, return `ProtocolDegradedState(reason="modbus_error", occurred_at=...)`
  - [x] For timeout, return `ProtocolDegradedState(reason="modbus_timeout", occurred_at=...)`
  - [x] Do not add normalized fields such as `power_kw`, `soc_percent`, `role`, `grid_power_kw`, or capability metadata

- [x] **Task 4: Implement `send_raw_command()`** (AC: AC3, AC4)
  - [x] Support explicit raw command payloads for `write_register` and, if needed, `write_registers`
  - [x] Require payload fields such as `operation`, `address`, `value` or `values`, and optional `modbus_device_id` override only if the design needs per-command targeting
  - [x] Reject malformed or unsupported payloads with `ProtocolCommandResult(protocol_status="error", raw_response={...})`; do not raise recoverable payload errors into callers
  - [x] Return `raw_response` as bytes or a plain dict containing protocol-level details only, such as address/value echo or Modbus exception metadata
  - [x] Preserve `correlation_id` and OPEN-EMS `device_id` from the incoming `RawProtocolCommand`

- [x] **Task 5: Add logging and failure boundaries** (AC: AC2, AC4, AC5)
  - [x] Use `structlog.get_logger(__name__)`
  - [x] Log timeout as `adapter_timeout`, disconnect/refused as `adapter_disconnected` or `adapter_connection_failed`, and Modbus exception responses as `adapter_protocol_error`
  - [x] Include `component="modbus"`, OPEN-EMS `device_id`, host, port, operation, and timeout where useful
  - [x] Do not write installer-facing event log rows; Epic 3 logs to stdout only
  - [x] Avoid `print()` and avoid broad `except Exception` that hides programming bugs

- [x] **Task 6: Add deterministic tests** (AC: AC1-AC6)
  - [x] Create `tests/unit/adapters/modbus/__init__.py`
  - [x] Create `tests/unit/adapters/modbus/test_tcp_adapter.py`
  - [x] Use a fake async client/client factory for unit tests so failures are deterministic and fast
  - [x] Cover config validation, successful read, successful write ACK, read timeout, write timeout, connection refused, Modbus exception response, malformed payload, per-instance isolation, reconnect after disconnect, UTC timestamps, and structural `ProtocolAdapter` satisfaction
  - [x] Add an integration-style simulated PyModbus server test only if it remains deterministic in CI; fake transport coverage is the minimum required guardrail

- [x] **Task 7: Final validation** (AC: all)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/`

## Dev Notes

### Current Codebase State

- `src/open_ems/adapters/protocol.py` already defines `ProtocolAdapter`, `RawProtocolCommand`, `ProtocolCommandResult`, `ProtocolDegradedState`, `RawModbusState`, and strict UTC/16-bit validation. Extend this package; do not redefine those contracts. [Source: src/open_ems/adapters/protocol.py]
- `src/open_ems/adapters/__init__.py` exports the stable raw protocol contract symbols. Add Modbus exports carefully if useful, but do not break existing imports. [Source: src/open_ems/adapters/__init__.py]
- `tests/unit/adapters/test_protocol_contract.py` proves the raw/domain boundary, strict protocol payloads, UTC timestamps, and Modbus register bounds. Reuse these patterns for Story 3.2 tests. [Source: tests/unit/adapters/test_protocol_contract.py]
- Current dependencies already include `pymodbus>=3.13.0`; do not add another Modbus library. [Source: pyproject.toml]
- Current quality gates are Python `>=3.12`, strict mypy, ruff line length 100, pytest-asyncio, and coverage fail-under 75. [Source: pyproject.toml]

### Architecture Guardrails

- Epic 3 is raw protocol communication only. Domain normalization, device roles, capability profiles, and energy sign conventions belong to Epic 4. [Source: _bmad-output/planning-artifacts/epics.md#Epic-3-Protocol-Adapters]
- Runtime communication failures must not escape adapter boundaries; they become `ProtocolDegradedState` or `ProtocolCommandResult(protocol_status="error"|"timeout")`. Startup/configuration errors may fail fast. Programming errors should propagate. [Source: _bmad-output/planning-artifacts/epics.md#Cross-story-constraints-applying-to-all-Epic-3-stories]
- All critical I/O must be non-blocking. Blocking adapters must be isolated behind timeouts and execution boundaries; this story should use the async PyModbus client directly. [Source: _bmad-output/planning-artifacts/architecture.md#Trade-offs]
- Adapter methods must return degraded state for recoverable communication failures, not raise into the control loop. [Source: _bmad-output/planning-artifacts/architecture.md#Safety-Critical-Patterns]
- All persisted/runtime timestamps must be timezone-aware UTC; use `datetime.now(UTC)`, never naive `datetime.now()`. [Source: _bmad-output/planning-artifacts/architecture.md#Naming-Conventions]
- Tests mirror source structure under `tests/unit/...`; use `tests/unit/adapters/modbus/` for this story. [Source: _bmad-output/planning-artifacts/architecture.md#Project-Structure-Patterns]
- Epic 3 uses structlog to stdout only. Installer-facing audit/event-log entries are created later when protocol failures affect system state. [Source: _bmad-output/planning-artifacts/epics.md#Cross-story-constraints-applying-to-all-Epic-3-stories]

### Suggested Implementation Shape

Use a small config and adapter class; keep helper protocols private so tests can inject fake clients without coupling production code to PyModbus internals.

```python
class ModbusTcpAdapterConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    host: NonEmptyStr
    port: int = Field(default=502, ge=1, le=65_535)
    modbus_device_id: int = Field(default=1, ge=1, le=247)
    registers: tuple[ModbusRegisterRange, ...]
    timeout_s: float = Field(default=10.0, gt=0, le=10.0)
    reconnect_delay_s: float = Field(default=0.1, ge=0)
    reconnect_delay_max_s: float = Field(default=30.0, ge=0)
```

This is guidance, not a required exact API. The important part is that constructor/config fields are explicit, validated, and separate the OPEN-EMS `device_id` from the Modbus unit id.

### Command Payload Contract

`RawProtocolCommand.payload` is intentionally generic, so Story 3.2 must define the Modbus payload shape in adapter tests and docstrings. Recommended v1 shapes:

```python
{"operation": "write_register", "address": 40001, "value": 123}
{"operation": "write_registers", "address": 40001, "values": [1, 2, 3]}
```

Validate address and values as unsigned 16-bit integers. Reject strings that would otherwise be coerced to integers. Keep payload errors recoverable by returning `ProtocolCommandResult(protocol_status="error")` with a small raw response dict.

### PyModbus 3.13 Notes

- Use `AsyncModbusTcpClient` from `pymodbus.client`. Official docs show `await client.connect()`, `client.close()`, and read/write calls as awaitable async operations. [Source: https://pymodbus.readthedocs.io/en/stable/source/client.html]
- PyModbus docs state that a TCP device should have its own client object, which matches AC5 per-device connection isolation. [Source: https://pymodbus.readthedocs.io/en/stable/source/client.html]
- PyModbus docs warn that the async client does not prevent semi-parallel calls; serialize operations per adapter instance with an `asyncio.Lock`. [Source: https://pymodbus.readthedocs.io/en/stable/source/client.html]
- `AsyncModbusTcpClient` accepts `timeout`, `retries`, `reconnect_delay`, and `reconnect_delay_max`. Still wrap OPEN-EMS operations with `asyncio.wait_for` to enforce the product-level 10-second maximum. [Source: https://pymodbus.readthedocs.io/en/stable/source/client.html]
- Current PyModbus APIs use `device_id=` for logical Modbus addressing. Name the OPEN-EMS config field `modbus_device_id` and map it intentionally to PyModbus `device_id=`. [Source: https://pymodbus.readthedocs.io/en/stable/source/client.html]
- The official API changes page for 3.13.0 notes recent API drift across 3.x. Avoid older examples using `unit=` or removed callback names. [Source: https://pymodbus.readthedocs.io/en/stable/source/api_changes.html]

### Previous Story Intelligence

- Story 3.1 review found that permissive coercion and loose timestamp validation were risky. Story 3.2 should use strict Pydantic validation for config and payload parsing, and tests should prove strings are rejected instead of coerced. [Source: _bmad-output/implementation-artifacts/3-1-define-protocol-adapter-contract-and-raw-protocol-types.md#Review-Findings]
- Story 3.1 established frozen Pydantic models with `extra="forbid"` to protect the raw/domain boundary. Keep that pattern for Modbus config and any internal command models. [Source: _bmad-output/implementation-artifacts/3-1-define-protocol-adapter-contract-and-raw-protocol-types.md#Completion-Notes-List]
- Epic 2 retrospective specifically called out fake transport fixtures, timeout, malformed payload, disconnect, reconnect, stale/unavailable data, and no blocking I/O as Epic 3 safeguards. [Source: _bmad-output/implementation-artifacts/epic-2-retro-2026-05-02.md#Action-Items]

### Anti-Patterns To Reject

- Using synchronous `ModbusTcpClient` or any blocking socket call inside the event loop.
- Letting `asyncio.TimeoutError`, socket errors, or PyModbus runtime communication exceptions escape from `get_raw_state()` or `send_raw_command()`.
- Catching all exceptions broadly and converting programming bugs into fake device errors.
- Importing or defining `DeviceState`, `DegradedDeviceState`, `CommandResult`, `DeviceCommand`, `PolicyGuard`, `StateStore`, device roles, capability profiles, repositories, routes, or templates.
- Writing installer-facing audit/event-log rows from this raw protocol layer.
- Naming raw fields with normalized semantics such as `power_kw`, `soc_percent`, `battery_state`, `grid_power_kw`, or `energy_kwh`.
- Sharing one PyModbus client across multiple configured devices.
- Confusing OPEN-EMS `device_id` with PyModbus `device_id=`.

### References

- Story requirements: [Source: _bmad-output/planning-artifacts/epics.md#Story-3.2-Implement-Modbus-TCP-adapter-with-connection-management-and-timeout-enforcement]
- Epic 3 constraints: [Source: _bmad-output/planning-artifacts/epics.md#Cross-story-constraints-applying-to-all-Epic-3-stories]
- Stack and library choices: [Source: _bmad-output/planning-artifacts/architecture.md#Stack-Selection]
- Async and degraded-state patterns: [Source: _bmad-output/planning-artifacts/architecture.md#Safety-Critical-Patterns]
- Project structure: [Source: _bmad-output/planning-artifacts/architecture.md#Project-Structure-Patterns]
- Existing adapter contract: [Source: src/open_ems/adapters/protocol.py]
- Current dependency and quality gates: [Source: pyproject.toml]
- PyModbus async client docs: [Source: https://pymodbus.readthedocs.io/en/stable/source/client.html]
- PyModbus API changes: [Source: https://pymodbus.readthedocs.io/en/stable/source/api_changes.html]

## Dev Agent Record

### Agent Model Used

Codex (GPT-5)

### Implementation Plan

- Add a narrow `open_ems.adapters.modbus` package with validated config and raw Modbus range models.
- Keep PyModbus behind a small client protocol so production uses `AsyncModbusTcpClient` while tests use deterministic fake clients.
- Enforce the 10-second product timeout with `asyncio.wait_for`, independent of PyModbus defaults.
- Translate recoverable runtime communication failures to protocol degraded/error results while letting programming errors fail fast.
- Test all required success and failure paths without physical hardware or LAN dependencies.

### Debug Log References

- `uv run python -m ruff check .` - pre-story gate passed.
- `uv run python -m ruff format --check .` - pre-story gate passed: 63 files already formatted.
- `uv run python -m mypy src/` - pre-story gate passed: no issues in 27 source files.
- `uv run python -m pytest tests/` - pre-story gate passed: 204 passed, 86.35% coverage.
- `uv run python -m pytest tests/unit/adapters/modbus/test_tcp_adapter.py --no-cov` - red phase failed before package existed, then passed after implementation: 16 passed.
- `uv run python -m ruff check src/open_ems/adapters tests/unit/adapters` - focused adapter lint passed after implementation.
- `uv run python -m ruff format --check src/open_ems/adapters tests/unit/adapters` - focused adapter format check passed after implementation.
- `uv run python -m mypy src/` - focused type check passed after implementation: no issues in 29 source files.
- `uv run python -m ruff check .` - final validation passed.
- `uv run python -m ruff format --check .` - final validation passed: 67 files already formatted.
- `uv run python -m mypy src/` - final validation passed: no issues in 29 source files.
- `uv run python -m pytest tests/` - final validation passed: 220 passed, 86.06% coverage.

### Completion Notes List

Ultimate context engine analysis completed - comprehensive developer guide created.
- Pre-story quality gate passed before implementation.
- Added the Modbus TCP adapter package with validated per-device config, register ranges, and explicit `modbus_device_id` handling.
- Implemented async connection reuse, per-adapter locking, timeout enforcement, close/reconnect behavior, raw register reads, and raw write command handling.
- Runtime communication failures now return `ProtocolDegradedState` or `ProtocolCommandResult(protocol_status="error"|"timeout")`; no installer-facing event log rows are written at this layer.
- Added deterministic fake-client tests covering reads, writes, timeouts, connection refusal, Modbus exception responses, malformed payloads, unexpected register payloads, reconnect, UTC timestamps, and `ProtocolAdapter` structural satisfaction.
- Final validation passed with ruff, format check, mypy, and the full pytest suite.

### File List

- `src/open_ems/adapters/modbus/__init__.py`
- `src/open_ems/adapters/modbus/tcp.py`
- `tests/unit/adapters/modbus/__init__.py`
- `tests/unit/adapters/modbus/test_tcp_adapter.py`
- `_bmad-output/implementation-artifacts/3-2-implement-modbus-tcp-adapter-with-connection-management-and-timeout-enforcement.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`

## Change Log

- Implemented Story 3.2 Modbus TCP adapter, deterministic fake-client tests, and final validation. (Date: 2026-05-02)

### Review Findings

- [x] [Review][Decision] Register collision validator conflates holding and input table address spaces — **Resolved 2026-05-02: keep flat dict and flat collision guard (Option 1); cross-table same-address configs are intentionally disallowed**

- [x] [Review][Patch] Factory exception in `_ensure_connected` not translated to `_ModbusCommunicationError` — if `self._client_factory(self.config)` raises (e.g., bad host format rejected by pymodbus), the raw exception escapes the adapter boundary [tcp.py:252]

- [x] [Review][Patch] `close()` not lock-guarded — called concurrently with an in-flight `get_raw_state()` or `send_raw_command()`, `_discard_client()` can close the socket the active coroutine is using [tcp.py:244]

- [x] [Review][Patch] `write_registers` address+count boundary not validated — `address=65_534` with `values=[1,2]` passes Pydantic but overflows the 16-bit address space; the `model_validator` present on `ModbusRegisterRange` is absent for `_WriteRegistersPayload` [tcp.py:151-157]

- [x] [Review][Patch] `FakeAsyncModbusClient` `count` parameter is positional instead of keyword-only, mismatching the `_AsyncModbusClient` Protocol — tests pass today because call sites use `count=...`, but the fake does not structurally satisfy the Protocol [test_tcp_adapter.py:63,80]

- [x] [Review][Patch] Write-timeout path not verified by any test — `test_send_raw_command_timeout_returns_timeout_result` asserts the result but does not use `structlog.testing.capture_logs()` to confirm `event="adapter_timeout"` is emitted [test_tcp_adapter.py:347]

- [x] [Review][Patch] `ProtocolDegradedState.occurred_at` UTC never asserted — all degraded-path tests check `isinstance(state, ProtocolDegradedState)` and `state.reason` but none verify `state.occurred_at.utcoffset().total_seconds() == 0` [test_tcp_adapter.py]

- [x] [Review][Patch] `pytest.raises(ValidationError)` without field-specific assertion — tests only confirm *some* error fires; a regression that moves the error to a different field keeps the test green (story intelligence explicitly called for proving the correct field rejects strings) [test_tcp_adapter.py:149]

- [x] [Review][Patch] `reconnect_delay_max_s` validator allows equal values despite "greater than" error message — guard is `< self.reconnect_delay_s` (strict), so equal values pass silently [tcp.py:92-93]

- [x] [Review][Patch] `bytes` payload to `send_raw_command` returns `protocol_status="error"` with no hint that a dict is required — `_parse_write_payload` silently returns `None` for any non-dict input including the valid `StrictBytes` path of `RawPayload` [tcp.py:419-432]

- [x] [Review][Defer] `asyncio.CancelledError` propagates from `get_raw_state`/`send_raw_command` — not in `_RECOVERABLE_EXCEPTIONS`; however, `CancelledError` is a `BaseException` and should propagate for cooperative task cancellation — deferring, correct Python async behavior
