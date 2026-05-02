# Story 3.3: Implement OCPP 1.6 Central System adapter

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want an OCPP 1.6 Central System that EV chargers connect to, with raw OCPP message data stored and accessible,
so that the system can communicate with EV chargers using the charger-initiates-connection model without polling.

## Acceptance Criteria

**AC1 - Charger connection and OCPP message framing**
**Given** the OCPP Central System is running
**When** an EV charger connects via WebSocket
**Then** the connection is accepted, the charger is registered by its `charge_point_id`, and OCPP 1.6 message framing (CALL/CALLRESULT/CALLERROR) is handled per specification
**And** incoming `BootNotification`, `Heartbeat`, and `StatusNotification` messages are processed and their raw payloads stored in adapter state
**And** `BootNotification` receives `RegistrationStatus.accepted` in CALLRESULT
**And** `Heartbeat` receives the current UTC time in CALLRESULT
**And** `StatusNotification` receives an empty CALLRESULT

**AC2 - `get_raw_state()` returns `RawOCPPState`**
**Given** `get_raw_state()` is called for a connected charger
**When** the call executes
**Then** it returns a `RawOCPPState` (already defined in `protocol.py`) containing:
- `device_id`: OPEN-EMS device ID from config
- `charge_point_id`: OCPP charge point identity
- `last_status_notification`: raw payload dict from the most recent BootNotification or StatusNotification (nullable)
- `last_heartbeat_at`: datetime of the most recent Heartbeat (nullable)
- `connection_status`: `"connected"`
- `last_call_result`: raw CALLRESULT payload dict from the most recent dispatched command (nullable)
- `last_call_error`: raw CALLERROR payload dict if the most recent command was rejected (nullable)
**And** no normalization (session_active, current_power_kw, etc.) is applied — raw OCPP message data only

**AC3 - Disconnected charger returns `ProtocolDegradedState`**
**Given** a charger has not connected yet, has disconnected, or no Heartbeat has been received within `heartbeat_timeout_s` (default: 60 seconds)
**When** `get_raw_state()` is called
**Then** it returns `ProtocolDegradedState(device_id=..., reason="ocpp_disconnected", occurred_at=...)`
**And** `occurred_at` is a timezone-aware UTC datetime
**And** a structlog warning is written: `event="adapter_disconnected"`, `component="ocpp"`, `device_id=...`, `charge_point_id=...` (emitted when disconnect is first detected, not on every `get_raw_state()` call)

**AC4 - `send_raw_command()` dispatches OCPP CALL and awaits CALLRESULT**
**Given** `send_raw_command()` is called with a raw OCPP action name and payload dict
**When** the command is dispatched
**Then** the adapter sends the OCPP CALL message and awaits CALLRESULT within `command_timeout_s` (default: 10 seconds)
**And** a `ProtocolCommandResult` is returned: `protocol_status="acked"` on CALLRESULT, `"error"` on CALLERROR, `"timeout"` if no response arrives within timeout
**And** a structlog warning is written: `event="adapter_timeout"`, `component="ocpp"`, `device_id=...`, `command=...`, `timeout_s=...` on timeout
**And** mapping of OPEN-EMS control intents to specific OCPP action names and payloads is not performed at this layer — that belongs to Epic 4 and Epic 8
**And** `send_raw_command()` returns `protocol_status="error"` if not connected, unknown action, or invalid payload dict

**AC5 - Runtime failure translation and isolation**
**Given** runtime OCPP failures occur (malformed message, WebSocket error, CALLERROR)
**When** the failure is encountered
**Then** it is translated to `ProtocolDegradedState` or `ProtocolCommandResult(protocol_status="error")` — no unhandled exception propagates from `get_raw_state()` or `send_raw_command()`
**And** malformed OCPP messages (invalid JSON, bad framing) do not crash the message loop — the loop continues processing subsequent valid messages
**And** startup configuration errors (invalid device_id, out-of-range timeout) may raise `ValidationError` at instantiation
**And** multiple concurrent charger connections are supported — each charger has its own `OCPPChargerAdapter` instance with independent state

