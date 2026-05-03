# Story 4.3: Implement OCPP and DSMR device normalization

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want OCPP and DSMR device adapters that translate raw protocol states into typed domain states with sign conventions and data-quality metadata applied,
so that the EV charger and grid meter are available to the decision engine through the same DeviceAdapter interface as Modbus devices, with honest representation of data availability.

## Acceptance Criteria

**AC1 — EVChargerAdapter: `get_state()` from `RawOCPPState`**
**Given** an OCPP EV charger has sent a `StatusNotification`
**When** `get_state()` is called on the EV charger adapter
**Then** it reads `RawOCPPState` and maps it to `EVChargerState`
**And** `EVChargerState` includes: `status` (available/charging/faulted/unavailable), `session_active` (bool)
**And** `current_power_kw` is nullable — it is null when the charger does not provide it and must never be fabricated or inferred from status alone

**AC2 — EVChargerState MeterValues data-quality metadata**
**Given** `current_power_kw` is derived from an OCPP `MeterValues` message
**When** `EVChargerState` is constructed
**Then** `power_source = "meter_values"` and `power_measured_at` (datetime of the MeterValues message) are populated
**And** when no MeterValues data is available, `power_source` and `power_measured_at` are both `None`

**AC3 — GridMeterAdapter: `get_state()` from `RawDSMRState`**
**Given** a DSMR telegram has been parsed and `RawDSMRState` is available
**When** `get_state()` is called on the grid meter adapter
**Then** it reads `RawDSMRState` and maps it to `GridMeterState`
**And** `GridMeterState` includes: `grid_power_kw` (sign convention applied), `energy_delivered_kwh`, `energy_returned_kwh`, `received_at`
**And** DSMR sign convention applied: `current_electricity_usage` (OBIS `1-0:1.7.0`) maps to positive `grid_power_kw`; `current_electricity_delivery` (OBIS `1-0:2.7.0`) maps to negative `grid_power_kw`
**And** `energy_delivered_kwh` = tariff_1_usage + tariff_2_usage; `energy_returned_kwh` = tariff_1_delivery + tariff_2_delivery

**AC4 — DSMR 60-second domain-level staleness gate**
**Given** `RawDSMRState` is returned by the protocol adapter but its `received_at` is older than 60 seconds
**When** `get_state()` is called on the grid meter adapter
**Then** it returns `DegradedDeviceState(reason="dsmr_stale")` — the `received_at` from `RawDSMRState` drives this evaluation at the domain layer
**And** this domain-level check is independent of the protocol-layer staleness in `DSMRAdapter` (defense in depth)

**AC5 — `ProtocolDegradedState` translation for both adapters**
**Given** the underlying `ProtocolAdapter` returns `ProtocolDegradedState` for either device type
**When** the device adapter processes it
**Then** it translates to domain-level `DegradedDeviceState` with role and device context
**And** a structured log entry is written: `event="device_degraded"`, `component="adapters"`, `device_id=...`, `role=...`

**AC6 — `EVChargerState` domain model extended**
**And** `EVChargerState` in `src/open_ems/core/devices.py` is extended with:
  - `power_source: Literal["meter_values"] | None = None`
  - `power_measured_at: datetime | None = None`
**And** `power_measured_at` is validated as timezone-aware UTC when not None (use `_require_utc()` pattern)
**And** `mypy --strict` passes with zero errors on all modified files

**AC7 — `RawOCPPState` and `OCPPChargerAdapter` extended for MeterValues**
**And** `RawOCPPState` in `src/open_ems/adapters/protocol.py` is extended with:
  - `last_meter_values_at: datetime | None = None`
  - `last_meter_values_power_kw: float | None = None`
**And** `_ChargerState` in `central_system.py` gains matching fields
**And** `_InternalChargePoint` has an `@on(Action.meter_values)` handler that extracts `Power.Active.Import` measurand and updates `_ChargerState`
**And** `OCPPChargerAdapter.get_raw_state()` includes these fields in the returned `RawOCPPState`

**AC8 — `DeviceAdapter` Protocol satisfaction**
**And** both `EVChargerAdapter` and `GridMeterAdapter` satisfy `isinstance(adapter, DeviceAdapter)` at runtime (structural subtyping via `@runtime_checkable`)

