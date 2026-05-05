# Story 8.2: Implement IntentExecutor, PolicyGuard, and command dispatch

Status: done

## Story

As a developer,
I want a two-stage command pipeline where IntentExecutor translates typed intents to candidate commands and PolicyGuard validates and dispatches them as the final mandatory safety gate,
so that PolicyGuard remains focused on safety enforcement rather than translation logic, and no device command can ever bypass the safety check.

## Acceptance Criteria

**AC1 — Two-stage pipeline: IntentExecutor → PolicyGuard**

GIVEN an `EvaluationResult` containing typed intents arrives at the execution layer
WHEN command preparation runs
THEN `IntentExecutor.translate(result, snapshot)` translates each typed intent to a candidate `DeviceCommand` object — mapping intent fields to adapter-level command parameters
AND each candidate command is then passed to `PolicyGuard.authorize_and_dispatch()` — translation and safety gate are separate, sequential responsibilities
AND hold intents (`BatteryIntentAction.hold`, `EVChargerIntentAction.hold`) produce NO candidate command and are silently skipped — no command is dispatched for a hold

**AC2 — PolicyGuard constraint and capability checks**

GIVEN a candidate `DeviceCommand` arrives at `PolicyGuard.authorize_and_dispatch()`
WHEN PolicyGuard evaluates it
THEN it checks the command against: active safety constraints (peak limit, battery reserve floor), command bounds, current `SystemOperatingMode`, and the declared device capability profile
AND if `SystemOperatingMode` is `fail_safe`, ALL commands are rejected regardless of other checks
AND if the device capability profile does not include the required `WriteCapability`, dispatch is blocked before any `send_command()` is called
AND if the battery SoC is at or below `reserve_floor_percent` and the command is a discharge, the command is rejected
AND if the commanded rate exceeds `peak_limit_kw` (absolute bounds check), the command is rejected
AND a command failing any check is returned as `CommandResult(status=CommandStatus.rejected, applied=False)` AND an audit event is emitted via `ObservabilityService.audit(event_type="CONSTRAINT", ...)` — this is MANDATORY, not conditional on log level

**AC3 — PolicyGuard dispatch and CommandResult**

GIVEN PolicyGuard dispatches an approved command
WHEN the adapter responds
THEN PolicyGuard calls `adapter.send_command(command)` wrapped in `asyncio.wait_for(..., timeout=10.0)` (NFR-I1)
AND the adapter returns a typed `CommandResult` with fields: `correlation_id`, `device_id`, `status` (one of `CommandStatus.success`, `CommandStatus.failed`, `CommandStatus.timeout`, `CommandStatus.rejected`), `applied` (bool), `reason` (str), `observed_state` (optional)
AND raw adapter exceptions do NOT propagate — all failures are caught and wrapped as `CommandResult(status=CommandStatus.failed)` (AR16)
AND adapter timeout is caught and returned as `CommandResult(status=CommandStatus.timeout, applied=False)` (not a raw exception)
AND `CommandResult.applied` is checked: if `False`, the control loop logs a warning; the system does NOT assume the device state has changed (FR14)

**AC4 — PolicyGuard is the single dispatch path (AR15)**

GIVEN any component outside Epic 8 needs to issue a device command
WHEN it attempts to do so
THEN it must call `PolicyGuard.authorize_and_dispatch()` — no adapter `send_command()` may be called from any other component directly

**AC5 — DeviceCommand model**

GIVEN any `DeviceCommand` is constructed
THEN it includes all required fields: `device_id`, `device_role`, `command_type` (discriminator literal), `origin` (one of `CommandOrigin.decision_engine`, `CommandOrigin.installer`, `CommandOrigin.homeowner`, `CommandOrigin.system`), `correlation_id` (UUID, auto-generated if not supplied)
AND command type-specific parameters are included in the subtype (e.g., `rate_kw` for charge/discharge commands)
AND `CommandResult.status` uses only defined `CommandStatus` enum values: `success`, `failed`, `timeout`, `rejected`

**AC6 — Control loop wiring**

GIVEN the control loop receives an `EvaluationResult` from `evaluate_cycle()`
WHEN `_handle_evaluation_result(result, snapshot)` is called
THEN it calls `IntentExecutor.translate(result, snapshot)` to get candidate commands
AND dispatches each command via `await PolicyGuard.authorize_and_dispatch(cmd)` — sequentially, one at a time
AND `self._last_mode = result.recommended_operating_mode` is set BEFORE dispatch (as before)
AND if `CommandResult.applied` is `False`, the control loop logs a `"command_not_applied"` warning with `device_id`, `status`, `reason`

**AC7 — app.py wiring**

GIVEN the application starts
WHEN the lifespan initializes the control loop
THEN `IntentExecutor()` (no-arg constructor) and `PolicyGuard(state_store=..., adapters={}, observability=ObservabilityService(), settings=settings)` are instantiated
AND `ControlLoop` is constructed with `intent_executor=intent_executor, policy_guard=policy_guard` in addition to existing args
AND no new database migrations are needed for this story

**AC8 — Tests**

