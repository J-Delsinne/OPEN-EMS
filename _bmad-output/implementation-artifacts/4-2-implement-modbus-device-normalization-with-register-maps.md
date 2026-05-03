# Story 4.2: Implement Modbus device normalization with register maps

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want Modbus-based device adapters that translate raw register values into typed domain states using per-model register maps,
so that the decision engine receives normalized, energy-meaningful values regardless of which supported Modbus device model is installed.

## Acceptance Criteria

**AC1 — InverterAdapter: successful `get_state()` for supported models**
**Given** a supported inverter model (Fronius Gen24, Huawei SUN2000, or Growatt hybrid) is connected via Modbus TCP
**When** `get_state()` is called on its device adapter
**Then** it calls the underlying `ModbusTcpAdapter.get_raw_state()` and maps raw `RawModbusState` register values to a typed `InverterState` using the model's register map
**And** `InverterState` includes: `pv_power_kw` (>= 0 always, PvPowerKw validated), `ac_power_kw` (float, signed), `operating_mode` (str), `fault_code` (str | None)

**AC2 — BatteryAdapter: successful `get_state()` for supported models**
**Given** a supported battery model (BYD HVS or BYD HVM) is connected via Modbus TCP
**When** `get_state()` is called on its device adapter
**Then** raw registers are mapped to `BatteryState` including: `soc_percent`, `battery_power_kw`, `capacity_kwh`, `operating_mode`
**And** battery power sign convention applied and documented: `battery_power_kw > 0` = charging; `battery_power_kw < 0` = discharging
**And** this convention is consistent with Story 4.1's system-wide declaration

**AC3 — ProtocolDegradedState translation**
**Given** the underlying `ModbusTcpAdapter` returns `ProtocolDegradedState`
**When** the device adapter processes it
**Then** it translates to domain-level `DegradedDeviceState` with the correct `role` added
**And** a structured log entry is written via structlog: `event="device_degraded"`, `component="adapters"`, `device_id=...`, `role=...`

**AC4 — Register map location and structure**
**And** register maps live in `src/open_ems/adapters/modbus/register_maps/` as per-model definitions
**And** each register map is a distinct Python module: `fronius_gen24_v1.py`, `huawei_sun2000_v3.py`, `growatt_hybrid_v1.py`, `byd_hvs_v1.py`, `byd_hvm_v1.py`

**AC5 — Unsupported model initialization failure**
**Given** an inverter or battery adapter is initialized with an unrecognized model identifier
**When** `__init__` is called
**Then** it raises a `ValueError` with a structured error message identifying the unknown model
**And** it does not silently default to a wrong register layout — wrong model = hard failure at init time

**AC6 — `get_capabilities()` returns a stub profile**
**Given** any supported device adapter's `get_capabilities()` is called
**Then** it returns a `DeviceCapabilityProfile` with the correct `device_id`, `model`, and `capability_status="full"` (to be refined in Story 4.4)

**AC7 — DeviceAdapter Protocol satisfaction**
**And** both `InverterAdapter` and `BatteryAdapter` satisfy `isinstance(adapter, DeviceAdapter)` at runtime (structural subtyping via `@runtime_checkable`)

**AC8 — Missing register handling**
**Given** a required register is missing from `RawModbusState.registers`
**When** `get_state()` is called on the adapter
**Then** the adapter returns `DegradedDeviceState` (not `KeyError`, not a crash)
**And** the `reason` field includes the missing register address (e.g. `"missing_register:40083"`)

**AC9 — RegisterMap typing enforced**
**And** all register map classes conform to `InverterRegisterMap` or `BatteryRegisterMap` Protocol types
**And** `_SUPPORTED_MODELS` dicts are explicitly typed as `dict[str, InverterRegisterMap]` / `dict[str, BatteryRegisterMap]`
**And** `mypy --strict` passes on all new modules with zero `# type: ignore` suppressions

**Tests:**
**And** unit tests for each supported inverter model verify that raw register values map to correct typed fields (at least one passing case and one boundary/error case per meaningful field)
**And** unit tests for each supported battery model verify the same
**And** unit tests verify battery sign convention: register value indicating charging → `battery_power_kw > 0`; discharging → `battery_power_kw < 0`
**And** unit tests verify `ProtocolDegradedState` → `DegradedDeviceState` translation with correct role
**And** unit tests verify that initializing with an unsupported model raises `ValueError`
**And** unit tests verify that a missing required register returns `DegradedDeviceState` with the register address in `reason`
**And** unit tests verify `connect()` is a safe no-op and `disconnect()` calls the underlying `close()`

## Tasks / Subtasks

