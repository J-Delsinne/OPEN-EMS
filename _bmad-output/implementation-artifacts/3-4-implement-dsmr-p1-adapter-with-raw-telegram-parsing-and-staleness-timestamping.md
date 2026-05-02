# Story 3.4: Implement DSMR P1 adapter with raw telegram parsing and staleness timestamping

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want a DSMR P1 adapter that continuously parses smart meter telegrams and timestamps each reading,
so that the system has access to raw grid meter data and can detect when that data has gone stale.

## Acceptance Criteria

**AC1 - Serial/USB path: continuous telegram reading**
**Given** a DSMR P1 smart meter is connected via serial/USB (primary v1 path)
**When** the adapter's background reader task is running
**Then** it reads lines continuously, buffers them in `TelegramBuffer`, and invokes `TelegramParser.parse()` on each complete telegram
**And** on each successful parse, the adapter updates its internal `_last_telegram` and `_received_at` atomically (under `asyncio.Lock`)
**And** the reading loop does not propagate exceptions from parse failures — `ParseError` and `InvalidChecksumError` are caught, logged, and the loop continues

**AC2 - TCP path: same parsing pipeline**
**Given** a TCP-connected P1 reader (e.g., Wi-Fi P1 dongle) is configured
**When** the adapter is configured for TCP mode (`host`/`port` set; `serial_port` is `None`)
**Then** it reads data from the TCP stream using `asyncio.open_connection`, the same `TelegramParser`/`TelegramBuffer` pipeline, and the same `ProtocolAdapter` interface as the serial path
**And** the calling code does not need to know which transport is active

**AC3 - `get_raw_state()` returns `RawDSMRState`**
**Given** `get_raw_state()` is called and a valid telegram has been received
**When** the call executes
**Then** it returns `RawDSMRState(device_id=..., telegram_fields=..., received_at=...)` where:
- `telegram_fields` is a `dict[str, Any]` built from the parsed `Telegram` object's `__iter__` items — OBIS key → `{"value": ..., "unit": ...}` for each defined field
- `received_at` is the timezone-aware UTC `datetime` captured at parse time
**And** no normalization to `GridMeterState`, energy-sign conventions, or unit conversions is applied — raw DSMR field values as parsed

**AC4 - Staleness detection returns `ProtocolDegradedState`**
**Given** `get_raw_state()` is called and the `received_at` of the last telegram is more than `stale_after_s` seconds ago (default: 60 seconds)
**When** the staleness check runs
**Then** it returns `ProtocolDegradedState(device_id=..., reason="dsmr_stale", occurred_at=datetime.now(UTC))`
**And** a structlog warning is written: `event="adapter_stale"`, `component="dsmr"`, `device_id=...`, `seconds_since_last_telegram=...`
**And** the log is emitted once per staleness transition (not on every `get_raw_state()` call)

**AC5 - No telegram yet / transport unavailable returns `ProtocolDegradedState`**
**Given** `get_raw_state()` is called before any telegram has been received
**When** the call executes
**Then** it returns `ProtocolDegradedState(device_id=..., reason="dsmr_unavailable", occurred_at=datetime.now(UTC))`

**Given** the serial port or TCP connection becomes unavailable (read failure, OSError, EOF)
**When** a read attempt fails
**Then** the adapter returns `ProtocolDegradedState(device_id=..., reason="dsmr_unavailable", occurred_at=...)` from `get_raw_state()`
**And** the adapter attempts to reopen the port or reconnect on the next read cycle (retry loop with configurable `reconnect_delay_s`)
**And** a structlog warning is written: `event="adapter_connection_failed"`, `component="dsmr"`, `device_id=...`, `reason=...`
**And** no exception propagates from `get_raw_state()` or `send_raw_command()`

**AC6 - `send_raw_command()` always returns error**
**Given** `send_raw_command()` is called on the DSMR adapter
**When** the call executes
**Then** it returns `ProtocolCommandResult(correlation_id=..., device_id=..., protocol_status="error", raw_response={"error": "dsmr_read_only"})`
**And** no exception propagates (DSMR P1 is a read-only protocol)

**AC7 - Lifecycle: `start()` and `stop()`**
**Given** the adapter is instantiated
**When** `await adapter.start()` is called
**Then** a background `asyncio.Task` is created and begins the read loop
**And** calling `start()` again on an already-running adapter is a no-op (idempotent)

**When** `await adapter.stop()` is called
**Then** the background task is cancelled and awaited; the transport is closed
**And** subsequent calls to `get_raw_state()` return `ProtocolDegradedState(reason="dsmr_unavailable")`