**AC6 - Tests**
**And** a simulated OCPP charger client (fake WebSocket using asyncio.Queue pairs) is used in tests to validate: BootNotification acceptance, StatusNotification storage, Heartbeat timestamp update, CALLRESULT for a dispatched command, CALLERROR response, and charger disconnect/reconnect
**And** timeout tests verify that `send_raw_command()` returns within `command_timeout_s` plus a small epsilon — no hang
**And** malformed OCPP message tests verify that parse errors are translated to `ProtocolDegradedState` (on next `get_raw_state()`) and do not crash the message loop
**And** tests use fake WebSocket transports only — no real network, no running FastAPI server

## Tasks / Subtasks

- [x] **Task 0: Pre-story quality gate** (AC: all)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/`

- [x] **Task 1: Add OCPP adapter package and configuration model** (AC: AC1, AC5)
  - [x] Create `src/open_ems/adapters/ocpp/__init__.py`
  - [x] Create `src/open_ems/adapters/ocpp/central_system.py`
  - [x] Define frozen Pydantic `OCPPAdapterConfig` with `device_id`, `charge_point_id`, `heartbeat_timeout_s` (default 60.0, gt=0), `command_timeout_s` (default 10.0, gt=0, le=10.0)
  - [x] Reject non-numeric values for float fields with a `field_validator` (mirrors Story 3.2 pattern)
  - [x] Keep the config model independent from the FastAPI web layer — no FastAPI imports in this package

- [x] **Task 2: Implement `_InternalChargePoint` and mutable charger state** (AC: AC1, AC2)
  - [x] Define `_ChargerState` as a plain mutable class (NOT a Pydantic model — it is updated from inside `@on` handlers): `connected: bool`, `last_status_notification: dict | None`, `last_heartbeat_at: datetime | None`, `last_call_result: dict | None`, `last_call_error: dict | None`
  - [x] Subclass `ocpp.v16.ChargePoint` as `_InternalChargePoint(charge_point_id, connection, state, response_timeout)` — takes an injected `_ChargerState` reference so the adapter can read it
  - [x] Implement `@on(Action.BootNotification)` handler: store `{"type": "BootNotification", "charge_point_model": ..., "charge_point_vendor": ..., **kwargs}` in `state.last_status_notification`; return `call_result.BootNotification(current_time=..., interval=10, status=RegistrationStatus.accepted)`
  - [x] Implement `@on(Action.Heartbeat)` handler: store `datetime.now(UTC)` in `state.last_heartbeat_at`; return `call_result.Heartbeat(current_time=datetime.now(UTC).isoformat())`
  - [x] Implement `@on(Action.StatusNotification)` handler: store `{"type": "StatusNotification", "connector_id": ..., "error_code": ..., "status": ..., **kwargs}` in `state.last_status_notification`; return `call_result.StatusNotification()`
  - [x] Import: `from ocpp.v16 import ChargePoint as _BaseChargePoint, call_result`; `from ocpp.routing import on`; `from ocpp.v16.enums import Action, RegistrationStatus`

- [x] **Task 3: Implement `OCPPChargerAdapter`** (AC: AC1–AC5)
  - [x] `__init__(self, config: OCPPAdapterConfig)` — stores config, initializes `self._state = _ChargerState()`, `self._handler: _InternalChargePoint | None = None`
  - [x] `async handle_connection(self, connection: Any) -> None` — creates fresh `_ChargerState`, creates `_InternalChargePoint` with injected state and `response_timeout = config.command_timeout_s + 1.0`, awaits `handler.start()`; `finally` sets `state.connected = False` and logs `adapter_disconnected`; re-calling after disconnect starts a new connection (reconnect)
  - [x] `async get_raw_state(self) -> RawOCPPState | ProtocolDegradedState` — if not connected, return `ProtocolDegradedState(reason="ocpp_disconnected")`; if `last_heartbeat_at` is not None and age > `heartbeat_timeout_s`, return `ProtocolDegradedState(reason="ocpp_disconnected")`; otherwise return `RawOCPPState` from current `_ChargerState`
  - [x] `async send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult` — validate connected/handler; validate payload is a dict; look up call class via `getattr(ocpp_call, command.command_name, None)`; construct dataclass with `**command.payload`; call with `asyncio.wait_for(handler.call(payload, suppress=False), timeout=config.command_timeout_s)`; catch `TimeoutError` → timeout result; catch `Exception` → error result; store CALLRESULT dict in `state.last_call_result`, CALLERROR in `state.last_call_error`
  - [x] `_degraded(self, reason: str) -> ProtocolDegradedState` — helper returning degraded state with `datetime.now(UTC)`

- [x] **Task 4: Implement `OCPPCentralSystem`** (AC: AC1, AC5)
  - [x] `__init__(self)` — `self._adapters: dict[str, OCPPChargerAdapter] = {}`
  - [x] `register(self, config: OCPPAdapterConfig) -> OCPPChargerAdapter` — creates `OCPPChargerAdapter(config)`, stores in `_adapters[config.charge_point_id]`, returns it
  - [x] `get_adapter(self, charge_point_id: str) -> OCPPChargerAdapter | None` — lookup
  - [x] `async handle_charger(self, charge_point_id: str, connection: Any) -> None` — looks up or auto-creates adapter, calls `await adapter.handle_connection(connection)`; blocks until charger disconnects (designed for use as the body of a FastAPI WebSocket route)

- [x] **Task 5: Add logging and failure boundaries** (AC: AC3, AC4, AC5)
  - [x] Use `structlog.get_logger(__name__)`
  - [x] Log timeout: `event="adapter_timeout"`, `component="ocpp"`, `device_id=...`, `charge_point_id=...`, `command=...`, `timeout_s=...`
  - [x] Log disconnect (in `handle_connection` finally): `event="adapter_disconnected"`, `component="ocpp"`, `device_id=...`, `charge_point_id=...`
  - [x] Log CALLERROR/protocol error: `event="adapter_protocol_error"`, `component="ocpp"`, `device_id=...`, `charge_point_id=...`, `command=...`, `reason=...`
  - [x] Do not write installer-facing event log rows; Epic 3 logs to stdout only
  - [x] Do not use broad `except Exception` that swallows programming bugs in `handle_connection` — only catch connection/protocol errors; let `asyncio.CancelledError` propagate

- [x] **Task 6: Add deterministic tests** (AC: AC1–AC6)
  - [x] Create `tests/unit/adapters/ocpp/__init__.py`
  - [x] Create `tests/unit/adapters/ocpp/test_ocpp_adapter.py`
  - [x] Use a dual-queue `_FakeWebSocket` (see Dev Notes) — no real network
  - [x] Cover: config validation, BootNotification acceptance + state update, Heartbeat UTC timestamp update, StatusNotification raw payload storage, get_raw_state when connected, get_raw_state when disconnected (reason="ocpp_disconnected"), get_raw_state when heartbeat stale, occurred_at UTC assertion, send_raw_command acked on CALLRESULT, send_raw_command error on CALLERROR, send_raw_command timeout with adapter_timeout log, send_raw_command malformed payload, send_raw_command unknown action, send_raw_command not-connected, malformed JSON does not crash loop, disconnect detection, reconnect after disconnect, per-instance isolation, ProtocolAdapter structural satisfaction
  - [x] All timeout tests verify return in `command_timeout_s + epsilon` — no hang

- [x] **Task 7: Final validation** (AC: all)

### Review Follow-ups (AI)

- [x] [Review][Decision] Heartbeat staleness does not emit `adapter_disconnected` log — Decision: keep as-is; heartbeat timeout is a distinct liveness condition from physical disconnect. [`central_system.py` `get_raw_state()` L176-179]
- [x] [Review][Decision] `last_call_error` not cleared after subsequent successful command — Decision: no change; each field reflects the most recent of that event type independently. [`central_system.py` `send_raw_command()` L251-252]
- [x] [Review][Patch] `except Exception: pass` swallows programming errors silently — Fixed: now logs `event="adapter_connection_error"` with exception type and detail. [`central_system.py` L159-160]
- [x] [Review][Patch] `on_heartbeat` captures `datetime.now(UTC)` twice — Fixed: captured once, reused for both `last_heartbeat_at` and CALLRESULT. [`central_system.py` L116-117]
- [x] [Review][Defer] `OCPPCentralSystem.register()` silently overwrites existing adapter — no guard or warning if called twice for the same `charge_point_id`. Pre-existing design choice; no story requirement to guard it. [`central_system.py` L274-278] — deferred, pre-existing

### Review Follow-ups (Senior Dev — 2026-05-02)

- [x] [Review][Patch] `finally` unconditionally clears `self._handler`, killing concurrent reconnect — Fixed: guarded with `if self._handler is handler`. [`central_system.py` L136-138]
- [x] [Review][Patch] `dataclasses.asdict(result)` raises `TypeError` uncaught for non-dataclass return — Fixed: wrapped in `try/except TypeError` with structured warning log. [`central_system.py` L226]
- [x] [Review][Patch] `handle_charger` auto-creates adapter silently — Fixed: raises `KeyError` for unknown `charge_point_id`; callers must pre-register. [`central_system.py` L259-268]

## Dev Notes

### Current Codebase State

- `src/open_ems/adapters/protocol.py` defines `RawOCPPState` (already complete — do NOT redefine). Its fields: `device_id`, `charge_point_id`, `last_status_notification`, `last_heartbeat_at`, `connection_status`, `last_call_result`, `last_call_error`. [Source: src/open_ems/adapters/protocol.py:102–119]
- `src/open_ems/adapters/__init__.py` already exports `RawOCPPState`, `ProtocolAdapter`, `ProtocolCommandResult`, `ProtocolDegradedState`, `RawProtocolCommand`, and others. Add OCPP exports carefully — do not break existing imports. [Source: src/open_ems/adapters/__init__.py]
- `src/open_ems/adapters/modbus/tcp.py` is the established pattern for a raw protocol adapter in this codebase. Mirror its structure: frozen config model, state injection for testability, structlog with the same event names, `asyncio.wait_for` timeout enforcement, no domain types. [Source: src/open_ems/adapters/modbus/tcp.py]
- `ocpp>=2.1.0` is already in `pyproject.toml` — do not add another OCPP library. [Source: pyproject.toml]
- Current quality gates: Python `>=3.12`, strict mypy, ruff line length 100, pytest-asyncio `asyncio_mode = "auto"`, coverage fail-under 75. [Source: pyproject.toml]
- `tests/unit/adapters/modbus/test_tcp_adapter.py` established the test patterns to follow. [Source: tests/unit/adapters/modbus/test_tcp_adapter.py]

### Architecture Guardrails

- Epic 3 is raw protocol only. Domain normalization (`EVChargerState`, session_active, current_power_kw), device roles, and capability profiles belong to Epic 4. [Source: _bmad-output/planning-artifacts/epics.md#Epic-3-Protocol-Adapters]
- Runtime communication failures must not escape `get_raw_state()` or `send_raw_command()`. Startup config errors (`ValidationError`) may propagate. Programming bugs (`AssertionError`, `AttributeError`) must NOT be swallowed. [Source: _bmad-output/planning-artifacts/epics.md#Cross-story-constraints]
- All persisted/runtime timestamps must be timezone-aware UTC: `datetime.now(UTC)`. Never `datetime.utcnow()`. [Source: _bmad-output/planning-artifacts/architecture.md#Naming-Conventions]
- Tests mirror source structure under `tests/unit/...` — use `tests/unit/adapters/ocpp/`. [Source: _bmad-output/planning-artifacts/architecture.md#Project-Structure-Patterns]
- Epic 3 uses structlog to stdout only. No installer-facing `event_log` entries at this layer. [Source: _bmad-output/planning-artifacts/epics.md#Cross-story-constraints]
- FastAPI WebSocket route `/ocpp/{charge_point_id}` is described in Decision 3.2 but is wired in the web layer, not in this adapter package. This story creates the adapter only; the FastAPI route uses `OCPPCentralSystem.handle_charger()`. [Source: _bmad-output/planning-artifacts/architecture.md#Decision-3.2]
- Architecture lists `charger_adapter.py` and `profiles/` under `adapters/ocpp/` — those belong to Epic 4 (domain normalization). Do NOT create them in this story. [Source: _bmad-output/planning-artifacts/architecture.md#Project-Structure]

### OCPP Library API (ocpp>=2.1.0, v16 module)

**Imports:**
```python
from ocpp.v16 import ChargePoint as _BaseChargePoint
from ocpp.v16 import call as ocpp_call      # CS→CP call dataclasses
from ocpp.v16 import call_result            # CS response dataclasses
from ocpp.routing import on                 # handler decorator
from ocpp.v16.enums import Action, RegistrationStatus
```

**Class names** — call classes have NO `Payload` suffix (confirmed):
- `call.BootNotification`, `call.ChangeAvailability`, `call.RemoteStartTransaction`, `call.Reset`, `call.UnlockConnector`, etc.
- `call_result.BootNotification(current_time, interval, status)`, `call_result.Heartbeat(current_time)`, `call_result.StatusNotification()`

**`ChargePoint.__init__(id, connection, response_timeout=30)`**
- `connection` must implement `async send(str) -> None` and `async recv() -> str` (duck-typed)
- `response_timeout` is the library's internal timeout for awaiting CALLRESULT
- Set `response_timeout = config.command_timeout_s + 1.0` so OPEN-EMS `asyncio.wait_for` fires first

**`ChargePoint.start()`**
- Loops: `recv()` → `route_message(raw_msg)`
- `route_message()` internally catches `OCPPError` from JSON parse failures — malformed messages are logged and discarded; loop continues ✓
- Propagates any other exception from `recv()` — this is how disconnect is detected

**`ChargePoint.call(payload, suppress=False)`**
- Pass `suppress=False` so CALLERROR raises instead of returning `None`
- Raises `asyncio.TimeoutError` when `_response_timeout` elapses
- Wraps with `asyncio.wait_for(..., timeout=config.command_timeout_s)` to enforce OPEN-EMS NFR independently
- Uses `_call_lock` internally — already serializes concurrent calls per charger instance

**`@on(Action.X)` decorator**
- Register handlers before `start()` is called (at class definition time, via class body)
- Handler receives snake_case kwargs matching the OCPP message fields
- Handler returns the appropriate `call_result.*` dataclass instance

**Dynamic command dispatch:**
```python
call_cls = getattr(ocpp_call, command.command_name, None)
# call_cls will be e.g. call.ChangeAvailability for command_name="ChangeAvailability"
if call_cls is None or not dataclasses.is_dataclass(call_cls):
    return _command_error(command, "unknown_action")