- [x] **Task 0: Pre-story quality gate** (AC: all)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/` — confirm 284 tests pass, ≥ 75% coverage

- [x] **Task 1: Create register_maps package** (AC: AC4, AC9)
  - [x] Create `src/open_ems/adapters/modbus/register_maps/__init__.py` — export `InverterRegisterMap`, `BatteryRegisterMap` Protocol types, `MissingRegisterError`, and all five model map instances
  - [x] Create `src/open_ems/adapters/modbus/register_maps/utils.py` — shared decoding helpers (see Dev Notes: Shared Modbus Decoding Utilities)
  - [x] Create `src/open_ems/adapters/modbus/register_maps/fronius_gen24_v1.py` — define register map for Fronius Gen24 inverter; include firmware header comment; use only `utils.py` helpers
  - [x] Create `src/open_ems/adapters/modbus/register_maps/huawei_sun2000_v3.py` — define register map for Huawei SUN2000 inverter; include firmware header comment; use only `utils.py` helpers
  - [x] Create `src/open_ems/adapters/modbus/register_maps/growatt_hybrid_v1.py` — define register map for Growatt hybrid inverter; include firmware header comment; use only `utils.py` helpers
  - [x] Create `src/open_ems/adapters/modbus/register_maps/byd_hvs_v1.py` — define register map for BYD HVS battery; include firmware header comment; use only `utils.py` helpers
  - [x] Create `src/open_ems/adapters/modbus/register_maps/byd_hvm_v1.py` — define register map for BYD HVM battery; include firmware header comment; use only `utils.py` helpers

- [x] **Task 2: Create InverterAdapter** (AC: AC1, AC3, AC5, AC6, AC7, AC8, AC9)
  - [x] Create `src/open_ems/adapters/modbus/inverter_adapter.py`
  - [x] Accept `device_id: str` and `model: str` explicitly in `__init__` — do NOT access `protocol_adapter.config` (see Device ID Access Rule)
  - [x] `_SUPPORTED_MODELS: dict[str, InverterRegisterMap]` — raise `ValueError` at init if model not in dict
  - [x] `connect()` → no-op with docstring explaining lazy-connect model (see Connection Lifecycle Rule)
  - [x] `disconnect()` → call `self._protocol_adapter.close()` (see Connection Lifecycle Rule)
  - [x] `get_state()`: call `get_raw_state()`; branch on `RawModbusState` vs `ProtocolDegradedState`; catch `MissingRegisterError` from register map; return `InverterState` or `DegradedDeviceState`
  - [x] Log `device_degraded` via structlog when returning `DegradedDeviceState` (see log pattern in Dev Notes)
  - [x] `get_capabilities()`: return stub `DeviceCapabilityProfile(device_id=self.device_id, model=self._model, firmware_version=None, capability_status="full")`

- [x] **Task 3: Create BatteryAdapter** (AC: AC2, AC3, AC5, AC6, AC7, AC8, AC9)
  - [x] Create `src/open_ems/adapters/modbus/battery_adapter.py`
  - [x] Same structure as InverterAdapter: accept `device_id: str` explicitly, typed `_SUPPORTED_MODELS: dict[str, BatteryRegisterMap]`, catch `MissingRegisterError`
  - [x] Returns `BatteryState` with sign convention applied; docstring declares `battery_power_kw > 0` = charging, `< 0` = discharging

- [x] **Task 4: Update `src/open_ems/adapters/modbus/__init__.py`** (AC: AC7)
  - [x] Add `InverterAdapter` and `BatteryAdapter` exports to the existing `__all__`

- [x] **Task 5: Write unit tests for InverterAdapter** (AC: AC1, AC3, AC5, AC7, AC8, AC9 tests)
  - [x] Create `tests/unit/adapters/modbus/test_inverter_adapter.py`
  - [x] Use fake `ModbusTcpAdapter` or inject `get_raw_state` return value via dependency injection — do NOT use real hardware
  - [x] Tests: one passing case per supported model with all `InverterState` fields verified
  - [x] Tests: `pv_power_kw` boundary — a register value that maps to a negative raises `DegradedDeviceState` (Pydantic fails → adapter catches → degraded), NOT a silent clamp
  - [x] Tests: `ProtocolDegradedState` in → `DegradedDeviceState` out with `role=DeviceRole.inverter`
  - [x] Tests: unsupported model name → `ValueError` at init
  - [x] Tests: missing required register → `DegradedDeviceState` with register address in `reason`
  - [x] Tests: `connect()` is a no-op (can be called safely without side effects)
  - [x] Tests: `disconnect()` calls underlying `close()`
  - [x] Tests: structlog log capture verifies `event="device_degraded"` on degraded path

- [x] **Task 6: Write unit tests for BatteryAdapter** (AC: AC2, AC3, AC5, AC7, AC8 tests)
  - [x] Create `tests/unit/adapters/modbus/test_battery_adapter.py`
  - [x] Tests: one passing case per supported model with all `BatteryState` fields verified
  - [x] Tests: battery sign convention — charging register values → `battery_power_kw > 0`; discharging → `< 0`
  - [x] Tests: `soc_percent` boundary — 0.0 and 100.0 accepted; out-of-range raw value → `DegradedDeviceState`, not a crash
  - [x] Tests: `ProtocolDegradedState` in → `DegradedDeviceState` out with `role=DeviceRole.battery`
  - [x] Tests: unsupported model name → `ValueError` at init
  - [x] Tests: missing required register → `DegradedDeviceState` with register address in `reason`

- [x] **Task 7: Final validation** (AC: all)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/` — must pass, coverage ≥ 75%
  - [x] Confirm: `isinstance(InverterAdapter(...), DeviceAdapter)` is `True` (structural check)
  - [x] Confirm: `isinstance(BatteryAdapter(...), DeviceAdapter)` is `True`
  - [x] Confirm: No `ProtocolDegradedState` or `RawModbusState` is re-exported from `open_ems.core`

