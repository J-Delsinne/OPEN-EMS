# Story 8.3: Implement retry policy, timeout handling, and idempotency enforcement

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want failed idempotent commands retried up to twice with safety re-checks before each retry, non-idempotent commands failed closed immediately, and all commands timed out within bounds,
so that transient device failures recover automatically without risking duplicate effects or safety violations from conflicting commands.

## Acceptance Criteria

**AC1 — Idempotency classification on every DeviceCommand subtype**

GIVEN any `DeviceCommand` subtype defined in `src/open_ems/core/commands.py`
THEN it carries an immutable, class-level `is_idempotent: ClassVar[bool]` attribute (NOT a constructor parameter — it is a property of the command type, not the instance)
AND the following classification holds:
- `SetBatteryChargeRateCommand.is_idempotent = True` — re-applying the same target rate converges to the same device state
- `SetBatteryDischargeRateCommand.is_idempotent = True` — same reasoning
- `SetEVChargingRateCommand.is_idempotent = True` — same reasoning
- `StopEVChargingCommand.is_idempotent = False` — terminates an OCPP session; re-issuing risks session-state churn (start/stop transitions are non-idempotent operations)
AND the classification is exposed via a single helper `command.is_idempotent` accessor (read directly from the class attribute via the instance)

**AC2 — Retry policy: idempotent commands retried up to N times after PolicyGuard re-check**

GIVEN PolicyGuard returns `CommandResult(status=CommandStatus.failed)` or `CommandResult(status=CommandStatus.timeout)` for a command where `command.is_idempotent is True`
WHEN the retry policy evaluates
THEN the runtime executes up to `Settings.command_max_retries` ADDITIONAL attempts (default 2 — total 3 attempts maximum) by re-invoking `PolicyGuard.authorize_and_dispatch(command)` with the SAME `command` instance (same `correlation_id`)
AND before each retry, PolicyGuard re-evaluates the FULL safety pipeline (operating mode, capability, safety constraints) — this is automatic because we re-enter `authorize_and_dispatch()` from the top
AND if a re-check produces `CommandResult(status=CommandStatus.rejected)`, retries STOP IMMEDIATELY: the rejected result is the final outcome, no further retries are attempted, and the rejection is treated as a final failure (not a retry-eligible failure)
AND between attempts, an `asyncio.sleep(Settings.command_retry_backoff_seconds)` (default 0.5 s) delays the next attempt — keep the backoff small because the surrounding evaluation cycle is bounded by `control_loop_interval_seconds` (default 10 s)
AND each retry attempt is counted: a `command_retry` debug log fires with `attempt`, `max_attempts`, `prior_status`, `prior_reason`, `device_id`, `correlation_id` — for observability

**AC3 — Non-idempotent commands fail closed on first failure**

GIVEN PolicyGuard returns `CommandResult(status=CommandStatus.failed)`, `status=CommandStatus.timeout`, or `status=CommandStatus.rejected` for a command where `command.is_idempotent is False`
WHEN the retry policy evaluates
THEN no retry is attempted — the first non-success result is the final outcome
AND a `non_idempotent_command_not_retried` debug log fires with `device_id`, `command_type`, `status`, `reason`, `correlation_id`

**AC4 — Final-failure audit event after retries exhausted (or non-idempotent fail-closed)**

GIVEN a command's final outcome is `CommandResult.applied is False` (any combination of `status` and `applied=False` after all retries)
WHEN the runtime processes the final result
THEN exactly ONE `ObservabilityService.audit()` call is made with:
- `actor="system"`
- `event_type="DEVICE"` (NOT `"CONSTRAINT"` — `"CONSTRAINT"` is reserved for PolicyGuard rejections, which already emit their own audit event from inside `_reject()`)
- `summary` is a plain-language string: `f"Command {command_type} for {device_id} not applied after {attempts} attempt(s): {final_reason}"`
- `device_id=command.device_id`
AND the audit event is emitted from the `RetryPolicy` (not from `PolicyGuard` — `PolicyGuard._reject()` continues to emit its own `CONSTRAINT` event independently per Story 8.2)
AND if the final outcome was `status=CommandStatus.rejected` (re-check failure during a retry cycle), the `event_type` is still `"DEVICE"` and the summary includes the rejection reason — the `CONSTRAINT` event from `PolicyGuard._reject()` records the rejection separately; the `DEVICE` event records the not-applied outcome

**AC5 — Successful command (after zero or more retries) emits NO additional audit event**

GIVEN a command's final outcome is `CommandResult.applied is True`
THEN no audit event is emitted by the retry layer (success is the expected case; an audit event for every successful command would flood the event log)
AND a `command_applied` debug log fires with `device_id`, `command_type`, `attempts`, `correlation_id` — for diagnostic observability only

**AC6 — Timeout handling already enforced by PolicyGuard; verify control loop never blocks beyond the bound**