Unit tests in `tests/unit/engine/test_policy_guard.py`:
- `test_constraint_violation_rejected_with_audit_event()` — `SetBatteryDischargeRateCommand` when battery SoC ≤ reserve floor → `CommandResult.status == CommandStatus.rejected` AND `ObservabilityService.audit()` called with `event_type="CONSTRAINT"`
- `test_capability_missing_blocks_send_command()` — adapter's `get_capabilities()` returns profile lacking required `WriteCapability` → `adapter.send_command()` is NEVER called
- `test_command_result_applied_false_is_not_treated_as_success()` — adapter returns `CommandResult(applied=False)` → control loop logs warning; no state mutation assumed
- `test_command_result_status_uses_enum_values()` — `CommandStatus` enum has exactly: `success`, `failed`, `timeout`, `rejected`
- `test_fail_safe_mode_rejects_all_commands()` — `SystemOperatingMode.fail_safe` → ALL commands rejected regardless of capability or constraints; no `send_command()` call

Unit tests in `tests/unit/engine/test_intent_executor.py`:
- `test_battery_hold_produces_no_command()` — `BatteryIntentAction.hold` → empty list
- `test_ev_hold_produces_no_command()` — `EVChargerIntentAction.hold` → empty list
- `test_battery_charge_produces_command()` — `BatteryIntentAction.charge` with valid snapshot → `SetBatteryChargeRateCommand`
- `test_ev_charge_produces_command()` — `EVChargerIntentAction.charge` → `SetEVChargingRateCommand`
- `test_ev_stop_produces_command()` — `EVChargerIntentAction.stop` → `StopEVChargingCommand`

Integration test in `tests/integration/engine/test_command_pipeline.py`:
- `test_full_pipeline_allowed_path()` — typed `BatteryIntent(charge)` → `IntentExecutor` → `SetBatteryChargeRateCommand` → `PolicyGuard` → simulated adapter → `CommandResult(status=success, applied=True)` → audit event NOT emitted (success path has no CONSTRAINT event)
- `test_full_pipeline_rejected_path()` — `SetBatteryDischargeRateCommand` with battery SoC at reserve floor → `PolicyGuard` → rejected → audit event with `event_type="CONSTRAINT"` IS emitted; `adapter.send_command()` NOT called

---

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — record baseline (774 tests passed)
  - [x] Run `uv run python -m ruff check .` (clean)
  - [x] Run `uv run python -m ruff format --check .` (clean: 153 files)
  - [x] Run `uv run python -m mypy src/` (clean: 74 source files)

- [x] Task 1: Create DeviceCommand model and CommandResult (AC5)
  - [x] Create `src/open_ems/core/commands.py` with `from __future__ import annotations`
  - [x] Define `CommandOrigin(enum.StrEnum)`: `decision_engine`, `installer`, `homeowner`, `system`
  - [x] Define `CommandStatus(enum.StrEnum)`: `success`, `failed`, `timeout`, `rejected`
  - [x] Define `DeviceCommandBase(BaseModel, frozen=True, extra="forbid")` with: `device_id: NonEmptyStr`, `device_role: DeviceRole`, `origin: CommandOrigin`, `correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)`
  - [x] Define subtypes using `Literal` discriminator: `SetBatteryChargeRateCommand`, `SetBatteryDischargeRateCommand`, `SetEVChargingRateCommand`, `StopEVChargingCommand` (see Dev Notes for exact fields)
  - [x] Define `DeviceCommand = SetBatteryChargeRateCommand | SetBatteryDischargeRateCommand | SetEVChargingRateCommand | StopEVChargingCommand` as type alias
  - [x] Define `CommandResult(BaseModel, frozen=True, extra="forbid")` with all fields (see Dev Notes)
  - [x] Add `send_command(self, cmd: DeviceCommand) -> CommandResult: ...` to `DeviceAdapter` protocol in `src/open_ems/core/devices.py` (update docstring — no longer "Epic 8 future")
  - [x] Export `CommandOrigin`, `CommandStatus`, `DeviceCommand`, `CommandResult` and all subtypes from `src/open_ems/core/__init__.py`
  - [x] Added `send_command` no-op stubs to existing adapters (BatteryAdapter, InverterAdapter, GridMeterAdapter, EVChargerAdapter) so they continue to satisfy the structural Protocol; updated `_ConcreteAdapter` in `tests/unit/core/test_devices.py` likewise

- [x] Task 2: Implement IntentExecutor (AC1)
  - [x] Create `src/open_ems/engine/intent_executor.py`
  - [x] Implement `IntentExecutor` class with no constructor args
  - [x] Implement `translate(self, result: EvaluationResult, snapshot: SystemSnapshot) -> list[DeviceCommand]`
  - [x] Battery hold → return nothing (skip); Battery charge → `SetBatteryChargeRateCommand`; Battery discharge → `SetBatteryDischargeRateCommand`
  - [x] EV hold → return nothing (skip); EV charge → `SetEVChargingRateCommand`; EV stop → `StopEVChargingCommand`
  - [x] Extract `device_id` from snapshot slot — if slot is not `BatteryState`/`EVChargerState`, skip that intent (covers `None` and `DegradedDeviceState`)
  - [x] Set `origin=CommandOrigin.decision_engine` on all produced commands
  - [x] Set `rate_kw` from `BatteryIntent.target_power_kw` (fallback `0.0` when `None`); same pattern for EV `target_charge_rate_kw`