try:
    call_payload = call_cls(**command.payload)  # payload is dict of snake_case kwargs
except TypeError:
    return _command_error(command, "invalid_payload")
```

**`asyncio.CancelledError` is `BaseException`, NOT `Exception`** — catch `Exception` only; `CancelledError` propagates naturally for cooperative task cancellation. Same rule as Story 3.2.

### Suggested Implementation Shape

```python
class _ChargerState:
    """Mutable state updated by ocpp @on handlers — NOT a Pydantic model."""
    def __init__(self) -> None:
        self.connected: bool = False
        self.last_status_notification: dict[str, Any] | None = None
        self.last_heartbeat_at: datetime | None = None
        self.last_call_result: dict[str, Any] | None = None
        self.last_call_error: dict[str, Any] | None = None

class _InternalChargePoint(_BaseChargePoint):
    def __init__(self, id: str, connection: Any, state: _ChargerState, response_timeout: float) -> None:
        super().__init__(id, connection, response_timeout=response_timeout)
        self._state = state

    @on(Action.BootNotification)
    def on_boot_notification(self, charge_point_model: str, charge_point_vendor: str, **kwargs: Any) -> call_result.BootNotification:
        self._state.last_status_notification = {"type": "BootNotification", ...}
        return call_result.BootNotification(
            current_time=datetime.now(UTC).isoformat(),
            interval=10,
            status=RegistrationStatus.accepted,
        )
    ...