### Senior Developer Review (AI)

- [x] [Review] Review register map correctness against real device documentation (if available) — flag any placeholder values. All five register maps carry `# PLACEHOLDER — register addresses must be verified against actual device documentation` headers and per-register `# PLACEHOLDER` inline comments. No undocumented placeholders. [fronius_gen24_v1.py, huawei_sun2000_v3.py, growatt_hybrid_v1.py, byd_hvs_v1.py, byd_hvm_v1.py]
- [x] [Review] Verify structlog event names follow A4 carry-forward: use snake_case, consistent with existing event names in `tcp.py` (`adapter_timeout`, `adapter_connection_failed`, `adapter_protocol_error`). Both `InverterAdapter` and `BatteryAdapter` emit `"device_degraded"` — snake_case, consistent with existing `tcp.py` event names. [inverter_adapter.py:100, battery_adapter.py:103]

## Dev Notes

### Current Codebase State (at story start)

- **`src/open_ems/core/devices.py`** — ALREADY EXISTS (Story 4.1). Contains all domain types. Do NOT re-create or modify these types in this story:
  - `DeviceRole` (StrEnum): `inverter`, `battery`, `ev_charger`, `grid_meter`
  - `InverterState(BaseModel)`: `device_id: NonEmptyStr`, `pv_power_kw: PvPowerKw`, `ac_power_kw: float`, `operating_mode: str`, `fault_code: str | None`, `read_at: datetime`
  - `BatteryState(BaseModel)`: `device_id: NonEmptyStr`, `soc_percent: SocPercent (0-100)`, `battery_power_kw: float`, `capacity_kwh: float`, `operating_mode: str`, `read_at: datetime`
  - `DegradedDeviceState(BaseModel)`: `device_id: NonEmptyStr`, `role: DeviceRole`, `reason: NonEmptyStr`, `occurred_at: datetime` (UTC validated)
  - `DeviceCapabilityProfile(BaseModel)`: `device_id`, `model`, `firmware_version: str | None`, `capability_status: Literal["full", "reduced", "unknown"]`
  - `DeviceAdapter(Protocol)`: `@runtime_checkable`, `device_id: str`, `connect()`, `disconnect()`, `get_state()`, `get_capabilities()`
  - Annotated types: `PvPowerKw = Annotated[float, Field(ge=0.0)]`, `SocPercent = Annotated[float, Field(ge=0.0, le=100.0)]`, `EnergyKwh = Annotated[float, Field(ge=0.0)]`
  - `_require_utc()` is module-private — do NOT import it; copy the pattern if needed in adapters
- **`src/open_ems/adapters/modbus/tcp.py`** — ALREADY EXISTS (Epic 3). `ModbusTcpAdapter` implements `ProtocolAdapter`. Its `get_raw_state()` returns `RawModbusState | ProtocolDegradedState`. `RawModbusState` has `device_id: str`, `registers: dict[int, int]` (16-bit keys and values), `read_at: datetime (UTC)`.
- **`src/open_ems/adapters/modbus/__init__.py`** — EXISTS; currently exports `ModbusRegisterRange`, `ModbusTcpAdapter`, `ModbusTcpAdapterConfig`. ADD `InverterAdapter`, `BatteryAdapter` to `__all__`.
- **`src/open_ems/adapters/modbus/register_maps/`** — DOES NOT EXIST. Create from scratch.
- **`src/open_ems/adapters/modbus/inverter_adapter.py`** — DOES NOT EXIST. Create from scratch.
- **`src/open_ems/adapters/modbus/battery_adapter.py`** — DOES NOT EXIST. Create from scratch.
- **`tests/unit/adapters/modbus/`** — EXISTS (has `test_tcp_adapter.py` and `__init__.py`). Add new test files here.