- [x] Task 3: Implement PolicyGuard (AC2, AC3, AC4)
  - [x] Create `src/open_ems/engine/policy_guard.py`
  - [x] Constructor: `__init__(self, *, state_store, adapters, observability, settings)`
  - [x] Implement `async def authorize_and_dispatch(self, command) -> CommandResult`
  - [x] Check 1: Operating mode — `fail_safe` → reject + audit (before adapter lookup)
  - [x] Check 2: Adapter exists for `command.device_role` — if missing → reject + audit
  - [x] Check 3: Capability — `asyncio.wait_for(adapter.get_capabilities(), timeout=5.0)`; missing or exception → reject + audit
  - [x] Check 4: Safety constraints — battery reserve floor (discharge), peak limit bounds (charge/EV rate), conservative-mode load suppression
  - [x] Dispatch: `asyncio.wait_for(adapter.send_command(command), timeout=COMMAND_DISPATCH_TIMEOUT_SECONDS)` (10.0 s)
  - [x] Wrap all adapter exceptions as `CommandResult(status=failed)` — never propagate
  - [x] Wrap `TimeoutError` as `CommandResult(status=timeout, applied=False)`
  - [x] Private `async def _reject(self, command, reason)` emits audit + returns rejected CommandResult
  - [x] Private `_required_write_capability(command)` helper
  - [x] Private `_check_safety_constraints(command, snapshot)` helper returning rejection reason or `None`

- [x] Task 4: Update control loop (AC6)
  - [x] Add `intent_executor` and `policy_guard` kwargs to `ControlLoop.__init__`
  - [x] `_handle_evaluation_result` is now `async` and takes `(result, snapshot)`
  - [x] Body translates intents and dispatches each via PolicyGuard
  - [x] Log `"command_not_applied"` warning when `cmd_result.applied is False`
  - [x] `_tick()` awaits `_handle_evaluation_result(result, snapshot)` — snapshot is reused, no second `get_snapshot()`
  - [x] Add imports for `IntentExecutor` and `PolicyGuard`

- [x] Task 5: Update app.py (AC7)
  - [x] Import `IntentExecutor`, `PolicyGuard`, `ObservabilityService`
  - [x] Instantiate `intent_executor = IntentExecutor()`
  - [x] Instantiate `policy_guard = PolicyGuard(state_store=..., adapters={}, observability=ObservabilityService(), settings=settings)`
  - [x] Pass them to `ControlLoop(...)`
  - [x] No new migration needed

- [x] Task 6: Update `engine/__init__.py`
  - [x] Export `IntentExecutor`
  - [x] Export `PolicyGuard`
  - [x] Added to `__all__`

- [x] Task 7: Write unit tests (AC8)
  - [x] `tests/unit/engine/test_policy_guard.py` — 14 tests (5 from AC8 + 9 covering peak/conservative/timeout/exception/adapter-missing/cap-fail/success/EV-stop/wait_for-timeout)
  - [x] `tests/unit/engine/test_intent_executor.py` — 8 tests (5 from AC8 + 3 covering missing slot, None target rate, ev_charge intent)
  - [x] Used `AsyncMock` for adapter and `ObservabilityService.audit`
  - [x] Used real `StateStore` (not mocked)
  - [x] Used `Settings(_env_file=None)`
  - [x] No `@pytest.mark.asyncio` decorators (asyncio_mode="auto")
  - [x] Updated `tests/unit/engine/test_control_loop.py::_control_loop()` helper to inject `IntentExecutor` and a mocked `PolicyGuard`

- [x] Task 8: Write integration test (AC8)
  - [x] `tests/integration/engine/__init__.py` (empty)
  - [x] `tests/integration/engine/test_command_pipeline.py` — 2 tests (allowed + rejected paths)
  - [x] `SimulatedBatteryAdapter` implements full `DeviceAdapter` protocol incl. `send_command`
  - [x] Audit event verified on rejected path; verified absent on success path

- [x] Task 9: Final validation (AC: all)
  - [x] Full suite passes: 798 tests (774 baseline + 24 new)
  - [x] `uv run python -m ruff check .` clean
  - [x] `uv run python -m ruff format --check .` clean (160 files)
  - [x] `uv run python -m mypy src/` clean (77 source files)

---

## Dev Notes

### Command Model Design (`src/open_ems/core/commands.py`)

**CRITICAL**: Import `NonEmptyStr`, `DeviceRole`, `WriteCapability` from `open_ems.core.devices` — do NOT redefine them.

```python
from __future__ import annotations

import enum
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from open_ems.core.devices import DeviceRole, NonEmptyStr

# Forward ref only — CommandResult.observed_state uses DeviceState | DegradedDeviceState | None
# Import at bottom of file inside TYPE_CHECKING to avoid circular import if needed
```

**Command subtypes** — exact fields required:

```python
class DeviceCommandBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: NonEmptyStr
    device_role: DeviceRole
    origin: CommandOrigin
    correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)

class SetBatteryChargeRateCommand(DeviceCommandBase):
    command_type: Literal["set_battery_charge_rate"] = "set_battery_charge_rate"
    rate_kw: Annotated[float, Field(ge=0.0)]

class SetBatteryDischargeRateCommand(DeviceCommandBase):
    command_type: Literal["set_battery_discharge_rate"] = "set_battery_discharge_rate"
    rate_kw: Annotated[float, Field(ge=0.0)]

class SetEVChargingRateCommand(DeviceCommandBase):
    command_type: Literal["set_ev_charging_rate"] = "set_ev_charging_rate"
    rate_kw: Annotated[float, Field(ge=0.0)]

class StopEVChargingCommand(DeviceCommandBase):
    command_type: Literal["stop_ev_charging"] = "stop_ev_charging"

DeviceCommand = (
    SetBatteryChargeRateCommand
    | SetBatteryDischargeRateCommand
    | SetEVChargingRateCommand
    | StopEVChargingCommand
)

class CommandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: uuid.UUID
    device_id: NonEmptyStr
    status: CommandStatus
    applied: bool
    reason: str
    observed_state: object = None  # DeviceState | DegradedDeviceState | None
```