class OCPPChargerAdapter:
    def __init__(self, config: OCPPAdapterConfig) -> None:
        self.config = config
        self._state = _ChargerState()
        self._handler: _InternalChargePoint | None = None

    async def handle_connection(self, connection: Any) -> None:
        """Manage OCPP message loop. Blocks until charger disconnects. Re-callable for reconnect."""
        state = _ChargerState()
        state.connected = True
        handler = _InternalChargePoint(
            self.config.charge_point_id, connection, state,
            response_timeout=self.config.command_timeout_s + 1.0,
        )
        self._state = state
        self._handler = handler
        try:
            await handler.start()
        except Exception:
            pass
        finally:
            state.connected = False
            self._handler = None
            logger.warning("adapter_disconnected", component="ocpp",
                           device_id=self.config.device_id,
                           charge_point_id=self.config.charge_point_id)

    async def get_raw_state(self) -> RawOCPPState | ProtocolDegradedState: ...
    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult: ...
```

The `OCPPCentralSystem.handle_charger()` blocks while the charger is connected — this is the correct pattern for FastAPI WebSocket routes where the handler must not return until the socket closes.

### FastAPI WebSocket Adapter (for production wiring — NOT needed for tests)

The `ocpp` library expects `send(str)` and `recv()` methods. FastAPI's `WebSocket` uses `send_text()` / `receive_text()`. A small adapter is needed when wiring the FastAPI route:

```python
class _FastAPIWebSocketAdapter:
    def __init__(self, websocket: Any) -> None:  # fastapi.WebSocket
        self._ws = websocket
    async def send(self, message: str) -> None:
        await self._ws.send_text(message)
    async def recv(self) -> str:
        return await self._ws.receive_text()
