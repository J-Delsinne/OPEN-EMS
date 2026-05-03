# Story 4.4: Implement device capability profile system and capability gate

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want each supported device model to have a versioned capability profile that gates unvalidated capabilities from being queried by higher layers,
so that the decision engine and PolicyGuard in Epic 8 are never offered capabilities that have not been validated for that model and firmware version.

## Acceptance Criteria

**AC1 — `get_capabilities()` returns a profile from the capability registry**
**Given** a device adapter is initialized for a specific device model
**When** `get_capabilities()` is called
**Then** it returns the matching `DeviceCapabilityProfile` from the capability registry (keyed by **model only** — firmware version is accepted as input but is not used for matching in Epic 4; firmware range matching is intentionally deferred to a future story)
**And** `DeviceCapabilityProfile` declares: supported read capabilities, supported write/control capabilities (as typed `WriteCapability` and `ReadCapability` enumerations), and known limitations for that model

**AC2 — Unsupported capabilities never exposed**
**Given** a higher layer (Epic 7 decision engine or Epic 8 PolicyGuard) queries a device capability profile
**When** the query resolves
**Then** the profile accurately reflects only validated capabilities for that model/firmware
**And** no control command is dispatched from Epic 4 — dispatching based on capability is PolicyGuard's responsibility in Epic 8
**And** unsupported capabilities are absent from the profile or explicitly marked via `CapabilityStatus` — they are never exposed as available to higher layers

**AC3 — Unknown model yields REDUCED profile with write capabilities blocked**
**Given** a device model does not match any known capability profile entry (or a firmware version is provided that does not match any firmware-specific override — placeholder behavior in Epic 4)
**When** the adapter's `get_capabilities()` is called
**Then** read-only state (`get_state()`) is still exposed if safe — an unknown model does not block visibility
**And** all write/control capabilities are blocked by default (empty `write_capabilities`)
**And** the profile is marked `CapabilityStatus.reduced` with `limitation_reason = "capability_profile_unknown: model={model} firmware={firmware}"`
**And** a structured log entry is written: `event="capability_profile_unknown"`, `component="adapters"`, including model and firmware

**AC4 — Capability profiles live in `adapters/capabilities/`**
**And** per-model capability profile definitions are declared in `adapters/capabilities/` as per-model Python modules
**And** FR6b is enforced here: unvalidated or unsupported capabilities are never present in the profile exposed to higher layers

**Tests:**
**And** unit tests verify that known model keys return a profile with the expected read and write capabilities
**And** unit tests verify that unknown firmware returns a REDUCED profile with all write/control capabilities absent (`write_capabilities == frozenset()`)
**And** unit tests verify that a device with unknown firmware still exposes read capabilities without error
**And** unit tests verify the `limitation_reason` string correctly identifies the model and firmware
**And** unit tests verify the `capability_profile_unknown` structlog entry is emitted on the unknown-firmware path
**And** unit tests verify each adapter's `get_capabilities()` delegates to the registry and returns a profile satisfying `DeviceAdapter`

## Tasks / Subtasks