**CommandResult.status values (from epics AC)**: `success`, `failed`, `timeout`, `rejected`
**Architecture pattern says**: `accepted / rejected / failed / timed_out` — IGNORE the architecture, use the EPIC story values: `success`, `failed`, `timeout`, `rejected`

**DeviceAdapter update** (`src/open_ems/core/devices.py:262`):

```python
@runtime_checkable
class DeviceAdapter(Protocol):
    """Structural contract for all domain-level device adapters."""
    device_id: str
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def get_state(self) -> DeviceState | DegradedDeviceState: ...
    async def get_capabilities(self) -> DeviceCapabilityProfile: ...
    async def send_command(self, cmd: "DeviceCommand") -> "CommandResult": ...  # Added in Epic 8
```

Use quoted strings for forward references in the Protocol since `DeviceCommand`/`CommandResult` will be in `core/commands.py` (same package). Update the class docstring: remove "send_command() is added in Epic 8" note — it's now implemented.

### IntentExecutor Design (`src/open_ems/engine/intent_executor.py`)

```python
from __future__ import annotations

import structlog
from open_ems.core import BatteryState, DeviceRole, EVChargerState, SystemSnapshot
from open_ems.core.commands import (CommandOrigin, DeviceCommand,
    SetBatteryChargeRateCommand, SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand, StopEVChargingCommand)
from open_ems.engine.result import EvaluationResult
from open_ems.engine.rules.battery_control import BatteryIntent, BatteryIntentAction
from open_ems.engine.rules.ev_scheduling import EVChargerIntent, EVChargerIntentAction

logger = structlog.get_logger(__name__)

class IntentExecutor:
    """Translates typed decision-engine intents to candidate DeviceCommands."""

    def translate(
        self,
        result: EvaluationResult,
        snapshot: SystemSnapshot,
    ) -> list[DeviceCommand]:
        commands: list[DeviceCommand] = []
        for intent in result.intents:
            if isinstance(intent, BatteryIntent):
                cmd = self._translate_battery(intent, snapshot)
                if cmd is not None:
                    commands.append(cmd)
            elif isinstance(intent, EVChargerIntent):
                cmd = self._translate_ev(intent, snapshot)
                if cmd is not None:
                    commands.append(cmd)
        return commands
```

**Battery translation rules:**
- `hold` → return `None` (no command)
- `charge` → `SetBatteryChargeRateCommand(device_id=battery_state.device_id, device_role=DeviceRole.battery, origin=CommandOrigin.decision_engine, rate_kw=intent.target_power_kw or 0.0)` — if `snapshot.battery` is not `BatteryState`, log debug and return `None`
- `discharge` → `SetBatteryDischargeRateCommand(device_id=battery_state.device_id, device_role=DeviceRole.battery, origin=CommandOrigin.decision_engine, rate_kw=intent.target_power_kw or 0.0)`

**EV translation rules:**
- `hold` → return `None` (no command)
- `charge` → `SetEVChargingRateCommand(device_id=ev_state.device_id, device_role=DeviceRole.ev_charger, origin=CommandOrigin.decision_engine, rate_kw=intent.target_charge_rate_kw or 0.0)` — if `snapshot.ev_charger` is not `EVChargerState`, log debug and return `None`
- `stop` → `StopEVChargingCommand(device_id=ev_state.device_id, device_role=DeviceRole.ev_charger, origin=CommandOrigin.decision_engine)`

**`rate_kw=None` handling**: `BatteryIntent.target_power_kw` can be `None` for hold-like cases where the engine doesn't specify a rate. Use `0.0` as fallback. `rate_kw: Field(ge=0.0)` accepts `0.0`.

**ALLOWED imports for `intent_executor.py`:**
- `open_ems.core` — `BatteryState`, `DeviceRole`, `EVChargerState`, `SystemSnapshot`
- `open_ems.core.commands` — all command types
- `open_ems.engine.result` — `EvaluationResult`
- `open_ems.engine.rules.battery_control` — `BatteryIntent`, `BatteryIntentAction`
- `open_ems.engine.rules.ev_scheduling` — `EVChargerIntent`, `EVChargerIntentAction`
- `structlog`

### PolicyGuard Design (`src/open_ems/engine/policy_guard.py`)

```python
COMMAND_DISPATCH_TIMEOUT_SECONDS: float = 10.0
CAPABILITY_CHECK_TIMEOUT_SECONDS: float = 5.0
```

**Capability → WriteCapability mapping:**

```python
from open_ems.core.devices import WriteCapability
from open_ems.core.commands import (SetBatteryChargeRateCommand, SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand, StopEVChargingCommand, DeviceCommand)

def _required_write_capability(command: DeviceCommand) -> WriteCapability:
    if isinstance(command, SetBatteryChargeRateCommand):
        return WriteCapability.set_charge_rate
    if isinstance(command, SetBatteryDischargeRateCommand):
        return WriteCapability.set_discharge_rate
    if isinstance(command, (SetEVChargingRateCommand, StopEVChargingCommand)):
        return WriteCapability.set_ev_charge_current
    raise TypeError(f"Unhandled command type: {type(command).__name__}")
```