**Tests:**
**And** unit tests verify OCPP status mapping: each OCPP status string maps to the correct domain `status` and `session_active` value
**And** unit tests verify `current_power_kw` is `None` when `last_meter_values_at` is `None` in `RawOCPPState`
**And** unit tests verify that `current_power_kw` derived from MeterValues includes correct `power_source="meter_values"` and `power_measured_at`
**And** unit tests verify DSMR sign convention: `1-0:1.7.0` usage field → positive `grid_power_kw`; `1-0:2.7.0` delivery field → negative `grid_power_kw`
**And** unit tests verify the 60-second DSMR staleness threshold triggers `DegradedDeviceState(reason="dsmr_stale")`
**And** unit tests verify `ProtocolDegradedState` → `DegradedDeviceState` translation with correct `role` for both adapters
**And** unit tests verify structlog `device_degraded` event captured on degraded paths
**And** unit tests verify missing DSMR OBIS field → `DegradedDeviceState` with reason identifying the missing key
**And** unit tests verify `connect()`/`disconnect()` lifecycle for both adapters

## Tasks / Subtasks

- [ ] **Task 0: Pre-story quality gate** (AC: all)
  - [ ] Run `uv run python -m pytest tests/ --no-cov -q` — confirm 348 tests pass
  - [ ] Run `uv run python -m ruff check .`
  - [ ] Run `uv run python -m mypy src/`