```

This adapter belongs in the web layer (not this package) since importing `fastapi.WebSocket` here would create an unwanted dependency. The FastAPI route would do:
```python
await central_system.handle_charger(charge_point_id, _FastAPIWebSocketAdapter(websocket))
```

**Do NOT import or use FastAPI in `central_system.py`.**

### Fake WebSocket Pattern for Tests

Create a dual-queue fake that simulates both sides of a WebSocket connection:

```python
class _FakeWebSocket:
    """Dual-queue fake WebSocket for deterministic OCPP unit tests."""
    def __init__(self) -> None:
        self._from_client: asyncio.Queue[str | None] = asyncio.Queue()
        self._to_client: asyncio.Queue[str] = asyncio.Queue()

    # Adapter (server) side — ocpp library calls these
    async def recv(self) -> str:
        msg = await self._from_client.get()
        if msg is None:
            raise ConnectionError("charger_disconnected")  # sentinel
        return msg

    async def send(self, message: str) -> None:
        await self._to_client.put(message)

    # Charger (test) side — test helpers for simulating the charger
    async def client_send(self, message: str) -> None:
        await self._from_client.put(message)

    async def client_recv(self) -> str:
        return await self._to_client.get()

    def close(self) -> None:
        self._from_client.put_nowait(None)  # triggers ConnectionError in recv()