**AC8 - Tests**
**And** tests use a fake transport (asyncio in-memory stream or queue) — no real serial port or network
**And** tests cover: successful parse → `RawDSMRState` with correct `telegram_fields` and UTC `received_at`, staleness detection (mock `datetime.now` past `stale_after_s`), `dsmr_unavailable` before first telegram, serial disconnect/reconnect cycle, malformed telegram (parse error → loop continues, no crash), `send_raw_command` always returns error, `ProtocolAdapter` structural satisfaction
**And** timeout/staleness tests verify the 60-second threshold triggers `ProtocolDegradedState` correctly
**And** all async tests use `pytest-asyncio` with `asyncio_mode = "auto"` (already configured in `pyproject.toml`)

## Tasks / Subtasks

- [x] **Task 0: Pre-story quality gate** (AC: all)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/`

- [x] **Task 1: Create DSMR adapter package and config model** (AC: AC1, AC2, AC5)
  - [x] Create `src/open_ems/adapters/dsmr/__init__.py`
  - [x] Create `src/open_ems/adapters/dsmr/p1.py`
  - [x] Define frozen Pydantic `DSMRAdapterConfig` with fields:
    - `device_id: NonEmptyStr`
    - `serial_port: str | None = None` (e.g. `"/dev/ttyUSB0"`)
    - `tcp_host: str | None = None`
    - `tcp_port: int | None = None` (1 ≤ port ≤ 65535 if set)
    - `dsmr_version: Literal["2.2", "4", "4+", "5", "5B"] = "5"` — selects `telegram_specifications.*`
    - `stale_after_s: float = Field(default=60.0, gt=0)` — staleness threshold
    - `reconnect_delay_s: float = Field(default=5.0, ge=0)` — delay between reconnect attempts
  - [x] Add `model_validator` ensuring exactly one of `serial_port` or (`tcp_host` + `tcp_port`) is set; raise `ValidationError` if neither or both
  - [x] Apply `_float_fields_must_be_numeric` field_validator pattern (identical to modbus and OCPP adapters — `isinstance(value, bool) or not isinstance(value, int | float)`)
  - [x] Keep config independent of FastAPI — no FastAPI imports in this package

- [x] **Task 2: Define transport abstraction for testability** (AC: AC1, AC2, AC8)
  - [x] Define `_DSMRTransport(Protocol)` with:
    - `async def read_line(self) -> bytes` — reads one line from the source; raises `OSError` on disconnect/EOF
    - `async def close(self) -> None`
  - [x] Implement `_SerialTransport` wrapping `serial_asyncio_fast.open_serial_connection` (using `dsmr_parser.clients.settings.SERIAL_SETTINGS_V5` or spec-matched settings as base, with `url=config.serial_port`)
  - [x] Implement `_TcpTransport` wrapping `asyncio.open_connection(host, port)` with `asyncio.StreamReader.readline()`
  - [x] Both concrete transports raise `OSError` on connect/read failure to trigger the reconnect loop

- [x] **Task 3: Implement reading loop with `TelegramParser` + `TelegramBuffer`** (AC: AC1, AC2, AC4, AC5)
  - [x] Define `_TelegramSource(Protocol)` factory: `async def open(self) -> _DSMRTransport` — allows test injection
  - [x] Implement `async _read_loop(self) -> None`:
    - Outer loop: attempt to open transport; catch `OSError` → log `adapter_connection_failed`, set `self._connected = False`, sleep `reconnect_delay_s`, retry
    - Inner loop: `line = await transport.read_line()` → decode ASCII → `self._buffer.append(decoded)` → for each complete telegram in `self._buffer.get_all()` → parse and update state
    - Catch `ParseError` / `InvalidChecksumError` per telegram: log `event="adapter_parse_error"`, `component="dsmr"`, continue loop
    - Catch `OSError` on `read_line()`: log `adapter_connection_failed`, `self._connected = False`, break inner loop to trigger reconnect
    - Let `asyncio.CancelledError` propagate (stop signal)
  - [x] Import: `from dsmr_parser.parsers import TelegramParser`, `from dsmr_parser.clients.telegram_buffer import TelegramBuffer`, `from dsmr_parser import telegram_specifications`, `from dsmr_parser.exceptions import ParseError, InvalidChecksumError`
  - [x] Telegram spec selection: `{"2.2": telegram_specifications.V2_2, "4": telegram_specifications.V4, "4+": telegram_specifications.V5, "5": telegram_specifications.V5, "5B": telegram_specifications.BELGIUM_FLUVIUS}[config.dsmr_version]`

- [x] **Task 4: Implement `DSMRAdapter`** (AC: AC3–AC7)
  - [x] `__init__(self, config: DSMRAdapterConfig, *, transport_factory: _TelegramSource | None = None)`:
    - Store config, init `TelegramParser(spec, apply_checksum_validation=True)`, `TelegramBuffer()`
    - `self._lock = asyncio.Lock()`
    - `self._last_telegram: dict[str, Any] | None = None`
    - `self._received_at: datetime | None = None`
    - `self._connected: bool = False`
    - `self._stale_logged: bool = False` — for one-shot staleness log
    - `self._task: asyncio.Task[None] | None = None`
    - `self._transport_factory = transport_factory or <default factory from config>`
  - [x] `async def start(self) -> None`: if `self._task` is None or done, create `asyncio.create_task(self._read_loop())`; idempotent
  - [x] `async def stop(self) -> None`: cancel and await task; call `transport.close()` if open; reset state
  - [x] `async def get_raw_state(self) -> RawDSMRState | ProtocolDegradedState`:
    - Acquire `_lock`
    - If `_received_at` is None: return `_degraded("dsmr_unavailable")`
    - If `(datetime.now(UTC) - _received_at).total_seconds() > stale_after_s`: emit one-shot staleness log, return `_degraded("dsmr_stale")`
    - Otherwise: reset `_stale_logged = False`, return `RawDSMRState(device_id=..., telegram_fields=self._last_telegram, received_at=self._received_at)`
  - [x] `async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult`:
    - Return `ProtocolCommandResult(correlation_id=..., device_id=..., protocol_status="error", raw_response={"error": "dsmr_read_only"})`
  - [x] `def _degraded(self, reason: str) -> ProtocolDegradedState` — helper with `datetime.now(UTC)`
  - [x] `def _telegram_to_dict(self, telegram: object) -> dict[str, Any]`:
    - Iterate `telegram` via `for obis_ref, cosem_obj in telegram`: build `{str(obis_ref): {"value": cosem_obj.value, "unit": cosem_obj.unit}}` for each field
    - Use `try/except Exception` defensively per field (CosemObject may have no `unit` attribute on some versions)

- [x] **Task 5: Update `src/open_ems/adapters/dsmr/__init__.py`** (AC: all)
  - [x] Export `DSMRAdapter`, `DSMRAdapterConfig`
  - [x] Do NOT re-export `RawDSMRState` — already exported from `open_ems.adapters`

- [x] **Task 6: Add logging** (AC: AC4, AC5)
  - [x] `structlog.get_logger(__name__)` in `p1.py`
  - [x] `event="adapter_stale"`, `component="dsmr"`, `device_id=...`, `seconds_since_last_telegram=...` (one-shot on transition)
  - [x] `event="adapter_connection_failed"`, `component="dsmr"`, `device_id=...`, `reason=...`
  - [x] `event="adapter_parse_error"`, `component="dsmr"`, `device_id=...`, `reason=...`
  - [x] Do NOT write installer-facing `event_log` rows — Epic 3 logs to stdout only

- [x] **Task 7: Add deterministic tests** (AC: AC1–AC8)
  - [x] Create `tests/unit/adapters/dsmr/__init__.py`
  - [x] Create `tests/unit/adapters/dsmr/test_dsmr_adapter.py`
  - [x] Use a fake `_DSMRTransport` backed by `asyncio.Queue[bytes]` — push telegram lines; raise `OSError` to simulate disconnect
  - [x] Test coverage:
    - Config validation (no transport set → `ValidationError`, both serial+tcp → `ValidationError`, valid serial, valid tcp)
    - `get_raw_state()` before first telegram → `ProtocolDegradedState(reason="dsmr_unavailable")`
    - Successful parse → `RawDSMRState` with correct `received_at` (UTC-aware) and `telegram_fields` dict
    - Staleness detection: mock `received_at` to `datetime.now(UTC) - timedelta(seconds=61)` and call `get_raw_state()` → `ProtocolDegradedState(reason="dsmr_stale")` with structlog warning
    - Staleness log is one-shot (second call doesn't repeat log)
    - After recovery (new telegram), staleness clears
    - Serial disconnect → `ProtocolDegradedState(reason="dsmr_unavailable")` from `get_raw_state()`
    - Reconnect: fake transport raises `OSError` then succeeds → adapter recovers
    - Malformed telegram (inject bad data) → loop continues, no crash
    - `send_raw_command()` always returns `protocol_status="error"` with `raw_response={"error": "dsmr_read_only"}`
    - `start()` is idempotent (calling twice doesn't create two tasks)
    - `stop()` cleans up and subsequent `get_raw_state()` returns `dsmr_unavailable`
    - `ProtocolAdapter` structural satisfaction: `assert isinstance(adapter, ProtocolAdapter)`

- [x] **Task 8: Final validation** (AC: all)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/` — must pass with coverage ≥ 75%