### Import Boundary — CRITICAL

```
core/  ← adapters/ may import FROM here
adapters/ ← adapters/ lives here (may import from core/)
```

- `InverterAdapter` and `BatteryAdapter` import from `open_ems.core.devices` (OK) and `open_ems.adapters.modbus.tcp` (OK)
- **`open_ems.core` must NOT import from `open_ems.adapters`** — do not add any import to `core/` in this story
- Do NOT re-export `ProtocolDegradedState`, `RawModbusState` from `open_ems.core`

### Adapter Implementation Pattern

Each domain adapter holds a `ModbusTcpAdapter` instance and delegates protocol I/O to it. The domain adapter's responsibility is translation only:

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from open_ems.adapters.modbus.tcp import ModbusTcpAdapter
from open_ems.adapters.protocol import ProtocolDegradedState, RawModbusState
from open_ems.core.devices import (
    BatteryState,
    DegradedDeviceState,
    DeviceCapabilityProfile,
    DeviceRole,
)

logger = structlog.get_logger(__name__)

from open_ems.adapters.modbus.register_maps import BatteryRegisterMap, MissingRegisterError
from pydantic import ValidationError

_SUPPORTED_MODELS: dict[str, BatteryRegisterMap] = {
    "byd_hvs_v1": BydHvsV1(),
    "byd_hvm_v1": BydHvmV1(),
}

class BatteryAdapter:
    """Domain-level battery adapter: translates RawModbusState → BatteryState."""

    def __init__(self, device_id: str, protocol_adapter: ModbusTcpAdapter, model: str) -> None:
        if model not in _SUPPORTED_MODELS:
            raise ValueError(f"Unsupported battery model: {model!r}. Supported: {list(_SUPPORTED_MODELS)}")
        self.device_id = device_id  # explicit — NOT derived from protocol_adapter internals
        self._protocol_adapter = protocol_adapter
        self._register_map = _SUPPORTED_MODELS[model]
        self._model = model

    async def connect(self) -> None:
        """No-op: ModbusTcpAdapter connects lazily on first get_raw_state() call."""

    async def disconnect(self) -> None:
        """Close the underlying protocol adapter connection."""
        await self._protocol_adapter.close()

    async def get_state(self) -> BatteryState | DegradedDeviceState:
        raw = await self._protocol_adapter.get_raw_state()
        if isinstance(raw, ProtocolDegradedState):
            return self._to_degraded(raw.reason, raw.occurred_at)
        try:
            return self._register_map.map_state(raw)
        except (MissingRegisterError, ValidationError) as exc:
            reason = str(exc) if isinstance(exc, MissingRegisterError) else f"validation_error:{exc}"
            logger.warning(
                "device_degraded",
                component="adapters",
                device_id=self.device_id,
                role=DeviceRole.battery.value,
                reason=reason,
            )
            return DegradedDeviceState(
                device_id=self.device_id,
                role=DeviceRole.battery,
                reason=reason,
                occurred_at=datetime.now(UTC),
            )

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        return DeviceCapabilityProfile(
            device_id=self.device_id,
            model=self._model,
            capability_status="full",
        )

    def _to_degraded(self, reason: str, occurred_at: datetime) -> DegradedDeviceState:
        logger.warning(
            "device_degraded",
            component="adapters",
            device_id=self.device_id,
            role=DeviceRole.battery.value,
            reason=reason,
        )
        return DegradedDeviceState(
            device_id=self.device_id,
            role=DeviceRole.battery,
            reason=reason,
            occurred_at=occurred_at,
        )
```

### Register Map Design

Each register map module defines how to convert `RawModbusState.registers: dict[int, int]` into domain field values. The values in `registers` are unsigned 16-bit integers (0–65535). Device documentation defines:
- Which register address corresponds to each field
- Scaling factors (e.g., divide by 10 for 0.1 kW resolution)
- Signed vs unsigned interpretation (e.g., use `int.from_bytes` for signed 16-bit two's complement)

**Suggested pattern for a register map:**

```python
# fronius_gen24_v1.py
from __future__ import annotations
from datetime import UTC, datetime
from open_ems.adapters.protocol import RawModbusState
from open_ems.core.devices import InverterState
from open_ems.adapters.modbus.register_maps.utils import int16, uint16, scale
from open_ems.adapters.modbus.register_maps import MissingRegisterError, _require_reg

# Device: Fronius Gen24 Plus / GEN24 series
# Protocol: Modbus TCP
# Firmware assumed: all firmware supporting SunSpec Modbus mapping (gen24_v1)
# PLACEHOLDER — register addresses must be verified against actual device documentation