**Safety constraint checks** (in `_check_safety_constraints(command, snapshot) -> str | None`):
1. Battery reserve floor: if `isinstance(command, SetBatteryDischargeRateCommand)` AND `isinstance(snapshot.battery, BatteryState)` AND `snapshot.battery.soc_percent <= self._settings.battery_reserve_floor_percent` → return `"battery_soc_at_or_below_reserve_floor"`
2. Peak limit bounds: if `isinstance(command, (SetBatteryChargeRateCommand, SetEVChargingRateCommand))` AND `command.rate_kw > self._settings.peak_limit_kw` → return `"commanded_rate_exceeds_peak_limit"`
3. Conservative mode suppression: if `snapshot.operating_mode == SystemOperatingMode.conservative` AND `isinstance(command, (SetBatteryDischargeRateCommand, SetEVChargingRateCommand))` → return `"conservative_mode_blocks_load_increase"` (per BAD-2: no discharge or EV charge in conservative mode; battery charge OK)

**`_reject()` helper pattern:**
```python
async def _reject(self, command: DeviceCommand, reason: str) -> CommandResult:
    await self._observability.audit(
        actor="system",
        event_type="CONSTRAINT",
        summary=f"Command rejected: {reason}",
        device_id=command.device_id,
    )
    return CommandResult(
        correlation_id=command.correlation_id,
        device_id=command.device_id,
        status=CommandStatus.rejected,
        applied=False,
        reason=reason,
    )
```

**Full `authorize_and_dispatch` flow:**
```
1. snapshot = self._state_store.get_snapshot()
2. if snapshot.operating_mode == fail_safe → _reject(..., "fail_safe_mode_active")
3. adapter = self._adapters.get(command.device_role); if None → _reject(..., "adapter_not_registered")
4. try: profile = await asyncio.wait_for(adapter.get_capabilities(), CAPABILITY_CHECK_TIMEOUT_SECONDS)
   except → _reject(..., "capability_check_failed")
5. required = _required_write_capability(command)
   if required not in profile.write_capabilities → _reject(..., "capability_missing: {required.value}")
6. constraint_violation = _check_safety_constraints(command, snapshot)
   if constraint_violation → _reject(..., constraint_violation)
7. try:
       result = await asyncio.wait_for(adapter.send_command(command), COMMAND_DISPATCH_TIMEOUT_SECONDS)
       return result
   except asyncio.TimeoutError:
       return CommandResult(status=CommandStatus.timeout, applied=False, reason="command_timeout", ...)
   except Exception as exc:
       return CommandResult(status=CommandStatus.failed, applied=False, reason=repr(exc), ...)
```

**IMPORTANT**: `get_snapshot()` is synchronous (not async) — do NOT await it. Verify in `src/open_ems/core/state_store.py` before calling.

**ALLOWED imports for `policy_guard.py`:**
- `open_ems.core` — `StateStore`, `DeviceAdapter`, `DeviceRole`, `BatteryState`, `SystemOperatingMode`, `WriteCapability`, `SystemSnapshot`
- `open_ems.core.commands` — all command/result types
- `open_ems.engine.result` — NOT needed; no EvaluationResult reference here
- `open_ems.services.audit_log` — `ObservabilityService`
- `open_ems.settings` — `Settings`
- stdlib: `asyncio`, `collections.abc`, `typing`
- `structlog`
- FORBIDDEN: `open_ems.web`, `open_ems.adapters`, `open_ems.storage`

### Control Loop Update (`src/open_ems/engine/control_loop.py`)

Add to `__init__`:
```python
def __init__(
    self,
    *,
    state_store: StateStore,
    adapters: Mapping[DeviceRole, DeviceAdapter],
    energy_repo: EnergyRepo,
    settings: Settings,
    intent_executor: IntentExecutor,
    policy_guard: PolicyGuard,
) -> None:
    ...
    self._intent_executor = intent_executor
    self._policy_guard = policy_guard
```

Update `_tick()` — the `snapshot` variable already exists from `await self._state_store.publish(...)`:
```python
# After: result = evaluate_cycle(evaluation_input)
await self._handle_evaluation_result(result, snapshot)
```

Replace `_handle_evaluation_result` stub:
```python
async def _handle_evaluation_result(
    self,
    result: EvaluationResult,
    snapshot: SystemSnapshot,
) -> None:
    self._last_mode = result.recommended_operating_mode
    logger.debug(
        "evaluation_cycle_complete",
        cycle_id=str(result.cycle_id),
        operating_mode=result.recommended_operating_mode.value,
        cycle_duration_ms=result.cycle_duration_ms,
        intent_count=len(result.intents),
        component="engine",
    )
    commands = self._intent_executor.translate(result, snapshot)
    for cmd in commands:
        cmd_result = await self._policy_guard.authorize_and_dispatch(cmd)
        if not cmd_result.applied:
            logger.warning(
                "command_not_applied",
                device_id=cmd_result.device_id,
                status=cmd_result.status.value,
                reason=cmd_result.reason,
                component="engine",
            )
```

**CRITICAL**: The `snapshot` in `_tick()` is returned from `await self._state_store.publish(...)` at the top of `_tick()`. It's already in scope — do NOT call `state_store.get_snapshot()` a second time in `_tick()` (potential race-free because PolicyGuard calls its own `get_snapshot()` internally for its checks, which is fine and consistent).

### app.py Wiring Pattern

Follow the existing pattern — construct objects in lifespan after database and StateStore init:

```python
# Existing imports (add to list):
from open_ems.engine.intent_executor import IntentExecutor
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.services.audit_log import ObservabilityService  # may already be imported

# After ControlLoop-related setup block:
intent_executor = IntentExecutor()
policy_guard = PolicyGuard(
    state_store=app.state.state_store,
    adapters={},   # Epic 9 installer setup will populate
    observability=ObservabilityService(),
    settings=settings,
)

# Update ControlLoop instantiation to add new args:
_control_loop = ControlLoop(
    state_store=app.state.state_store,
    adapters={},
    energy_repo=EnergyRepo(),
    settings=settings,
    intent_executor=intent_executor,
    policy_guard=policy_guard,
)
```