## Dev Notes

### Current Codebase State

- `src/open_ems/adapters/protocol.py` defines `RawDSMRState` (already complete — do NOT redefine). Its fields: `device_id: NonEmptyStr`, `telegram_fields: dict[str, Any]`, `received_at: datetime` (UTC-validated). [Source: src/open_ems/adapters/protocol.py:121–131]
- `src/open_ems/adapters/__init__.py` already exports `RawDSMRState`, `ProtocolAdapter`, `ProtocolCommandResult`, `ProtocolDegradedState`, `RawProtocolCommand`. Add DSMR exports to `src/open_ems/adapters/dsmr/__init__.py`, NOT to the top-level `__init__.py`. [Source: src/open_ems/adapters/__init__.py]
- `dsmr-parser>=1.6.0` is already in `pyproject.toml` — do not add another DSMR library. `dsmr_parser.*` is already in `mypy` `ignore_missing_imports` overrides. [Source: pyproject.toml]
- Pattern to mirror: `src/open_ems/adapters/modbus/tcp.py` (frozen config, structlog, asyncio.wait_for, typed degraded state) and `src/open_ems/adapters/ocpp/central_system.py` (background state, async lifecycle, lock-guarded state update). [Source: src/open_ems/adapters/modbus/tcp.py, src/open_ems/adapters/ocpp/central_system.py]
- Current quality gates: Python `>=3.12`, strict mypy, ruff line-length 100, `pytest-asyncio asyncio_mode = "auto"`, coverage fail-under 75. [Source: pyproject.toml]