GIVEN PolicyGuard already wraps `adapter.send_command()` in `asyncio.wait_for(..., timeout=COMMAND_DISPATCH_TIMEOUT_SECONDS)` (Story 8.2)
THEN the new `RetryPolicy` MUST NOT add a separate timeout around `policy_guard.authorize_and_dispatch()` — PolicyGuard owns the per-attempt timeout
AND the maximum total time spent in `RetryPolicy.execute(command)` is bounded by `(COMMAND_DISPATCH_TIMEOUT_SECONDS + CAPABILITY_CHECK_TIMEOUT_SECONDS + command_retry_backoff_seconds) * (1 + command_max_retries)` ≤ ~46 s with defaults
AND the control loop runs `RetryPolicy.execute()` SEQUENTIALLY for each command (consistent with Story 8.2's sequential dispatch loop) — concurrent dispatch across commands is NOT introduced by this story (deferred to a future story)

**AC7 — RetryPolicy is the new sole call site for `PolicyGuard.authorize_and_dispatch()` from the control loop**

GIVEN the control loop (`engine/control_loop.py`) iterates over commands produced by `IntentExecutor.translate()`
WHEN it dispatches each command
THEN it MUST call `await self._retry_policy.execute(command)` — NOT `await self._policy_guard.authorize_and_dispatch(command)` directly
AND `RetryPolicy.execute(command) -> CommandResult` returns the FINAL `CommandResult` (after all retries)
AND the existing `if not cmd_result.applied: logger.warning("command_not_applied", ...)` log in `_handle_evaluation_result` is REMOVED — it is replaced by the AC4/AC5 logging inside `RetryPolicy`
AND `PolicyGuard.authorize_and_dispatch()` remains the SOLE call site for `adapter.send_command()` (AR15 unchanged)
AND no other component outside the engine calls `PolicyGuard.authorize_and_dispatch()` directly today; if such a call is added in a future story (Epic 9 installer setup, Epic 10 homeowner overrides), that caller must also route through `RetryPolicy` — document this in the new module's docstring

**AC8 — Settings extensions for retry tunables**

GIVEN `src/open_ems/settings.py` exposes the existing `Settings` class
THEN it gains exactly these two new fields:
- `command_max_retries: int = Field(default=2, ge=0, le=5)` — additional attempts after the first; `0` disables retries entirely; capped at 5 to prevent runaway retry storms
- `command_retry_backoff_seconds: float = Field(default=0.5, ge=0.0, le=5.0)` — fixed backoff between attempts; `0.0` disables sleep
AND both fields are wired through environment variables via `pydantic-settings` automatic mapping (`COMMAND_MAX_RETRIES`, `COMMAND_RETRY_BACKOFF_SECONDS`)
AND existing `Settings()` constructions in tests using `Settings(_env_file=None)` continue to work unchanged

**AC9 — app.py wires RetryPolicy into ControlLoop**

GIVEN application startup runs `src/open_ems/web/app.py` lifespan
WHEN ControlLoop is instantiated
THEN `RetryPolicy(policy_guard=policy_guard, observability=ObservabilityService(), settings=settings)` is constructed BEFORE `ControlLoop(...)`
AND ControlLoop receives `retry_policy=retry_policy` as a new keyword argument
AND the existing `policy_guard=policy_guard` argument to `ControlLoop` is REMOVED (the loop no longer dispatches directly; it dispatches via `RetryPolicy`)
AND `ObservabilityService()` should be constructed once in lifespan and shared between `PolicyGuard` and `RetryPolicy` — do NOT construct two separate `ObservabilityService` instances; this matches the current 8.2 pattern where `app.py` constructs one observability instance and passes it to PolicyGuard
AND no new database migration is required for this story

**AC10 — Tests**

Unit tests in `tests/unit/engine/test_retry_policy.py` (NEW file):

1. `test_idempotent_command_retried_up_to_max_retries_after_failure()` — `is_idempotent=True` command + adapter returns `failed` 3 times → `policy_guard.authorize_and_dispatch` called exactly 3 times (1 + 2 retries) → final result is `failed` → ONE `audit(event_type="DEVICE")` call
2. `test_idempotent_command_succeeds_on_second_attempt_no_audit_event()` — `is_idempotent=True` command + adapter returns `failed` then `success` → `policy_guard.authorize_and_dispatch` called exactly 2 times → final result is `success`/`applied=True` → ZERO `audit()` calls from RetryPolicy
3. `test_idempotent_command_succeeds_on_first_attempt_no_audit_event()` — `is_idempotent=True` command + adapter returns `success` → exactly 1 call → ZERO `audit()` calls from RetryPolicy
4. `test_non_idempotent_command_not_retried_on_failure()` — `is_idempotent=False` (`StopEVChargingCommand`) + adapter returns `failed` → exactly 1 call to `policy_guard.authorize_and_dispatch` → ONE `audit(event_type="DEVICE")` call → final result is `failed`
5. `test_non_idempotent_command_not_retried_on_timeout()` — `is_idempotent=False` + PolicyGuard returns `timeout` → exactly 1 call → ONE `audit(event_type="DEVICE")` call
6. `test_non_idempotent_command_not_retried_on_rejection()` — `is_idempotent=False` + PolicyGuard returns `rejected` → exactly 1 call → ONE `audit(event_type="DEVICE")` call (RetryPolicy emits the DEVICE event; PolicyGuard's CONSTRAINT event is emitted from inside `_reject()` independently)
7. `test_retry_stops_immediately_on_rejection_during_retry_cycle()` — `is_idempotent=True` + adapter returns `failed`, then on retry PolicyGuard returns `rejected` (e.g., constraints changed mid-cycle) → exactly 2 calls (1 + 1 retry that was rejected) → no further retries → ONE `audit(event_type="DEVICE")` from RetryPolicy → final result is `rejected`
8. `test_retry_count_zero_disables_retries()` — `Settings(command_max_retries=0)` + idempotent command + adapter returns `failed` → exactly 1 call → ONE `audit(event_type="DEVICE")` call
9. `test_backoff_sleep_invoked_between_retries()` — `is_idempotent=True` + adapter returns `failed` twice then `success` + `Settings(command_retry_backoff_seconds=0.01)` → `asyncio.sleep` called 2 times (between attempt 1→2 and 2→3); use `monkeypatch` on `open_ems.engine.retry_policy.asyncio.sleep` (NOT the module-level `asyncio` import in tests) or use `unittest.mock.patch("open_ems.engine.retry_policy.asyncio.sleep", new=AsyncMock())`
10. `test_audit_event_summary_includes_attempt_count_and_final_reason()` — `is_idempotent=True` + adapter returns `failed("communication_error")` for all 3 attempts → audit summary string contains `"after 3 attempt(s)"` AND `"communication_error"`
11. `test_correlation_id_preserved_across_retries()` — `is_idempotent=True` + adapter returns `failed` then `success` → both `policy_guard.authorize_and_dispatch` calls receive an instance with the SAME `correlation_id`

Unit test in `tests/unit/core/test_commands.py` (NEW file or extend existing if present):

12. `test_command_subtypes_idempotency_classification()` — assert exact classification: `SetBatteryChargeRateCommand.is_idempotent is True`, `SetBatteryDischargeRateCommand.is_idempotent is True`, `SetEVChargingRateCommand.is_idempotent is True`, `StopEVChargingCommand.is_idempotent is False`
13. `test_is_idempotent_is_classvar_not_instance_field()` — assert `is_idempotent` does NOT appear in `model_fields` (it must be a `ClassVar`, not a Pydantic field — otherwise `extra="forbid"` would reject it as an unexpected init kwarg)

Unit test in `tests/unit/engine/test_control_loop.py` (UPDATE existing helper):

14. Update `_control_loop()` helper to inject a `MagicMock(spec=RetryPolicy)` instead of `policy_guard` (or in addition, depending on signature change); existing 7+ control-loop tests must continue to pass
15. NEW test `test_control_loop_dispatches_via_retry_policy_not_policy_guard()` — assert `RetryPolicy.execute` is awaited once per produced command and `PolicyGuard.authorize_and_dispatch` is NOT called directly from the control loop

Integration test in `tests/integration/engine/test_retry_pipeline.py` (NEW file):

16. `test_full_pipeline_with_idempotent_retry_recovery()` — typed `BatteryIntent(charge)` → `IntentExecutor` → `SetBatteryChargeRateCommand` → `RetryPolicy` → `PolicyGuard` → `SimulatedBatteryAdapter` configured to fail twice then succeed → final `CommandResult(status=success, applied=True)` → ZERO `DEVICE` audit events; ZERO `CONSTRAINT` audit events
17. `test_full_pipeline_with_non_idempotent_fail_closed()` — typed `EVChargerIntent(stop)` → `IntentExecutor` → `StopEVChargingCommand` → `RetryPolicy` → `PolicyGuard` → `SimulatedEVChargerAdapter` configured to fail once → final `CommandResult(status=failed, applied=False)` → ZERO retries → ONE `DEVICE` audit event

---

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — record baseline test count (expected: 798 from Story 8.2)
  - [x] Run `uv run python -m ruff check .` — must be clean
  - [x] Run `uv run python -m ruff format --check .` — must be clean
  - [x] Run `uv run python -m mypy src/` — must be clean

- [x] Task 1: Add `is_idempotent` ClassVar to command subtypes (AC1, AC10 #12, #13)
  - [x] Open `src/open_ems/core/commands.py`
  - [x] Add `from typing import ClassVar` to existing typing imports (already imports `Annotated`, `Literal`)
  - [x] Add `is_idempotent: ClassVar[bool]` to each of the 4 concrete command classes:
    - `SetBatteryChargeRateCommand.is_idempotent: ClassVar[bool] = True`
    - `SetBatteryDischargeRateCommand.is_idempotent: ClassVar[bool] = True`
    - `SetEVChargingRateCommand.is_idempotent: ClassVar[bool] = True`
    - `StopEVChargingCommand.is_idempotent: ClassVar[bool] = False`
  - [x] **Do NOT** add it to `DeviceCommandBase` — that would push a default into all subtypes; force each subtype to declare its idempotency explicitly. This is intentional: any new command subtype added in a future story MUST think about idempotency, not inherit it silently. (If you choose to put it on the base for forward-compat, you MUST set it to `False` as the safe default — do NOT default to `True`.)
  - [x] Verify via `python -c "from open_ems.core.commands import StopEVChargingCommand; print(StopEVChargingCommand.is_idempotent)"` — should print `False`
  - [x] Update module docstring to document the idempotency classification rationale
  - [x] Create `tests/unit/core/test_commands.py` with tests #12 and #13 from AC10

- [x] Task 2: Implement `RetryPolicy` (AC2, AC3, AC4, AC5, AC6)
  - [x] Create `src/open_ems/engine/retry_policy.py`
  - [x] Define class `RetryPolicy` with `__init__(self, *, policy_guard: PolicyGuard, observability: ObservabilityService, settings: Settings) -> None`
  - [x] Define `async def execute(self, command: DeviceCommand) -> CommandResult` returning the final result after all retries
  - [x] Implement the loop:
    ```python
    max_attempts = self._settings.command_max_retries + 1
    last_result: CommandResult | None = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            logger.debug("command_retry", attempt=attempt, max_attempts=max_attempts,
                         prior_status=last_result.status.value, prior_reason=last_result.reason,
                         device_id=command.device_id, correlation_id=str(command.correlation_id),
                         component="engine")
            if self._settings.command_retry_backoff_seconds > 0:
                await asyncio.sleep(self._settings.command_retry_backoff_seconds)
        last_result = await self._policy_guard.authorize_and_dispatch(command)
        if last_result.applied:
            logger.debug("command_applied", device_id=command.device_id,
                         command_type=type(command).__name__, attempts=attempt,
                         correlation_id=str(command.correlation_id), component="engine")
            return last_result
        if not command.is_idempotent:
            logger.debug("non_idempotent_command_not_retried", device_id=command.device_id,
                         command_type=type(command).__name__, status=last_result.status.value,
                         reason=last_result.reason, correlation_id=str(command.correlation_id),
                         component="engine")
            break
        if last_result.status is CommandStatus.rejected:
            # Re-check rejection — stop immediately, don't keep retrying a known-unsafe command
            break
    # Emit final-failure DEVICE audit event
    assert last_result is not None  # at least one attempt ran
    await self._emit_failure_audit(command, last_result, attempts=attempt)
    return last_result
    ```
  - [x] Implement `async def _emit_failure_audit(self, command, result, *, attempts: int) -> None` — wraps `observability.audit()` in a `try/except` that logs `audit_emit_failed` on error and returns (mirrors the pattern in `PolicyGuard._reject()` from Story 8.2 review). Audit-write failures must NOT mask the underlying command failure.
  - [x] Audit summary: `f"Command {type(command).__name__} for {command.device_id} not applied after {attempts} attempt(s): {result.reason}"`
  - [x] **Forbidden imports**: `open_ems.web`, `open_ems.adapters`, `open_ems.storage` (same boundary rules as PolicyGuard)
  - [x] **Allowed imports**: `asyncio`, `structlog`, `open_ems.core.commands`, `open_ems.engine.policy_guard`, `open_ems.services.audit_log`, `open_ems.settings`

- [x] Task 3: Wire RetryPolicy into ControlLoop (AC7)
  - [x] Open `src/open_ems/engine/control_loop.py`
  - [x] **Replace** the `policy_guard: PolicyGuard` constructor kwarg with `retry_policy: RetryPolicy` (do NOT keep both — the loop no longer dispatches directly)
  - [x] Update import: `from open_ems.engine.retry_policy import RetryPolicy` (remove the `PolicyGuard` import if no longer referenced from this module)
  - [x] In `_handle_evaluation_result`, replace `cmd_result = await self._policy_guard.authorize_and_dispatch(cmd)` with `cmd_result = await self._retry_policy.execute(cmd)`
  - [x] **REMOVE** the `if not cmd_result.applied: logger.warning("command_not_applied", ...)` block — the new logging is inside `RetryPolicy` (AC5 emits debug on success; AC4 emits audit on failure). The control loop no longer logs per-command outcomes.
  - [x] Update class docstring to reflect that dispatch goes through `RetryPolicy`

- [x] Task 4: Settings — add retry tunables (AC8)
  - [x] Open `src/open_ems/settings.py`
  - [x] Add after `battery_reserve_floor_percent`:
    ```python
    command_max_retries: int = Field(default=2, ge=0, le=5)
    command_retry_backoff_seconds: float = Field(default=0.5, ge=0.0, le=5.0)
    ```
  - [x] No env-var aliases needed — pydantic-settings auto-derives `COMMAND_MAX_RETRIES` and `COMMAND_RETRY_BACKOFF_SECONDS`

- [x] Task 5: Wire app.py lifespan (AC9)
  - [x] Open `src/open_ems/web/app.py`
  - [x] Add `from open_ems.engine.retry_policy import RetryPolicy`
  - [x] After `policy_guard = PolicyGuard(...)` and BEFORE `ControlLoop(...)` instantiation:
    ```python
    retry_policy = RetryPolicy(
        policy_guard=policy_guard,
        observability=observability,  # SAME instance passed to PolicyGuard
        settings=settings,
    )
    ```
  - [x] If the current code constructs a fresh `ObservabilityService()` inline inside the `PolicyGuard(...)` call instead of binding it to a local variable, REFACTOR that to a single `observability = ObservabilityService()` line above the PolicyGuard construction, then pass the same `observability` to both PolicyGuard and RetryPolicy. This is required by AC9 and prevents two diverging audit sinks.
  - [x] Replace `policy_guard=policy_guard` in `ControlLoop(...)` with `retry_policy=retry_policy`
  - [x] No new database migration

- [x] Task 6: Update `engine/__init__.py` (AC: housekeeping)
  - [x] Add `from open_ems.engine.retry_policy import RetryPolicy`
  - [x] Add `"RetryPolicy"` to `__all__` (alphabetical position between `PolicyGuard` and `StrategyEvaluation`)

- [x] Task 7: Write unit tests (AC10 #1–#11, #14, #15)
  - [x] Create `tests/unit/engine/test_retry_policy.py` — implement tests #1–#11 from AC10
  - [x] Use `MagicMock(spec=PolicyGuard)` with `authorize_and_dispatch = AsyncMock(side_effect=[...])` to script per-attempt results
  - [x] Use the `_observability_with_audit_spy()` pattern from `test_policy_guard.py` to spy on audit calls (read it for the exact pattern)
  - [x] Patch `asyncio.sleep` via `unittest.mock.patch("open_ems.engine.retry_policy.asyncio.sleep", new=AsyncMock())` for backoff-related tests — DO NOT actually sleep in tests
  - [x] Update `tests/unit/engine/test_control_loop.py::_control_loop()` helper to inject a `MagicMock(spec=RetryPolicy)` (see Dev Notes for exact pattern)
  - [x] Add test #15 from AC10 to `test_control_loop.py`

- [x] Task 8: Write integration tests (AC10 #16, #17)
  - [x] Create `tests/integration/engine/test_retry_pipeline.py`
  - [x] Reuse the `SimulatedBatteryAdapter` pattern from `tests/integration/engine/test_command_pipeline.py` — extend it with a configurable `_failure_count` so the first N `send_command()` calls return `failed` and subsequent calls return `success`
  - [x] Add a `SimulatedEVChargerAdapter` (analogous structure) for the non-idempotent test
  - [x] Use REAL `RetryPolicy`, REAL `PolicyGuard`, REAL `IntentExecutor`, REAL `StateStore`, REAL `Settings(_env_file=None)`; mock only the `EventLogRepo` underneath `ObservabilityService` (use `AsyncMock(spec=EventLogRepo)`)

- [x] Task 9: Final validation (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — all tests pass; record final count
  - [x] Run `uv run python -m ruff check .` — clean
  - [x] Run `uv run python -m ruff format --check .` — clean
  - [x] Run `uv run python -m mypy src/` — clean
  - [x] Manually verify the new `Settings` fields appear correctly when constructed: `python -c "from open_ems.settings import Settings; s = Settings(_env_file=None); print(s.command_max_retries, s.command_retry_backoff_seconds)"` → expected `2 0.5`

### Review Findings

- [x] [Review][Patch] Annotate DEVICE audit summary as indeterminate when `result.status is CommandStatus.timeout` (timeout is indeterminate — the command may or may not have been applied; "not applied" wording risks operator double-issue races, especially for non-idempotent commands like StopEVChargingCommand). New format on timeout: `"Command {type} for {device_id} outcome indeterminate after {attempts} attempt(s) (timeout): {reason}"`. Other statuses unchanged. [src/open_ems/engine/retry_policy.py:120-123]

- [x] [Review][Patch] Remove dead `attempt = 0` initialization above the for-loop [src/open_ems/engine/retry_policy.py:68]
- [x] [Review][Patch] Add exception details to `audit_emit_failed` error log (currently logs only static fields with no `exc_info` / `error` capture, making audit-write failures undiagnosable) [src/open_ems/engine/retry_policy.py:131-139]
- [x] [Review][Patch] Add `__init_subclass__` on `DeviceCommandBase` enforcing `is_idempotent` is declared in subclass `__dict__` (not silently inherited from a sibling concrete class) — current docstring promise has no code enforcement [src/open_ems/core/commands.py:59-67]
- [x] [Review][Patch] Add `model_validator` to `CommandResult` enforcing `applied is True ⇔ status is CommandStatus.success` — guards against a malformed adapter result with `applied=True, status=failed` silently short-circuiting retry [src/open_ems/core/commands.py CommandResult]
- [x] [Review][Patch] Remove the unused `patch("open_ems.engine.retry_policy.asyncio.sleep", ...)` from `test_full_pipeline_with_idempotent_retry_recovery` — `command_retry_backoff_seconds=0.0` already short-circuits the sleep, so the patch verifies nothing [tests/integration/engine/test_retry_pipeline.py ~230-237]

- [x] [Review][Defer] `asyncio.sleep` between attempts not shielded against cancellation — control-loop shutdown mid-retry drops in-flight retry without DEVICE audit [src/open_ems/engine/retry_policy.py:82-83] — deferred, broader cancellation policy belongs to Story 8.4 watchdog work
- [x] [Review][Defer] No test covers heterogeneous retry-status sequences (e.g., failed → timeout → rejected); audit summary only carries final reason [tests/unit/engine/test_retry_policy.py] — deferred, coverage gap not bug
- [x] [Review][Defer] `RetryPolicy.execute` has no per-device serialization — future external callers (Epic 9 installer / Epic 10 homeowner) could race with control-loop dispatch [src/open_ems/engine/retry_policy.py] — deferred, deliberately out of scope per Dev Notes "deferred items"

---

## Dev Notes

### Why this story exists in this exact shape

Story 8.2 already implements: PolicyGuard, IntentExecutor, control loop wiring, per-command 10 s `asyncio.wait_for` timeout inside `PolicyGuard.authorize_and_dispatch()`, and exception wrapping (no raw exceptions propagate — AR16). What is **missing** and is this story's job:

1. There is no retry on `failed` or `timeout` results today — a single transient communication error permanently fails the command for that cycle. (Cycle re-runs naturally re-evaluate from scratch on the next tick, but within-cycle recovery is missing.)
2. There is no idempotency contract on commands — every command is treated identically, but `StopEVChargingCommand` cannot be safely retried (start/stop session transitions).
3. There is no `DEVICE` audit event for unacknowledged commands — only `CONSTRAINT` (PolicyGuard rejections) and operational `command_not_applied` warnings; FR14 + epic AC4 require a `DEVICE` audit event for not-applied final outcomes.

The retry layer sits BETWEEN the control loop and PolicyGuard. The control loop no longer calls PolicyGuard directly; it calls `RetryPolicy.execute(cmd)`, which in turn calls `PolicyGuard.authorize_and_dispatch(cmd)` 1–N times.

```
EvaluationResult.intents
    ↓ IntentExecutor.translate()        [Story 8.2 — unchanged]
list[DeviceCommand]
    ↓ RetryPolicy.execute(cmd)          [Story 8.3 — NEW]
        ↓ PolicyGuard.authorize_and_dispatch(cmd)   [Story 8.2 — unchanged, called 1–N times]
            ↓ adapter.send_command(cmd)             [Story 8.2 — unchanged, sole call site]
        ← CommandResult (per attempt)
    ← CommandResult (final)
```

### Idempotency classification — design rationale

- `SetBatteryChargeRateCommand` / `SetBatteryDischargeRateCommand` / `SetEVChargingRateCommand` are **rate-setpoint commands**. Re-sending the same setpoint to a battery or EV charger converges to the same physical state — the device interprets the command as "set the rate to X kW" regardless of how many times it's received.
- `StopEVChargingCommand` is a **session-state transition** in OCPP terms (terminates an active session via `RemoteStopTransaction` or equivalent). Re-issuing a stop after the first one succeeded can either:
  - Fail (no active session to stop) — produces a confusing error
  - Affect a **subsequent** session if the first stop completed and a new session began between attempts — a real safety concern in EV charging
- The `is_idempotent: ClassVar[bool]` attribute is a **class-level** property (NOT a per-instance field) because idempotency is intrinsic to the command type, not to a particular instance. Pydantic `BaseModel` with `ConfigDict(extra="forbid")` will reject `is_idempotent=...` as a constructor kwarg — `ClassVar` is the correct typing-level signal that this is not a Pydantic field. Verify this works: `SetBatteryChargeRateCommand(device_id="x", device_role=DeviceRole.battery, origin=CommandOrigin.system, rate_kw=1.0)` should succeed; `SetBatteryChargeRateCommand(..., is_idempotent=False)` should raise `ValidationError`.

### What "before each retry, PolicyGuard re-checks" really means

The epic AC says: *"before each retry, PolicyGuard re-checks the current constraints and operating mode — if conditions have changed such that re-issuing would be unsafe, the retry is blocked and treated as a final failure"*.

You do **not** need to write new re-check code. PolicyGuard's `authorize_and_dispatch()` already runs the FULL safety pipeline (operating mode, capability, constraints) on every call. Calling `authorize_and_dispatch(command)` a second time IS the re-check. If the snapshot has changed between attempts (e.g., operating mode dropped to `fail_safe`), PolicyGuard will return `rejected` on the retry and `RetryPolicy.execute()` stops immediately per AC2 ("if a re-check produces `CommandResult(status=CommandStatus.rejected)`, retries STOP IMMEDIATELY").

This is why `RetryPolicy` does NOT need its own snapshot reading or constraint-checking logic — it delegates everything to `PolicyGuard`.

### Audit event taxonomy — DEVICE vs CONSTRAINT

Two distinct event types, two distinct emitters:

- `event_type="CONSTRAINT"` — emitted by `PolicyGuard._reject()` ([src/open_ems/engine/policy_guard.py:163-184](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\policy_guard.py)) when the safety pipeline rejects a command. This story does NOT change PolicyGuard's audit behavior. Every PolicyGuard rejection still emits its own `CONSTRAINT` event.
- `event_type="DEVICE"` — emitted by `RetryPolicy._emit_failure_audit()` (NEW) when a command's final outcome (after all retries) is `applied=False`. Per the epic AC: "the failure is logged as an audit event with `event_type=DEVICE` and a plain-language summary".

A rejected command will produce **two** audit events: one `CONSTRAINT` (from PolicyGuard, recording the rejection reason) and one `DEVICE` (from RetryPolicy, recording the not-applied final outcome). This is intentional and matches the architecture's separation of concerns: PolicyGuard records *why* the safety gate triggered; RetryPolicy records *what* the runtime experienced (a command did not take effect).

`VALID_EVENT_TYPES` already includes both `"CONSTRAINT"` and `"DEVICE"` — see [src/open_ems/services/audit_log.py:16-18](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\services\audit_log.py).

### Settings field placement

Add the two new fields to the existing `Settings` class in `src/open_ems/settings.py`. The placement matters — append AFTER `battery_reserve_floor_percent` (line 32) so all engine-related tunables cluster together:

```python
control_loop_interval_seconds: float = Field(default=10.0, gt=0.0)
peak_limit_kw: float = Field(default=25.0, gt=0.0)
battery_reserve_floor_percent: float = Field(default=20.0, ge=0.0, le=100.0)
command_max_retries: int = Field(default=2, ge=0, le=5)              # NEW
command_retry_backoff_seconds: float = Field(default=0.5, ge=0.0, le=5.0)  # NEW
```

The `command_max_retries` upper bound of `5` is a defensive cap — preventing a misconfiguration from creating a retry storm that exceeds the 60 s control-cycle deadline (NFR-R5). With defaults (2 retries × 10 s timeout × 0.5 s backoff ≈ 31 s worst case), there is comfortable margin under the 60 s deadline.

### app.py current state vs target

Read `src/open_ems/web/app.py` around the `_control_loop` setup. Story 8.2 already added:

```python
intent_executor = IntentExecutor()
policy_guard = PolicyGuard(
    state_store=app.state.state_store,
    adapters={},
    observability=ObservabilityService(),
    settings=settings,
)
_control_loop = ControlLoop(
    state_store=app.state.state_store,
    adapters={},
    energy_repo=EnergyRepo(),
    settings=settings,
    intent_executor=intent_executor,
    policy_guard=policy_guard,
)
```

Target shape:

```python
observability = ObservabilityService()  # extracted to share between PolicyGuard and RetryPolicy
intent_executor = IntentExecutor()
policy_guard = PolicyGuard(
    state_store=app.state.state_store,
    adapters={},
    observability=observability,
    settings=settings,
)
retry_policy = RetryPolicy(
    policy_guard=policy_guard,
    observability=observability,
    settings=settings,
)
_control_loop = ControlLoop(
    state_store=app.state.state_store,
    adapters={},
    energy_repo=EnergyRepo(),
    settings=settings,
    intent_executor=intent_executor,
    retry_policy=retry_policy,   # changed from policy_guard
)
```

If `app.py` currently constructs `ObservabilityService()` inline inside `PolicyGuard(...)`, refactor to the shared-instance pattern shown above. The PolicyGuard test fixture in `tests/unit/engine/test_policy_guard.py:96-100` already shows the pattern with a separately-injected observability spy.

### Test patterns — RetryPolicy

Mirror the `test_policy_guard.py` patterns. Recommended fixture:

```python
from unittest.mock import AsyncMock, MagicMock

from open_ems.core.commands import (
    CommandOrigin, CommandResult, CommandStatus,
    SetBatteryChargeRateCommand, StopEVChargingCommand,
)
from open_ems.core import DeviceRole
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.engine.retry_policy import RetryPolicy
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings


def _settings(*, max_retries: int = 2, backoff: float = 0.0) -> Settings:
    # backoff=0.0 disables sleep — keep tests fast
    return Settings(
        _env_file=None,
        command_max_retries=max_retries,
        command_retry_backoff_seconds=backoff,
    )  # type: ignore[call-arg]


def _observability_spy() -> tuple[ObservabilityService, AsyncMock]:
    spy = AsyncMock()
    obs = ObservabilityService(repo=MagicMock())
    obs.audit = spy  # type: ignore[method-assign]
    return obs, spy


def _success_result(cmd) -> CommandResult:
    return CommandResult(
        correlation_id=cmd.correlation_id,
        device_id=cmd.device_id,
        status=CommandStatus.success,
        applied=True,
        reason="ok",
    )


def _failed_result(cmd, *, reason: str = "communication_error") -> CommandResult:
    return CommandResult(
        correlation_id=cmd.correlation_id,
        device_id=cmd.device_id,
        status=CommandStatus.failed,
        applied=False,
        reason=reason,
    )


async def test_idempotent_command_retried_up_to_max_retries_after_failure() -> None:
    cmd = SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )
    pg = MagicMock(spec=PolicyGuard)
    pg.authorize_and_dispatch = AsyncMock(side_effect=[_failed_result(cmd)] * 3)
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    result = await rp.execute(cmd)

    assert pg.authorize_and_dispatch.await_count == 3
    assert result.status is CommandStatus.failed
    assert result.applied is False
    audit_spy.assert_awaited_once()
    args = audit_spy.await_args
    assert args.kwargs["event_type"] == "DEVICE"
    assert "after 3 attempt(s)" in args.kwargs["summary"]
    assert "communication_error" in args.kwargs["summary"]
```

### Test pattern — control loop helper update

Existing helper at `tests/unit/engine/test_control_loop.py::_control_loop()` injects `IntentExecutor()` and `MagicMock(spec=PolicyGuard)` (Story 8.2). Update to:

```python
from open_ems.engine.retry_policy import RetryPolicy

def _control_loop(
    *,
    intent_executor: IntentExecutor | None = None,
    retry_policy: RetryPolicy | None = None,   # was: policy_guard
    ...
) -> ControlLoop:
    ...
    return ControlLoop(
        ...,
        intent_executor=intent_executor or IntentExecutor(),
        retry_policy=retry_policy or _default_retry_policy_mock(),  # was: policy_guard
    )

def _default_retry_policy_mock() -> MagicMock:
    rp = MagicMock(spec=RetryPolicy)
    rp.execute = AsyncMock(return_value=CommandResult(
        correlation_id=uuid.uuid4(), device_id="x",
        status=CommandStatus.success, applied=True, reason="ok",
    ))
    return rp
```

The 7 prior control-loop tests should not need changes if the helper signature accepts `retry_policy=None` and falls back to a MagicMock — verify each existing test still passes after the helper change.

### Common pitfalls — read before coding

1. **Do NOT add `is_idempotent` as a Pydantic Field** — it would become a constructor kwarg, and `extra="forbid"` would reject existing call sites that don't pass it. Use `ClassVar[bool]` exactly as shown.
2. **Do NOT add a `time.sleep` anywhere** — architecture forbids it ([architecture.md:740](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md)). Use `asyncio.sleep`.
3. **Do NOT add a separate `asyncio.wait_for` around `policy_guard.authorize_and_dispatch()` in `RetryPolicy`** — PolicyGuard owns the per-attempt timeout. Adding another wait_for would double-bound the wait and obscure timeout attribution.
4. **Do NOT call `audit()` for successful commands** — flooding the event log with one entry per successful command would defeat its purpose (FR31's "human-actionable signal" intent). AC5 mandates ZERO audit events on success.
5. **Do NOT propagate audit-write exceptions** — wrap `await self._observability.audit(...)` in `try/except Exception: logger.error("audit_emit_failed", ...)`. This mirrors the [PolicyGuard._reject() pattern](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\policy_guard.py) (line 163-184) added in Story 8.2's review fixes.
6. **Do NOT change `PolicyGuard`** at all in this story. The retry layer wraps PolicyGuard; PolicyGuard is unchanged.
7. **The control loop's `_handle_evaluation_result` becomes simpler, not more complex** — it removes the `if not cmd_result.applied: logger.warning(...)` branch (AC7) because all per-command logging now lives in RetryPolicy. The control loop just dispatches and moves on.
8. **Use `assert last_result is not None` after the loop** — mypy will narrow the type. The loop runs at least once because `range(1, max_attempts + 1)` with `max_attempts ≥ 1` is non-empty (Settings constraint `ge=0` on `command_max_retries` ensures `max_attempts ≥ 1`).
9. **Asynchronous ordering**: `await asyncio.sleep(backoff)` BEFORE the next dispatch attempt, NOT after. Sleep order: `[attempt 1] → [sleep backoff] → [attempt 2] → [sleep backoff] → [attempt 3]`. Do NOT sleep after the final attempt (AC9 test #9 verifies sleep is called exactly N-1 times across N attempts).
10. **Use `command.is_idempotent` directly on the instance** — Python resolves `ClassVar` access on instances (`cmd.is_idempotent`) by reading the class attribute. Do not use `type(cmd).is_idempotent` — it works but is needlessly verbose.

### Project Structure Notes

```
src/open_ems/
├── core/
│   ├── commands.py          ← MODIFY: add `is_idempotent: ClassVar[bool]` to 4 subtypes
│   └── __init__.py          ← unchanged
├── engine/
│   ├── retry_policy.py      ← NEW: RetryPolicy class
│   ├── control_loop.py      ← MODIFY: replace policy_guard kwarg with retry_policy; simplify _handle_evaluation_result
│   ├── policy_guard.py      ← UNCHANGED
│   ├── intent_executor.py   ← UNCHANGED
│   └── __init__.py          ← MODIFY: export RetryPolicy
├── services/
│   └── audit_log.py         ← UNCHANGED (uses existing event_type="DEVICE")
├── settings.py              ← MODIFY: add command_max_retries, command_retry_backoff_seconds
└── web/
    └── app.py               ← MODIFY: extract `observability` local; instantiate RetryPolicy; replace policy_guard arg with retry_policy in ControlLoop call

tests/
├── unit/
│   ├── core/
│   │   └── test_commands.py     ← NEW: idempotency classification tests (#12, #13)
│   └── engine/
│       ├── test_retry_policy.py ← NEW: 11 unit tests (#1–#11)
│       └── test_control_loop.py ← MODIFY: helper + new test (#15)
└── integration/
    └── engine/
        ├── test_command_pipeline.py  ← UNCHANGED
        └── test_retry_pipeline.py    ← NEW: 2 integration tests (#16, #17)
```

**No new database migration.** No schema changes. No alembic file in this story.

**Architecture compliance verified**:
- [architecture.md:100](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md) — "Retry logic must be bounded and must not create oscillating or unsafe behavior" — bounded by `command_max_retries ≤ 5`; unsafe-prevention via PolicyGuard re-check on every attempt.
- [architecture.md:711](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md) — "Retries must be gated on `CommandResult.status` and must not retry if retrying would risk violating safety constraints" — gating on `status` + idempotency; PolicyGuard re-check is the safety guard.
- [architecture.md:417-428](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md) — adapter exception handling is PolicyGuard's responsibility; RetryPolicy does NOT add another exception handler around PolicyGuard.

### Deferred items (explicitly NOT implemented in this story)

- **Concurrent command dispatch across multiple devices** — Story 8.2 dispatches sequentially; this story preserves that. Parallel dispatch with per-device retry is a Story 8.4 (or later) concern.
- **Exponential backoff** — fixed backoff is sufficient for the 5–15 s control loop window. Switch to exponential when adapter behavior under sustained communication degradation is empirically measured (post-Epic 9).
- **Circuit-breaker pattern** — repeated failures across cycles are not deduplicated; every cycle starts fresh. Add a per-device circuit breaker if production metrics show retry storms (deferred to a future operations story).
- **Watchdog interaction** — RetryPolicy emits no watchdog signals; the watchdog is Story 8.4's scope. The retry layer must not block beyond the per-cycle deadline; verify in AC6's bounded-time analysis.
- **Hydrating retry-state to DB** — retries are in-memory only. A process restart loses in-flight retry state; the next evaluation cycle re-evaluates from fresh state. This matches the architecture's "next evaluation cycle re-evaluates from fresh state" principle (epic AC2).
- **Per-command-type retry-count overrides** — a single `Settings.command_max_retries` applies to all idempotent commands. Per-type tuning would require adding a method to each command class (e.g., `max_retries: ClassVar[int]`); deferred until measured need.
- **Adapter-level idempotency markers** — adapters do not currently know whether a command is idempotent. The classification lives entirely in `core/commands.py`. If an adapter needs to short-circuit a duplicate stop request, that is an adapter-internal concern (Epic 9).

### References

- [Source: epics.md — Story 8.3 full AC text, lines 1778–1808](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\epics.md)
- [Source: epics.md — Epic 8 cross-story constraints, lines 1855–1862 — "Non-idempotent commands are never retried automatically"](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\epics.md)
- [Source: architecture.md — Idempotency and command ACK principle, line 100](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md)
- [Source: architecture.md — Retry safety gating, line 711](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md)
- [Source: architecture.md — Adapter exception → degraded/CommandResult contract, lines 414-428](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md)
- [Source: architecture.md — `time.sleep` forbidden anti-pattern, line 740](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md)
- [Source: src/open_ems/engine/policy_guard.py:66-139 — `authorize_and_dispatch` flow with built-in 10 s wait_for and exception wrapping](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\policy_guard.py)
- [Source: src/open_ems/engine/policy_guard.py:163-184 — `_reject()` audit-emit-with-try/except pattern to mirror in `_emit_failure_audit()`](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\policy_guard.py)
- [Source: src/open_ems/core/commands.py:47-82 — DeviceCommandBase + 4 subtypes; ConfigDict(frozen=True, extra="forbid")](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\core\commands.py)
- [Source: src/open_ems/engine/control_loop.py:151-176 — `_handle_evaluation_result` to update](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\control_loop.py)
- [Source: src/open_ems/engine/control_loop.py:44-63 — `__init__` signature to update](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\control_loop.py)
- [Source: src/open_ems/services/audit_log.py:16-72 — `ObservabilityService.audit()` signature; `VALID_EVENT_TYPES` includes "DEVICE"](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\services\audit_log.py)
- [Source: src/open_ems/settings.py:9-32 — `Settings` class to extend](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\settings.py)
- [Source: tests/unit/engine/test_policy_guard.py:96-100 — `_observability_with_audit_spy()` pattern to reuse](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\tests\unit\engine\test_policy_guard.py)
- [Source: tests/integration/engine/test_command_pipeline.py — `SimulatedBatteryAdapter` pattern to extend with configurable failure count](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\tests\integration\engine\test_command_pipeline.py)
- [Source: 8-2 story Dev Agent Record — Review Findings show `_reject()` audit-exception wrapping was added in patch](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\implementation-artifacts\8-2-implement-intentexecutor-policyguard-and-command-dispatch.md)
- [Source: deferred-work.md — 8-2 deferred items for context; nothing here blocks 8.3](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\implementation-artifacts\deferred-work.md)

---

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Amelia / bmad-dev-story)

### Debug Log References

Pre-story baseline (2026-05-06):
- pytest: 799 passed, 43 warnings in 43.44s
- ruff check: All checks passed
- ruff format --check: 160 files already formatted
- mypy src/: Success: no issues found in 77 source files

Final validation (2026-05-06):
- pytest: 817 passed, 43 warnings in 47.15s (+18 new)
- ruff check: All checks passed
- ruff format --check: 164 files already formatted
- mypy src/: Success: no issues found in 78 source files
- Settings smoke test: `command_max_retries=2`, `command_retry_backoff_seconds=0.5` (defaults match AC8)

No HALT conditions encountered. No deferred items. No additional dependencies introduced.

### Completion Notes List

- AC1 (idempotency classification): `is_idempotent: ClassVar[bool]` declared on each of the 4 concrete command subtypes; intentionally NOT defaulted on `DeviceCommandBase` to force explicit declaration on any future subtype. `tests/unit/core/test_commands.py` adds tests #12, #13, plus a sanity test that the ClassVar is accessible via instance.
- AC2/AC3/AC4/AC5 (retry / fail-closed / DEVICE-audit / no-success-audit): implemented in `src/open_ems/engine/retry_policy.py` exactly as specified in the story Dev Notes pseudocode; debug logs `command_retry`, `command_applied`, `non_idempotent_command_not_retried` fire as required.
- AC6 (timeout bound): no `asyncio.wait_for` added in RetryPolicy — per-attempt timeout remains owned by PolicyGuard. Sequential per-command dispatch preserved (concurrent dispatch deferred per Dev Notes).
- AC7 (sole call site): control loop now calls `await self._retry_policy.execute(cmd)` and the `command_not_applied` warning is removed; `policy_guard` kwarg replaced with `retry_policy`. Module docstring updated.
- AC8 (Settings): `command_max_retries: int = Field(default=2, ge=0, le=5)` and `command_retry_backoff_seconds: float = Field(default=0.5, ge=0.0, le=5.0)` appended after `battery_reserve_floor_percent` exactly as specified.
- AC9 (app.py wiring): `observability = ObservabilityService()` extracted to a single shared local; `RetryPolicy` constructed between `PolicyGuard` and `ControlLoop`; `policy_guard` kwarg in `ControlLoop(...)` replaced with `retry_policy`. No new database migration.
- AC10 (tests): 13 unit tests in `tests/unit/engine/test_retry_policy.py` (covers AC10 #1–#11 plus a defensive backoff=0 test and an audit-failure-swallowed test); 3 tests in `tests/unit/core/test_commands.py` (#12, #13 plus instance-access sanity); 2 integration tests in `tests/integration/engine/test_retry_pipeline.py` (#16, #17). The existing `test_handle_evaluation_result_logs_warning_when_command_not_applied` test was replaced with `test_control_loop_dispatches_via_retry_policy_not_policy_guard` (#15) since AC7 removes the `command_not_applied` warning. The `test_control_loop.py::_control_loop()` helper now accepts `retry_policy=` and falls back to a `MagicMock(spec=RetryPolicy)`. All 7 prior control-loop tests still pass unchanged.
- Defensive: `RetryPolicy._emit_failure_audit` swallows audit-write exceptions (logs `audit_emit_failed`) — mirrors the post-review fix in `PolicyGuard._reject()` so audit failures never mask command failures.

### File List

**Modified:**
- `src/open_ems/core/commands.py` — added `ClassVar` import; added `is_idempotent: ClassVar[bool]` to 4 subtypes; expanded module docstring with three-stage pipeline diagram and idempotency rationale
- `src/open_ems/engine/control_loop.py` — replaced `policy_guard: PolicyGuard` kwarg with `retry_policy: RetryPolicy`; updated `_handle_evaluation_result` to dispatch via `RetryPolicy.execute`; removed obsolete `command_not_applied` warning; updated module docstring
- `src/open_ems/engine/__init__.py` — exported `RetryPolicy`
- `src/open_ems/settings.py` — added `command_max_retries` and `command_retry_backoff_seconds` fields
- `src/open_ems/web/app.py` — added `RetryPolicy` import; extracted shared `observability` local; constructed `RetryPolicy`; swapped `policy_guard=policy_guard` → `retry_policy=retry_policy` in `ControlLoop(...)` call
- `tests/unit/engine/test_control_loop.py` — added `_default_retry_policy_mock()`; updated `_control_loop()` helper signature; replaced obsolete `command_not_applied` test with `test_control_loop_dispatches_via_retry_policy_not_policy_guard`

**Created:**
- `src/open_ems/engine/retry_policy.py` — `RetryPolicy` class
- `tests/unit/core/test_commands.py` — idempotency-classification tests
- `tests/unit/engine/test_retry_policy.py` — RetryPolicy unit tests (AC10 #1–#11 + 2 defensive)
- `tests/integration/engine/test_retry_pipeline.py` — full-pipeline integration tests (AC10 #16, #17)

### Change Log

| Date       | Author        | Change |
|------------|---------------|--------|
| 2026-05-06 | Amelia (Dev)  | Story 8.3 implementation complete: bounded retry layer with idempotency-aware fail-closed semantics; DEVICE audit on final-failure; control loop now dispatches through RetryPolicy. 18 new tests; 817 passing. |
