# Story 8.5: End-to-End Decision Engine Simulation

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As an installer or developer validating a newly configured site,
I want to run a scripted end-to-end simulation of the decision engine against a synthetic energy scenario,
so that I can confirm the full control path — from grid reading through evaluation through PolicyGuard dispatch — behaves correctly before live operation begins, with every command emitting a traceable `event_log` row tied to its `correlation_id`.

## Acceptance Criteria

**AC1 — DECISION audit event emitted per dispatched command**

GIVEN `RetryPolicy.execute(command)` returns a `CommandResult` with `status=CommandStatus.success`
WHEN the success path completes (any attempt: first try OR after one or more retries)
THEN exactly ONE `ObservabilityService.audit()` call is made with:
- `actor="system"`
- `event_type="DECISION"`
- `device_id=command.device_id`
- `summary` is a plain-language string: `f"Command {type(command).__name__} for {command.device_id} dispatched: success (applied={result.applied}) after {attempts} attempt(s)"`
- `detail={"correlation_id": str(command.correlation_id), "command_type": type(command).__name__, "command_status": "success", "applied": result.applied, "attempts": attempts}`
AND audit emission is wrapped in the same `try/except` shielding pattern as `RetryPolicy._emit_failure_audit` at `src/open_ems/engine/retry_policy.py:131-148` (failure logs `audit_emit_failed` and continues — the structured success log line is the floor)
AND a single `logger.info("command_dispatched", device_id=..., command_type=..., correlation_id=..., attempts=..., component="engine")` is emitted in the same path (this is the structured-log fallback if the audit DB write fails)
AND the success-audit emit point is at the end of the success branch — AFTER `result.status is CommandStatus.success` is confirmed and BEFORE `return result` — so a partial-success early return cannot bypass the audit
AND the existing terminal-failure `_emit_failure_audit` path (`event_type="DEVICE"`) is UNCHANGED — DEVICE and DECISION are mutually exclusive: a command that fails after retries emits ONE DEVICE audit; a command that succeeds emits ONE DECISION audit; never both, never neither
AND the cancellation path from Story 8.4 AC7 (`event_type="DEVICE"`, `summary` containing `"cancelled mid-retry"`) is UNCHANGED — cancelled mid-flight does NOT emit a DECISION audit (the result is indeterminate by definition)

**Rationale:** Today `RetryPolicy.execute()` only emits an audit on terminal failure (DEVICE) or cancellation (DEVICE). Successful dispatches leave no `event_log` row, so simulation tests cannot assert per-command traceability by `correlation_id`. Architecture decision 5.1 (lines 312–316 of `architecture.md`) explicitly says "Control decisions" go to the audit_log; that gap closes here. The emission point is `RetryPolicy` (not `PolicyGuard` and not `IntentExecutor`) because RetryPolicy is the only layer that knows the *terminal* outcome after all attempts.

**AC2 — Simulation scenario fixture infrastructure (`tests/fixtures/scenarios.py`)**

GIVEN integration tests need parameterisable end-to-end scenarios
THEN `tests/fixtures/scenarios.py` is created (NEW file) and exposes:

```python
@dataclass(frozen=True)
class SimulationScenario:
    """Parameterisable scenario for end-to-end decision-engine simulation."""

    name: str                                  # e.g., "peak_tariff_curtail_load"
    grid_power_kw: float                       # GridMeterState.grid_power_kw (sign per architecture.md:489-493)
    pv_power_kw: float                         # InverterState.pv_power_kw (≥ 0)
    inverter_ac_power_kw: float                # InverterState.ac_power_kw
    battery_soc_percent: float                 # BatteryState.soc_percent (0.0–100.0)
    battery_capacity_kwh: float                # BatteryState.capacity_kwh
    ev_session_active: bool                    # EVChargerState.session_active
    ev_current_power_kw: float | None          # EVChargerState.current_power_kw; None when no session
    strategy: EnergyStrategy                   # active strategy
    peak_limit_kw: float                       # configured peak limit (Settings.peak_limit_kw override)
    battery_reserve_floor_percent: float       # Settings.battery_reserve_floor_percent override
    homeowner_override_active: bool = False    # EV homeowner override flag
    ev_charging_window: EVChargingWindow | None = None  # local-time EV window (None = no window)
    target_ev_charge_rate_kw: float | None = None       # optional dynamic-rate target


def build_scenario_state_store(scenario: SimulationScenario) -> StateStore: ...
def build_scenario_adapters(scenario: SimulationScenario) -> dict[DeviceRole, DeviceAdapter]: ...
def build_scenario_settings(scenario: SimulationScenario) -> Settings: ...
def build_scenario_evaluation_input(
    scenario: SimulationScenario,
    snapshot: SystemSnapshot,
    *,
    at: datetime,
) -> EvaluationInput: ...
```

AND `build_scenario_state_store(scenario)` constructs a `StateStore` with `system_clock_status="valid"`, publishes the four device states derived from the scenario, and returns the live store
AND `build_scenario_adapters(scenario)` returns a mapping with all four `DeviceRole` keys, each mapped to a `_StubAdapter` instance (see AC3) that returns the scenario-derived state and records every `send_command` call
AND `build_scenario_settings(scenario)` returns a `Settings(_env_file=None, peak_limit_kw=scenario.peak_limit_kw, battery_reserve_floor_percent=scenario.battery_reserve_floor_percent, command_max_retries=0, command_retry_backoff_seconds=0.0, ...)` — `command_max_retries=0` is critical so success/failure semantics aren't muddied by retry behaviour
AND `build_scenario_evaluation_input(scenario, snapshot, at=at)` constructs a complete `EvaluationInput` from the snapshot using:
- `peak_context` built directly (NOT via PartialIntervalTracker — simulation should be deterministic in `at`); uses `current_partial_window_projection_kw=scenario.grid_power_kw + scenario.ev_current_power_kw or 0.0` (or simply `scenario.grid_power_kw` for AC1's pure-grid scenarios), `configured_peak_limit_kw=scenario.peak_limit_kw`, `current_monthly_recorded_peak_kw=0.0`, `current_interval_start=_floor_15min(at)`, `current_interval_elapsed_seconds=0`
- `strategy=scenario.strategy`
- `battery_control=BatteryControlContext(reserve_floor_percent=scenario.battery_reserve_floor_percent, capability_profile=<scenario battery capability profile>)`
- `ev_scheduling=EVSchedulingContext(capability_profile=<scenario ev capability profile>, charging_window=scenario.ev_charging_window, homeowner_override_active=scenario.homeowner_override_active, evaluated_at=at, target_charge_rate_kw=scenario.target_ev_charge_rate_kw)`

AND the helpers MUST NOT touch the database, MUST NOT call real protocol libraries, and MUST NOT instantiate `EnergyRepo` (the simulation does not run the runtime `ControlLoop` end-to-end — it composes the engine + execution layers explicitly per AC4)
AND `tests/fixtures/__init__.py` is created (NEW empty file) so `tests/fixtures/scenarios.py` can be imported as `tests.fixtures.scenarios`

**AC3 — Stub adapter (`_StubAdapter`) implements `DeviceAdapter` protocol per role**

GIVEN scenarios need a single adapter type per role that returns scenario-derived state and records dispatched commands
THEN `tests/fixtures/scenarios.py` defines four classes:
- `_StubInverterAdapter` — returns `InverterState` with `pv_power_kw=scenario.pv_power_kw`, `ac_power_kw=scenario.inverter_ac_power_kw`, `operating_mode="normal"`, `read_at=now`; capability profile has read capabilities `{state, power}` and EMPTY write capabilities
- `_StubBatteryAdapter` — returns `BatteryState` with `soc_percent=scenario.battery_soc_percent`, `battery_power_kw=0.0`, `capacity_kwh=scenario.battery_capacity_kwh`, `operating_mode="normal"`, `read_at=now`; capability profile has read capabilities `{state, soc}` and write capabilities `{set_charge_rate, set_discharge_rate}`
- `_StubEVChargerAdapter` — returns `EVChargerState` with `session_active=scenario.ev_session_active`, `status="charging"` if active else `"available"`, `current_power_kw=scenario.ev_current_power_kw`, `power_source="meter_values"` if active else `None`, `power_measured_at=now` if active else `None`, `read_at=now`; capability profile has write capabilities `{set_ev_charge_current}`
- `_StubGridMeterAdapter` — returns `GridMeterState` with `grid_power_kw=scenario.grid_power_kw`, `energy_delivered_kwh=0.0`, `energy_returned_kwh=0.0`, `received_at=now`; capability profile has read capabilities `{power, energy}` and EMPTY write capabilities

AND every stub adapter implements the full `DeviceAdapter` structural protocol: `device_id` attribute, `connect()`, `disconnect()`, `get_state()`, `get_capabilities()`, `send_command()` — REUSE the patterns from `tests/integration/engine/test_command_pipeline.py:SimulatedBatteryAdapter` and `tests/integration/engine/test_retry_pipeline.py:SimulatedEVChargerAdapter`
AND every stub adapter records every `send_command()` call to `self.send_command_calls: list[DeviceCommand]` and returns `CommandResult(correlation_id=cmd.correlation_id, device_id=self.device_id, status=CommandStatus.success, applied=True, reason="ok")` on dispatch — scenarios do not need failure injection in this story
AND `_StubInverterAdapter.send_command()` and `_StubGridMeterAdapter.send_command()` raise `AssertionError("<role> has no write capability")` — these roles never receive write commands by design and any send_command call indicates a test or pipeline bug
AND every stub adapter's `device_id` is fixed and predictable per role: `inv-001`, `bat-001`, `ev-001`, `grid-001` — matching the existing simulated-adapter convention from Story 8.4's `test_fail_safe_lifecycle.py`
AND the capability profile `device_id` field MUST equal the adapter's `device_id` (PolicyGuard's `get_capabilities()` consumer expects this; `evaluate_battery_control._has_capability` also verifies `profile.device_id == battery.device_id`)

**AC4 — Simulation runner (`run_simulation_cycle`) composes the full pipeline explicitly**

GIVEN `tests/integration/engine/test_decision_engine_simulation.py` (NEW file) needs to drive scenarios deterministically
THEN it defines a helper:

```python
async def run_simulation_cycle(
    scenario: SimulationScenario,
    *,
    at: datetime | None = None,
) -> SimulationCycleResult:
    """Run one decision-engine cycle end-to-end for the given scenario.

    Composes the full pipeline:
        1. Build StateStore + adapters + settings from scenario
        2. Build EvaluationInput from snapshot + scenario
        3. Call evaluate_cycle() to produce EvaluationResult
        4. Translate intents to commands via IntentExecutor
        5. Execute each command through RetryPolicy → PolicyGuard → adapter
        6. Return all results + audit calls + adapter dispatch records
    """
    ...
```

AND `SimulationCycleResult` is a frozen dataclass with fields: `evaluation_result: EvaluationResult`, `commands: list[DeviceCommand]`, `command_results: list[CommandResult]`, `audit_calls: list[dict[str, object]]`, `adapter_calls: dict[DeviceRole, list[DeviceCommand]]`
AND the runner uses real production classes — `IntentExecutor`, `PolicyGuard`, `RetryPolicy`, `evaluate_cycle()`, `ObservabilityService` — and a mocked `EventLogRepo` (`AsyncMock(spec=EventLogRepo)`) so audits are recorded via the spy pattern from Story 8.4's `_audit_observability()` at `tests/integration/engine/test_fail_safe_lifecycle.py:164-180`
AND the runner does NOT instantiate `ControlLoop` — the loop's polling, watchdog, fail-safe lifecycle, and PartialIntervalTracker are out of scope; this story exercises the **decision + execution** path only (the loop is exercised by Story 8.4's tests #33, #34)
AND `at` defaults to `datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)` — fixed for deterministic peak_context construction
AND the runner returns successfully even when zero commands are dispatched (scenarios that produce only `hold` intents) — `command_results` and `adapter_calls` lists are simply empty

**AC5 — "Peak tariff — curtail load" scenario produces a successful EV-suppression / battery-discharge command**

GIVEN a `SimulationScenario` named `peak_tariff_curtail_load` with:
- `grid_power_kw=8.0` (importing 8 kW)
- `pv_power_kw=0.0`, `inverter_ac_power_kw=0.0` (no PV)
- `battery_soc_percent=80.0`, `battery_capacity_kwh=10.0` (well above reserve)
- `ev_session_active=True`, `ev_current_power_kw=4.0` (active EV session adding load)
- `strategy=EnergyStrategy.maximize_self_consumption`
- `peak_limit_kw=5.0` (projection 8.0+4.0=12.0 kW exceeds 5.0 kW)
- `battery_reserve_floor_percent=20.0` (battery is well above)
- `homeowner_override_active=True`, `ev_charging_window=None` (so EV charge is permitted)

WHEN `run_simulation_cycle(scenario)` runs
THEN `result.evaluation_result.recommended_operating_mode is SystemOperatingMode.normal` — peak overshoot is a control signal, NOT a fail-safe trigger (per peak_limiting.py and operating_mode.py)
AND the engine produces a `BatteryIntent(action=BatteryIntentAction.discharge, ...)` (peak-limiting candidate `discharge_battery` wins the safety-band conflict resolution per `evaluator.py:78-87`)
AND `IntentExecutor` translates this to a `SetBatteryDischargeRateCommand`
AND `RetryPolicy.execute()` returns `CommandResult.status is CommandStatus.success` AND `result.applied is True`
AND `PolicyGuard.authorize_and_dispatch()` was invoked exactly once for the discharge command (verified by `result.adapter_calls[DeviceRole.battery]` having exactly one entry)
AND ONE DECISION audit was emitted with `event_type="DECISION"`, `device_id="bat-001"`, `summary` containing `"SetBatteryDischargeRateCommand"` and `"success"`, and `detail["correlation_id"]` matching the dispatched command's correlation_id (string form)
AND ZERO CONSTRAINT audits were emitted (no rejection)
AND ZERO DEVICE audits were emitted (no failure / no cancellation)

**Note:** This scenario relies on the peak-limiting safety band winning over the strategy band. `maximize_self_consumption` would also emit a `battery_discharge_to_avoid_import` candidate (grid_power_kw > 0), so both peak_limiting and strategy contribute discharge candidates — the safety band's lower band-rank wins per `resolve_conflicts` total order at `energy_balancing.py:113-134`. The dispatched discharge is therefore from the safety band, not the strategy band.

**AC6 — "Excess solar — charge battery" scenario produces a successful battery-charge command**

GIVEN a `SimulationScenario` named `excess_solar_charge_battery` with:
- `grid_power_kw=-2.0` (exporting 2 kW back to grid — surplus)
- `pv_power_kw=6.0`, `inverter_ac_power_kw=4.0` (PV producing more than household consumes)
- `battery_soc_percent=50.0`, `battery_capacity_kwh=10.0` (mid-range, well below 100% — charge is appropriate)
- `ev_session_active=False`, `ev_current_power_kw=None`
- `strategy=EnergyStrategy.maximize_self_consumption`
- `peak_limit_kw=25.0` (no peak issue — projection well below limit)
- `battery_reserve_floor_percent=20.0`

WHEN `run_simulation_cycle(scenario)` runs
THEN `result.evaluation_result.recommended_operating_mode is SystemOperatingMode.normal`
AND the engine produces a `BatteryIntent(action=BatteryIntentAction.charge, ...)` from the `maximize_self_consumption` strategy emitting `battery_charge_from_pv` candidate (PV power > AC power per `_evaluate_maximize_self_consumption` at `energy_balancing.py:170-206`)
AND `IntentExecutor` translates this to a `SetBatteryChargeRateCommand`
AND `RetryPolicy.execute()` returns `CommandResult.status is CommandStatus.success` AND `result.applied is True`
AND `PolicyGuard.authorize_and_dispatch()` was invoked exactly once and `_StubBatteryAdapter.send_command_calls` has exactly one entry
AND ONE DECISION audit was emitted with `event_type="DECISION"`, `device_id="bat-001"`, `summary` containing `"SetBatteryChargeRateCommand"` and `"success"`, and `detail["correlation_id"]` matching the dispatched command's correlation_id

**Capability gating note:** because `_evaluate_maximize_self_consumption` emits a charge candidate when `pv_power_kw > 0 AND pv_power_kw > ac_power_kw`, both must hold in the scenario (6.0 > 4.0 ✓ AND 6.0 > 0 ✓). `evaluate_battery_control` then requires the battery's `capability_profile.device_id` to equal `battery.device_id` AND `WriteCapability.set_charge_rate` to be present — both are provided by `_StubBatteryAdapter`. The scenario's `BatteryControlContext.capability_profile` MUST be the same `DeviceCapabilityProfile` that the stub adapter's `get_capabilities()` returns — see AC2 for the wiring.

**AC7 — "Battery below reserve — PolicyGuard rejection" scenario blocks discharge before adapter call**

GIVEN a `SimulationScenario` named `battery_below_reserve_rejection` with:
- `grid_power_kw=8.0` (importing — would trigger discharge candidate)
- `pv_power_kw=0.0`, `inverter_ac_power_kw=0.0`
- `battery_soc_percent=20.0`, `battery_capacity_kwh=10.0` (AT reserve floor — `<=` triggers rejection)
- `ev_session_active=False`, `ev_current_power_kw=None`
- `strategy=EnergyStrategy.maximize_self_consumption`
- `peak_limit_kw=25.0` (no peak overshoot — discharge candidate comes from STRATEGY, not safety; this isolates the SoC-reserve check)
- `battery_reserve_floor_percent=20.0`

WHEN `run_simulation_cycle(scenario)` runs
THEN one of two valid intent paths must occur (the dev MUST verify which actually fires by running the test, then assert that branch precisely):
- **Path A (engine-side hold):** `evaluate_battery_control` at `battery_control.py:62-67` returns `BatteryIntent(action=BatteryIntentAction.hold, reason_code="battery_reserve_floor_reached")` because `soc_percent <= reserve_floor_percent`. `IntentExecutor` produces NO command (`hold` is silently skipped per `intent_executor.py:62-63`). `result.commands == []`, `result.command_results == []`, ZERO audit events of any type.
- **Path B (PolicyGuard-side rejection):** the engine produces a discharge intent (e.g., from a peak-limiting-style path that bypasses the engine-side reserve check); `IntentExecutor` translates to `SetBatteryDischargeRateCommand`; `PolicyGuard._check_safety_constraints` at `policy_guard.py:147-150` rejects with `reason="battery_soc_at_or_below_reserve_floor"`; `CommandResult.status is CommandStatus.rejected`; `_StubBatteryAdapter.send_command_calls == []` (adapter never called); ONE CONSTRAINT audit emitted with `event_type="CONSTRAINT"`, `device_id="bat-001"`, `summary` containing `"battery_soc_at_or_below_reserve_floor"`.

**The dev MUST run the test once to determine which path fires** (engine-side hold is the most likely outcome given the current rule ordering — strategy candidates flow through `evaluate_battery_control` which gates on the reserve floor before producing a discharge intent), then write the assertions to match that exact path. If Path A fires, this AC's tests MUST construct a SECOND test that bypasses the engine-side hold by directly constructing a `SetBatteryDischargeRateCommand` and calling `RetryPolicy.execute()` to validate the PolicyGuard rejection in isolation — the AC's contract is that *PolicyGuard rejects below-reserve discharge*, and that contract must be exercised regardless of whether the engine-side hold pre-empts it in this specific scenario.

AND in EITHER case `_StubBatteryAdapter.send_command_calls == []` — confirming "no adapter call is made" per the epic AC
AND ZERO DECISION audits are emitted (no successful dispatch occurred)

**AC8 — All commands in scenarios produce event_log rows with correct event_type and correlation_id**