- [ ] **Task 1: Extend `EVChargerState` with MeterValues data-quality fields** (AC: AC2, AC6)
  - [ ] In `src/open_ems/core/devices.py`, add to `EVChargerState`:
    - `power_source: Literal["meter_values"] | None = None`
    - `power_measured_at: datetime | None = None`
  - [ ] Add a field validator for `power_measured_at` using the same `_require_utc()` pattern as `read_at`; must be a no-op when value is `None`
  - [ ] Update the module docstring if needed (sign conventions don't change)

- [ ] **Task 2: Extend `RawOCPPState` and `OCPPChargerAdapter` for MeterValues** (AC: AC7)
  - [ ] In `src/open_ems/adapters/protocol.py`, add to `RawOCPPState`:
    - `last_meter_values_at: datetime | None = None`
    - `last_meter_values_power_kw: float | None = None`
  - [ ] Add a field validator for `last_meter_values_at` using `_require_utc()` pattern (no-op when `None`)
  - [ ] In `src/open_ems/adapters/ocpp/central_system.py`:
    - Add `last_meter_values_at: datetime | None = None` and `last_meter_values_power_kw: float | None = None` to `_ChargerState.__init__`
    - Add `@on(Action.meter_values)` handler in `_InternalChargePoint` — extract `Power.Active.Import` measurand value (convert from string to float, divide by 1 if unit is already kW; see Dev Notes for measurand extraction)
    - Update `OCPPChargerAdapter.get_raw_state()` to pass `last_meter_values_at` and `last_meter_values_power_kw` to `RawOCPPState`

- [ ] **Task 3: Create `EVChargerAdapter`** (AC: AC1, AC2, AC5, AC6, AC8)
  - [ ] Create `src/open_ems/adapters/ocpp/charger_adapter.py` (matches architecture spec `ocpp/charger_adapter.py`)
  - [ ] Accept `device_id: str` and `protocol_adapter: OCPPChargerAdapter` explicitly in `__init__` — do NOT access `protocol_adapter.config.device_id` (Device ID Access Rule from Story 4.2)
  - [ ] `connect()` → no-op with docstring explaining OCPP is charger-initiated; WebSocket server manages connection via `OCPPCentralSystem.handle_charger()`
  - [ ] `disconnect()` → no-op with docstring (OCPP disconnect is charger-driven; no `close()` exists on protocol adapter)
  - [ ] `get_state()`:
    - Call `await self._protocol_adapter.get_raw_state()`
    - If `ProtocolDegradedState` → log `device_degraded` and return `DegradedDeviceState(role=DeviceRole.ev_charger, ...)`
    - If `RawOCPPState` with `last_status_notification is None` → check `raw.connection_status`:
    - `connection_status == "disconnected"` → `DegradedDeviceState(reason="ocpp_disconnected")`
    - `connection_status == "connected"` (no status yet) → `DegradedDeviceState(reason="ocpp_no_status")`
    - Map OCPP status string to domain status + `session_active` (see OCPP Status Mapping in Dev Notes)
    - For unknown OCPP status → `DegradedDeviceState(reason=f"ocpp_unknown_status:{raw_status}")`
    - Set `current_power_kw`, `power_source`, `power_measured_at` from `raw.last_meter_values_*` (see MeterValues Mapping in Dev Notes)
    - Return `EVChargerState(device_id=self.device_id, ..., read_at=datetime.now(UTC))`
  - [ ] `get_capabilities()` → return `DeviceCapabilityProfile(device_id=self.device_id, model="ocpp_1_6", capability_status="full")`

- [ ] **Task 4: Update `src/open_ems/adapters/ocpp/__init__.py`** (AC: AC8)
  - [ ] Add `EVChargerAdapter` import from `charger_adapter` and add to `__all__`

- [ ] **Task 5: Create `GridMeterAdapter`** (AC: AC3, AC4, AC5, AC8)
  - [ ] Create `src/open_ems/adapters/dsmr/meter_adapter.py` (matches architecture spec `dsmr/meter_adapter.py`)
  - [ ] Accept `device_id: str` and `protocol_adapter: DSMRAdapter` explicitly in `__init__`
  - [ ] Define `_DSMR_STALE_SECONDS: float = 60.0` as a module-level constant
  - [ ] `connect()` → `await self._protocol_adapter.start()` (starts the background read loop)
  - [ ] `disconnect()` → `await self._protocol_adapter.stop()` (cancels the background read loop)
  - [ ] `get_state()`:
    - Call `await self._protocol_adapter.get_raw_state()`
    - If `ProtocolDegradedState` → log `device_degraded` and return `DegradedDeviceState(role=DeviceRole.grid_meter, ...)`
    - If `RawDSMRState`: domain-level staleness check — if `(datetime.now(UTC) - raw.received_at).total_seconds() > _DSMR_STALE_SECONDS` → `DegradedDeviceState(reason="dsmr_stale")`
    - Call `_map_telegram(raw)` to extract `GridMeterState` — raises `MissingDSMRFieldError` on missing OBIS key; catch and return `DegradedDeviceState`
  - [ ] Define `MissingDSMRFieldError(Exception)` in this module — `__init__(self, key: str)` with `self.key = key; super().__init__(f"missing_dsmr_field:{key}")`
  - [ ] `_map_telegram(raw: RawDSMRState) -> GridMeterState` — extract OBIS fields, apply sign convention, validate `ValidationError` from Pydantic (see DSMR Field Extraction in Dev Notes)
  - [ ] `get_capabilities()` → return `DeviceCapabilityProfile(device_id=self.device_id, model="dsmr_p1", capability_status="full")`

- [ ] **Task 6: Update `src/open_ems/adapters/dsmr/__init__.py`** (AC: AC8)
  - [ ] Add `GridMeterAdapter` import from `meter_adapter` and add to `__all__`

- [ ] **Task 7: Write unit tests for `EVChargerAdapter`** (AC: AC1, AC2, AC5, AC8 tests)
  - [ ] Create `tests/unit/adapters/ocpp/test_charger_adapter.py`
  - [ ] Use `FakeOCPPProtocolAdapter` (controls `get_raw_state()` return value; does NOT need to be a full `OCPPChargerAdapter` — see Fake Adapter Pattern in Dev Notes)
  - [ ] Tests: each OCPP status → correct domain `status` and `session_active` (cover all 9 OCPP status values)
  - [ ] Tests: `current_power_kw` is `None` when `last_meter_values_at is None` in `RawOCPPState`
  - [ ] Tests: `current_power_kw`, `power_source="meter_values"`, `power_measured_at` correct when `last_meter_values_*` is populated
  - [ ] Tests: `ProtocolDegradedState` in → `DegradedDeviceState(role=DeviceRole.ev_charger)` out
  - [ ] Tests: no `last_status_notification` → `DegradedDeviceState(reason="ocpp_no_status")`
  - [ ] Tests: unknown OCPP status string → `DegradedDeviceState` with reason containing the unknown status
  - [ ] Tests: `connect()` is a no-op (no exception, no method call on fake adapter)
  - [ ] Tests: `disconnect()` is a no-op (no exception, no method call on fake adapter)
  - [ ] Tests: `isinstance(EVChargerAdapter(...), DeviceAdapter)` is `True`
  - [ ] Tests: structlog `device_degraded` captured on degraded path (verify `event`, `component`, `device_id`, `role` fields)

- [ ] **Task 8: Write unit tests for `GridMeterAdapter`** (AC: AC3, AC4, AC5, AC8 tests)
  - [ ] Create `tests/unit/adapters/dsmr/test_meter_adapter.py`
  - [ ] Use `FakeDSMRProtocolAdapter` (controls `get_raw_state()` return; tracks `start()`/`stop()` calls — see Fake Adapter Pattern in Dev Notes)
  - [ ] Tests: DSMR sign convention — usage field → positive `grid_power_kw`; delivery field → negative `grid_power_kw`; both zero → `grid_power_kw = 0.0`
  - [ ] Tests: `energy_delivered_kwh` = tariff_1_usage + tariff_2_usage
  - [ ] Tests: `energy_returned_kwh` = tariff_1_delivery + tariff_2_delivery
  - [ ] Tests: staleness — `received_at` > 60s ago → `DegradedDeviceState(reason="dsmr_stale")`
  - [ ] Tests: staleness boundary — `received_at` exactly 60s ago (edge) and 59s ago (not stale) — verify boundary
  - [ ] Tests: `ProtocolDegradedState(reason="dsmr_stale")` in → `DegradedDeviceState(reason="dsmr_stale")` out
  - [ ] Tests: `ProtocolDegradedState(reason="dsmr_unavailable")` in → `DegradedDeviceState` with reason preserved
  - [ ] Tests: missing required OBIS key → `DegradedDeviceState` with `reason` containing the missing key (e.g., `"missing_dsmr_field:1-0:1.7.0"`)
  - [ ] Tests: `connect()` calls `start()` on protocol adapter
  - [ ] Tests: `disconnect()` calls `stop()` on protocol adapter
  - [ ] Tests: `isinstance(GridMeterAdapter(...), DeviceAdapter)` is `True`
  - [ ] Tests: structlog `device_degraded` captured on degraded path (verify `event`, `component`, `device_id`, `role` fields)

- [ ] **Task 9: Final validation** (AC: all)
  - [ ] Run `uv run python -m ruff check .`
  - [ ] Run `uv run python -m ruff format --check .`
  - [ ] Run `uv run python -m mypy src/`
  - [ ] Run `uv run python -m pytest tests/ --no-cov -q` — all tests pass
  - [ ] Confirm: `isinstance(EVChargerAdapter(...), DeviceAdapter)` is `True`
  - [ ] Confirm: `isinstance(GridMeterAdapter(...), DeviceAdapter)` is `True`
  - [ ] Confirm: No `ProtocolDegradedState` or `RawOCPPState` or `RawDSMRState` re-exported from `open_ems.core`

## Dev Notes

### Current Codebase State (at story start)

**Existing — do NOT re-create or modify unless specified:**
- **`src/open_ems/core/devices.py`** — ALREADY EXISTS (Story 4.1). Contains all domain types.
  - `EVChargerState(BaseModel)`: `device_id`, `status: Literal["available","charging","faulted","unavailable"]`, `session_active: bool`, `current_power_kw: float | None = None`, `read_at: datetime`
  - **THIS STORY ADDS** `power_source: Literal["meter_values"] | None = None` and `power_measured_at: datetime | None = None` to `EVChargerState`
  - `GridMeterState(BaseModel)`: `device_id`, `grid_power_kw: float`, `energy_delivered_kwh: EnergyKwh`, `energy_returned_kwh: EnergyKwh`, `received_at: datetime` — **DO NOT MODIFY**
  - `DegradedDeviceState(BaseModel)`: `device_id: NonEmptyStr`, `role: DeviceRole`, `reason: NonEmptyStr`, `occurred_at: datetime`
  - `DeviceCapabilityProfile(BaseModel)`: `device_id`, `model`, `firmware_version: str | None`, `capability_status: Literal["full", "reduced", "unknown"]`
  - `DeviceAdapter(Protocol)`: `@runtime_checkable`, `device_id: str`, `connect()`, `disconnect()`, `get_state()`, `get_capabilities()`
  - `_require_utc()` is module-private — do NOT import it from core; copy the pattern if needed

- **`src/open_ems/adapters/protocol.py`** — ALREADY EXISTS (Epic 3). Contains raw protocol types.
  - `RawOCPPState(RawProtocolState)`: `device_id`, `charge_point_id`, `last_status_notification: RawMessagePayload = None`, `last_heartbeat_at: datetime | None = None`, `connection_status: ConnectionStatus`, `last_call_result: RawMessagePayload = None`, `last_call_error: RawMessagePayload = None`
  - **THIS STORY ADDS** `last_meter_values_at: datetime | None = None` and `last_meter_values_power_kw: float | None = None` to `RawOCPPState`
  - `RawDSMRState(RawProtocolState)`: `telegram_fields: dict[str, Any]`, `received_at: datetime` — **DO NOT MODIFY**
  - `ProtocolDegradedState(BaseModel)`: `device_id: NonEmptyStr`, `reason: NonEmptyStr`, `occurred_at: datetime`

- **`src/open_ems/adapters/ocpp/central_system.py`** — ALREADY EXISTS (Epic 3). `OCPPChargerAdapter` is the raw OCPP adapter. `_ChargerState` tracks connected/status/heartbeat. `_InternalChargePoint` has handlers for `boot_notification`, `heartbeat`, `status_notification`.
  - **THIS STORY ADDS** MeterValues tracking: `_ChargerState` gains `last_meter_values_at` and `last_meter_values_power_kw`; `_InternalChargePoint` gains `@on(Action.meter_values)` handler; `get_raw_state()` forwards these fields

- **`src/open_ems/adapters/dsmr/p1.py`** — ALREADY EXISTS (Epic 3). `DSMRAdapter` implements `ProtocolAdapter`. Its `get_raw_state()` returns `RawDSMRState | ProtocolDegradedState`. Already applies 60s staleness at protocol layer (returns `ProtocolDegradedState(reason="dsmr_stale")`). Has `start()` and `stop()` for background read loop.

- **`src/open_ems/adapters/ocpp/__init__.py`** — EXISTS; exports `OCPPAdapterConfig`, `OCPPCentralSystem`, `OCPPChargerAdapter`. **ADD** `EVChargerAdapter`.

- **`src/open_ems/adapters/dsmr/__init__.py`** — EXISTS; exports `DSMRAdapter`, `DSMRAdapterConfig`. **ADD** `GridMeterAdapter`.

- **`src/open_ems/adapters/modbus/inverter_adapter.py`** and **`battery_adapter.py`** — ALREADY EXISTS (Story 4.2). Use as the implementation pattern reference.

**New files to create:**
- `src/open_ems/adapters/ocpp/charger_adapter.py` — `EVChargerAdapter` class
- `src/open_ems/adapters/dsmr/meter_adapter.py` — `GridMeterAdapter` class
- `tests/unit/adapters/ocpp/test_charger_adapter.py`
- `tests/unit/adapters/dsmr/test_meter_adapter.py`

**Baseline:** 348 tests pass (from Story 4.2).

---

### Import Boundary — CRITICAL

```
core/     ← adapters/ may import FROM here
adapters/ ← adapters/ lives here (may import from core/)
```

- `EVChargerAdapter` imports from `open_ems.core.devices` (OK) and `open_ems.adapters.ocpp.central_system` (OK)
- `GridMeterAdapter` imports from `open_ems.core.devices` (OK) and `open_ems.adapters.dsmr.p1` (OK)
- **`open_ems.core` must NOT import from `open_ems.adapters`** — do not add any import to `core/`
- Do NOT re-export `ProtocolDegradedState`, `RawOCPPState`, or `RawDSMRState` from `open_ems.core`

---

### OCPP Status Mapping

The `last_status_notification["status"]` value is a raw OCPP string. Map it to domain types:

```python
_OCPP_STATUS_MAP: dict[str, tuple[str, bool]] = {
    # (domain_status, session_active)
    "Available":     ("available",   False),
    "Preparing":     ("available",   True),   # vehicle connected, not yet charging
    "Charging":      ("charging",    True),
    "SuspendedEVSE": ("charging",    True),
    "SuspendedEV":   ("charging",    True),
    "Finishing":     ("charging",    True),
    "Reserved":      ("unavailable", False),
    "Unavailable":   ("unavailable", False),
    "Faulted":       ("faulted",     False),
}
```

If `raw_status` is NOT in this dict → return `DegradedDeviceState(reason=f"ocpp_unknown_status:{raw_status}")`.

`session_active = True` for status values where a vehicle is physically present ("Preparing" through "Finishing"). `status` reflects actual power delivery — "Preparing" means the vehicle is connected but charging has not started, so `status="available"`. Never infer `session_active` from any field other than the status mapping.

---

### MeterValues Mapping (OCPP → `current_power_kw`)

In the `@on(Action.meter_values)` handler added to `_InternalChargePoint`:

```python
@on(Action.meter_values)
def on_meter_values(
    self,
    connector_id: int,
    meter_value: list[dict[str, Any]],
    **kwargs: Any,
) -> Any:
    # Find the most recent Power.Active.Import sampled value
    power_kw: float | None = None
    measurement_ts: datetime | None = None
    for mv in reversed(meter_value):  # most recent first
        # Extract timestamp from MeterValues payload; NEVER use now() when
        # the payload carries a real measurement time (data integrity requirement)
        raw_ts: str | None = mv.get("timestamp")
        parsed_ts: datetime | None = None
        if raw_ts:
            try:
                from datetime import timezone
                from dateutil.parser import parse as parse_dt  # dateutil is a transitive dep
                dt = parse_dt(raw_ts)
                parsed_ts = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=UTC)
            except (ValueError, TypeError, OverflowError):
                parsed_ts = None
        for sv in mv.get("sampled_value", []):
            if sv.get("measurand") == "Power.Active.Import":
                try:
                    raw_val = float(sv["value"])
                    unit = sv.get("unit", "W")
                    # OCPP default unit for power is W; convert to kW
                    power_kw = raw_val / 1000.0 if unit != "kW" else raw_val
                    measurement_ts = parsed_ts  # tie timestamp to this measurement
                except (ValueError, KeyError):
                    continue
                break
        if power_kw is not None:
            break
    if power_kw is not None:
        # Use the payload timestamp; only fall back to now() if absent or unparseable
        self._state.last_meter_values_at = measurement_ts or datetime.now(UTC)
        self._state.last_meter_values_power_kw = power_kw
    return call_result.MeterValues()
```

In `EVChargerAdapter.get_state()`:

```python
meter_at = raw.last_meter_values_at
power_kw = raw.last_meter_values_power_kw
if meter_at is not None and power_kw is not None:
    # Negative power is physically invalid for EV charging; fail-fast visibility
    if power_kw < 0:
        return self._to_degraded("ocpp_invalid_power", datetime.now(UTC))
    current_power_kw: float | None = power_kw
    power_source: Literal["meter_values"] | None = "meter_values"
    power_measured_at: datetime | None = meter_at
else:
    current_power_kw = None
    power_source = None
    power_measured_at = None
```

`current_power_kw` MUST be `None` when MeterValues data is absent — never infer from status. Negative `current_power_kw` MUST return `DegradedDeviceState(reason="ocpp_invalid_power")` — never silently accept.

---

### OCPP Connection Lifecycle

`OCPPChargerAdapter` does NOT have a `connect()` or `close()` method — the connection lifecycle is managed externally via `OCPPCentralSystem.handle_charger()` / `handle_connection()`. Therefore:

```python
async def connect(self) -> None:
    """No-op: OCPP connections are charger-initiated.
    Connection lifecycle is managed by OCPPCentralSystem.handle_charger()
    via the FastAPI WebSocket endpoint at /ocpp/{charge_point_id}.
    """

async def disconnect(self) -> None:
    """No-op: OCPP disconnect is charger-driven.
    The OCPPChargerAdapter has no close() method; connection teardown
    is handled by the WebSocket server task lifecycle.
    """
```

Both methods must be no-ops — never raise from them.

---

### DSMR Field Extraction

The `telegram_fields` dict in `RawDSMRState` uses OBIS reference strings as keys (from `str(obis_ref)` in `DSMRAdapter._telegram_to_dict()`). Each value is `{"value": X, "unit": "kW" or "kWh"}`.

**OBIS keys to extract for `GridMeterState`:**

```python
_OBIS_USAGE    = "1-0:1.7.0"   # current_electricity_usage (kW, always >= 0)
_OBIS_DELIVERY = "1-0:2.7.0"   # current_electricity_delivery (kW, always >= 0)
_OBIS_USED_T1  = "1-0:1.8.1"   # electricity_used_tariff_1 (kWh)
_OBIS_USED_T2  = "1-0:1.8.2"   # electricity_used_tariff_2 (kWh)
_OBIS_DELIV_T1 = "1-0:2.8.1"   # electricity_delivered_tariff_1 (kWh)
_OBIS_DELIV_T2 = "1-0:2.8.2"   # electricity_delivered_tariff_2 (kWh)
```

> **NOTE:** These are DSMR v5 standard OBIS codes. For DSMR v2.2 and v4, field availability may differ. This story implements the DSMR v5 default and should document this assumption as a `# PLACEHOLDER — verify against actual P1 spec for other DSMR versions`.

**Sign convention application:**
```python
def _get_field(fields: dict[str, Any], key: str) -> float:
    entry = fields.get(key)
    if entry is None or entry.get("value") is None:
        raise MissingDSMRFieldError(key)
    try:
        return float(entry["value"])
    except (TypeError, ValueError):
        raise MissingDSMRFieldError(key)

usage_kw   = _get_field(fields, _OBIS_USAGE)
delivery_kw = _get_field(fields, _OBIS_DELIVERY)
grid_power_kw = usage_kw - delivery_kw  # positive = import, negative = export
```

**Energy cumulative fields:**
```python
energy_delivered_kwh = _get_field(fields, _OBIS_USED_T1) + _get_field(fields, _OBIS_USED_T2)
energy_returned_kwh  = _get_field(fields, _OBIS_DELIV_T1) + _get_field(fields, _OBIS_DELIV_T2)
```

All OBIS key lookups use `_get_field()` which raises `MissingDSMRFieldError`. The adapter's `get_state()` catches this and returns `DegradedDeviceState(reason=str(exc))` — e.g., `reason="missing_dsmr_field:1-0:1.7.0"`.

Also catch `ValidationError` from Pydantic (e.g., negative cumulative energy) and convert:
```python
except (MissingDSMRFieldError, ValidationError) as exc:
    reason = str(exc) if isinstance(exc, MissingDSMRFieldError) else f"validation_error:{exc}"
    logger.warning("device_degraded", ..., reason=reason)
    return DegradedDeviceState(...)
```

---

### Adapter Implementation Pattern (from Story 4.2)

Follow the same pattern as `InverterAdapter` / `BatteryAdapter`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import structlog

from open_ems.core.devices import (
    DegradedDeviceState,
    DeviceCapabilityProfile,
    DeviceRole,
    EVChargerState,
    GridMeterState,
)
from open_ems.adapters.protocol import ProtocolDegradedState

logger = structlog.get_logger(__name__)
```

Degraded path log pattern (consistent with Story 4.2 and retro A4 — snake_case event names):
```python
logger.warning(
    "device_degraded",
    component="adapters",
    device_id=self.device_id,
    role=DeviceRole.ev_charger.value,  # or DeviceRole.grid_meter.value
    reason=reason,
)
```

Device ID rule: always pass `device_id` explicitly to the adapter constructor — do NOT derive it from `protocol_adapter.config.device_id` (breaks the domain/protocol boundary).

---

### Fake Adapter Pattern for Tests

Do NOT use real `OCPPChargerAdapter` or `DSMRAdapter` — inject a fake that controls `get_raw_state()`:

```python
class FakeOCPPProtocolAdapter:
    """Minimal fake that controls get_raw_state() for EVChargerAdapter tests."""

    def __init__(self) -> None:
        self.next_state: RawOCPPState | ProtocolDegradedState | None = None

    async def get_raw_state(self) -> RawOCPPState | ProtocolDegradedState:
        assert self.next_state is not None
        return self.next_state


class FakeDSMRProtocolAdapter:
    """Minimal fake that controls get_raw_state() and tracks start/stop calls."""

    def __init__(self) -> None:
        self.next_state: RawDSMRState | ProtocolDegradedState | None = None
        self.start_called: int = 0
        self.stop_called: int = 0

    async def get_raw_state(self) -> RawDSMRState | ProtocolDegradedState:
        assert self.next_state is not None
        return self.next_state

    async def start(self) -> None:
        self.start_called += 1

    async def stop(self) -> None:
        self.stop_called += 1
```

Tests pass `device_id` directly to adapter constructor:
```python
fake = FakeOCPPProtocolAdapter()
fake.next_state = RawOCPPState(
    device_id="ev-001",
    charge_point_id="cp-001",
    connection_status="connected",
    last_status_notification={"type": "StatusNotification", "status": "Charging", ...},
)
adapter = EVChargerAdapter(device_id="ev-001", protocol_adapter=fake)
```

---

### Structlog Event Names — Carry-Forward from Story 4.2

Per Epic 3 retro action A4: use snake_case event names consistent with existing events. All new events in this story use `device_degraded` (identical to Story 4.2). Do NOT use `deviceDegraded`, `device-degraded`, or other variants.

---

### DSMR Staleness — Domain Layer is Authoritative

**Rule:** `GridMeterAdapter` is the **only** authoritative staleness enforcer. The 60-second business rule belongs exclusively to the domain layer.

**Protocol layer responsibility (existing `DSMRAdapter`):**
- Returns `RawDSMRState` as long as data exists (regardless of age)
- Returns `ProtocolDegradedState` only for connection failures (`dsmr_unavailable`, `dsmr_disconnected`)
- The existing `DSMRAdapter.get_raw_state()` already returns `ProtocolDegradedState(reason="dsmr_stale")` when data is older than `stale_after_s` — this is a legacy behavior. When `GridMeterAdapter` receives this, it passes it through as `DegradedDeviceState(reason="dsmr_stale")` transparently.

**Domain layer responsibility (`GridMeterAdapter.get_state()`):**
- When `RawDSMRState` is received: check `(datetime.now(UTC) - raw.received_at).total_seconds() > _DSMR_STALE_SECONDS` — if stale, return `DegradedDeviceState(reason="dsmr_stale")`
- The `raw.received_at` field is the authoritative measurement timestamp — always use it, never substitute `datetime.now(UTC)` for staleness evaluation
- When `ProtocolDegradedState` is received for any reason: translate to `DegradedDeviceState` and pass through

**This means:** if `DSMRAdapter` is configured with `stale_after_s=120` but the domain requires 60s, the domain check still enforces 60s correctly because it uses `raw.received_at`. Domain logic always wins.

---

### Quality Gates (mandatory per Epic 3/4 retro)

Before marking this story done:
- ✔ Zero open `[ ]` review findings in this story file
- ✔ `uv run python -m ruff check .` passes (BLE001 rule active — no bare `except Exception` without structlog)
- ✔ `uv run python -m mypy src/` passes with zero errors (strict mode)
- ✔ `uv run python -m pytest tests/ --no-cov -q` passes

### Project Structure Notes

- `charger_adapter.py` placed in `adapters/ocpp/` (per architecture `ocpp/charger_adapter.py`)
- `meter_adapter.py` placed in `adapters/dsmr/` (per architecture `dsmr/meter_adapter.py`)
- Test files mirror source files:
  - `src/open_ems/adapters/ocpp/charger_adapter.py` → `tests/unit/adapters/ocpp/test_charger_adapter.py`
  - `src/open_ems/adapters/dsmr/meter_adapter.py` → `tests/unit/adapters/dsmr/test_meter_adapter.py`
- `MissingDSMRFieldError` lives in `meter_adapter.py` (not in a shared module — parallel to `MissingRegisterError` in register_maps, which is specific to Modbus)

### References

- Domain types: [Source: src/open_ems/core/devices.py]
- Protocol raw types: [Source: src/open_ems/adapters/protocol.py]
- OCPP raw adapter: [Source: src/open_ems/adapters/ocpp/central_system.py]
- DSMR raw adapter: [Source: src/open_ems/adapters/dsmr/p1.py]
- Modbus adapter pattern (follow this): [Source: src/open_ems/adapters/modbus/inverter_adapter.py, battery_adapter.py]
- Architecture file locations: [Source: _bmad-output/planning-artifacts/architecture.md, lines 841–850]
- Architecture import boundary: [Source: _bmad-output/planning-artifacts/architecture.md — Architectural Boundaries / Import boundary]
- Architecture sign conventions: [Source: _bmad-output/planning-artifacts/architecture.md, lines 490–493]
- Epic 4 story requirements: [Source: _bmad-output/planning-artifacts/epics.md#Story-4.3, lines 1127–1159]
- Story 4.2 device ID rule and adapter pattern: [Source: _bmad-output/implementation-artifacts/4-2-implement-modbus-device-normalization-with-register-maps.md#Device-ID-Access-Rule]
- Story 4.2 structlog carry-forward (A4): [Source: _bmad-output/implementation-artifacts/4-2-implement-modbus-device-normalization-with-register-maps.md#Structlog-Event-Names]
- Story 4.2 no-silent-correction policy: [Source: _bmad-output/implementation-artifacts/4-2-implement-modbus-device-normalization-with-register-maps.md#Data-Validation-Policy]
- DSMR test telegram OBIS keys: [Source: tests/unit/adapters/dsmr/test_dsmr_adapter.py]

## Dev Agent Record

### Agent Model Used

{{agent_model_name_version}}

### Debug Log References

### Completion Notes List

### File List

**New files to create:**
- `src/open_ems/adapters/ocpp/charger_adapter.py`
- `src/open_ems/adapters/dsmr/meter_adapter.py`
- `tests/unit/adapters/ocpp/test_charger_adapter.py`
- `tests/unit/adapters/dsmr/test_meter_adapter.py`

**Files to update:**
- `src/open_ems/core/devices.py` — add `power_source` and `power_measured_at` to `EVChargerState`
- `src/open_ems/adapters/protocol.py` — add `last_meter_values_at` and `last_meter_values_power_kw` to `RawOCPPState`
- `src/open_ems/adapters/ocpp/central_system.py` — add MeterValues tracking to `_ChargerState` and `_InternalChargePoint`
- `src/open_ems/adapters/ocpp/__init__.py` — add `EVChargerAdapter` to exports
- `src/open_ems/adapters/dsmr/__init__.py` — add `GridMeterAdapter` to exports