```

**Test pattern — run message loop as background task:**
```python
async def test_boot_notification_updates_state() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    
    loop_task = asyncio.create_task(adapter.handle_connection(ws))
    
    # Simulate charger sending BootNotification (camelCase per OCPP spec)
    await ws.client_send(json.dumps([2, "msg-1", "BootNotification", {
        "chargePointVendor": "Wallbox", "chargePointModel": "Pulsar Plus",
    }]))
    # Receiving CALLRESULT confirms state has been updated
    response = json.loads(await asyncio.wait_for(ws.client_recv(), timeout=1.0))
    assert response[0] == 3  # MessageType.CallResult
    assert response[2]["status"] == "Accepted"
    
    state = await adapter.get_raw_state()
    assert isinstance(state, RawOCPPState)
    assert state.connection_status == "connected"
    assert state.last_status_notification["type"] == "BootNotification"
    
    ws.close()
    await asyncio.wait_for(loop_task, timeout=1.0)
```

**Test pattern — dispatching a command (concurrent tasks needed):**
```python
async def test_send_raw_command_acked_on_callresult() -> None:
    ws = _FakeWebSocket()
    adapter = OCPPChargerAdapter(make_config())
    loop_task = asyncio.create_task(adapter.handle_connection(ws))
    
    # Connect the charger first
    await ws.client_send(json.dumps([2, "b1", "BootNotification",
        {"chargePointVendor": "Test", "chargePointModel": "Test"}]))
    await asyncio.wait_for(ws.client_recv(), timeout=1.0)
    
    command = RawProtocolCommand(
        correlation_id="corr-1", device_id="ev-1",
        command_name="ChangeAvailability",
        payload={"connector_id": 0, "type": "Operative"},
    )
    
    async def charger_side() -> None:
        # Wait for the CALL the adapter sends
        msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
        call_msg = json.loads(msg_str)
        unique_id = call_msg[1]
        # Respond with CALLRESULT
        await ws.client_send(json.dumps([3, unique_id, {"status": "Accepted"}]))
    
    charger_task = asyncio.create_task(charger_side())
    result = await asyncio.wait_for(adapter.send_raw_command(command), timeout=2.0)
    await asyncio.wait_for(charger_task, timeout=1.0)
    
    assert result.protocol_status == "acked"
    assert result.raw_response == {"status": "Accepted"}
    
    ws.close()
    await asyncio.wait_for(loop_task, timeout=1.0)