GIVEN any scenario from AC5–AC7 has run via `run_simulation_cycle()`
WHEN the test inspects `result.audit_calls`
THEN every entry has `kwargs["actor"] == "system"` AND `kwargs["event_type"]` is one of `{"DECISION", "CONSTRAINT", "DEVICE"}` AND `kwargs["device_id"]` matches the targeted command's device_id
AND for every dispatched command (`result.commands`), there is exactly ONE matching audit entry with `detail["correlation_id"] == str(command.correlation_id)` (DECISION on success; DEVICE on terminal failure; CONSTRAINT on rejection — emitted by PolicyGuard, which writes correlation_id via `command.device_id` and the audit detail in PolicyGuard's `_reject` method MUST include `correlation_id` — see AC9)
AND no command produces zero audit rows AND no command produces multiple audit rows of the same `event_type` (DECISION + CONSTRAINT can co-occur if PolicyGuard rejects then RetryPolicy attempts further retries return failed, but in the scenarios above each command terminates with exactly one outcome)
AND a parametrized test `test_all_commands_produce_event_log_rows` runs both AC5 (curtail) and AC6 (excess-solar) scenarios via `pytest.mark.parametrize` and asserts the per-command audit invariant for each

**AC9 — PolicyGuard `_reject` audit emission includes correlation_id in detail**

GIVEN PolicyGuard rejects a command (any of the rejection branches at `policy_guard.py:67-105`)
WHEN `_reject(command, reason)` runs
THEN `await self._observability.audit(...)` is called with the existing positional arguments PLUS a new `detail={"correlation_id": str(command.correlation_id), "command_type": type(command).__name__, "rejection_reason": reason}` keyword
AND the existing `actor="system"`, `event_type="CONSTRAINT"`, `summary=f"Command rejected: {reason}"`, `device_id=command.device_id` are UNCHANGED
AND existing PolicyGuard tests in `tests/unit/engine/test_policy_guard.py` and `tests/integration/engine/test_command_pipeline.py:test_full_pipeline_rejected_path` are updated to verify `detail["correlation_id"]` is the string form of the rejected command's correlation_id

**Rationale:** Today PolicyGuard's CONSTRAINT audit row carries no correlation_id — only the device_id appears in the row's column. AC8 requires per-command correlation traceability across all three audit event_types; this AC closes the gap on the rejection path. The same shape is then symmetric across DECISION (AC1), CONSTRAINT (AC9), and DEVICE (Story 8.3's existing `_emit_failure_audit` already includes correlation_id in the summary; this story extends it to also include `detail["correlation_id"]` for consistency — see AC10).

**AC10 — RetryPolicy `_emit_failure_audit` includes correlation_id in detail**

GIVEN `RetryPolicy._emit_failure_audit(command, last_result, attempts)` is called (terminal failure path)
WHEN the audit is emitted
THEN the existing `actor="system"`, `event_type="DEVICE"`, `summary` (per Story 8.3 spec), `device_id=command.device_id` are UNCHANGED
AND a new `detail={"correlation_id": str(command.correlation_id), "command_type": type(command).__name__, "command_status": last_result.status.value, "applied": last_result.applied, "attempts": attempts, "final_reason": last_result.reason}` keyword is added to the call
AND the cancellation-path audit (Story 8.4 AC7) gets the same `detail` extension: `detail={"correlation_id": str(command.correlation_id), "command_type": type(command).__name__, "command_status": "cancelled", "attempts": attempts}`
AND existing tests in `tests/unit/engine/test_retry_policy.py` that assert on `audit_spy.await_args.kwargs` are updated to verify `detail["correlation_id"]` matches the command's correlation_id

**AC11 — Existing success-path tests are updated for AC1 (NEW DECISION emission)**

GIVEN AC1 introduces a NEW DECISION audit on every successful command dispatch
THEN the following existing tests have their assertions on `audit_spy.assert_not_awaited()` REPLACED with `audit_spy.assert_awaited_once()` + `event_type="DECISION"` shape checks:
- `tests/unit/engine/test_retry_policy.py:test_idempotent_command_succeeds_on_first_attempt_no_audit_event` (line ~145) — rename to `test_idempotent_command_succeeds_on_first_attempt_emits_decision_audit` AND update assertion to verify ONE DECISION audit with `attempts=1`
- `tests/unit/engine/test_retry_policy.py:test_idempotent_command_succeeds_on_second_attempt_no_audit_event` (line ~128) — rename to `test_idempotent_command_succeeds_on_second_attempt_emits_decision_audit` AND update assertion to verify ONE DECISION audit with `attempts=2`
- `tests/integration/engine/test_command_pipeline.py:test_full_pipeline_allowed_path` (line ~125) — replace `audit_spy.assert_not_awaited()  # success path emits NO CONSTRAINT event` with `audit_spy.assert_awaited_once()` and assert `event_type="DECISION"`, `device_id="bat-001"`, `detail["correlation_id"]` matches the dispatched command's correlation_id
- `tests/integration/engine/test_retry_pipeline.py:test_full_pipeline_with_idempotent_retry_recovery` (line ~222) — replace `audit_spy.assert_not_awaited()  # ZERO DEVICE events; ZERO CONSTRAINT events` with `audit_spy.assert_awaited_once()` and assert `event_type="DECISION"` (with `attempts=3`: 2 failures + 1 success)
- The comment-only references to "no audit on success" in any docstring or comment are revised to reflect the new behavior

AND any other test in `tests/unit/engine/` or `tests/integration/engine/` that asserts `audit_spy.assert_not_awaited()` after a successful `RetryPolicy.execute()` call MUST be updated — grep for `audit_spy.assert_not_awaited` before completing this AC; the file list above is necessary but the dev MUST verify completeness via grep
AND no existing test that verifies failure-path audits (DEVICE) or rejection-path audits (CONSTRAINT) needs functional change — only the success-path tests change behaviour
AND the docstring of `tests/integration/engine/test_command_pipeline.py:test_full_pipeline_rejected_path` is updated only if it asserts "no DECISION audit" (currently it asserts CONSTRAINT only); the test's behaviour does not change because PolicyGuard rejection short-circuits before RetryPolicy's success branch runs

**Note for the dev:** any `test_<...>_no_audit_event` test name involving a successful retry/dispatch is now factually wrong and MUST be renamed. Test names must reflect post-AC1 reality.

**AC12 — Tests**

Unit tests in `tests/unit/engine/test_retry_policy.py` (UPDATE existing file):
1. `test_decision_audit_emitted_on_first_attempt_success()` — single success result; assert ONE DECISION audit with `attempts=1`, `event_type="DECISION"`, `device_id` match, `detail["correlation_id"]` match, `detail["command_status"]="success"`, `detail["applied"]=True`, `detail["attempts"]=1`
2. `test_decision_audit_emitted_on_retry_recovery_success()` — failed result then success on attempt 2; assert ONE DECISION audit with `attempts=2`; ZERO DEVICE audits
3. `test_decision_audit_emit_failure_swallowed()` — `observability.audit` raises `RuntimeError` on the success path; `RetryPolicy.execute()` still returns `CommandResult.status=success`; `audit_emit_failed` is logged via structlog
4. `test_failure_audit_includes_correlation_id_in_detail()` — terminal failure case; assert `kwargs["detail"]["correlation_id"]` is the string form of the command's correlation_id
5. `test_cancellation_audit_includes_correlation_id_in_detail()` — script the cancellation path from Story 8.4 AC7; assert `kwargs["detail"]["correlation_id"]` is set
6. Rename and update `test_idempotent_command_succeeds_on_first_attempt_no_audit_event` and `test_idempotent_command_succeeds_on_second_attempt_no_audit_event` per AC11

Unit tests in `tests/unit/engine/test_policy_guard.py` (UPDATE existing file):

7. `test_reject_audit_includes_correlation_id_in_detail()` — drive any rejection branch (e.g., capability missing); assert `kwargs["detail"]["correlation_id"]` is the string form of the rejected command's correlation_id

Integration tests in `tests/integration/engine/test_decision_engine_simulation.py` (NEW file):

8. `test_peak_tariff_curtail_load_dispatches_battery_discharge()` — runs AC5 scenario; asserts `CommandResult.status is CommandStatus.success`, ONE `SetBatteryDischargeRateCommand` was dispatched to `_StubBatteryAdapter`, ONE DECISION audit emitted, ZERO CONSTRAINT, ZERO DEVICE
9. `test_excess_solar_charges_battery()` — runs AC6 scenario; asserts ONE `SetBatteryChargeRateCommand` was dispatched, `CommandResult.status is CommandStatus.success`, ONE DECISION audit emitted with correlation_id matching dispatched command
10. `test_battery_below_reserve_rejection_no_adapter_call()` — runs AC7 scenario; the dev MUST first run the simulation to determine which path fires (engine-side hold OR PolicyGuard rejection), then assert that exact path. EITHER WAY, asserts `_StubBatteryAdapter.send_command_calls == []`. If Path B (PolicyGuard rejection) fires: ONE CONSTRAINT audit with `detail["correlation_id"]` set. If Path A (engine-side hold) fires: ZERO commands, ZERO audits — see test #11
11. `test_below_reserve_policy_guard_rejection_isolated()` — directly construct a `SetBatteryDischargeRateCommand` (bypassing the engine-side hold) and call `RetryPolicy.execute()` against the AC7 scenario's StateStore + adapters; assert `CommandResult.status is CommandStatus.rejected`, `reason="battery_soc_at_or_below_reserve_floor"`, ZERO `_StubBatteryAdapter.send_command_calls`, ONE CONSTRAINT audit emitted with `detail["correlation_id"]` matching the constructed command
12. `test_all_commands_produce_event_log_rows()` — `pytest.mark.parametrize` over the AC5 (curtail) and AC6 (excess-solar) scenarios; for each: dispatch all commands and assert that for every entry in `result.commands`, `result.audit_calls` contains exactly one entry where `detail["correlation_id"] == str(command.correlation_id)` AND `kwargs["device_id"] == command.device_id` AND `kwargs["event_type"]` is one of the valid set
13. `test_simulation_does_not_touch_real_adapters()` — sanity check: instantiate scenario adapters and assert each is an instance of `_StubInverterAdapter` / `_StubBatteryAdapter` / `_StubEVChargerAdapter` / `_StubGridMeterAdapter` — no `pymodbus`, `dsmr_parser`, or `ocpp` imports in the call path (verified by `sys.modules` snapshot before/after the test, OR by patching `pymodbus.client.AsyncModbusTcpClient` to raise `AssertionError("real adapter touched")` and confirming it is never invoked)

Integration tests in `tests/integration/engine/test_command_pipeline.py` (UPDATE existing file):

14. Update `test_full_pipeline_allowed_path` per AC11 — assert ONE DECISION audit on success path; remove the `audit_spy.assert_not_awaited()` assertion
15. Update `test_full_pipeline_rejected_path` to assert `kwargs["detail"]["correlation_id"]` matches the rejected command's correlation_id

Integration tests in `tests/integration/engine/test_retry_pipeline.py` (UPDATE existing file):

16. Update `test_full_pipeline_with_idempotent_retry_recovery` per AC11 — assert ONE DECISION audit (attempts=3); remove the `audit_spy.assert_not_awaited()` assertion
17. Update `test_full_pipeline_with_non_idempotent_fail_closed` — assert `kwargs["detail"]["correlation_id"]` matches the failed command's correlation_id (existing test verifies summary text; this AC adds the detail-field check)

**AC13 — Pre/post quality gate**

GIVEN the story is closed
THEN the pre-story baseline (expected: 858 from Story 8.4 review patches, per sprint-status.yaml line 38) is recorded
AND post-story tests passing increases by the new tests (~10–13 new + ~5 updates), all green
AND `uv run python -m ruff check .` → clean
AND `uv run python -m ruff format --check .` → clean
AND `uv run python -m mypy src/` → clean
AND `uv run python -m pytest tests/ --no-cov -q` → all passing
AND a smoke check: `python -c "from tests.fixtures.scenarios import SimulationScenario; print(SimulationScenario.__dataclass_fields__.keys())"` → lists all scenario fields (sanity check that fixtures are importable from the test root)

---

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC13)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — record baseline test count (expected: 858 per sprint-status.yaml line 38)
  - [x] Run `uv run python -m ruff check .` — must be clean
  - [x] Run `uv run python -m ruff format --check .` — must be clean
  - [x] Run `uv run python -m mypy src/` — must be clean

- [x] Task 1: Add DECISION audit emission to RetryPolicy success path (AC1; tests AC12 #1, #2, #3)
  - [x] Open `src/open_ems/engine/retry_policy.py`
  - [x] Add a private helper `async def _emit_success_audit(self, command: DeviceCommand, result: CommandResult, attempts: int) -> None` mirroring the structure of `_emit_failure_audit` (try/except wrapper around `await self._observability.audit(...)`; on failure log `audit_emit_failed` and continue)
  - [x] In the success branch of `execute()` (immediately before the `return result` for the `status is CommandStatus.success` case), call `await self._emit_success_audit(command, result, attempts=attempt)` with the current `attempt` count
  - [x] Audit kwargs: `actor="system"`, `event_type="DECISION"`, `summary=f"Command {type(command).__name__} for {command.device_id} dispatched: success (applied={result.applied}) after {attempts} attempt(s)"`, `device_id=command.device_id`, `detail={"correlation_id": str(command.correlation_id), "command_type": type(command).__name__, "command_status": "success", "applied": result.applied, "attempts": attempts}`
  - [x] Add a `logger.info("command_dispatched", device_id=command.device_id, command_type=type(command).__name__, correlation_id=str(command.correlation_id), attempts=attempts, component="engine")` BEFORE the audit emission so the structured log is the floor regardless of audit DB availability
  - [x] Add tests #1, #2, #3 to `tests/unit/engine/test_retry_policy.py` per AC12

- [x] Task 2: Extend `_emit_failure_audit` and the cancellation audit with `detail` correlation_id (AC10; tests AC12 #4, #5)
  - [x] In `RetryPolicy._emit_failure_audit` (`src/open_ems/engine/retry_policy.py`), add `detail={"correlation_id": str(command.correlation_id), "command_type": type(command).__name__, "command_status": last_result.status.value, "applied": last_result.applied, "attempts": attempts, "final_reason": last_result.reason}` to the audit kwargs
  - [x] In the `CancelledError` audit branch (Story 8.4 AC7), add `detail={"correlation_id": str(command.correlation_id), "command_type": type(command).__name__, "command_status": "cancelled", "attempts": attempt}` to the audit kwargs — preserving the existing summary and re-raise behaviour
  - [x] Update existing tests in `tests/unit/engine/test_retry_policy.py` whose assertions check `audit_spy.await_args.kwargs` to also verify `kwargs["detail"]["correlation_id"]` (grep for `audit_spy.await_args` to find them; affected: most non-renamed AC10 tests #1, #4–#8 from Story 8.3)
  - [x] Add tests #4 and #5 from AC12

- [x] Task 3: Extend PolicyGuard `_reject` audit with `detail` correlation_id (AC9; test AC12 #7)
  - [x] In `PolicyGuard._reject` at `src/open_ems/engine/policy_guard.py:163-184`, add `detail={"correlation_id": str(command.correlation_id), "command_type": type(command).__name__, "rejection_reason": reason}` to the `await self._observability.audit(...)` call
  - [x] No change to `actor`, `event_type`, `summary`, `device_id`
  - [x] Update existing tests in `tests/unit/engine/test_policy_guard.py` whose assertions check `audit_spy.await_args.kwargs` to also verify `kwargs["detail"]["correlation_id"]` matches the rejected command's correlation_id
  - [x] Add test #7 from AC12

- [x] Task 4: Update existing success-path tests for AC11 (tests AC12 #6, #14, #16)
  - [x] Grep `tests/unit/engine` and `tests/integration/engine` for `audit_spy.assert_not_awaited` — these are the candidates that may need updating (note: cancellation-related sleep-mock asserts are not in scope)
  - [x] In `tests/unit/engine/test_retry_policy.py`:
    - [x] Rename `test_idempotent_command_succeeds_on_first_attempt_no_audit_event` → `test_idempotent_command_succeeds_on_first_attempt_emits_decision_audit`; replace the `audit_spy.assert_not_awaited()` with `audit_spy.assert_awaited_once()` and assert `event_type="DECISION"`, `attempts=1`
    - [x] Rename `test_idempotent_command_succeeds_on_second_attempt_no_audit_event` → `test_idempotent_command_succeeds_on_second_attempt_emits_decision_audit`; replace `audit_spy.assert_not_awaited()` with the AC12 #2 shape (`attempts=2`)
  - [x] In `tests/integration/engine/test_command_pipeline.py:test_full_pipeline_allowed_path`:
    - [x] Replace `audit_spy.assert_not_awaited()  # success path emits NO CONSTRAINT event` with `audit_spy.assert_awaited_once()`
    - [x] Add: `kwargs = audit_spy.await_args.kwargs; assert kwargs["event_type"] == "DECISION"; assert kwargs["device_id"] == "bat-001"; assert kwargs["detail"]["correlation_id"] == str(commands[0].correlation_id)`
    - [x] Update the comment to reflect new behaviour (also wired the test through `RetryPolicy` since DECISION emission lives there, not in PolicyGuard)
  - [x] In `tests/integration/engine/test_retry_pipeline.py:test_full_pipeline_with_idempotent_retry_recovery`:
    - [x] Replace `audit_spy.assert_not_awaited()  # ZERO DEVICE events; ZERO CONSTRAINT events` with `audit_spy.assert_awaited_once()`
    - [x] Add: `kwargs = audit_spy.await_args.kwargs; assert kwargs["event_type"] == "DECISION"; assert kwargs["detail"]["attempts"] == 3`
  - [x] Run full test suite to confirm no other test still asserts `audit_spy.assert_not_awaited` after a successful `RetryPolicy.execute()` call

- [x] Task 5: Create `tests/fixtures/scenarios.py` and `tests/fixtures/__init__.py` (AC2, AC3)
  - [x] Create `tests/fixtures/__init__.py` (empty)
  - [x] Create `tests/fixtures/scenarios.py` with:
    - [x] `SimulationScenario` frozen dataclass per AC2 (use `@dataclass(frozen=True)` from `dataclasses`)
    - [x] Four `_Stub<Role>Adapter` classes per AC3 — each exposes `device_id`, `connect`, `disconnect`, `get_state`, `get_capabilities`, `send_command`; each tracks dispatched commands; capability profiles use the `_<role>_default_capability_profile()` helpers below
    - [x] `_inverter_default_capability_profile(device_id) -> DeviceCapabilityProfile` — read `{state, power}`, write `frozenset()`
    - [x] `_battery_default_capability_profile(device_id) -> DeviceCapabilityProfile` — read `{state, soc}`, write `{set_charge_rate, set_discharge_rate}`
    - [x] `_ev_charger_default_capability_profile(device_id) -> DeviceCapabilityProfile` — read `{state}`, write `{set_ev_charge_current}`
    - [x] `_grid_meter_default_capability_profile(device_id) -> DeviceCapabilityProfile` — read `{power, energy}`, write `frozenset()`
    - [x] `build_scenario_state_store(scenario, *, at)` — async; returns a `StateStore` with snapshot publishing complete
    - [x] `build_scenario_adapters(scenario)` — sync; returns `dict[DeviceRole, DeviceAdapter]`
    - [x] `build_scenario_settings(scenario)` — sync; returns `Settings(_env_file=None, peak_limit_kw=..., battery_reserve_floor_percent=..., command_max_retries=0, command_retry_backoff_seconds=0.0)`
    - [x] `build_scenario_evaluation_input(scenario, snapshot, *, at)` — sync; returns a complete `EvaluationInput` (the peak_context's `current_interval_start` MUST be floored to a 15-minute boundary and timezone-aware UTC; use a private `_floor_15min(at)` helper modeled on `partial_interval_tracker._floor_to_15_min`)
  - [x] Use `_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)` as the module's default `at` value to match Story 8.4's `tests/integration/engine/test_fail_safe_lifecycle.py:38`
  - [x] Add module-level docstring explaining: this is the scenario fixture for end-to-end decision-engine simulation tests; not the test runner (the runner is in `test_decision_engine_simulation.py`)
  - [x] Smoke-check importability: `python -c "from tests.fixtures.scenarios import SimulationScenario; print(list(SimulationScenario.__dataclass_fields__))"`

- [x] Task 6: Create `tests/integration/engine/test_decision_engine_simulation.py` (AC4, AC5–AC8; tests AC12 #8–#13)
  - [x] Create the file with `from __future__ import annotations` and full type hints
  - [x] Define `SimulationCycleResult` frozen dataclass with the fields listed in AC4
  - [x] Define `async def run_simulation_cycle(scenario, *, at=_NOW) -> SimulationCycleResult`:
    - [x] Build state_store, adapters, settings via fixture helpers
    - [x] Build the `ObservabilityService` with mocked `EventLogRepo` AND audit spy (mirror the `_audit_observability()` pattern from `tests/integration/engine/test_fail_safe_lifecycle.py:164-180`)
    - [x] Build snapshot via `state_store.publish(...)` (call this BEFORE the engine — adapters publish first)
    - [x] Build `EvaluationInput` via `build_scenario_evaluation_input(scenario, snapshot, at=at)`
    - [x] Call `evaluate_cycle(eval_input)` to produce `EvaluationResult`
    - [x] Construct `IntentExecutor`, `PolicyGuard`, `RetryPolicy` from settings + adapters + observability + state_store
    - [x] Call `executor.translate(result, snapshot)` to produce `commands`
    - [x] For each command: call `await retry_policy.execute(cmd)` and append the result
    - [x] Collect adapter dispatch records: `{role: list(adapter.send_command_calls) for role, adapter in adapters.items()}`
    - [x] Return populated `SimulationCycleResult`
  - [x] Define test scenario constants at module level: `PEAK_TARIFF_CURTAIL = SimulationScenario(...)`, `EXCESS_SOLAR = SimulationScenario(...)`, `BATTERY_BELOW_RESERVE = SimulationScenario(...)` per AC5–AC7
  - [x] Implement tests #8, #9, #10, #11, #12, #13 from AC12
  - [x] For test #13 (no real adapter touched): use `sys.modules` snapshot before/after the cycle and assert no `pymodbus`/`ocpp`/`dsmr_parser` modules were loaded as a side effect (the AC's task lists this as the alternative to `MonkeyPatch.setattr` since no Modbus adapter is wired into the engine path; the snapshot is the cleaner sentinel against future coupling)

- [x] Task 7: Final validation (AC13)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — all tests pass; record final count (~870+ expected: 858 baseline + 13 new tests + 5 updated tests) — final count: **871 passed**
  - [x] Run `uv run python -m ruff check .` — clean
  - [x] Run `uv run python -m ruff format --check .` — clean
  - [x] Run `uv run python -m mypy src/` — clean
  - [x] Verify scenario smoke: `python -c "from tests.fixtures.scenarios import SimulationScenario, build_scenario_settings; ..."` → `25.0`

---

## Dev Notes

### Why this story exists in this exact shape

Stories 8.1–8.4 implemented the runtime control loop, IntentExecutor, PolicyGuard, RetryPolicy with retry/timeout/idempotency, and the watchdog + fail-safe lifecycle. The pieces are individually unit-tested AND there are two integration tests for the command pipeline (`test_command_pipeline.py`, `test_retry_pipeline.py`) AND one integration test for the fail-safe lifecycle (`test_fail_safe_lifecycle.py`). What is **missing** and is this story's job:

1. **No scripted scenario coverage.** Tests today exercise individual paths (success, retry, rejection, fail-safe) but no test runs a *coherent installer-validation scenario* against the engine. The epic's AC literally calls this out: "an installer or developer validating a newly configured site… can confirm the full control path… behaves correctly before live operation begins."
2. **No DECISION audit emission.** PolicyGuard emits CONSTRAINT on rejection; RetryPolicy emits DEVICE on terminal failure or cancellation. **Successful command dispatch leaves no event_log row.** This is a stated gap in `architecture.md:312-316` (the audit_log table should hold "Control decisions, constraint enforcement events, degraded-mode transitions, and recovery events"). The simulation AC asserts per-command event_log rows with correlation_ids; that requires DECISION emission to be added.
3. **No correlation_id traceability across audit row types.** PolicyGuard's CONSTRAINT row has `device_id` but no `correlation_id` in `detail`. RetryPolicy's DEVICE row puts `correlation_id` in the summary text but not in `detail`. End-to-end traceability ("show me every audit row for command X") is impossible without consistent `detail["correlation_id"]` across all three event_types.
4. **No reusable scenario fixture infrastructure.** Every existing integration test rolls its own simulated adapter (`SimulatedBatteryAdapter` defined three times in three files). Future tests (Epic 9 deployment validation, Epic 10 homeowner override, Epic 11 installer monitoring) will repeat this pattern unless a scenario fixture exists. This story creates that fixture.

### Architecture compliance and the "single-evaluator" principle

From the Epic 7 retro (memory: `project_epic7_retro.md`, document: `_bmad-output/implementation-artifacts/epic-7-retro-2026-05-05.md`): *`evaluate_cycle()` is the **sole generator of control intents** in the system. Every other component in Epic 8 either provides input to the evaluator or enforces safety on the output. No component may generate competing control decisions. Every dispatched command must be traceable to an `EvaluationResult` intent.*

Story 8.5 honors this by wiring the simulation through the production composition: `evaluate_cycle()` → `IntentExecutor.translate()` → `RetryPolicy.execute()` → `PolicyGuard.authorize_and_dispatch()` → adapter. The simulation does NOT short-circuit any layer. The "stub adapter" only replaces the protocol I/O at the very end of the pipe — every safety, capability, and policy check between the engine and the adapter still runs.

The DECISION audit added in AC1 also reinforces this principle: every audit row carries `correlation_id` linking back to the `DeviceCommand`, which is itself produced by `IntentExecutor` from an `EvaluationResult` intent, which carries a `cycle_id`. Forensic traceability becomes: `event_log.detail["correlation_id"]` → `DeviceCommand.correlation_id` → `EvaluationResult.cycle_id`. (The `cycle_id` is not yet captured in any audit row because no per-cycle DECISION audit is emitted by the loop today; this story emits PER-COMMAND DECISION audits, which is the appropriate granularity for AC8's per-command traceability assertion. A per-cycle DECISION audit can be added in a future story if needed for cycle-level forensics.)

### What "integration test mode" means in the AC

The epic AC says: *"Given the decision engine, PolicyGuard, and adapter layer are running in integration test mode."* This is shorthand for: real production engine + real production execution layers (`IntentExecutor`, `RetryPolicy`, `PolicyGuard`) + real `ObservabilityService` + real `evaluate_cycle()` + STUB adapters that replace the protocol I/O at the bottom of the pipe + a MOCKED `EventLogRepo` so audit emission is observable but not persisted.

This is **not** end-to-end-with-the-control-loop. The control loop's polling and watchdog are exercised by Story 8.4's `test_fail_safe_lifecycle.py`. This story's `run_simulation_cycle()` runs ONE evaluation cycle deterministically with explicit inputs.

### Why `command_max_retries=0` in scenario settings

Scenarios should produce predictable single-attempt results so the per-command audit assertions in AC8 are unambiguous. Allowing retries (default `command_max_retries=2`) would make `attempts` non-deterministic if any flaky behaviour crept in. RetryPolicy's behaviour with retries is already tested in `test_retry_pipeline.py`; this story is about *scenario semantics*, not retry semantics.

### `EvaluationInput` construction in scenarios — bypassing `PartialIntervalTracker`

The scenarios construct `PeakContext` directly (not via `PartialIntervalTracker.build_peak_context()`) because:
1. The tracker's `update(power_kw, at=now)` is stateful — driving it deterministically requires a multi-tick sequence, which obscures the scenario's intent.
2. The tracker's projection logic is independently tested in `tests/unit/engine/test_partial_interval_tracker.py`.
3. The scenario only needs *the engine to see a particular projection value*; how that projection is calculated is irrelevant to the simulation.

The scenario fixture builds `PeakContext` with `current_partial_window_projection_kw` set directly from scenario parameters. This separates "what the engine sees" from "how the runtime loop builds it", which is exactly the abstraction `PeakContext` was designed to enable (per Story 7.2 dev notes).

### How Path A vs Path B in AC7 plays out — what to expect

In the AC7 scenario (battery at reserve floor, grid importing, no peak overshoot), the chain is:

```
1. _evaluate_maximize_self_consumption emits battery_discharge_to_avoid_import candidate
   (grid_power_kw > 0, battery is healthy)
2. evaluate_peak_limiting emits NO peak candidates (peak_limit_kw=25.0, projection=8.0 — no overshoot)
3. resolve_conflicts picks the strategy candidate (no safety candidate to override it)
4. evaluate_battery_control sees action_type=battery_discharge_to_avoid_import AND
   battery.soc_percent (20.0) <= reserve_floor_percent (20.0) → returns
   BatteryIntent(action=hold, reason_code="battery_reserve_floor_reached")  ← Path A
5. IntentExecutor sees BatteryIntent.action=hold → produces NO command
6. RetryPolicy never runs; PolicyGuard never runs
7. Result: ZERO commands, ZERO audits
```

Path A is the engine-side hold. PolicyGuard's reserve-floor check is a *redundant* safety layer for this specific scenario — it would only fire if some non-engine origin (e.g., installer override in Epic 9, homeowner override in Epic 10) constructed a discharge command and routed it through `RetryPolicy.execute()`. Test #11 (the "isolated" test) covers exactly that: it constructs the command directly and verifies the PolicyGuard rejection.

This dual coverage is intentional. Both paths are part of the safety story — engine-side hold is the *normal* path; PolicyGuard rejection is the *defense-in-depth* path that catches non-engine command origins.

### Where the DECISION audit should be emitted — RetryPolicy, NOT PolicyGuard or IntentExecutor

- **NOT IntentExecutor**: it doesn't know if the command will succeed (its job is translation, not dispatch).
- **NOT PolicyGuard**: it returns mid-pipeline; the *outer* RetryPolicy decides if a result is terminal (success vs. retry-eligible failure). PolicyGuard fires per-attempt; we want per-command terminal audits.
- **YES RetryPolicy**: it owns the terminal outcome. Success is terminal; failure-after-retries is terminal; cancellation is terminal-indeterminate. Three terminal states, three (mutually exclusive) audit emissions: DECISION, DEVICE, DEVICE.

### Avoiding double-emission of DECISION + DEVICE

The success branch and the failure branch in `RetryPolicy.execute()` are sequential and mutually exclusive — a single execution returns from exactly one branch. The success-audit emission must be at the END of the success branch, AFTER `result.status is CommandStatus.success` is confirmed and BEFORE `return result`. The failure-audit emission stays at the END of the failure branch (existing). The cancellation audit stays in the `except CancelledError` block (existing).

A common mistake: emitting the DECISION audit early in the success branch and then having an exception path re-emit a DEVICE audit. To prevent this, the success-audit MUST be the *last* statement before `return` in the success branch. Cancellation during success-audit emission propagates `CancelledError` and falls into the except block — but at that point, the success result has already been determined; the DEVICE audit emitted in the cancellation branch is the correct outcome (the audit was attempted, the result is indeterminate from the caller's perspective).

### Common pitfalls — read before coding

1. **Do NOT add `loop_liveness` or any LoopLiveness/control-loop machinery to the scenario fixture.** The scenarios run the engine + execution layers, NOT the runtime loop. `loop_liveness` is the loop's internal liveness signal; scenarios don't have a loop.
2. **Do NOT instantiate `EnergyRepo` or `PartialIntervalTracker` in the scenario fixture.** Both are runtime-loop dependencies. Scenarios construct `PeakContext` directly.
3. **Do NOT use `Settings()` (without `_env_file=None`).** Pydantic-settings will read from `.env` and override your test parameters. Always use `Settings(_env_file=None, ...)` in tests; mirror the pattern from `tests/integration/engine/test_command_pipeline.py:80`.
4. **Do NOT set `_StubBatteryAdapter.send_command_calls = []` at class level.** This is a Python mutable-default-argument trap — every instance would share the same list. Use `self.send_command_calls: list[DeviceCommand] = []` in `__init__`.
5. **Do NOT use `assert_awaited_once()` AND `await_args.kwargs` checks for tests that previously asserted `assert_not_awaited()` without first verifying the AC1 emission is in place.** The test order matters: complete Task 1 (DECISION emission) BEFORE Task 4 (test updates), or the updated tests will fail against an unchanged production path.
6. **Do NOT include `correlation_id` directly as an `audit()` kwarg.** The `ObservabilityService.audit` signature does not accept `correlation_id` — it takes `detail: dict[str, object] | None = None`. Put correlation_id inside the `detail` dict.
7. **Do NOT serialize `correlation_id` as a `uuid.UUID` object inside `detail`.** `serialize_audit_detail` in `services/audit_log.py:21-27` calls `json.dumps(detail, allow_nan=False)`, which raises `TypeError` on raw UUIDs. Always store the string form: `str(command.correlation_id)`.
8. **Do NOT skip the `audit_emit_failed` try/except wrapper for the new success audit.** Every audit emission in this codebase is wrapped — review patterns in `RetryPolicy._emit_failure_audit`, `PolicyGuard._reject`, `ControlLoop._emit_fail_safe_entry_audit`, `watchdog._emit_stall_audit`. The success path is no different: a DB hang must not block dispatch return.
9. **Do NOT import from `web/`, `storage/` (other than the explicit `EventLogRepo` mock), or `services/external/` in the scenario fixture.** The fixture must respect the engine's import boundaries (architecture.md:1029-1036): `core/` → standalone; `engine/` may import `core/` and service interfaces; tests may import any of these but must not pull in the web layer or external services.
10. **Do NOT use real `datetime.now(UTC)` for the scenario's `at` parameter in tests.** Use the module-level constant `_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)`. This keeps `peak_context.current_interval_start` deterministic (it would be `2026-05-10 12:00:00` floored to the 15-min boundary, also `2026-05-10 12:00:00`).
11. **Do NOT couple scenario adapter `device_id` strings to anywhere except the fixture itself.** Use `inv-001`, `bat-001`, `ev-001`, `grid-001` consistently. If any test asserts a different device_id, it's a coupling bug — fix the assertion.
12. **Do NOT add a "scenario as YAML" loading mechanism.** Scenarios are Python dataclass instances. Future stories may need to load scenarios from a config file (e.g., installer wizard's saved scenarios) but that's Epic 9 / Epic 12 scope.
13. **Do NOT change `ObservabilityService.audit()` signature.** The `detail` field is the right home for correlation_id. Adding `correlation_id` as a top-level kwarg would require migrating every existing audit call site and is out of scope.

### Project Structure Notes

```
src/open_ems/
├── engine/
│   ├── retry_policy.py         ← MODIFY: add _emit_success_audit; emit DECISION on success;
│   │                             extend _emit_failure_audit and CancelledError audit with detail.correlation_id
│   ├── policy_guard.py         ← MODIFY: extend _reject audit with detail.correlation_id
│   ├── intent_executor.py      ← UNCHANGED
│   ├── evaluator.py            ← UNCHANGED
│   ├── operating_mode.py       ← UNCHANGED
│   ├── result.py               ← UNCHANGED
│   ├── models.py               ← UNCHANGED (EvaluationInput / PeakContext / etc.)
│   ├── partial_interval_tracker.py ← UNCHANGED (scenarios bypass it deliberately)
│   ├── control_loop.py         ← UNCHANGED (this story does NOT touch the loop)
│   └── rules/                  ← UNCHANGED
├── services/
│   ├── audit_log.py            ← UNCHANGED (signature accepts detail dict already)
│   ├── loop_liveness.py        ← UNCHANGED
│   ├── watchdog.py             ← UNCHANGED
│   └── readiness.py            ← UNCHANGED
└── core/
    ├── commands.py             ← UNCHANGED (correlation_id is already a UUID4 default)
    └── state.py                ← UNCHANGED

tests/
├── fixtures/                   ← NEW directory
│   ├── __init__.py             ← NEW empty
│   └── scenarios.py            ← NEW: SimulationScenario dataclass + 4 stub adapters + 4 helpers
├── unit/
│   └── engine/
│       ├── test_retry_policy.py    ← UPDATE: rename 2 success tests; add tests #1–#5 (AC12)
│       └── test_policy_guard.py    ← UPDATE: add test #7 (AC12) for detail.correlation_id
└── integration/
    └── engine/
        ├── test_command_pipeline.py     ← UPDATE: tests #14, #15 (AC11, AC9)
        ├── test_retry_pipeline.py       ← UPDATE: tests #16, #17 (AC11, AC10)
        ├── test_fail_safe_lifecycle.py  ← UNCHANGED (Story 8.4 territory)
        └── test_decision_engine_simulation.py  ← NEW: tests #8–#13 (AC4–AC8, AC12)
```

**No new database migration.** No schema changes. No alembic file in this story.

### Architecture compliance verification

- `architecture.md:312-316` (Decision 5.1 — Structured logging): "Decision audit log — Control decisions, constraint enforcement events, degraded-mode transitions, and recovery events written as rows to the `event_log` SQLite table with human-readable `description` field. This is the log the installer reads in the UI." → AC1 closes the gap on "Control decisions" by emitting a DECISION audit per successful command.
- `architecture.md:381-397` (Pattern: All device commands pass through the PolicyGuard): "Every `DeviceCommand` must include: `device_id`, `command_type`, requested parameters, `origin` (one of `decision_engine` / `installer` / `homeowner` / `system`), and `correlation_id`. These fields are required for auditability, command tracing, and safe authorization." → AC8/AC9/AC10 honor this by carrying `correlation_id` into all three audit event_types.
- `architecture.md:686-696` (Adapter Interface Pattern): "Simulated adapters implement the same protocol and are used in all tests. Simulated adapters must support injecting specific states and failure modes for full scenario coverage." → AC3's stub adapters implement the full structural protocol; failure-mode injection is intentionally deferred (the simulation in this story uses success-only adapters; Story 8.4's `_FailingThenHealthyGridMeter` is the existing pattern for failure injection in fail-safe tests).
- `epics.md:1865-1888` (Story 8.5 full AC text): mapped 1-to-1 onto AC4 (parameterisable scenarios), AC5 (curtail-load), AC6 (excess-solar), AC7 (PolicyGuard rejection), AC8 (event_log rows with correlation_id).
- `epics.md:1855-1862` (Epic 8 cross-story constraints): "PolicyGuard.authorize_and_dispatch() is the single required dispatch path — no adapter send_command() is called from any other component (AR15)." → AC4's `run_simulation_cycle` honors this by routing every command through `RetryPolicy → PolicyGuard → adapter.send_command`. Test #13 is a sentinel against future regression.
- Epic 7 retro single-evaluator principle: every dispatched command in the simulation traces back to an `EvaluationResult.intents` entry; the simulation runner makes this traceability explicit by carrying both `evaluation_result` and `commands` on `SimulationCycleResult`.

### Deferred items folded into this story (with backreferences)

This story is the natural home for two correlation_id-traceability gaps deferred from Epic 8 reviews:

- `_bmad-output/implementation-artifacts/deferred-work.md:23` (from Story 8-2 review) — *"`correlation_id` mismatch from adapter `CommandResult` passed through unchecked"* — partially addressed by AC9/AC10 making correlation_id first-class in audit detail; the assertion that `result.correlation_id == command.correlation_id` belongs to a future adapter-contract hardening story (Epic 9 territory) and is NOT in scope here.
- The DECISION emission gap is implicit in the architecture's audit_log design (`architecture.md:312-316`) but was never explicitly tracked as a deferred item — this story formalizes it.

### Deferred items (explicitly NOT implemented in this story)

- **Per-cycle DECISION audit (one row per `EvaluationResult`)** — useful for cycle-level forensics ("what decisions did the engine make at 12:34:56?") but redundant with per-command DECISION audits for the simulation's traceability requirement. Add when installer dashboard (Epic 11) needs cycle-level event filtering.
- **Failure injection in scenarios** — `_StubBatteryAdapter` always succeeds. A `failure_count` parameter on stub adapters (mirroring `tests/integration/engine/test_retry_pipeline.py:SimulatedBatteryAdapter:45`) would extend scenarios to cover retry-recovery and terminal-failure cases. Add when Story 9.4 (deployment validation) needs simulated unreachable devices.
- **Multi-cycle scenarios** — `run_simulation_cycle` runs ONE cycle. Multi-cycle scenarios (e.g., "ramp grid load over 5 cycles, verify peak limiter activates on cycle 3") would require maintaining `PartialIntervalTracker` state across calls. Add when peak-limiter timing tests are needed.
- **YAML/JSON scenario loading** — scenarios are Python dataclass instances. Loading from a file format is Epic 9/12 scope.
- **Capability profile failure-mode injection** — stubs return `CapabilityStatus.unsupported`-equivalent profiles by setting empty write_capabilities; injecting a specific REDUCED capability profile per scenario field is out of scope. Add when Epic 9's capability-strategy consistency check needs simulated REDUCED devices.
- **Concurrent-cycle scenarios** — `run_simulation_cycle` is synchronous within a single asyncio task. Concurrent dispatches (e.g., installer override racing decision-engine intents) belong to Epic 9.
- **Adapter `correlation_id` round-trip assertion** — see deferred-work.md:23 above; out of scope.

### Concrete code snippets the dev should crib from

- Audit-failure shielding pattern: `src/open_ems/engine/retry_policy.py:_emit_failure_audit` (lines 131–148)
- Simulated battery adapter shape: `tests/integration/engine/test_retry_pipeline.py:SimulatedBatteryAdapter` (lines 42–91) — same protocol, same capability profile shape, same `send_command_calls` recording
- Simulated EV charger adapter shape: `tests/integration/engine/test_retry_pipeline.py:SimulatedEVChargerAdapter` (lines 94–142)
- Audit spy with real ObservabilityService + mocked EventLogRepo: `tests/integration/engine/test_fail_safe_lifecycle.py:_audit_observability` (lines 164–180)
- Settings construction in tests: `tests/integration/engine/test_retry_pipeline.py:_settings` (lines 145–150) — including `command_max_retries=0` pattern
- StateStore + scenario state publishing: `tests/integration/engine/test_command_pipeline.py:_state_store_with_battery` (lines 83–94)

### References

- [Source: epics.md — Story 8.5 full AC text, lines 1865-1888](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\epics.md)
- [Source: epics.md — Epic 8 cross-story constraints, lines 1855-1862](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\epics.md)
- [Source: architecture.md — Decision 5.1 Structured logging, lines 311-316](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md)
- [Source: architecture.md — Pattern: All device commands pass through the PolicyGuard, lines 381-397](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md)
- [Source: architecture.md — Adapter Interface Pattern + Simulated adapters, lines 683-696](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md)
- [Source: architecture.md — CommandResult Pattern, lines 700-712](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md)
- [Source: architecture.md — Energy sign convention, lines 489-493](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md)
- [Source: src/open_ems/engine/retry_policy.py — execute() at line 63; _emit_failure_audit at lines 131-148; CancelledError audit branch from Story 8.4 AC7](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\retry_policy.py)
- [Source: src/open_ems/engine/policy_guard.py — _reject at lines 163-184; _check_safety_constraints at lines 141-161; capability check at lines 76-100](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\policy_guard.py)
- [Source: src/open_ems/engine/intent_executor.py — translate at lines 35-55; _translate_battery at lines 57-86; hold short-circuit at lines 62-63](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\intent_executor.py)
- [Source: src/open_ems/engine/evaluator.py — evaluate_cycle at line 33; peak-candidate-to-action mapping at lines 70-102](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\evaluator.py)
- [Source: src/open_ems/engine/operating_mode.py — derive_recommended_operating_mode (Story 7.1)](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\operating_mode.py)
- [Source: src/open_ems/engine/rules/peak_limiting.py — evaluate_peak_limiting at line 36; suppression modes at line 33](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\rules\peak_limiting.py)
- [Source: src/open_ems/engine/rules/energy_balancing.py — _evaluate_maximize_self_consumption at lines 170-206; resolve_conflicts total order at lines 113-134](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\rules\energy_balancing.py)
- [Source: src/open_ems/engine/rules/battery_control.py — evaluate_battery_control at line 35; reserve floor check at line 62](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\rules\battery_control.py)
- [Source: src/open_ems/engine/rules/ev_scheduling.py — evaluate_ev_scheduling at line 37; window check at lines 67-82](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\rules\ev_scheduling.py)
- [Source: src/open_ems/engine/models.py — EvaluationInput at line 108; PeakContext at line 29; from_snapshot helper at line 130](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\models.py)
- [Source: src/open_ems/services/audit_log.py — ObservabilityService.audit signature at line 34; VALID_EVENT_TYPES at line 16; serialize_audit_detail at line 21](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\services\audit_log.py)
- [Source: src/open_ems/core/commands.py — DeviceCommandBase with correlation_id default at line 67; CommandStatus at line 50; subtype is_idempotent invariant at lines 69-79](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\core\commands.py)
- [Source: src/open_ems/core/state_store.py — StateStore.publish at line 65; system_clock_status="valid" usage in tests](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\core\state_store.py)
- [Source: src/open_ems/core/state.py — SystemSnapshot at line 146; SystemOperatingMode at line 62; EnergyStrategy at line 71](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\core\state.py)
- [Source: src/open_ems/core/devices.py — DeviceRole at line 76; BatteryState/InverterState/EVChargerState/GridMeterState at lines 85-162; DeviceCapabilityProfile at line 213; DeviceAdapter Protocol](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\core\devices.py)
- [Source: src/open_ems/settings.py — Settings class at line 9; peak_limit_kw, battery_reserve_floor_percent, command_max_retries defaults at lines 30-34](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\settings.py)
- [Source: tests/integration/engine/test_command_pipeline.py — SimulatedBatteryAdapter pattern at lines 37-77; _state_store_with_battery at lines 83-94; _result_with at lines 107-115](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\tests\integration\engine\test_command_pipeline.py)
- [Source: tests/integration/engine/test_retry_pipeline.py — SimulatedEVChargerAdapter pattern at lines 94-142; _audit_spy_observability pattern at lines 213-219](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\tests\integration\engine\test_retry_pipeline.py)
- [Source: tests/integration/engine/test_fail_safe_lifecycle.py — _audit_observability spy pattern at lines 164-180; _SimulatedInverter at lines 44-70](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\tests\integration\engine\test_fail_safe_lifecycle.py)
- [Source: tests/conftest.py — _set_secret_key fixture at line 45 (autouse, sets SECRET_KEY env var so Settings construction succeeds)](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\tests\conftest.py)
- [Source: _bmad-output/implementation-artifacts/8-3-implement-retry-policy-timeout-handling-and-idempotency-enforcement.md — Story 8.3 dev notes (audit pattern, ClassVar idempotency, correlation_id presence in summary)](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\implementation-artifacts\8-3-implement-retry-policy-timeout-handling-and-idempotency-enforcement.md)
- [Source: _bmad-output/implementation-artifacts/8-4-implement-watchdog-heartbeat-stalled-loop-detection-fail-safe-transition-and-recovery.md — Story 8.4 dev notes (audit shielding pattern, asyncio.shield rationale, fail-safe state machine)](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\implementation-artifacts\8-4-implement-watchdog-heartbeat-stalled-loop-detection-fail-safe-transition-and-recovery.md)
- [Source: _bmad-output/implementation-artifacts/deferred-work.md:23 — correlation_id traceability gap (deferred from 8-2)](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\implementation-artifacts\deferred-work.md)
- [Source: _bmad-output/implementation-artifacts/epic-7-retro-2026-05-05.md — single-evaluator principle (Epic 8 cross-story constraint), P4 integration test infrastructure note](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\implementation-artifacts\epic-7-retro-2026-05-05.md)

---

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 / bmad-dev-story

### Debug Log References

- Pre-story baseline: 858 tests passing (matches sprint-status.yaml line 38).
- Post-story: 871 tests passing (delta = 13 new tests + 5 updates absorbed in-place).
- AC7 path verified empirically: Path A (engine-side hold in `evaluate_battery_control`) fires
  for the `BATTERY_BELOW_RESERVE` scenario — exactly as predicted by the dev notes
  ("Path A is the engine-side hold"). Path B (PolicyGuard rejection in isolation) is
  exercised by test #11 by directly constructing a `SetBatteryDischargeRateCommand` and
  routing it through `RetryPolicy.execute()` against the same scenario StateStore.
- AC11 / test #14 nuance: `test_full_pipeline_allowed_path` was previously hitting
  `PolicyGuard.authorize_and_dispatch` directly. Since DECISION emission is owned by
  `RetryPolicy` (not `PolicyGuard`), the test was rewired to dispatch through
  `RetryPolicy.execute()` so the AC1 emission contract is exercised end-to-end.
- AC12 test #13 sentinel: implemented as a `sys.modules` snapshot before/after the
  cycle (not a `MonkeyPatch.setattr` on `AsyncModbusTcpClient`) because no Modbus /
  OCPP / DSMR adapter is wired into the engine import path; the snapshot is the
  cleaner future-coupling sentinel and the AC explicitly permits this alternative.

### Completion Notes List

- AC1 (DECISION audit on success) — implemented in `RetryPolicy._emit_success_audit`,
  emitted at the end of the success branch immediately before `return last_result` so a
  partial-success early return cannot bypass it. The `command_dispatched` info log
  precedes the audit so the structured-log floor holds even if the audit DB write fails.
- AC9 (PolicyGuard `_reject` `detail.correlation_id`) and AC10 (`_emit_failure_audit` /
  cancellation `detail.correlation_id`) were minimal additions to the existing audit
  kwargs — `actor` / `event_type` / `summary` / `device_id` are unchanged across all
  three paths.
- AC11 success-path test renames: `test_idempotent_command_succeeds_on_first_attempt_no_audit_event`
  → `..._emits_decision_audit` (and the second-attempt counterpart). The rename + 5 new
  AC12 unit tests for retry, plus the parametrized AC12 #12 across two scenarios, plus
  detail-field assertions on every existing failure / cancellation / rejection test give
  full per-event-type correlation_id traceability coverage.
- AC2 / AC3 fixture: `tests/fixtures/scenarios.py` exposes a frozen `SimulationScenario`
  dataclass plus four `_Stub<Role>Adapter` classes that implement the `DeviceAdapter`
  structural protocol. `command_max_retries=0` is enforced via `build_scenario_settings`
  so attempt counts in audits are deterministic. Capability profiles use the adapter's
  own `device_id` (matching the engine's capability check that compares
  `profile.device_id == battery.device_id`).
- AC4 simulation runner uses real `IntentExecutor`, `PolicyGuard`, `RetryPolicy`,
  `evaluate_cycle`, `ObservabilityService` plus a mocked `EventLogRepo` — adapters are
  the ONLY simulated layer. The control loop, watchdog, and `PartialIntervalTracker`
  are intentionally out of scope (they're exercised by Story 8.4's tests).
- AC13 quality gates: 871 tests passing (baseline 858 + 13 new), ruff check clean,
  ruff format clean, mypy `src/` clean, scenario smoke check imports and prints
  `peak_limit_kw=25.0`.
- Deferred items (per Dev Notes "Deferred items explicitly NOT implemented"): per-cycle
  DECISION audit, failure injection in stub adapters, multi-cycle scenarios, YAML
  scenario loading, capability-profile failure-mode injection, concurrent-cycle
  scenarios, adapter `correlation_id` round-trip assertion. None are in scope here.

### File List

**Production source — modified**
- `src/open_ems/engine/retry_policy.py` — added `_emit_success_audit`; emit DECISION
  audit + `command_dispatched` info log on success path; extended `_emit_failure_audit`
  and `_emit_cancellation_audit` with `detail.correlation_id` payloads; updated module
  docstring to reflect new DECISION emission contract.
- `src/open_ems/engine/policy_guard.py` — extended `_reject` audit kwargs with
  `detail={"correlation_id", "command_type", "rejection_reason"}`.

**Tests — new**
- `tests/fixtures/__init__.py` — empty package marker so `tests.fixtures.scenarios`
  is importable.
- `tests/fixtures/scenarios.py` — `SimulationScenario` frozen dataclass; four
  `_Stub<Role>Adapter` classes; capability-profile helpers; `_floor_15min`;
  `build_scenario_state_store` / `build_scenario_adapters` /
  `build_scenario_settings` / `build_scenario_evaluation_input` builders.
- `tests/integration/engine/test_decision_engine_simulation.py` — `SimulationCycleResult`
  dataclass; `run_simulation_cycle` runner; `_audit_spy_observability` spy helper; tests
  #8–#13 from AC12 (curtail-load, excess-solar, AC7 Path A engine-side hold, AC7 Path B
  PolicyGuard rejection in isolation, parametrized AC8 per-command audit invariant,
  no-real-adapter-touched sentinel).

**Tests — updated**
- `tests/unit/engine/test_retry_policy.py` — renamed two `..._no_audit_event` tests to
  `..._emits_decision_audit` and updated assertions; added five AC12 tests (#1 first-
  attempt success DECISION audit, #2 retry-recovery DECISION audit, #3 audit-emit
  failure swallowed on success path, #4 failure audit `detail.correlation_id`,
  #5 cancellation audit `detail.correlation_id`); added `detail.correlation_id`
  assertions to every existing failure / timeout / rejection / retry-stop test.
- `tests/unit/engine/test_policy_guard.py` — added AC9 `detail.correlation_id`
  assertion to `test_constraint_violation_rejected_with_audit_event`; added new AC12
  test #7 `test_reject_audit_includes_correlation_id_in_detail`.
- `tests/integration/engine/test_command_pipeline.py` — rewired
  `test_full_pipeline_allowed_path` through `RetryPolicy` and replaced the
  `assert_not_awaited()` with a DECISION audit assertion; added AC9 `detail.correlation_id`
  check to `test_full_pipeline_rejected_path`.
- `tests/integration/engine/test_retry_pipeline.py` — replaced
  `audit_spy.assert_not_awaited()` with DECISION audit assertion in
  `test_full_pipeline_with_idempotent_retry_recovery` (attempts=3); added AC10
  `detail.correlation_id` block to `test_full_pipeline_with_non_idempotent_fail_closed`.

### Change Log

| Date | Author | Change |
|---|---|---|
| 2026-05-10 | bmad-create-story | Initial story creation |
| 2026-05-10 | bmad-dev-story | Implemented all ACs (AC1–AC13). Added DECISION audit on RetryPolicy success path; extended PolicyGuard `_reject` and RetryPolicy failure / cancellation audits with `detail.correlation_id`; created `tests/fixtures/scenarios.py` with `SimulationScenario` + 4 stub adapters; created `tests/integration/engine/test_decision_engine_simulation.py` with `run_simulation_cycle` and 6 scenario tests; updated 4 existing tests for the new DECISION emission contract. Final: 871 tests passing; ruff / format / mypy(src) all clean. |
| 2026-05-10 | bmad-code-review | Adversarial review (Blind Hunter + Edge Case Hunter + Acceptance Auditor). 4 patches recommended, 5 deferred, 14 dismissed. No decision-needed findings. No spec-blocking issues. |
| 2026-05-10 | bmad-code-review | Applied all 4 review patches: stub-adapter device_id asserts, explicit `None` check on `ev_current_power_kw`, module-level imports in `test_retry_policy.py`, dropped unused `default_factory` on `SimulationCycleResult.adapter_calls`. Post-patch: 871 tests passing, ruff/format/mypy(src) clean. Status → `done`. |

### Review Findings

- [x] [Review][Patch] `_StubXAdapter.send_command` should assert `cmd.device_id == self.device_id` to fail loudly on cross-wired tests [`tests/fixtures/scenarios.py` — `_StubBatteryAdapter.send_command`, `_StubEVChargerAdapter.send_command`] — applied 2026-05-10
- [x] [Review][Patch] Replace `or 0.0` truthy collapse with explicit `None` check on `ev_current_power_kw` [`tests/fixtures/scenarios.py` — `build_scenario_evaluation_input` projection_kw line] — applied 2026-05-10
- [x] [Review][Patch] Move mid-function `import asyncio` / `import pytest` to module-level imports [`tests/unit/engine/test_retry_policy.py:test_cancellation_audit_includes_correlation_id_in_detail`] — applied 2026-05-10 (also cleaned up two pre-existing duplicate local imports at lines ~417 and ~485 since module-level import now covers them)
- [x] [Review][Patch] Drop the unused `default_factory=dict` on `SimulationCycleResult.adapter_calls` — runner always populates it; the default masks "forgot to record" [`tests/integration/engine/test_decision_engine_simulation.py` — `SimulationCycleResult.adapter_calls`] — applied 2026-05-10 (also dropped now-unused `field` import)
- [x] [Review][Defer] Cancellation during `asyncio.sleep(backoff)` reports `attempts=N` even though only N-1 dispatches completed [`src/open_ems/engine/retry_policy.py:execute` — `attempt` loop variable + line 131] — deferred, pre-existing (Story 8.4 cancellation handling, not introduced by 8.5)
- [x] [Review][Defer] Cancellation audit `detail` lacks `final_reason` while failure audit includes it — schema asymmetry [`src/open_ems/engine/retry_policy.py:_emit_cancellation_audit` vs `_emit_failure_audit`] — deferred, downstream consumers parsing `detail["final_reason"]` blindly will `KeyError` on cancellation rows; document the asymmetry or unify
- [x] [Review][Defer] EXCESS_SOLAR scenario relies on dataclass defaults for `homeowner_override_active`, `ev_charging_window`, `target_ev_charge_rate_kw` rather than pinning explicit values [`tests/integration/engine/test_decision_engine_simulation.py` — `EXCESS_SOLAR`] — deferred, robustness nit; a future default change would silently shift scenario behaviour
- [x] [Review][Defer] "Structured-log floor" comment is misleading — the info log only emits on the success branch, not as a global dispatch floor [`src/open_ems/engine/retry_policy.py:101-102`] — deferred, comment-only refinement
- [x] [Review][Defer] DECISION-audit summary substring assertions (e.g., `"success" in summary`) are weak — pin the template structurally [multiple test files] — deferred, hardening, not a bug

#### Dismissed (noise / by-design / spec-mandated)

- Cancellation during `_emit_success_audit` propagates `CancelledError` and triggers `_emit_cancellation_audit` (DEVICE) — **spec-approved** in Dev Notes "Avoiding double-emission of DECISION + DEVICE": *"Cancellation during success-audit emission propagates `CancelledError` and falls into the except block — but at that point, the success result has already been determined; the DEVICE audit emitted in the cancellation branch is the correct outcome."*
- Cancellation `detail.command_status` is the literal string `"cancelled"` rather than a `CommandStatus` enum value — **spec-mandated** by AC10: *`detail={..., "command_status": "cancelled", ...}`* (no enum value exists for cancellation by design — result is indeterminate).
- Rejected commands produce both CONSTRAINT (PolicyGuard) and DEVICE (RetryPolicy) audits — **by-design**, documented in `RetryPolicy` module docstring lines 19–21: *"PolicyGuard's separate CONSTRAINT audit events for rejections are unchanged."*
- `if last_result.applied:` gate without explicit `status is CommandStatus.success` check — **equivalent**: `CommandResult._applied_iff_success` Pydantic validator (`src/open_ems/core/commands.py:130-140`) makes the two predicates mathematically identical at construction time.
- Test imports `_StubXAdapter` underscore-prefixed names across module boundary — **acceptable**: tests are part of the test infrastructure; this is a common test-fixture convention.
- `sys.modules` snapshot uses delta (`after - before`) rather than absolute (`after == ∅`) check — **spec-permitted** per AC12 #13 (*"OR by patching..."*) and dev log: the delta is sufficient as a "future coupling" sentinel.
- Spy monkey-patches `obs.audit` directly — **spec-mandated pattern** from `_audit_observability` at `tests/integration/engine/test_fail_safe_lifecycle.py:164-180` (cited in spec Concrete Code Snippets).
- AC8 test asserts `event_type in {"DECISION", "CONSTRAINT", "DEVICE"}` (set membership) rather than pinning DECISION specifically — **spec-mandated** AC8 wording: *"`kwargs['event_type']` is one of `{"DECISION", "CONSTRAINT", "DEVICE"}`"*.
- AC7 fixture exercises only the SoC-equal-to-floor boundary, not strictly-below — **spec-mandated** AC7 wording: *"AT reserve floor — `<=` triggers rejection"*.
- `_floor_15min` raises `ValueError` on non-UTC `at` — **fail-loud is correct**.
- `pv == ac` boundary in EXCESS_SOLAR is not exercised — **out of scope**; AC6 specifies `pv=6.0, ac=4.0` exactly.
- Hardcoded `"success"` in success-audit summary while reading `result.applied` — **gate ensures `applied=True`** when this branch runs.
- `frozen=True` `SimulationCycleResult` contains mutable list/dict — **harmless today** (test scope); a real concern only if `run_simulation_cycle` is reused across cycles, which is out of scope for 8.5.
- `assert_awaited_once()` on integration tests masks future double-DECISION regressions — **mitigated**: per-event-type uniqueness IS asserted in `test_all_commands_produce_event_log_rows` (#12), which is the AC8 contract.