- [x] **Task 0: Pre-story quality gate** (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — confirm 392 tests pass
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m mypy src/`

- [x] **Task 1: Expand `DeviceCapabilityProfile` in `core/devices.py`** (AC: AC1, AC2)
  - [x] Add `ReadCapability(enum.StrEnum)` **before** `DeviceCapabilityProfile`:
    - `state = "state"` — ability to return a typed domain state from `get_state()`
    - `power = "power"` — real-time power measurement (kW)
    - `energy = "energy"` — cumulative energy counters (kWh)
    - `soc = "soc"` — state-of-charge percentage (batteries only)
  - [x] Add `WriteCapability(enum.StrEnum)` **before** `DeviceCapabilityProfile`:
    - `set_charge_rate = "set_charge_rate"` — command to set battery/EV charge power
    - `set_discharge_rate = "set_discharge_rate"` — command to set battery discharge power
    - `set_operating_mode = "set_operating_mode"` — command to set inverter/battery operating mode
    - `set_ev_charge_current = "set_ev_charge_current"` — command to set EV charger current limit
  - [x] Add `CapabilityStatus(enum.StrEnum)` **before** `DeviceCapabilityProfile`:
    - `full = "full"` — all capabilities for the device type are validated and available
    - `reduced = "reduced"` — partial support; one or more capabilities absent (e.g., unknown firmware)
    - `unsupported = "unsupported"` — capability explicitly not supported for this model
  - [x] Replace the stub `DeviceCapabilityProfile` with the expanded version:
    - `device_id: NonEmptyStr`
    - `model: str`
    - `firmware_version: str | None = None`
    - `capability_status: CapabilityStatus = CapabilityStatus.unsupported`
    - `read_capabilities: frozenset[ReadCapability] = frozenset()`
    - `write_capabilities: frozenset[WriteCapability] = frozenset()`
    - `known_limitations: tuple[str, ...] = ()` — use `tuple` (hashable/immutable) not `list`
    - `limitation_reason: str | None = None`
  - [x] `model_config = ConfigDict(frozen=True, extra="forbid")` — keep frozen
  - [x] Update module docstring: document the new enums and their purpose

- [x] **Task 2: Create `adapters/capabilities/` registry** (AC: AC1, AC3, AC4)
  - [x] Create `src/open_ems/adapters/capabilities/__init__.py`:
    - Export: `get_profile`, `INVERTER_PROFILES`, `BATTERY_PROFILES`, `EV_CHARGER_PROFILES`, `GRID_METER_PROFILES`
    - `def get_profile(device_id: str, model: str, firmware_version: str | None = None) -> DeviceCapabilityProfile`:
      - Look up model in the combined registry (`_ALL_PROFILES`)
      - If `model` not found → return `_unknown_profile(device_id, model, firmware_version)` (do NOT raise)
      - If found but `firmware_version` is not `None` and does not match any known firmware entry for that model → return `_unknown_profile(...)`
      - Otherwise return the matching profile with `device_id` substituted in
    - `def _unknown_profile(device_id: str, model: str, firmware_version: str | None) -> DeviceCapabilityProfile`:
      - `limitation_reason = f"capability_profile_unknown: model={model} firmware={firmware_version}"`
      - Return `DeviceCapabilityProfile(device_id=device_id, model=model, firmware_version=firmware_version, capability_status=CapabilityStatus.reduced, read_capabilities=_SAFE_READ_CAPS, write_capabilities=frozenset(), known_limitations=(limitation_reason,), limitation_reason=limitation_reason)`
      - `_SAFE_READ_CAPS = frozenset({ReadCapability.state})` — only basic state read is safe for unknown firmware
  - [x] Create `src/open_ems/adapters/capabilities/inverter.py`:
    - `INVERTER_PROFILES: dict[str, DeviceCapabilityProfile]` keyed by model string (no `device_id` — substituted at lookup time, set `device_id="__placeholder__"` or use a factory)
    - Profile template for all three known models (`fronius_gen24_v1`, `huawei_sun2000_v3`, `growatt_hybrid_v1`):
      - `capability_status=CapabilityStatus.full`
      - `read_capabilities=frozenset({ReadCapability.state, ReadCapability.power})`
      - `write_capabilities=frozenset({WriteCapability.set_operating_mode})`
      - `known_limitations=()`
    - Note: PV inverters do not support `set_charge_rate` or `set_discharge_rate` — those are battery/EV capabilities
  - [x] Create `src/open_ems/adapters/capabilities/battery.py`:
    - `BATTERY_PROFILES: dict[str, DeviceCapabilityProfile]` for `byd_hvs_v1`, `byd_hvm_v1`
    - `capability_status=CapabilityStatus.full`
    - `read_capabilities=frozenset({ReadCapability.state, ReadCapability.power, ReadCapability.soc})`
    - `write_capabilities=frozenset({WriteCapability.set_charge_rate, WriteCapability.set_discharge_rate, WriteCapability.set_operating_mode})`
  - [x] Create `src/open_ems/adapters/capabilities/ev_charger.py`:
    - `EV_CHARGER_PROFILES: dict[str, DeviceCapabilityProfile]` for `ocpp_1_6`
    - `capability_status=CapabilityStatus.full`
    - `read_capabilities=frozenset({ReadCapability.state, ReadCapability.power})`
    - `write_capabilities=frozenset({WriteCapability.set_ev_charge_current})`
  - [x] Create `src/open_ems/adapters/capabilities/grid_meter.py`:
    - `GRID_METER_PROFILES: dict[str, DeviceCapabilityProfile]` for `dsmr_p1`
    - `capability_status=CapabilityStatus.full`
    - `read_capabilities=frozenset({ReadCapability.state, ReadCapability.power, ReadCapability.energy})`
    - `write_capabilities=frozenset()` — grid meter is strictly read-only; no write capabilities, ever
    - No `set_charge_rate`, `set_discharge_rate`, `set_ev_charge_current`, or `set_operating_mode`
  - [x] In `__init__.py`, combine all profile dicts into `_ALL_PROFILES: dict[str, DeviceCapabilityProfile]` and implement the `get_profile()` and `_unknown_profile()` functions

- [x] **Task 3: Update `InverterAdapter.get_capabilities()`** (AC: AC1, AC3)
  - [x] In `src/open_ems/adapters/modbus/inverter_adapter.py`:
    - Import `get_profile` from `open_ems.adapters.capabilities`
    - Replace the stub `get_capabilities()` with:
      ```python
      async def get_capabilities(self) -> DeviceCapabilityProfile:
          profile = get_profile(
              device_id=self.device_id,
              model=self._model,
              firmware_version=None,  # firmware not read from device in Epic 4
          )
          if profile.limitation_reason is not None:
              logger.warning(
                  "capability_profile_unknown",
                  component="adapters",
                  device_id=self.device_id,
                  model=self._model,
                  firmware_version=None,
              )
          return profile
      ```
  - [x] Remove the "expanded in Story 4.4" docstring comment

- [x] **Task 4: Update `BatteryAdapter.get_capabilities()`** (AC: AC1, AC3)
  - [x] Same pattern as Task 3 in `src/open_ems/adapters/modbus/battery_adapter.py`
  - [x] Import `get_profile` from `open_ems.adapters.capabilities`
  - [x] Replace stub `get_capabilities()` with the registry-backed version
  - [x] Remove the "expanded in Story 4.4" docstring comment

- [x] **Task 5: Update `EVChargerAdapter.get_capabilities()`** (AC: AC1, AC3)
  - [x] In `src/open_ems/adapters/ocpp/charger_adapter.py`:
    - Import `get_profile` from `open_ems.adapters.capabilities`
    - Replace the stub `get_capabilities()`:
      - Model is always `"ocpp_1_6"` (hardcoded for this adapter)
      - `firmware_version=None`
      - Log `capability_profile_unknown` if `profile.limitation_reason is not None`

- [x] **Task 6: Update `GridMeterAdapter.get_capabilities()`** (AC: AC1, AC3)
  - [x] In `src/open_ems/adapters/dsmr/meter_adapter.py`:
    - Import `get_profile` from `open_ems.adapters.capabilities`
    - Replace stub `get_capabilities()`:
      - Model is always `"dsmr_p1"` (hardcoded for this adapter)
      - `firmware_version=None`

- [x] **Task 7: Write unit tests for the capability registry** (AC: AC1, AC2, AC3, AC4 tests)
  - [x] Create `tests/unit/adapters/test_capability_registry.py`
  - [x] Tests for `get_profile()` with known model keys:
    - `get_profile("dev-1", "fronius_gen24_v1")` → `capability_status=full`, `ReadCapability.power` in `read_capabilities`, `WriteCapability.set_operating_mode` in `write_capabilities`, `write_capabilities` does NOT contain `set_charge_rate` or `set_discharge_rate`
    - `get_profile("dev-1", "byd_hvs_v1")` → `capability_status=full`, `ReadCapability.soc` in `read_capabilities`, both `set_charge_rate` and `set_discharge_rate` in `write_capabilities`
    - `get_profile("dev-1", "dsmr_p1")` → `capability_status=full`, `write_capabilities == frozenset()` (read-only device)
    - `get_profile("dev-1", "ocpp_1_6")` → `capability_status=full`, `WriteCapability.set_ev_charge_current` in `write_capabilities`
  - [x] Tests for unknown model:
    - `get_profile("dev-1", "unknown_brand_xyz")` → `capability_status=reduced`, `write_capabilities == frozenset()`, `limitation_reason` contains `"capability_profile_unknown: model=unknown_brand_xyz firmware=None"`
    - `get_profile("dev-1", "unknown_brand_xyz", "3.1.0")` → `limitation_reason` contains both model and firmware version string
  - [x] Test that unknown model profile still has `read_capabilities` containing `ReadCapability.state` (not empty — basic read still safe)
  - [x] Test `device_id` is correctly substituted in returned profile (not the placeholder value)
  - [x] Test `frozenset` return type — verify `write_capabilities` is a `frozenset` instance
  - [x] Test `CapabilityStatus.full`, `CapabilityStatus.reduced`, `CapabilityStatus.unsupported` are valid enum members

- [x] **Task 8: Write unit tests for adapter `get_capabilities()` integration** (AC: AC1, AC3 tests)
  - [x] Create `tests/unit/adapters/test_adapter_capabilities.py`
  - [x] For each adapter type (use existing fake protocol adapters from existing test files as patterns):
    - `InverterAdapter("inv-001", fake_modbus, model="fronius_gen24_v1").get_capabilities()` → `capability_status=full`, `device_id="inv-001"`, `model="fronius_gen24_v1"`, write caps include `set_operating_mode`
    - `BatteryAdapter("bat-001", fake_modbus, model="byd_hvs_v1").get_capabilities()` → full, write caps include `set_charge_rate`
    - `EVChargerAdapter("ev-001", fake_ocpp).get_capabilities()` → full, `write_capabilities` includes `set_ev_charge_current`
    - `GridMeterAdapter("meter-001", fake_dsmr).get_capabilities()` → full, `write_capabilities == frozenset()`
  - [x] Test structlog `capability_profile_unknown` event: mock `get_profile` to return a REDUCED profile with non-None `limitation_reason`, verify log event is emitted with correct fields (`event`, `component`, `device_id`, `model`, `firmware_version`)
  - [x] FR6b guard test: for grid meter adapter, assert `WriteCapability.set_charge_rate not in profile.write_capabilities` and `WriteCapability.set_ev_charge_current not in profile.write_capabilities`

- [x] **Task 9: Final validation** (AC: all)
  - [x] Run `uv run python -m ruff check .`
  - [x] Run `uv run python -m ruff format --check .`
  - [x] Run `uv run python -m mypy src/`
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — all tests pass (≥392 + new tests)
  - [x] Confirm: `DeviceCapabilityProfile` is still importable from `open_ems.core.devices` (no import breakage for existing adapters)
  - [x] Confirm: `get_profile("dev-1", "dsmr_p1").write_capabilities == frozenset()` (FR6b enforcement for read-only meter)

### Review Findings

- [x] [Review][Patch] No collision guard in `_ALL_PROFILES` dict merge [`src/open_ems/adapters/capabilities/__init__.py:34`] — `{**INVERTER_PROFILES, **BATTERY_PROFILES, **EV_CHARGER_PROFILES, **GRID_METER_PROFILES}` silently overwrites duplicate keys; an `assert len(_ALL_PROFILES) == sum(...)` at module level would catch cross-registry collisions at import time.
- [x] [Review][Patch] Missing `GridMeterAdapter` test for `capability_profile_unknown` log on reduced path [`tests/unit/adapters/test_adapter_capabilities.py`] — Task 8 spec requires structlog event coverage for all four adapters; `GridMeterAdapter` has no mock + `capture_logs` test for the `limitation_reason is not None` branch.
- [x] [Review][Patch] No explicit test confirming "known model + any firmware → `CapabilityStatus.full`" [`tests/unit/adapters/test_capability_registry.py`] — `test_get_profile_stores_firmware_version_in_returned_profile` stores `firmware="1.2.3"` but never asserts `capability_status == CapabilityStatus.full`; the deferred-matching guarantee (known model always returns full regardless of firmware) is not explicitly verified.
- [x] [Review][Defer] Template profiles with `device_id="__placeholder__"` included in public `__all__` export [`src/open_ems/adapters/capabilities/__init__.py`] — deferred, pre-existing design choice documented in spec; direct dict access bypasses `get_profile` and returns structurally valid but semantically broken profiles with no runtime signal.
- [x] [Review][Defer] `_SUPPORTED_MODELS` in adapters and capability registry dicts have no startup cross-validation [`src/open_ems/adapters/modbus/battery_adapter.py`, `inverter_adapter.py`] — deferred, pre-existing coupling concern; a new model added to one without the other silently returns a REDUCED profile (handled via warning log, but no assertion).
- [x] [Review][Defer] `model: str` on `DeviceCapabilityProfile` allows empty/whitespace strings [`src/open_ems/core/devices.py`] — deferred, pre-existing field definition unchanged since story 4.1; `NonEmptyStr` would be more defensive.
- [x] [Review][Defer] `model_copy` in `get_profile()` propagates any accidental `limitation_reason` from a misconfigured template [`src/open_ems/adapters/capabilities/__init__.py:66`] — deferred, theoretical risk; current templates are clean and frozen Pydantic validation would surface most misconfiguration at import time.

## Dev Notes

### Current Codebase State (at story start)

**Existing — understand before modifying:**

- **`src/open_ems/core/devices.py`** — ALREADY EXISTS. Contains ALL domain types including the stub `DeviceCapabilityProfile`:
  ```python
  class DeviceCapabilityProfile(BaseModel):
      """Per-device capability profile. Expanded in Story 4.4."""
      model_config = ConfigDict(frozen=True, extra="forbid")
      device_id: NonEmptyStr
      model: str
      firmware_version: str | None = None
      capability_status: Literal["full", "reduced", "unknown"] = "unknown"
  ```
  **THIS STORY:** Replace the stub with the expanded model (add `ReadCapability`, `WriteCapability`, `CapabilityStatus` enums and new fields). Change `capability_status` from `Literal["full", "reduced", "unknown"]` to `CapabilityStatus` enum.

- **`src/open_ems/adapters/modbus/inverter_adapter.py`** — ALREADY EXISTS (Stories 4.1, 4.2). Has stub `get_capabilities()` with comment "expanded in Story 4.4":
  ```python
  async def get_capabilities(self) -> DeviceCapabilityProfile:
      """Return a stub capability profile (expanded in Story 4.4)."""
      return DeviceCapabilityProfile(device_id=self.device_id, model=self._model, firmware_version=None, capability_status="full")
  ```
  `_SUPPORTED_MODELS` = `{"fronius_gen24_v1": ..., "huawei_sun2000_v3": ..., "growatt_hybrid_v1": ...}`

- **`src/open_ems/adapters/modbus/battery_adapter.py`** — ALREADY EXISTS (Stories 4.1, 4.2). Same stub pattern.
  `_SUPPORTED_MODELS` = `{"byd_hvs_v1": ..., "byd_hvm_v1": ...}`

- **`src/open_ems/adapters/ocpp/charger_adapter.py`** — ALREADY EXISTS (Story 4.3). Stub `get_capabilities()` returns `model="ocpp_1_6"`.

- **`src/open_ems/adapters/dsmr/meter_adapter.py`** — ALREADY EXISTS (Story 4.3). Stub `get_capabilities()` returns `model="dsmr_p1"`.

**New files to create:**
- `src/open_ems/adapters/capabilities/__init__.py`
- `src/open_ems/adapters/capabilities/inverter.py`
- `src/open_ems/adapters/capabilities/battery.py`
- `src/open_ems/adapters/capabilities/ev_charger.py`
- `src/open_ems/adapters/capabilities/grid_meter.py`
- `tests/unit/adapters/test_capability_registry.py`
- `tests/unit/adapters/test_adapter_capabilities.py`

**Baseline:** 392 tests pass (from Story 4.3).

---

### Breaking Change Warning — `capability_status` field type

The current stub `DeviceCapabilityProfile` has:
```python
capability_status: Literal["full", "reduced", "unknown"] = "unknown"
```

After this story it becomes:
```python
capability_status: CapabilityStatus = CapabilityStatus.unsupported
```

All current usages of `capability_status="full"` in adapter stubs must be updated to `capability_status=CapabilityStatus.full`. Search for `capability_status=` before starting.

Grep command to find all usages:
```
grep -r "capability_status" src/
```

Expected hits:
- `src/open_ems/core/devices.py` — the model definition
- `src/open_ems/adapters/modbus/inverter_adapter.py` — stub (to be replaced)
- `src/open_ems/adapters/modbus/battery_adapter.py` — stub (to be replaced)
- `src/open_ems/adapters/ocpp/charger_adapter.py` — stub (to be replaced)
- `src/open_ems/adapters/dsmr/meter_adapter.py` — stub (to be replaced)

---

### Import Boundary — CRITICAL

```
core/     ← zero external imports (no adapters)
adapters/ ← may import from core/
```

- `adapters/capabilities/__init__.py` may import from `open_ems.core.devices` (ReadCapability, WriteCapability, CapabilityStatus, DeviceCapabilityProfile) — ✅ OK
- `adapters/capabilities/*.py` may import from `open_ems.adapters.capabilities.__init__` — ✅ OK
- **`open_ems.core` must NOT import from `open_ems.adapters`** — do not add any import to `core/devices.py` pointing at adapters

---

### Capability Registry Design

The registry in `adapters/capabilities/__init__.py` uses a simple `dict[str, DeviceCapabilityProfile]` keyed by `model` string. The `device_id` is not part of the key — it is substituted at lookup time via Pydantic `model_copy(update={"device_id": device_id})`.

**Firmware version matching is not implemented in Epic 4.** All known profiles accept any `firmware_version` value — the parameter is stored in the returned profile but is not used to select between entries. Version range validation is deferred to a future story once firmware is actually read from devices. The `firmware_version=None` passed by all current adapters is the correct placeholder behavior.

```python
# adapters/capabilities/__init__.py sketch

from open_ems.adapters.capabilities.inverter import INVERTER_PROFILES
from open_ems.adapters.capabilities.battery import BATTERY_PROFILES
from open_ems.adapters.capabilities.ev_charger import EV_CHARGER_PROFILES
from open_ems.adapters.capabilities.grid_meter import GRID_METER_PROFILES

_ALL_PROFILES: dict[str, DeviceCapabilityProfile] = {
    **INVERTER_PROFILES,
    **BATTERY_PROFILES,
    **EV_CHARGER_PROFILES,
    **GRID_METER_PROFILES,
}

_SAFE_READ_CAPS: frozenset[ReadCapability] = frozenset({ReadCapability.state})


def get_profile(
    device_id: str,
    model: str,
    firmware_version: str | None = None,
) -> DeviceCapabilityProfile:
    template = _ALL_PROFILES.get(model)
    if template is None:
        return _unknown_profile(device_id, model, firmware_version)
    # Firmware version matching is deferred (Epic 4 placeholder: all known profiles accept any
    # firmware_version). Store the value in the returned profile for future use only.
    return template.model_copy(update={"device_id": device_id, "firmware_version": firmware_version})


def _unknown_profile(
    device_id: str, model: str, firmware_version: str | None
) -> DeviceCapabilityProfile:
    reason = f"capability_profile_unknown: model={model} firmware={firmware_version}"
    return DeviceCapabilityProfile(
        device_id=device_id,
        model=model,
        firmware_version=firmware_version,
        capability_status=CapabilityStatus.reduced,
        read_capabilities=_SAFE_READ_CAPS,
        write_capabilities=frozenset(),
        known_limitations=(reason,),
        limitation_reason=reason,
    )
```

The per-model profile files use `device_id="__placeholder__"` (a sentinel never exposed to callers). `get_profile()` always substitutes the real `device_id` before returning.

---

### Per-model Profile Definitions

**`adapters/capabilities/inverter.py`** — placeholder device_id substituted at lookup:
```python
_FULL_CAPS = frozenset({ReadCapability.state, ReadCapability.power})
_WRITE_CAPS = frozenset({WriteCapability.set_operating_mode})

INVERTER_PROFILES: dict[str, DeviceCapabilityProfile] = {
    "fronius_gen24_v1": DeviceCapabilityProfile(
        device_id="__placeholder__", model="fronius_gen24_v1",
        capability_status=CapabilityStatus.full,
        read_capabilities=_FULL_CAPS, write_capabilities=_WRITE_CAPS,
    ),
    "huawei_sun2000_v3": DeviceCapabilityProfile(...),
    "growatt_hybrid_v1": DeviceCapabilityProfile(...),
}
```

**`adapters/capabilities/battery.py`:**
```python
_FULL_CAPS = frozenset({ReadCapability.state, ReadCapability.power, ReadCapability.soc})
_WRITE_CAPS = frozenset({WriteCapability.set_charge_rate, WriteCapability.set_discharge_rate, WriteCapability.set_operating_mode})
```

**`adapters/capabilities/ev_charger.py`:**
```python
_FULL_CAPS = frozenset({ReadCapability.state, ReadCapability.power})
_WRITE_CAPS = frozenset({WriteCapability.set_ev_charge_current})
```

**`adapters/capabilities/grid_meter.py`:**
```python
_FULL_CAPS = frozenset({ReadCapability.state, ReadCapability.power, ReadCapability.energy})
_WRITE_CAPS: frozenset[WriteCapability] = frozenset()  # read-only device — FR6b enforced here
```

---

### Updated `get_capabilities()` Pattern for All Adapters

All four adapters follow this pattern:

```python
async def get_capabilities(self) -> DeviceCapabilityProfile:
    profile = get_profile(
        device_id=self.device_id,
        model=self._model,   # use self._model for Modbus adapters; hardcoded string for OCPP/DSMR
        firmware_version=None,
    )
    if profile.limitation_reason is not None:
        logger.warning(
            "capability_profile_unknown",
            component="adapters",
            device_id=self.device_id,
            model=self._model,
            firmware_version=None,
        )
    return profile
```

For `EVChargerAdapter`, `self._model` doesn't exist — use the string literal `"ocpp_1_6"` directly (or store it as `self._model = "ocpp_1_6"` in `__init__`). For `GridMeterAdapter`, use `"dsmr_p1"`.

---

### Testing Patterns

Use `structlog.testing.capture_logs()` context manager to verify log events — consistent with Stories 4.2/4.3:
```python
with structlog.testing.capture_logs() as cap:
    profile = await adapter.get_capabilities()
events = [e for e in cap if e.get("event") == "capability_profile_unknown"]
assert len(events) == 1
assert events[0]["component"] == "adapters"
assert events[0]["model"] == "unknown_model_xyz"
```

For fake adapters in `test_adapter_capabilities.py`, reuse the `FakeProtocolAdapter` pattern established in previous stories (no `.config` property, just `get_raw_state()` and `close()`/`start()`/`stop()`). You do NOT need `get_raw_state()` to work for capability tests — the adapters don't call `get_state()` in `get_capabilities()`.

**Pydantic frozenset validation**: Pydantic v2 handles `frozenset[EnumType]` natively. Do not use `Field(default_factory=frozenset)` — just use `frozenset()` as the default directly.

**`model_copy()` note**: Pydantic v2 `model_copy(update={...})` returns a new model instance with updated fields. Since `DeviceCapabilityProfile` is frozen, this is the correct way to substitute `device_id`.

---

### What This Story Does NOT Implement

- Reading firmware version from device hardware (Epic 8 / future story)
- Firmware version range matching logic (placeholder — future story when firmware is actually read)
- OCPP per-charger-make profiles (`wallbox_pulsar.py`, `goe_charger.py`) — architecture aspiration; deferred to Epic 9 installer setup where charger make/model is configured
- Control command dispatch — that is PolicyGuard's responsibility in Epic 8
- Database storage of capability profiles — profiles are static code definitions (no DB required in Epic 4)

---

### References

- [Source: epics.md — Epic 4 scope] `adapters/capabilities/` per-model definitions, FR5, FR6b
- [Source: architecture.md — Directory structure] `core/capabilities.py` note, `adapters/ocpp/profiles/`, `adapters/modbus/register_maps/`
- [Source: prd.md — FR5] "The system maintains a versioned capability profile per supported device model and firmware version, validated through read and write/control testing before listing as supported"
- [Source: prd.md — FR6b] "The system does not expose unsupported or unvalidated device capabilities to the decision engine"
- [Source: story 4-2] `InverterAdapter` and `BatteryAdapter` implementation with `_SUPPORTED_MODELS` dict pattern and `get_capabilities()` stub
- [Source: story 4-3] `EVChargerAdapter` and `GridMeterAdapter` implementation with `get_capabilities()` stub returning `model="ocpp_1_6"` / `"dsmr_p1"`
- [Source: src/open_ems/core/devices.py] Current `DeviceCapabilityProfile` stub with note "Expanded in Story 4.4"

## Dev Agent Record

### Agent Model Used

Claude Sonnet 4.6

### Debug Log References

No blocking issues encountered.

### Completion Notes List

- Expanded `DeviceCapabilityProfile` in `core/devices.py` with `ReadCapability`, `WriteCapability`, `CapabilityStatus` enums and new fields (`read_capabilities`, `write_capabilities`, `known_limitations`, `limitation_reason`). Changed `capability_status` from `Literal` to `CapabilityStatus` enum.
- Created `adapters/capabilities/` package with 5 files: `__init__.py` (registry + `get_profile()` + `_unknown_profile()`), `inverter.py`, `battery.py`, `ev_charger.py`, `grid_meter.py`.
- Updated all four adapters (`InverterAdapter`, `BatteryAdapter`, `EVChargerAdapter`, `GridMeterAdapter`) to delegate `get_capabilities()` to the registry and emit `capability_profile_unknown` log warnings for REDUCED profiles.
- FR6b enforced: `dsmr_p1` profile has `write_capabilities=frozenset()` — confirmed by test and manual verification.
- 32 new unit tests added (21 registry tests, 11 adapter integration tests). Total: 424 passing (392 baseline + 32 new). Zero regressions.

### File List

- `src/open_ems/core/devices.py` (modified — expanded enums and DeviceCapabilityProfile)
- `src/open_ems/adapters/capabilities/__init__.py` (new)
- `src/open_ems/adapters/capabilities/inverter.py` (new)
- `src/open_ems/adapters/capabilities/battery.py` (new)
- `src/open_ems/adapters/capabilities/ev_charger.py` (new)
- `src/open_ems/adapters/capabilities/grid_meter.py` (new)
- `src/open_ems/adapters/modbus/inverter_adapter.py` (modified — registry-backed get_capabilities)
- `src/open_ems/adapters/modbus/battery_adapter.py` (modified — registry-backed get_capabilities)
- `src/open_ems/adapters/ocpp/charger_adapter.py` (modified — registry-backed get_capabilities)
- `src/open_ems/adapters/dsmr/meter_adapter.py` (modified — registry-backed get_capabilities)
- `tests/unit/adapters/test_capability_registry.py` (new)
- `tests/unit/adapters/test_adapter_capabilities.py` (new)

## Change Log

- 2026-05-03: Implemented device capability profile system and capability gate (Story 4.4). Added ReadCapability, WriteCapability, CapabilityStatus enums. Created adapters/capabilities/ registry with per-model profiles for all supported device types. Updated all four adapters to use registry-backed get_capabilities(). Added 32 unit tests. FR6b enforced: grid meter write_capabilities=frozenset().