_REG_PV_POWER_W = 40083      # uint16, unit: W (divide by 1000 → kW)
_REG_AC_POWER_W = 40084      # int16 signed two's complement, unit: W
_REG_OPERATING_MODE = 40085  # uint16 enum: 0=Standby, 1=MPPT, 2=Throttled, 3=Fault
_REG_FAULT_CODE = 40110      # uint16, 0 = no fault

_OPERATING_MODE_MAP = {0: "standby", 1: "mppt", 2: "throttled", 3: "fault"}


class FroniusGen24V1:
    def map_state(self, raw: RawModbusState) -> InverterState:
        regs = raw.registers
        pv_raw = _require_reg(regs, _REG_PV_POWER_W)
        ac_raw = _require_reg(regs, _REG_AC_POWER_W)
        mode_raw = _require_reg(regs, _REG_OPERATING_MODE)
        fault_raw = regs.get(_REG_FAULT_CODE, 0)  # optional — default 0 = no fault
        return InverterState(
            device_id=raw.device_id,
            pv_power_kw=scale(uint16(pv_raw), 1 / 1000.0),
            ac_power_kw=scale(int16(ac_raw), 1 / 1000.0),
            operating_mode=_OPERATING_MODE_MAP.get(mode_raw, "unknown"),
            fault_code=str(fault_raw) if fault_raw != 0 else None,
            read_at=raw.read_at,
        )
```

> **IMPORTANT:** The register addresses and scaling factors above are ILLUSTRATIVE PLACEHOLDERS. The implementor MUST use the actual register specifications from each device's Modbus TCP documentation. The architecture doc references `docs/device-integration.md` for supported device/firmware notes. Use realistic but clearly placeholder values if actual specs are not available, and mark them as `# PLACEHOLDER — verify against device spec`.

### Battery Sign Convention — Explicit Declaration

Per system-wide declaration from Story 4.1 (`src/open_ems/core/devices.py` module docstring):
- `battery_power_kw > 0` = battery is **charging** (consuming power from grid or PV)
- `battery_power_kw < 0` = battery is **discharging** (providing power to loads or grid)

The BYD register maps must apply this convention. BYD devices typically report power with a direction register or sign bit. The register map function is responsible for mapping the device's native representation to this convention. Document the conversion explicitly in the register map module.

### Structlog Event Names — A4 Carry-Forward

Per Epic 3 retro action A4 (carry-forward): use snake_case event names consistent with existing adapter events. Existing events in `tcp.py`:
- `adapter_timeout`
- `adapter_connection_failed`
- `adapter_protocol_error`

New event for this story: `device_degraded` (already specified in AC3). Do not use `deviceDegraded`, `device-degraded`, or other variants.

### Fake Protocol Adapter for Tests

Tests must NOT use real hardware. Inject a fake `ModbusTcpAdapter` or a lightweight fake that controls `get_raw_state()` return values. Pattern from `test_tcp_adapter.py` (which uses `FakeAsyncModbusClient`):

```python
class FakeProtocolAdapter:
    """Minimal fake that controls get_raw_state() return value.
    Exposes only get_raw_state() and close() — no .config property."""

    def __init__(self) -> None:
        self.next_state: RawModbusState | ProtocolDegradedState | None = None
        self.close_called: bool = False

    async def get_raw_state(self) -> RawModbusState | ProtocolDegradedState:
        assert self.next_state is not None
        return self.next_state

    async def close(self) -> None:
        self.close_called = True
```

Tests pass `device_id` directly to the adapter constructor:

```python
fake = FakeProtocolAdapter()
fake.next_state = RawModbusState(device_id="bat-001", registers={...}, read_at=...)
adapter = BatteryAdapter(device_id="bat-001", protocol_adapter=fake, model="byd_hvs_v1")
```

### Test Naming Consistency

Test files mirror source files:
- `src/open_ems/adapters/modbus/inverter_adapter.py` → `tests/unit/adapters/modbus/test_inverter_adapter.py`
- `src/open_ems/adapters/modbus/battery_adapter.py` → `tests/unit/adapters/modbus/test_battery_adapter.py`
- Register map modules do not require their own test files — they are exercised through the adapter tests

### Pydantic Patterns (from existing code)

```python
# All models: frozen + extra="forbid"
model_config = ConfigDict(frozen=True, extra="forbid")

# Imports from core (correct — adapters may import from core)
from open_ems.core.devices import (
    InverterState, BatteryState, DegradedDeviceState,
    DeviceRole, DeviceCapabilityProfile, DeviceAdapter,
)
```

### RegisterMap Protocols (STRICT TYPING)

Define explicit Protocols in `register_maps/__init__.py` so mypy --strict can validate all map implementations:

```python
from typing import Protocol
from open_ems.adapters.protocol import RawModbusState
from open_ems.core.devices import InverterState, BatteryState

class InverterRegisterMap(Protocol):
    def map_state(self, raw: RawModbusState) -> InverterState: ...

class BatteryRegisterMap(Protocol):
    def map_state(self, raw: RawModbusState) -> BatteryState: ...
```

Typed model dicts in each adapter:

```python
_SUPPORTED_MODELS: dict[str, InverterRegisterMap] = {
    "fronius_gen24_v1": FroniusGen24V1(),
    ...
}
```

**Explicitly forbidden:**
- Untyped functions (`def map(...) -> Any`)
- Lambdas as register map implementations
- `dict[str, Callable]` or other loosely typed structures
- `# type: ignore` to suppress mypy errors in new modules

---

### Register Presence Validation (FAIL-FAST)

Register maps **MUST NOT** assume all registers are present in `RawModbusState.registers`. A device may return fewer registers than expected due to communication errors or firmware differences.

Define `MissingRegisterError` in `register_maps/__init__.py`:

```python
class MissingRegisterError(Exception):
    """Raised when a required register is absent from RawModbusState."""
    def __init__(self, address: int) -> None:
        self.address = address
        super().__init__(f"missing_register:{address}")
```

Register map pattern — use `get` with explicit error:

```python
def _require_reg(registers: dict[int, int], address: int) -> int:
    value = registers.get(address)
    if value is None:
        raise MissingRegisterError(address)
    return value
```

Adapter `get_state()` catches `MissingRegisterError` and converts:

```python
async def get_state(self) -> InverterState | DegradedDeviceState:
    raw = await self._protocol_adapter.get_raw_state()
    if isinstance(raw, ProtocolDegradedState):
        return self._to_degraded(raw.reason, raw.occurred_at)
    try:
        return self._register_map.map_state(raw)
    except MissingRegisterError as exc:
        logger.warning(
            "device_degraded",
            component="adapters",
            device_id=self.device_id,
            role=DeviceRole.inverter.value,
            reason=str(exc),
        )
        return DegradedDeviceState(
            device_id=self.device_id,
            role=DeviceRole.inverter,
            reason=str(exc),
            occurred_at=datetime.now(UTC),
        )
```

**NEVER** let `MissingRegisterError` or `KeyError` propagate to callers — always convert to `DegradedDeviceState`.

---

### Shared Modbus Decoding Utilities

Create `src/open_ems/adapters/modbus/register_maps/utils.py` with typed helpers:

```python
from __future__ import annotations


def int16(value: int) -> int:
    """Interpret unsigned 16-bit register value as signed two's complement."""
    return value if value < 32768 else value - 65536


def uint16(value: int) -> int:
    """Pass-through for unsigned 16-bit values (documenting intent)."""
    return value


def scale(value: int, factor: float) -> float:
    """Apply a linear scaling factor to a raw register integer."""
    return value * factor
```

**Rules:**
- All five register map modules MUST import and use these helpers
- Inline `value if value < 32768 else value - 65536` logic is NOT allowed in map modules
- `uint16()` is explicit documentation of intent — use it even though it's a pass-through

---

### Device ID Access Rule

Adapters **MUST NOT** access `self._protocol_adapter.config.device_id` — this leaks protocol-layer internals into the domain adapter.

**Use Option A (preferred):** pass `device_id` explicitly at construction time:

```python
class InverterAdapter:
    def __init__(
        self,
        device_id: str,
        protocol_adapter: ModbusTcpAdapter,
        model: str,
    ) -> None:
        self.device_id = device_id  # explicit — not derived from .config
        self._protocol_adapter = protocol_adapter
        ...
```

Do NOT add a `device_id` property to `ModbusTcpAdapter` — that would expose config internals. The explicit constructor argument is the boundary.

---

### Data Validation Policy (NO SILENT CORRECTION)

Adapters **MUST NOT** clamp or silently normalize physically invalid values:

```python
# FORBIDDEN — hides invalid data from a misbehaving device
pv_power_kw = max(0.0, raw_value / 1000.0)
```

Instead, pass the raw decoded value directly to `InverterState`. If the value is physically impossible (e.g., negative PV power), Pydantic's `PvPowerKw = Annotated[float, Field(ge=0.0)]` validator will raise `ValidationError`. The adapter **MUST** catch `ValidationError` and convert to `DegradedDeviceState`:

```python
from pydantic import ValidationError

try:
    return self._register_map.map_state(raw)
except (MissingRegisterError, ValidationError) as exc:
    reason = str(exc) if isinstance(exc, MissingRegisterError) else f"validation_error:{exc}"
    logger.warning("device_degraded", ..., reason=reason)
    return DegradedDeviceState(..., reason=reason, ...)
```