Check if `ObservabilityService` is already imported in `app.py` before adding the import.

### Testing Approach

**`asyncio_mode = "auto"`** in `pyproject.toml` — no `@pytest.mark.asyncio` decorators needed. All async test functions run automatically.

**PolicyGuard test setup pattern:**

```python
from unittest.mock import AsyncMock, MagicMock

from open_ems.core import DeviceRole, SystemOperatingMode
from open_ems.core.commands import CommandResult, CommandStatus, SetBatteryDischargeRateCommand, ...
from open_ems.core.devices import BatteryState, DeviceCapabilityProfile, WriteCapability
from open_ems.core.state_store import StateStore
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings

def _state_store_with_mode(mode: SystemOperatingMode) -> StateStore:
    ss = StateStore(system_clock_status="valid")
    # publish a battery state to set snapshot — or leave empty for no-device tests
    return ss

def _mock_adapter(
    *,
    device_id: str = "bat-001",
    write_caps: frozenset[WriteCapability] = frozenset({WriteCapability.set_discharge_rate}),
    send_command_result: CommandResult | None = None,
) -> MagicMock:
    adapter = MagicMock()
    adapter.device_id = device_id
    adapter.get_capabilities = AsyncMock(return_value=DeviceCapabilityProfile(
        device_id=device_id,
        model="TestBattery",
        write_capabilities=write_caps,
    ))
    if send_command_result is not None:
        adapter.send_command = AsyncMock(return_value=send_command_result)
    else:
        adapter.send_command = AsyncMock(return_value=CommandResult(
            correlation_id=uuid.uuid4(),
            device_id=device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        ))
    return adapter
```

**Integration test pattern** — use a full in-process `PolicyGuard` with a `SimulatedBatteryAdapter` that implements ALL `DeviceAdapter` methods:

```python
class SimulatedBatteryAdapter:
    device_id = "bat-001"
    
    async def connect(self) -> None: pass
    async def disconnect(self) -> None: pass
    async def get_state(self) -> ...: ...
    async def get_capabilities(self) -> DeviceCapabilityProfile: ...
    async def send_command(self, cmd: DeviceCommand) -> CommandResult: ...
```

The integration test verifies the FULL pipeline without any mocking — use real `IntentExecutor`, real `PolicyGuard`, real `ObservabilityService` (with `AsyncMock` for the underlying repo), and a real `StateStore`.

### SystemSnapshot Access for PolicyGuard

`StateStore.get_snapshot()` — verify it is SYNCHRONOUS in `src/open_ems/core/state_store.py:get_snapshot()` before writing PolicyGuard. Do NOT call `await state_store.get_snapshot()`.

`SystemSnapshot.operating_mode` — verify the field name in `src/open_ems/core/state.py`. The snapshot may expose operating mode via `snapshot.operating_mode` or via `snapshot.global_state.operating_mode` — check actual field names before writing PolicyGuard constraint checks. **Do not guess — read the file.**

`SystemSnapshot.battery` — returns `BatteryState | DegradedDeviceState | None`. In PolicyGuard's constraint check, guard with `isinstance(snapshot.battery, BatteryState)` before accessing `soc_percent`.

### Deferred Items (Do NOT implement in this story)