```

### Command Payload Contract

`command.command_name` = OCPP action class name (PascalCase, matches `ocpp.v16.call.*`):
- Valid: `"ChangeAvailability"`, `"RemoteStartTransaction"`, `"Reset"`, `"UnlockConnector"`, etc.
- `command.payload` = dict of snake_case kwargs matching the dataclass fields

Example:
```python
RawProtocolCommand(
    correlation_id="corr-1", device_id="ev-1",
    command_name="ChangeAvailability",
    payload={"connector_id": 0, "type": "Operative"},  # snake_case!
)
```

The `ocpp` library converts snake_case fields to camelCase in the JSON message internally. Do NOT pass camelCase dict keys.

Return `protocol_status="error"` for: unknown action (class not in `ocpp.v16.call`), invalid payload (TypeError from dataclass constructor), not connected, bytes payload.

### Previous Story Intelligence

- Story 3.2 review found that permissive coercion and missing field-specific ValidationError assertions were risky. Use `extra="forbid"` on config models and test that `ValidationError.errors()` targets the specific field. [Source: 3-2 Review Findings]
- Story 3.2 established frozen Pydantic config with `extra="forbid"`. Use the same for `OCPPAdapterConfig`. [Source: src/open_ems/adapters/modbus/tcp.py:60-73]
- Story 3.2 used a `_float_fields_must_be_numeric` field validator to prevent bool/string coercion of float fields. Apply the same pattern to `command_timeout_s` and `heartbeat_timeout_s`. [Source: src/open_ems/adapters/modbus/tcp.py:74-79]
- Epic 2 retrospective called out these specific Epic 3 safeguards: fake transport fixtures, timeout tests, malformed payload, disconnect/reconnect, no blocking I/O. All are required here. [Source: _bmad-output/implementation-artifacts/3-2-implement-modbus-tcp-adapter-with-connection-management-and-timeout-enforcement.md#Previous-Story-Intelligence]
- Story 3.2 deferred `asyncio.CancelledError` propagation as correct behavior. Same applies here — do not catch `CancelledError`. [Source: _bmad-output/implementation-artifacts/deferred-work.md]
- Story 3.1 established UTC timestamp validation via `_require_utc()`. Reuse `datetime.now(UTC)` everywhere; assert `occurred_at.utcoffset().total_seconds() == 0` in degraded-state tests. [Source: src/open_ems/adapters/protocol.py:30-38]

### Anti-Patterns To Reject

- Importing `fastapi.WebSocket` or any FastAPI type in `central_system.py` — the ocpp library accepts duck-typed connections.
- Defining `EVChargerState`, `ChargerState`, `session_active`, `current_power_kw`, or any domain-normalized type — those belong to Epic 4.
- Sharing one `_InternalChargePoint` instance across multiple charger connections — each connection gets its own instance with fresh `_ChargerState`.
- Catching `asyncio.CancelledError` in `handle_connection()` or `send_raw_command()` — it is a `BaseException`, not `Exception`; let it propagate for cooperative task cancellation.
- Using `suppress=True` (the default) in `handler.call()` — this silently eats CALLERROR and returns `None`; use `suppress=False` and catch `Exception` to distinguish CALLERROR from programming bugs.
- Calling `dataclasses.asdict()` directly on the CALLRESULT object when the result may be `None` — check `result is not None` first (though with `suppress=False`, a `None` result only occurs on malformed CALLRESULT, which would raise before reaching `asdict`).
- Running `start()` synchronously or blocking the event loop — `handle_connection()` is `async` and must `await handler.start()` in a `create_task`-based loop or directly (FastAPI WebSocket handler blocks naturally).
- Writing installer-facing audit/event-log rows from this raw protocol layer.
- Confusing `device_id` (OPEN-EMS identity, config field) with `charge_point_id` (OCPP identity) — both are stored separately in `OCPPAdapterConfig`.

### References

- Story requirements: [Source: _bmad-output/planning-artifacts/epics.md#Story-3.3]
- Epic 3 constraints: [Source: _bmad-output/planning-artifacts/epics.md#Cross-story-constraints-applying-to-all-Epic-3-stories]
- OCPP WebSocket integration pattern: [Source: _bmad-output/planning-artifacts/architecture.md#Decision-3.2]
- Project structure for OCPP adapter: [Source: _bmad-output/planning-artifacts/architecture.md#Project-Structure lines 841-847]
- Existing adapter contract (do not modify): [Source: src/open_ems/adapters/protocol.py]
- Modbus adapter as implementation reference: [Source: src/open_ems/adapters/modbus/tcp.py]
- Modbus test patterns as test reference: [Source: tests/unit/adapters/modbus/test_tcp_adapter.py]
- Current dependencies and quality gates: [Source: pyproject.toml]

## Dev Agent Record

### Senior Developer Review (AI)

**Review Outcome:** Changes Requested
**Review Date:** 2026-05-02
**Action Items:** 4 total — 2 Decision-Needed (High), 2 Patch (Med/Low), 1 Deferred

#### Action Items

- [ ] [High] Heartbeat staleness should emit `adapter_disconnected` log — AC3 violation in spirit (decision needed on scope)
- [ ] [High] `except Exception: pass` swallows programming errors — violates AC5 anti-pattern rule from Dev Notes
- [ ] [Med] `last_call_error` not cleared after successful subsequent command — misleading state for consumers
- [ ] [Low] `on_heartbeat` captures `datetime.now(UTC)` twice — inconsistent timestamps
- [x] [Low] `register()` silently overwrites adapters — deferred as pre-existing design choice

### Agent Model Used

claude-sonnet-4-6

### Debug Log References

### Completion Notes List

Ultimate context engine analysis completed - comprehensive developer guide created.

**Story 3.3 implementation complete (2026-05-03):**
- Implemented `OCPPAdapterConfig` (frozen Pydantic, `extra="forbid"`, numeric validator on float fields)
- Implemented `_ChargerState` (plain mutable class) + `_InternalChargePoint` (ocpp ChargePoint subclass) with BootNotification/Heartbeat/StatusNotification handlers using `Action.boot_notification` / `Action.heartbeat` / `Action.status_notification` snake_case enum values
- Implemented `OCPPChargerAdapter` with `handle_connection`, `get_raw_state`, `send_raw_command`, `_degraded` — full degraded/connected/stale logic; CALLERROR stored in `last_call_error`, CALLRESULT in `last_call_result`
- Implemented `OCPPCentralSystem` with `register`, `get_adapter`, `handle_charger` (auto-creates adapter for unregistered chargers)
- Added structlog events: `adapter_disconnected`, `adapter_timeout`, `adapter_protocol_error`
- 34 unit tests across all ACs using `_FakeWebSocket` dual-queue approach; all pass
- Final: 254 tests passed, 87.25% coverage, ruff clean, mypy clean

### File List

- `src/open_ems/adapters/ocpp/__init__.py`
- `src/open_ems/adapters/ocpp/central_system.py`
- `tests/unit/adapters/ocpp/__init__.py`
- `tests/unit/adapters/ocpp/test_ocpp_adapter.py`
- `_bmad-output/implementation-artifacts/3-3-implement-ocpp-16-central-system-adapter.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`

## Change Log

- Story 3.3 OCPP 1.6 Central System adapter story file created. (Date: 2026-05-02)
- Story 3.3 implementation complete: OCPPAdapterConfig, OCPPChargerAdapter, OCPPCentralSystem, 34 unit tests, 87.25% coverage, all quality gates green. (Date: 2026-05-03)