**Explicitly forbidden:**
- `max(0.0, ...)` or `min(100.0, ...)` to clamp values
- `abs(value)` to correct sign
- Any normalization that hides device misbehavior

---

### Connection Lifecycle Rule

`ModbusTcpAdapter` uses **lazy connection** — it connects on first `get_raw_state()` call, not on `connect()`. Domain adapters must document this clearly:

```python
async def connect(self) -> None:
    """No-op: ModbusTcpAdapter connects lazily on first get_raw_state() call."""

async def disconnect(self) -> None:
    """Close the underlying protocol adapter connection."""
    await self._protocol_adapter.close()
```

- `connect()` **must** be a no-op with an explanatory docstring — never raise from it
- `disconnect()` **must** call `close()` to release the TCP connection
- Tests must verify both: `connect()` completes without side effects; `disconnect()` triggers `close()` on the fake adapter

---

### Firmware Awareness

Each register map module **MUST** include a comment header:

```python
# Device: Fronius Gen24 Plus / GEN24 series
# Protocol: Modbus TCP
# Firmware assumed: all firmware supporting SunSpec Modbus mapping (gen24_v1)
# Source: Fronius Solar API 2.0 / Modbus TCP spec (request from installer)
# PLACEHOLDER — register addresses must be verified against actual device documentation
```

This prevents silent incompatibility when a device runs unexpected firmware. If the register layout is known, document the firmware version. If it is a placeholder, the `# PLACEHOLDER` comment is mandatory.

---

### Quality Gates (mandatory per Epic 3/4 retro)

Before marking this story done:
- ✔ Zero open `[ ]` review findings in this story file
- ✔ `uv run python -m ruff check .` passes (BLE001 rule active — no `except Exception` without structlog)
- ✔ `uv run python -m mypy src/` passes with zero errors (strict mode)
- ✔ `uv run python -m pytest tests/` passes, ≥ 75% coverage

### References