### Architecture Guardrails

- Epic 3 is raw protocol only. `GridMeterState`, energy-sign conventions, import/export normalization belong to Epic 4. Return raw OBIS fields only. [Source: _bmad-output/planning-artifacts/epics.md#Epic-3-Protocol-Adapters]
- Runtime communication failures must NOT escape `get_raw_state()` or `send_raw_command()`. Startup config errors (`ValidationError`) may propagate. Programming bugs (`AssertionError`, `AttributeError`) must NOT be swallowed — let `asyncio.CancelledError` propagate in the read loop. [Source: _bmad-output/planning-artifacts/epics.md#Cross-story-constraints]
- All timestamps must be timezone-aware UTC: `datetime.now(UTC)`. Never `datetime.utcnow()`. [Source: _bmad-output/planning-artifacts/architecture.md#Naming-Conventions]
- File location: `src/open_ems/adapters/dsmr/p1.py` and `tests/unit/adapters/dsmr/test_dsmr_adapter.py`. Tests mirror source structure. [Source: _bmad-output/planning-artifacts/architecture.md#Project-Structure-Patterns]
- Epic 3 uses structlog to stdout only. No installer-facing `event_log` entries at this layer.
- `NFR-I3`: DSMR data timestamp recorded at parse time for staleness evaluation consumed by Epic 4. This is the core contract. [Source: _bmad-output/planning-artifacts/epics.md#Epic-3-scope]

### dsmr-parser 1.6.0 API (authoritative — read from installed package)

**Key imports:**
```python
from dsmr_parser.parsers import TelegramParser
from dsmr_parser.clients.telegram_buffer import TelegramBuffer
from dsmr_parser import telegram_specifications
from dsmr_parser.exceptions import ParseError, InvalidChecksumError
from dsmr_parser.clients.settings import SERIAL_SETTINGS_V5  # for serial port settings
```

**`TelegramParser(telegram_specification, apply_checksum_validation=True)`**
- `.parse(telegram_data: str) -> Telegram` — raises `ParseError` or `InvalidChecksumError`
- `telegram_data` is the full raw telegram string from `/` to `!XXXX\r\n`

**`TelegramBuffer()`**
- `.append(data: str)` — feed decoded ASCII lines/chunks
- `.get_all()` — generator yielding complete telegram strings, removing them from buffer
- Telegram detection regex: `/[^\/]+?\![A-F0-9]{0,4}\0?\r\n` (handles optional CRC)

**`Telegram` object (from `parse()`):**
- Iterable: `for obis_ref, cosem_obj in telegram` — yields `(str_obis_ref, CosemObject)` pairs
- Each `CosemObject` has `.value` and `.unit` attributes
- Build `telegram_fields` dict: `{str(obis_ref): {"value": obj.value, "unit": obj.unit}}`

**Telegram specifications:**
```python
telegram_specifications.V2_2    # DSMR 2.2
telegram_specifications.V4      # DSMR 4
telegram_specifications.V5      # DSMR 4+ and 5 (most common Netherlands)
telegram_specifications.BELGIUM_FLUVIUS  # Belgium "5B"
```

**Async serial via `serial_asyncio_fast` (transitive dep of dsmr-parser):**
```python
import serial_asyncio_fast
reader, writer = await serial_asyncio_fast.open_serial_connection(
    url="/dev/ttyUSB0", baudrate=115200, bytesize=8, parity="N", stopbits=1
)
line = await reader.readline()  # bytes
```
- Use `SERIAL_SETTINGS_V5` as the base dict for baud/parity/stopbits — add `url=config.serial_port`
- `serial_asyncio_fast` is a transitive dependency of dsmr-parser, not a direct dep; do not add it to `pyproject.toml`

**TCP (no async TCP reader in dsmr-parser — implement directly):**
```python
reader, writer = await asyncio.open_connection(host, port)
line = await reader.readline()  # bytes; returns b"" on EOF → raise OSError
```

**⚠️ Windows / CI note:** `dsmr_parser.clients.serial_` and `.clients.protocol` import `serial_asyncio_fast` at module level and trigger `ZoneInfoNotFoundError` on Python 3.14 without `tzdata`. **Do NOT import from `dsmr_parser.clients.*`** — import only from `dsmr_parser.parsers`, `dsmr_parser.clients.telegram_buffer`, `dsmr_parser.telegram_specifications`, and `dsmr_parser.exceptions`. The transport layer is implemented directly using `serial_asyncio_fast` and `asyncio.open_connection`.

### Previous Story Intelligence (Story 3.3)

- **Mutable state under `asyncio.Lock`**: OCPP used `_ChargerState` (plain class) updated in async handlers. DSMR uses the same principle — `_last_telegram` and `_received_at` must be updated under `self._lock` in the background task and read under the same lock in `get_raw_state()`.
- **`handle_connection` finally guard**: Story 3.3 patch — `finally` block must be guarded to avoid cancelling the wrong task when reconnecting. Apply same discipline to DSMR's `_read_loop`.
- **`asyncio.CancelledError` must propagate**: Let it bubble in `_read_loop` — it is the stop signal. Never catch `CancelledError` in a bare `except Exception`.
- **Config validator pattern**: `isinstance(value, bool) or not isinstance(value, int | float)` → raise `ValueError`. Mirror exactly.
- **One-shot log**: `_stale_logged` flag pattern (emit warning only on first stale call, reset on recovery) to avoid log flooding — this is your responsibility; it is NOT in the epic spec but is a quality requirement.
- **Structlog event names**: `adapter_timeout`, `adapter_connection_failed`, `adapter_disconnected`, `adapter_protocol_error` — DSMR uses `adapter_stale` and `adapter_parse_error` as new events; follow the same snake_case naming.

### Project Structure Notes

New files to create:
```
src/open_ems/adapters/dsmr/__init__.py     (NEW)
src/open_ems/adapters/dsmr/p1.py           (NEW)
tests/unit/adapters/dsmr/__init__.py       (NEW)
tests/unit/adapters/dsmr/test_dsmr_adapter.py  (NEW)
```

Do NOT create:
- `src/open_ems/adapters/dsmr/normalizer.py` — belongs to Epic 4
- `src/open_ems/adapters/dsmr/device_adapter.py` — belongs to Epic 4
- Any `profiles/` directory — belongs to Epic 4

### References

- [Source: src/open_ems/adapters/protocol.py#RawDSMRState] — `RawDSMRState` already defined, do not redefine
- [Source: src/open_ems/adapters/modbus/tcp.py] — frozen config, degraded helper, structlog pattern
- [Source: src/open_ems/adapters/ocpp/central_system.py] — async lifecycle, lock-guarded state, background task
- [Source: _bmad-output/planning-artifacts/epics.md#Story-3.4] — acceptance criteria source
- [Source: _bmad-output/planning-artifacts/architecture.md#Project-Structure-Patterns] — file locations
- [Source: .venv/Lib/site-packages/dsmr_parser/] — authoritative library API (v1.6.0)

## Dev Agent Record

### Agent Model Used

Claude Sonnet 4.6 (claude-sonnet-4.6)

### Debug Log References

- `ZoneInfoNotFoundError` on Python 3.14/Windows: `dsmr_parser.value_types` calls `ZoneInfo("Europe/Amsterdam")` at module level. Fixed by adding `tzdata>=2026.2` as a project dependency via `uv add tzdata`.
- `Telegram.__iter__` yields `(attr_name_str, cosem_obj)` pairs where attr_name is e.g. `"ELECTRICITY_USED_TARIFF_1"` (not OBIS regex). `str(obis_ref)` used as dict key.
- CRC for V5 test telegram computed as `0x6039` using `TelegramParser.crc16()`. V2.2 spec has no checksum (`checksum_support=False`) — used in bad-checksum test.

### Completion Notes List

- ✅ Implemented `DSMRAdapterConfig` (frozen Pydantic, transport mutex validator, float validator)
- ✅ Implemented `_DSMRTransport` / `_TelegramSource` Protocol abstractions for testability
- ✅ Implemented `_SerialTransport` and `_TcpTransport` concrete transports
- ✅ Implemented `_read_loop` with outer reconnect loop, inner read loop, TelegramBuffer/TelegramParser pipeline
- ✅ Implemented `DSMRAdapter` with `start()`, `stop()`, `get_raw_state()`, `send_raw_command()`, `_degraded()`, `_telegram_to_dict()`
- ✅ One-shot staleness logging via `_stale_logged` flag (set on first stale, reset on fresh telegram)
- ✅ 29 deterministic tests using `FakeTransport` backed by `asyncio.Queue[bytes]`
- ✅ All 284 tests pass with 86.70% coverage (≥75% required)
- ✅ ruff check, ruff format, mypy all pass
- ✅ Added `tzdata>=2026.2` as direct dependency for Python 3.14/Windows compatibility

### File List

- `src/open_ems/adapters/dsmr/__init__.py` (NEW)
- `src/open_ems/adapters/dsmr/p1.py` (NEW)
- `tests/unit/adapters/dsmr/__init__.py` (NEW)
- `tests/unit/adapters/dsmr/test_dsmr_adapter.py` (NEW)
- `pyproject.toml` (MODIFIED — added `tzdata>=2026.2` dependency)
- `uv.lock` (MODIFIED — updated by uv automatically)

### Review Findings

- [x] [Review][Patch] `_received_at` not reset on disconnect — AC5 violated: after a valid telegram, transport loss never produces `dsmr_unavailable` [p1.py: `except OSError` block ~line 299]
- [x] [Review][Patch] `TelegramBuffer` not cleared on reconnect — stale partial telegram fragment from prior session can corrupt first telegram of new session [p1.py: `__init__` / `_read_loop` reconnect path]
- [x] [Review][Patch] `StreamWriter.close()` without `await writer.wait_closed()` — FD leak on reconnect storms in both `_SerialTransport.close()` and `_TcpTransport.close()` [p1.py:114, 129]
- [x] [Review][Patch] TCP connect has no timeout — `asyncio.open_connection()` hangs indefinitely on silent packet drops (no OS-level timeout set) [p1.py:154-158]
- [x] [Review][Patch] Non-`ParseError`/`InvalidChecksumError` exceptions from `TelegramParser.parse()` kill `_read_loop` permanently — not caught by inner `except (ParseError, InvalidChecksumError)` nor by outer `except OSError` [p1.py:286-292]
- [x] [Review][Defer] Serial baudrate hardcoded at 115200 for all DSMR versions — spec says "use V5 settings as base"; design decision, not a bug — deferred, pre-existing
- [x] [Review][Defer] `serial_asyncio_fast` `ImportError` propagates as non-`OSError` and kills read_loop permanently — transitive dep of dsmr-parser so normally always present — deferred, pre-existing
- [x] [Review][Defer] `_connected` flag written both inside and outside `_lock` — flag not currently exposed or read externally — deferred, pre-existing

## Change Log

- Added DSMR P1 adapter (`DSMRAdapter`, `DSMRAdapterConfig`) in `src/open_ems/adapters/dsmr/` with serial/TCP transport abstraction, TelegramBuffer/TelegramParser pipeline, staleness detection with one-shot logging, and asyncio lifecycle (Date: 2025-07-17)