- Retry policy (Story 8.3): retrying failed/timeout commands is Story 8.3 scope
- Watchdog fail-safe escalation (Story 8.4): `PolicyGuard` rejecting all commands in `fail_safe` is the required behavior; the ENTRY into fail-safe mode from loop death is Story 8.4
- Conservative mode EV charge suppression: BAD-2 specifies "no new EV charge commands" in conservative mode; this story adds conservative mode check for discharge and EV charge (per constraint check #3 above); Story 8.4 will harden this further
- `SimulatedPolicyGuard` for other test modules: architecture mentions it; create only when needed in future stories
- `config_repo.py` for active constraints: not needed for Story 8.2; Settings provides peak_limit_kw and battery_reserve_floor_percent for now; Epic 9 installer flow will add DB-backed constraints that PolicyGuard reloads

### Project Structure Notes

```
src/open_ems/
├── core/
│   ├── commands.py          ← NEW: DeviceCommand + subtypes, CommandResult, CommandStatus, CommandOrigin
│   ├── devices.py           ← MODIFY: add send_command() to DeviceAdapter protocol
│   └── __init__.py          ← MODIFY: export command types
├── engine/
│   ├── intent_executor.py   ← NEW: IntentExecutor class
│   ├── policy_guard.py      ← NEW: PolicyGuard class
│   ├── control_loop.py      ← MODIFY: add fields, async _handle_evaluation_result
│   └── __init__.py          ← MODIFY: export IntentExecutor, PolicyGuard
└── web/
    └── app.py               ← MODIFY: instantiate IntentExecutor + PolicyGuard; update ControlLoop call

tests/
├── unit/engine/
│   ├── test_policy_guard.py    ← NEW: 5 unit tests
│   └── test_intent_executor.py ← NEW: 5 unit tests
└── integration/engine/
    ├── __init__.py             ← NEW: empty
    └── test_command_pipeline.py ← NEW: 2 integration tests
```

**CRITICAL**: architecture confirms `engine/policy_guard.py` — this is the mandated path. Do NOT put PolicyGuard in `services/` or `core/`.

**EXISTING test files that will be affected**: `tests/unit/engine/test_control_loop.py` uses `_control_loop()` helper. After this story, `ControlLoop.__init__` gains two new required args (`intent_executor`, `policy_guard`). Update `_control_loop()` helper in `test_control_loop.py` to pass `IntentExecutor()` and a `MagicMock()` (or real) `PolicyGuard`. Do NOT break existing 7 tests.

### References

- [Source: epics.md — Story 8.2 full AC text, lines 1737–1775]
- [Source: epics.md — Epic 8 preamble, Story 8.3 cross-story constraint (AR15), lines 1695–1860]
- [Source: architecture.md — Safety-Critical Patterns: PolicyGuard (lines 381–433)]
- [Source: architecture.md — DeviceAdapter protocol with send_command, CommandResult pattern (lines 685–734)]
- [Source: architecture.md — Project structure: engine/policy_guard.py, core/commands.py (lines 795–865)]
- [Source: architecture.md — Import boundary rules (lines 1028–1036)]
- [Source: architecture.md — BAD-2: Degradation matrix (conservative mode: no EV charge, no battery discharge) (lines 1080–1107)]
- [Source: architecture.md — BAD-3: Constraint change flow, PolicyGuard reads active_constraints (lines 1110–1151)]
- [Source: src/open_ems/engine/control_loop.py — _handle_evaluation_result stub to replace (lines 145–155)]
- [Source: src/open_ems/engine/result.py — EvaluationResult.intents: tuple[BatteryIntent | EVChargerIntent, ...]]
- [Source: src/open_ems/engine/rules/battery_control.py — BatteryIntent, BatteryIntentAction enum values]
- [Source: src/open_ems/engine/rules/ev_scheduling.py — EVChargerIntent, EVChargerIntentAction enum values]
- [Source: src/open_ems/core/devices.py:193 — WriteCapability enum values: set_charge_rate, set_discharge_rate, set_operating_mode, set_ev_charge_current]
- [Source: src/open_ems/services/audit_log.py — ObservabilityService.audit() signature, VALID_EVENT_TYPES includes "CONSTRAINT"]
- [Source: src/open_ems/settings.py — peak_limit_kw, battery_reserve_floor_percent fields (added in 8.1)]
- [Source: tests/unit/engine/test_control_loop.py — _control_loop() helper pattern to update; asyncio_mode="auto" pattern]
- [Source: 8-1 story Dev Agent Record — existing test pattern, review findings (lines 432–478)]
- [Source: deferred-work.md — 8-1 deferred items not to address in this story]

---

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Claude Opus 4.7) — bmad-dev-story workflow

### Debug Log References

- Pre-story baseline: 774 tests pass; ruff/format/mypy clean.
- Final validation: 798 tests pass; ruff/format/mypy clean (160 files formatted, 77 source files type-checked).

### Completion Notes List

- Two-stage pipeline implemented exactly as specified. `IntentExecutor` is purely translational (no side effects, no I/O) and produces no command for hold intents. `PolicyGuard.authorize_and_dispatch()` is the sole legal call site for `adapter.send_command()` (AR15).
- PolicyGuard order of checks: fail_safe mode → adapter registration → capability lookup (5 s timeout) → required `WriteCapability` present → safety constraints (battery reserve floor, peak limit bounds, conservative-mode load suppression) → dispatch with 10 s timeout. Every rejection emits a CONSTRAINT audit event via `ObservabilityService.audit()` (mandatory).
- Adapter exceptions are wrapped as `CommandResult(status=failed)`; `TimeoutError` is wrapped as `CommandResult(status=timeout, applied=False)`. Raw exceptions never propagate to the control loop (AR16). Per ruff UP041, the implementation uses the builtin `TimeoutError` (alias of `asyncio.TimeoutError` in 3.11+).
- Adding `send_command()` to the `DeviceAdapter` Protocol is a structural change. To preserve `isinstance(adapter, DeviceAdapter)` guarantees from Epic 4, no-op `send_command()` stubs (raise `NotImplementedError`) were added to `BatteryAdapter`, `InverterAdapter`, `GridMeterAdapter`, and `EVChargerAdapter`. The DSMR meter is read-only by design; the others will receive real implementations during Epic 9 installer setup. The protocol's docstring no longer says "send_command() is added in Epic 8".
- `core/devices.py` uses a `TYPE_CHECKING` import for `DeviceCommand`/`CommandResult` to keep the Protocol annotations resolvable for type checkers while avoiding a runtime circular import with `core/commands.py`. With `from __future__ import annotations` already in place, no string forward-refs are needed.
- Control loop's `_tick` reuses the snapshot returned by `state_store.publish(...)` and passes it through to `_handle_evaluation_result` rather than calling `get_snapshot()` a second time. PolicyGuard does its own `get_snapshot()` internally for safety checks — that is by design and is fine.
- Existing `test_control_loop.py::_control_loop()` helper updated to inject `IntentExecutor()` and a `MagicMock(spec=PolicyGuard)`. All 7 prior control-loop tests still pass without modification.
- 24 new tests added (8 `test_intent_executor.py` + 14 `test_policy_guard.py` + 2 `test_command_pipeline.py`). The integration test uses a `SimulatedBatteryAdapter` that satisfies the full structural protocol, verifying audit emission on rejection and the absence of CONSTRAINT events on success.
- `Settings` exposes `peak_limit_kw` and `battery_reserve_floor_percent` (added in 8.1). PolicyGuard reads from these via the injected `Settings`. DB-backed active-constraints reload is deferred to Epic 9.
- No new database migrations needed.

### File List