- Domain types: [Source: src/open_ems/core/devices.py]
- ModbusTcpAdapter (protocol layer): [Source: src/open_ems/adapters/modbus/tcp.py]
- RawModbusState, ProtocolDegradedState: [Source: src/open_ems/adapters/protocol.py]
- Architecture project structure: [Source: architecture.md — Complete Project Directory Structure, lines 826–850]
- Architecture import boundary rules: [Source: architecture.md — Architectural Boundaries / Import boundary]
- Epic 4 story requirements: [Source: _bmad-output/planning-artifacts/epics.md#Story-4.2]
- Previous story learnings: [Source: _bmad-output/implementation-artifacts/4-1-define-domain-device-state-model-deviceadapter-protocol-and-energy-sign-conventions.md#Dev-Notes]
- Epic 3 retro carry-forward items (A4, C3–C7): [Source: _bmad-output/implementation-artifacts/epic-3-retro-2026-05-03.md]
- Modbus test fake client pattern: [Source: tests/unit/adapters/modbus/test_tcp_adapter.py]

## Dev Agent Record

### Agent Model Used

Claude Sonnet 4.6 (claude-sonnet-4.6)

### Debug Log References

- PV power decoding: Initially used `uint16` for PV register, making the negative-PV boundary test impossible. Corrected to `int16` (consistent with SunSpec int16 DC power type) so that physically-impossible negative values correctly raise `ValidationError` → `DegradedDeviceState` rather than being silently clamped.

### Completion Notes List

- Created `register_maps/` package with `_base.py` (Protocols + MissingRegisterError), `utils.py` (int16/uint16/scale helpers), and 5 model modules (fronius_gen24_v1, huawei_sun2000_v3, growatt_hybrid_v1, byd_hvs_v1, byd_hvm_v1). All register addresses are marked PLACEHOLDER.
- `InverterAdapter` and `BatteryAdapter` satisfy `DeviceAdapter` Protocol (runtime_checkable verified). Both catch `(MissingRegisterError, ValidationError)` and return `DegradedDeviceState`. `connect()` is a documented no-op; `disconnect()` delegates to `protocol_adapter.close()`.
- Battery sign convention: `battery_power_kw > 0` = charging; `< 0` = discharging. BYD direction register (0=charge, 1=discharge) drives sign. Convention documented in `battery_adapter.py` docstring and `byd_hvs_v1.py`/`byd_hvm_v1.py` module comments.
- PV power uses `int16` decoding (SunSpec DC power is signed int16) so physically-impossible negative PV triggers `ValidationError` → `DegradedDeviceState` per the no-silent-clamp policy.
- Final: 348 tests pass (up from 313 baseline), 88% coverage, ruff ✅, mypy 45 files zero errors ✅.

### File List

**New files to create:**
- `src/open_ems/adapters/modbus/register_maps/__init__.py`
- `src/open_ems/adapters/modbus/register_maps/utils.py`
- `src/open_ems/adapters/modbus/register_maps/fronius_gen24_v1.py`
- `src/open_ems/adapters/modbus/register_maps/huawei_sun2000_v3.py`
- `src/open_ems/adapters/modbus/register_maps/growatt_hybrid_v1.py`
- `src/open_ems/adapters/modbus/register_maps/byd_hvs_v1.py`
- `src/open_ems/adapters/modbus/register_maps/byd_hvm_v1.py`
- `src/open_ems/adapters/modbus/inverter_adapter.py`
- `src/open_ems/adapters/modbus/battery_adapter.py`
- `tests/unit/adapters/modbus/test_inverter_adapter.py`
- `tests/unit/adapters/modbus/test_battery_adapter.py`

**Files to update:**
- `src/open_ems/adapters/modbus/__init__.py` — add `InverterAdapter`, `BatteryAdapter` to exports

## Change Log

- 2025-06-03: Story 4-2 implemented by Claude Sonnet 4.6. Created register_maps package (5 model modules), InverterAdapter, BatteryAdapter, and 35 unit tests. 348 tests pass, 88% coverage. All ACs satisfied.

### Review Findings

- [x] [Review][Decision] PV power uses `int16()` but register is unsigned — resolved: switched to `uint16()` in all three inverter maps. The `PvPowerKw >= 0` Pydantic validator now provides the domain constraint; no false-degradation ceiling for large inverters. Test `test_fronius_gen24_negative_pv_gives_degraded_state` replaced with `test_fronius_gen24_large_pv_decoded_as_uint16`. [fronius_gen24_v1.py, huawei_sun2000_v3.py, growatt_hybrid_v1.py]
- [x] [Review][Patch] `device_id` inconsistency between success and failure paths — added `raw.device_id != self.device_id` guard at the top of `get_state()`; mismatch returns `DegradedDeviceState(reason="device_id_mismatch")`. Also fixed `occurred_at` in error branches to use `raw.read_at`. [inverter_adapter.py, battery_adapter.py]
- [x] [Review][Patch] Degraded-state `occurred_at` uses wall clock instead of `raw.read_at` — fixed in both adapters; error branches now pass `occurred_at=raw.read_at`. [inverter_adapter.py, battery_adapter.py]
- [x] [Review][Patch] `_require_reg` exported via `__all__` — removed from `__all__` and import in `register_maps/__init__.py`; now internal-only. [src/open_ems/adapters/modbus/register_maps/__init__.py]
- [x] [Review][Patch] Battery direction register has no domain validation — added `InvalidRegisterValueError` to `_base.py`; both BYD maps now raise it for `direction_raw not in {0, 1}`; adapters catch it alongside `MissingRegisterError`. [byd_hvs_v1.py, byd_hvm_v1.py, _base.py]
- [x] [Review][Patch] AC3 log assertions incomplete — added `device_id` and `role` field assertions to both inverter and battery log tests. [tests/unit/adapters/modbus/test_inverter_adapter.py, test_battery_adapter.py]
- [x] [Review][Defer] Shared singleton register map instances — `_SUPPORTED_MODELS` holds module-level singleton instances; currently safe (maps are stateless), but latent cross-adapter contamination risk if future maps accumulate per-call state. [inverter_adapter.py, battery_adapter.py] — deferred, pre-existing
- [x] [Review][Defer] Huawei `_DEVICE_STATUS_MAP` missing value 3 — status code 3 silently maps to "unknown"; gap between values 2 and 4 may be intentional per device spec but undocumented. Defer until real Huawei register spec is confirmed. [huawei_sun2000_v3.py] — deferred, pre-existing
- [x] [Review][Defer] `ValidationError` reason string unbounded — large register dumps or long error messages could appear in `DegradedDeviceState.reason` without truncation. Pre-existing architectural choice; no `max_length` on `NonEmptyStr`. — deferred, pre-existing
- [x] [Review][Defer] BYD register gap at address 103 — `_REG_POWER_W=102` and `_REG_DIRECTION=104` skip address 103 with no comment. May be a reserved/unused register per BYD spec; defer until real BYD HVS/HVM Modbus spec is confirmed. [byd_hvs_v1.py, byd_hvm_v1.py] — deferred, pre-existing
- [x] [Review][Defer] `int16()`/`uint16()` accept out-of-range inputs silently — utility functions do not enforce 16-bit bounds; the upstream Modbus protocol layer is expected to provide valid 16-bit integers. Defensive bounds enforcement deferred until a real protocol-layer validation gap is observed. [register_maps/utils.py] — deferred, pre-existing