**New:**
- `src/open_ems/core/commands.py`
- `src/open_ems/engine/intent_executor.py`
- `src/open_ems/engine/policy_guard.py`
- `tests/unit/engine/test_intent_executor.py`
- `tests/unit/engine/test_policy_guard.py`
- `tests/integration/engine/__init__.py`
- `tests/integration/engine/test_command_pipeline.py`

**Modified:**
- `src/open_ems/core/__init__.py` — export command/result types and subtypes
- `src/open_ems/core/devices.py` — add `send_command()` to `DeviceAdapter` protocol; updated docstring; TYPE_CHECKING import for forward refs
- `src/open_ems/engine/__init__.py` — export `IntentExecutor`, `PolicyGuard`
- `src/open_ems/engine/control_loop.py` — add `intent_executor`, `policy_guard` kwargs; `_handle_evaluation_result` is now `async` and takes the published snapshot
- `src/open_ems/web/app.py` — instantiate `IntentExecutor`, `PolicyGuard`, `ObservabilityService`; pass to `ControlLoop`
- `src/open_ems/adapters/modbus/battery_adapter.py` — `send_command` no-op stub
- `src/open_ems/adapters/modbus/inverter_adapter.py` — `send_command` no-op stub
- `src/open_ems/adapters/dsmr/meter_adapter.py` — `send_command` no-op stub (read-only meter)
- `src/open_ems/adapters/ocpp/charger_adapter.py` — `send_command` no-op stub
- `tests/unit/core/test_devices.py` — `_ConcreteAdapter` updated with `send_command`
- `tests/unit/engine/test_control_loop.py` — `_control_loop` helper accepts and defaults `intent_executor`, `policy_guard`

### Review Findings

- [x] [Review][Patch] `_reject()` audit exception propagates uncaught — AR16 violation [src/open_ems/engine/policy_guard.py:154-167]
- [x] [Review][Patch] TypeError from unhandled intent/command type crashes control loop [src/open_ems/engine/intent_executor.py:47, src/open_ems/engine/policy_guard.py:89,170-177]
- [x] [Review][Patch] `capability_missing` reason string missing space after colon — spec deviation [src/open_ems/engine/policy_guard.py:91]
- [x] [Review][Patch] Timeout reason uses `"command_dispatch_timeout"` instead of spec's `"command_timeout"` [src/open_ems/engine/policy_guard.py:114]
- [x] [Review][Patch] Timeout test mutates module-level constant without `mock.patch` [tests/unit/engine/test_policy_guard.py:475]
- [x] [Review][Patch] `test_command_result_applied_false_is_not_treated_as_success` doesn't verify control loop warning [tests/unit/engine/test_policy_guard.py:200]
- [x] [Review][Defer] Control loop crash not restarted after unhandled exception [src/open_ems/engine/control_loop.py, src/open_ems/web/app.py] — deferred, Story 8.4 scope
- [x] [Review][Defer] First tick publishes `operating_mode=None` — pre-existing from Story 8.1 [src/open_ems/engine/control_loop.py:73-76] — deferred, pre-existing
- [x] [Review][Defer] `adapters={}` in production silently rejects all commands — intentional, Epic 9 populates [src/open_ems/web/app.py:223-236] — deferred, intentional design
- [x] [Review][Defer] Degraded grid meter returns 0.0 to tracker — pre-existing from Story 8.1 [src/open_ems/engine/control_loop.py] — deferred, pre-existing
- [x] [Review][Defer] Timeout constants (`CAPABILITY_CHECK_TIMEOUT_SECONDS`, `COMMAND_DISPATCH_TIMEOUT_SECONDS`) not exposed via Settings [src/open_ems/engine/policy_guard.py:44-45] — deferred, spec prescribes module-level
- [x] [Review][Defer] PolicyGuard re-fetches snapshot independently — theoretical stale-read in current sequential arch [src/open_ems/engine/policy_guard.py:67] — deferred, pre-existing
- [x] [Review][Defer] Discharge command bypasses SoC floor check when battery state is unavailable (DegradedDeviceState/None) [src/open_ems/engine/policy_guard.py:137-141] — deferred, Epic 9 safety concern (IntentExecutor prevents this path currently)
- [x] [Review][Defer] Constraint ordering emits wrong audit reason for conservative+below-floor case [src/open_ems/engine/policy_guard.py:137-150] — deferred, matches spec Dev Notes order exactly
- [x] [Review][Defer] `correlation_id` mismatch from adapter `CommandResult` passed through unchecked [src/open_ems/engine/policy_guard.py:97-100] — deferred, adapter contract concern for Epic 9
- [x] [Review][Defer] Peak limit check uses strict `>` — `rate_kw == peak_limit_kw` passes [src/open_ems/engine/policy_guard.py:143-145] — deferred, spec says "exceeds" = strict greater than
- [x] [Review][Defer] Zero-rate commands (`rate_kw=0.0`) produced and dispatched [src/open_ems/engine/intent_executor.py:66] — deferred, intentional per spec (rate_kw=0.0 is valid by ge=0.0)

### Change Log

- 2026-05-06: Implemented two-stage command pipeline (Story 8.2). Added `core/commands.py` with `DeviceCommand`/`CommandResult` types; added `send_command()` to `DeviceAdapter` Protocol with stubs in existing adapters; created `IntentExecutor` (intent-to-command translation) and `PolicyGuard` (mandatory safety gate with capability/constraint enforcement, exception wrapping, and CONSTRAINT audit emission); wired them into `ControlLoop` and `app.py`. 24 new tests; full suite 798 passing; ruff/format/mypy clean.
