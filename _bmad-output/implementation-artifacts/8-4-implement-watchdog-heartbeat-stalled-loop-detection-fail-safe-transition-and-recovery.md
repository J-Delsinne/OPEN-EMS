# Story 8.4: Implement watchdog heartbeat, stalled loop detection, fail-safe transition, and recovery

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want the runtime loop to send watchdog heartbeats only while it is making progress, recover stalled cycles via supervisor-managed restart, transition to fail-safe mode on unrecoverable failure, and exit fail-safe only when all required conditions are explicitly met,
so that stalled loops recover via the process supervisor rather than silent inaction, and fail-safe entry and exit are both logged, guarded against flapping, and unambiguous.

## Acceptance Criteria

**AC1 — Heartbeat is gated on cycle completion (NOT a standalone timer)**

GIVEN the control loop is running
WHEN an evaluation cycle completes successfully (i.e., `_tick()` returns without raising and `_handle_evaluation_result()` returned)
THEN the loop records the completion timestamp on a shared `LoopLiveness` object: `last_cycle_completed_at_monotonic = time.monotonic()`
AND the existing `watchdog_task` (in `src/open_ems/services/watchdog.py`) is refactored to consult this `LoopLiveness` signal: `sd_notify("WATCHDOG=1")` is sent ONLY when the loop has reported a completion within the last `(2 × control_loop_interval_seconds)` window — capped at `watchdog_cycle_deadline_seconds` (default 60.0)
AND if the loop has NOT reported within that window, the watchdog task SKIPS the `sd_notify` call (so systemd's `WatchdogSec` will elapse and trigger a supervisor restart) AND logs a single `watchdog_missed` event per missed-cycle count increment with `event="watchdog_missed"` (structlog only — audit event is AC2's responsibility)
AND the existing precursor "delayed wake-up" check in `watchdog_task` (lines 47-55) is REMOVED — it is replaced by the cycle-progress check; do NOT keep both
AND skipping the sd_notify also means `LoopLiveness` reports `is_alive=False` for the duration of the stall

**Rationale:** Today the watchdog task in `services/watchdog.py` sends heartbeats on its own timer regardless of whether the control loop is actually progressing — a deadlocked or crashed loop can still appear healthy to systemd because the heartbeat task lives in a separate asyncio task. This AC ties heartbeat to actual cycle progress, which matches Architecture Decision 5.2 ("dedicated asyncio task tracks the timestamp of the last completed control loop evaluation").

**AC2 — Stalled loop emits SYSTEM audit event when missed-cycle threshold reached**

GIVEN the control loop has not reported a successful cycle completion
WHEN the elapsed duration since the last completion crosses `watchdog_missed_cycle_threshold × control_loop_interval_seconds` (default 2 × 10 s = 20 s) OR the absolute `watchdog_cycle_deadline_seconds` (default 60 s) — whichever comes first
THEN exactly ONE `ObservabilityService.audit()` call is made with:
- `actor="system"`
- `event_type="SYSTEM"`
- `summary` is a plain-language string: `f"Control loop stalled: no successful evaluation cycle in {elapsed_seconds:.1f} s (missed {missed_count} consecutive cycles); supervisor restart pending"`
AND the audit event is emitted EXACTLY ONCE per stall — not on every watchdog tick (track an `_audit_emitted_for_current_stall: bool` flag that resets when the loop recovers)
AND the audit emit failure is wrapped in `try/except` (mirror the `_emit_failure_audit` pattern in `RetryPolicy._emit_failure_audit` at `src/open_ems/engine/retry_policy.py:131-148`)
AND the preferred recovery path is supervisor-managed: NO in-process loop restart is implemented in this story — systemd's `WatchdogSec` (native) and Docker's healthcheck restart policy (container) own the recovery
AND the cross-story rule from epics.md:1861 is honored: if a future story introduces internal subtask restart, that branch MUST emit a distinct audit event identifying it as a subtask restart (NOT included in this story)

**AC3 — `/health/ready` returns 503 when control loop is stalled OR fail-safe is unrecovered**

GIVEN the application has completed startup (`mark_ready()` called)
WHEN `/health/ready` is queried
THEN it returns HTTP 200 ONLY when ALL of: (a) `is_ready() is True`, (b) `LoopLiveness.is_alive` is True (i.e., last cycle completion was within the freshness window), (c) `LoopLiveness.in_fail_safe` is False
AND if any of (b) or (c) are False, the endpoint returns HTTP 503 with body `{"status": "degraded", "reason": "<specific>"}` where `<specific>` identifies the failure mode: `"control_loop_stalled"` or `"fail_safe_active"`
AND when the loop has not yet completed its first cycle but startup is complete, `LoopLiveness.is_alive` is False — this returns 503 (intentional: a never-ticked loop is not healthy regardless of `is_ready()`)
AND the existing `is_ready()` check is preserved for the "still starting" case → returns 503 with `{"status": "starting"}` (unchanged behavior from Story 1.3); do not regress this
AND `/health/live` is UNCHANGED — it must continue to return 200 unconditionally as long as the ASGI worker is responding (liveness is process-existence; readiness is loop-progress)

**AC4 — Fail-safe entry: suppress all dispatch + emit SYSTEM audit on transition**

GIVEN the most recent `EvaluationResult.recommended_operating_mode` is `SystemOperatingMode.fail_safe`
WHEN `_handle_evaluation_result` runs
THEN the loop does NOT call `IntentExecutor.translate()` AND does NOT call `RetryPolicy.execute()` — command construction is short-circuited entirely (no commands produced means no commands dispatched; this is the strongest form of "ceases issuing all control commands")
AND `LoopLiveness.in_fail_safe` is set to True
AND IF the previous cycle's mode was NOT `fail_safe` (i.e., this is a fresh transition INTO fail-safe), an `ObservabilityService.audit()` call is made with:
- `actor="system"`
- `event_type="SYSTEM"`
- `summary` is a plain-language string identifying which devices triggered fail-safe entry: `f"Fail-safe entered: {device_summary} (cycle {cycle_id})"` where `device_summary` is built from the snapshot's degraded device roles, e.g. `"grid_meter, battery degraded"` — only roles whose slot is `DegradedDeviceState`
AND IF the previous mode was already `fail_safe`, NO audit event is emitted (entry is a one-shot per stretch — no flapping spam)
AND the cycle still publishes the snapshot to `StateStore` with `operating_mode=fail_safe` (unchanged from Story 8.1) — fail-safe is observable to UI even though no commands are dispatched
AND `LoopLiveness.last_cycle_completed_at_monotonic` is still updated — fail-safe is a successful evaluation outcome (the engine produced a decision: "do nothing"), NOT a stall

**AC5 — Fail-safe exit: ALL conditions required, audit event on transition out**

GIVEN `LoopLiveness.in_fail_safe is True`
WHEN a new evaluation cycle completes
THEN exit eligibility requires ALL of:
- (a) the new `EvaluationResult.recommended_operating_mode` is NOT `fail_safe` (engine has determined the system is no longer in critical-failure state)
- (b) the snapshot used by that cycle contains valid `DeviceState` (not `DegradedDeviceState` and not `None`) for every role that was degraded in the snapshot at fail-safe entry — track entry-degraded roles in `_fail_safe_entry_degraded_roles: frozenset[DeviceRole]`
- (c) the cycle completed without raising (i.e., `_handle_evaluation_result` returned normally)
AND a single healthy adapter read is INSUFFICIENT to exit: condition (c) means a full evaluation cycle must have run end-to-end with the recovered state — the safety pipeline (PolicyGuard → RetryPolicy) is NOT exercised on the exit cycle (because no commands are issued during fail-safe), but the evaluation engine and StateStore must have processed the recovered state
AND when ALL conditions are met, `LoopLiveness.in_fail_safe` is set to False and `_fail_safe_entry_degraded_roles` is cleared
AND a recovery audit event is emitted with:
- `actor="system"`
- `event_type="SYSTEM"`
- `summary`: `f"Fail-safe exited: {recovered_devices} restored; cycle {cycle_id} completed without errors"` where `recovered_devices` lists the entry-degraded roles whose state is now healthy
AND on the SAME cycle that exits fail-safe, command dispatch is allowed (the recovered state has already been validated by the engine; suppressing dispatch on the recovery cycle would create an artificial extra delay)
AND if condition (b) is met but condition (a) is NOT (engine still recommends `fail_safe`), the loop stays in fail-safe — exit requires the engine's explicit determination

**AC6 — Control-loop crash escalates to fail-safe (NOT silent error log)**

GIVEN the control loop's `run()` task raises an unhandled exception (e.g., `_tick()` propagates a non-cancellation exception)
WHEN the `_on_control_loop_done` callback fires (in `src/open_ems/web/app.py:245-249`)
THEN in addition to the existing `logger.error("control_loop_died", ...)` call, the callback:
- (a) sets `LoopLiveness.in_fail_safe = True` (so `/health/ready` returns 503)
- (b) records the crash via `LoopLiveness.mark_crashed(exc)` — sets a `crashed: bool` flag and stores the exception type+repr for observability
- (c) emits exactly ONE `ObservabilityService.audit(event_type="SYSTEM", summary=f"Control loop crashed: {type(exc).__name__}: {exc!r}; entering fail-safe pending supervisor restart")` — wrapped in try/except so audit emission failure does not mask the crash log
AND the crash audit emission is best-effort: if the event loop is shutting down or the database connection is closed, log `audit_emit_failed` and continue (audit must never raise from inside a done-callback)
AND because `_on_control_loop_done` is a synchronous callback, the audit emission is dispatched via `asyncio.create_task(...)` — if there is no running loop (process exit), skip the audit dispatch and only log
AND no in-process loop restart is attempted (per AC2 cross-story rule): supervisor restart is the recovery path
AND because `LoopLiveness.in_fail_safe` is now True, `/health/ready` returns 503 (per AC3) → Docker healthcheck restart policy fires

**AC7 — RetryPolicy backoff sleep is shielded against task cancellation; in-flight retry emits DEVICE audit on cancel**

GIVEN `RetryPolicy.execute()` is mid-loop with one or more retries pending and an `asyncio.sleep(backoff)` is active (at `src/open_ems/engine/retry_policy.py:81-82`)
WHEN the surrounding control-loop task is cancelled (e.g., during shutdown or fail-safe transition)
THEN `asyncio.CancelledError` propagates out of `execute()` AS REQUIRED by cooperative cancellation — it MUST NOT be swallowed
AND BEFORE re-raising, the in-flight retry emits a single best-effort DEVICE audit event with `summary=f"Command {type(command).__name__} for {command.device_id} cancelled mid-retry after {attempts} attempt(s): pending result indeterminate"` so post-mortem can reconstruct that a command was partially attempted before shutdown
AND the audit emission uses the same `try/except` shielding pattern as `_emit_failure_audit` (audit-write failure logs `audit_emit_failed` and continues to re-raise `CancelledError`)
AND the audit emission itself MUST NOT use `asyncio.shield(...)` around the audit call — emitting an audit event that takes longer than the cancellation grace period would block shutdown; if the audit is interrupted, the structured log line (`logger.warning("retry_cancelled", ...)`) is the fallback record
AND a `logger.warning("retry_cancelled", device_id=..., command_type=..., attempts=..., correlation_id=..., component="engine")` is emitted before re-raising — even if the audit DB write fails, this structured log appears in journald/Docker logs for post-mortem
AND the existing successful-completion / final-failure paths are unchanged

**AC8 — Settings: watchdog tunables**

GIVEN `src/open_ems/settings.py` exposes the existing `Settings` class
THEN it gains exactly these new fields, appended AFTER `command_retry_backoff_seconds` (line 34):
- `watchdog_missed_cycle_threshold: int = Field(default=2, ge=1, le=10)` — number of consecutive missed cycles tolerated before stall is triggered; matches NFR-R5 ("≤ 2 missed evaluation cycles")
- `watchdog_cycle_deadline_seconds: float = Field(default=60.0, gt=0.0, le=300.0)` — absolute upper bound for any single cycle, matching NFR-P1's 60-second degraded-connectivity ceiling; cap at 300 s defensively
AND both fields are wired through environment variables via `pydantic-settings` automatic mapping (`WATCHDOG_MISSED_CYCLE_THRESHOLD`, `WATCHDOG_CYCLE_DEADLINE_SECONDS`)
AND existing `Settings(_env_file=None)` constructions in tests continue to work unchanged
AND no new database migration is required for this story

**AC9 — `LoopLiveness` is a NEW shared signal object; `app.py` constructs and shares ONE instance**

GIVEN `src/open_ems/services/loop_liveness.py` is created (NEW file)
THEN it defines a single class `LoopLiveness`:
- Fields: `last_cycle_completed_at_monotonic: float | None = None`, `in_fail_safe: bool = False`, `crashed: bool = False`, `crash_reason: str | None = None`, `missed_cycle_threshold_seconds: float`, `cycle_deadline_seconds: float`
- Methods (all synchronous — this object is read from sync contexts like the health endpoint):
  - `mark_cycle_complete() -> None` — sets `last_cycle_completed_at_monotonic = time.monotonic()`
  - `mark_fail_safe_entered() -> None` and `mark_fail_safe_exited() -> None` — set/clear `in_fail_safe`
  - `mark_crashed(exc: BaseException) -> None` — sets `crashed=True`, stores `crash_reason=f"{type(exc).__name__}: {exc!r}"[:500]`, sets `in_fail_safe=True`
  - `is_alive(now_monotonic: float | None = None) -> bool` — returns True if `last_cycle_completed_at_monotonic is not None` AND `now - last < min(threshold_seconds, deadline_seconds)`; returns False if `crashed is True` regardless
  - `seconds_since_last_cycle(now_monotonic: float | None = None) -> float | None` — for the audit summary; returns None if never ticked
- The class has NO async methods, NO database access, NO logging — it is a pure state holder consumed by ControlLoop, watchdog_task, and the health endpoint
- It is NOT thread-safe but IS asyncio-safe in single-task asyncio (writes happen sequentially within the loop's task context; the health endpoint and watchdog task only READ)
AND `src/open_ems/web/app.py` lifespan constructs ONE `LoopLiveness` instance and stores it on `app.state.loop_liveness`:
```python
loop_liveness = LoopLiveness(
    missed_cycle_threshold_seconds=(
        settings.watchdog_missed_cycle_threshold * settings.control_loop_interval_seconds
    ),
    cycle_deadline_seconds=settings.watchdog_cycle_deadline_seconds,
)
app.state.loop_liveness = loop_liveness
```
AND the same instance is passed to:
- `ControlLoop(..., loop_liveness=loop_liveness)` (NEW required kwarg)
- `RetryPolicy(..., observability=observability, ...)` — UNCHANGED; RetryPolicy does NOT need `loop_liveness` (its cancellation handling is local)
- `watchdog_task(interval, loop_liveness=loop_liveness)` — NEW required kwarg
- The health endpoint via FastAPI's `Request.app.state.loop_liveness` (do NOT add another DI singleton — use the existing app.state convention)
AND `_on_control_loop_done(t)` callback also receives `loop_liveness` via closure capture from lifespan and `observability` for crash-audit emission
AND no new database migration is required for this story

**AC10 — `ControlLoop` and `watchdog_task` are refactored to consume `LoopLiveness`**

GIVEN `src/open_ems/engine/control_loop.py` is updated
THEN `ControlLoop.__init__` accepts a new required kwarg `loop_liveness: LoopLiveness` and stores it as `self._loop_liveness`
AND `ControlLoop` accepts a new required kwarg `observability: ObservabilityService` (required for AC4/AC5 audit events) and stores it as `self._observability` — REUSE the same `observability` instance shared with `PolicyGuard` and `RetryPolicy`; do NOT construct a new one
AND `_handle_evaluation_result` is restructured:
```python
async def _handle_evaluation_result(self, result, snapshot):
    new_mode = result.recommended_operating_mode
    self._last_mode = new_mode
    logger.debug("evaluation_cycle_complete", ...)  # unchanged

    # Fail-safe entry (AC4)
    if new_mode is SystemOperatingMode.fail_safe:
        if not self._loop_liveness.in_fail_safe:
            await self._emit_fail_safe_entry_audit(result, snapshot)
            self._fail_safe_entry_degraded_roles = self._degraded_roles_in(snapshot)
            self._loop_liveness.mark_fail_safe_entered()
        # AC4: command dispatch fully suppressed
        return

    # Fail-safe exit (AC5) — must be evaluated BEFORE dispatching commands
    if self._loop_liveness.in_fail_safe:
        if not self._can_exit_fail_safe(snapshot):
            # Engine recommended non-fail-safe but degraded devices haven't recovered → stay in fail-safe
            return
        await self._emit_fail_safe_exit_audit(result, snapshot)
        self._loop_liveness.mark_fail_safe_exited()
        self._fail_safe_entry_degraded_roles = frozenset()
        # Continue to dispatch on this cycle (AC5 last clause)

    # Normal dispatch path (Story 8.3 unchanged)
    commands = self._intent_executor.translate(result, snapshot)
    for cmd in commands:
        await self._retry_policy.execute(cmd)
```
AND the cycle-completion mark `self._loop_liveness.mark_cycle_complete()` is called from `_tick()` AFTER `_handle_evaluation_result` returns successfully — NOT inside `_handle_evaluation_result` (so a fail-safe-suppressed cycle still counts as a completion); AND not on the `evaluation_skipped_missing_required_slots` path either — that path returns from `_tick` early; do NOT mark completion when slots are missing because the engine never produced a result
AND `services/watchdog.py:watchdog_task` is updated:
```python
async def watchdog_task(interval: float, *, loop_liveness: LoopLiveness) -> None:
    while True:
        await asyncio.sleep(interval)
        if loop_liveness.is_alive():
            sd_notify("WATCHDOG=1")
        # else: silently skip — let WatchdogSec elapse → systemd restart
```
AND the existing precursor `last_sent`/`elapsed > interval * 1.5` block (lines 43-55 of `services/watchdog.py`) is REMOVED — replaced by `is_alive()` consultation
AND the `_monotonic` module-level reference is preserved (existing tests patch it; do not break them)

**AC11 — Stalled-loop watchdog also emits the SYSTEM audit (AC2 callsite)**

GIVEN the `watchdog_task` detects `loop_liveness.is_alive()` is False
WHEN the audit threshold is reached (single emission per stall stretch)
THEN the watchdog task ALSO holds an `_audit_emitted_for_current_stall: bool = False` local flag and an injected `observability: ObservabilityService` reference
AND on the FIRST tick where `is_alive()` is False, it emits the AC2 SYSTEM audit event AND sets `_audit_emitted_for_current_stall = True`
AND on subsequent ticks while still stalled, it does NOT re-emit the audit (one-shot per stall)
AND on the FIRST tick where `is_alive()` becomes True again, it RESETS `_audit_emitted_for_current_stall = False` (so a future stall can audit again)
AND the watchdog task signature becomes `watchdog_task(interval: float, *, loop_liveness: LoopLiveness, observability: ObservabilityService, send_sd_notify: bool) -> None` — `send_sd_notify=False` for Docker deployments (no systemd watchdog), `send_sd_notify=True` for native systemd deployments
AND audit emission is wrapped in try/except (mirror `_emit_failure_audit` pattern); failure logs `audit_emit_failed` and continues (the stall is the priority signal; an audit DB failure does not change recovery behavior)

**AC11b — Watchdog/stall-monitor task ALWAYS runs (Docker parity)**

GIVEN `src/open_ems/web/app.py` lifespan currently gates the watchdog task start on `WATCHDOG_USEC` being set (lines 184-197 of `app.py` — `interval = get_watchdog_interval(); if interval is not None: _watchdog_task = asyncio.create_task(...)`)
THEN this gating is REMOVED — the task ALWAYS starts, in BOTH native systemd and Docker deployments
AND the start-time decision becomes:
```python
systemd_interval = get_watchdog_interval()  # None if no systemd watchdog
if systemd_interval is not None:
    interval = systemd_interval
    send_sd_notify = True
    logger.info("watchdog_started", interval_seconds=round(interval, 3),
                send_sd_notify=True, component="startup")
else:
    # Docker / no-systemd path: poll at the control-loop interval so stalls are
    # detected at the same cadence as on systemd
    interval = settings.control_loop_interval_seconds
    send_sd_notify = False
    logger.info("stall_monitor_started", interval_seconds=round(interval, 3),
                send_sd_notify=False, component="startup")
_watchdog_task = asyncio.create_task(
    watchdog_task(interval, loop_liveness=loop_liveness,
                  observability=observability, send_sd_notify=send_sd_notify)
)
_watchdog_task.add_done_callback(_on_watchdog_done)
```
AND inside `watchdog_task`, the heartbeat call is conditional:
```python
if loop_liveness.is_alive():
    if send_sd_notify:
        sd_notify("WATCHDOG=1")
    audit_emitted_for_current_stall = False
else:
    if not audit_emitted_for_current_stall:
        await _emit_stall_audit(observability, loop_liveness)
        audit_emitted_for_current_stall = True
```
AND in Docker mode the stall is detected by the same task and emits the SYSTEM audit; the 503 response from `/health/ready` (AC3) is the supervisor-restart trigger
AND the existing test `test_interval_none_when_no_env` (which asserts `get_watchdog_interval() returns None`) is unchanged — only the lifespan gating logic is changed; `get_watchdog_interval()` itself still returns None when no systemd watchdog is configured

**AC12 — Tests**

Unit tests in `tests/unit/services/test_loop_liveness.py` (NEW file):

1. `test_initial_state_is_not_alive_and_not_in_fail_safe()` — fresh `LoopLiveness` → `is_alive() is False`, `in_fail_safe is False`, `crashed is False`
2. `test_is_alive_true_after_recent_mark_cycle_complete()` — call `mark_cycle_complete()`, then `is_alive()` returns True
3. `test_is_alive_false_when_last_cycle_older_than_threshold()` — set monotonic via injected `now_monotonic` arg to be > threshold older → False
4. `test_is_alive_false_when_crashed()` — call `mark_crashed(exc)`, even if `last_cycle_completed_at_monotonic` is recent → False (crashed dominates)
5. `test_mark_crashed_sets_in_fail_safe()` — `mark_crashed` also sets `in_fail_safe=True`
6. `test_mark_crashed_truncates_long_repr()` — exception with very long `repr` truncates `crash_reason` to ≤ 500 chars
7. `test_seconds_since_last_cycle_returns_none_when_never_ticked()` — fresh object → `seconds_since_last_cycle()` is None
8. `test_seconds_since_last_cycle_returns_elapsed()` — after `mark_cycle_complete()` and a fixed `now_monotonic`, returns the elapsed difference

Unit tests in `tests/unit/services/test_watchdog.py` (UPDATE existing file):

9. `test_watchdog_skips_sd_notify_when_loop_not_alive()` — inject a `LoopLiveness` with `last_cycle_completed_at_monotonic=None` → after one sleep iteration, `sd_notify` is NOT called; the precursor "watchdog_missed delay check" must no longer fire (it has been removed)
10. `test_watchdog_sends_sd_notify_when_loop_alive()` — inject a `LoopLiveness` with `mark_cycle_complete()` just called → `sd_notify("WATCHDOG=1")` IS called once per tick
11. `test_watchdog_emits_audit_once_per_stall_stretch()` — inject a `MagicMock(spec=ObservabilityService)`; loop transitions alive → not-alive → not-alive → alive → not-alive; audit is called exactly 2 times (once per stall onset, not on every "still stalled" tick)
12. `test_watchdog_audit_event_type_is_SYSTEM()` — assert `event_type="SYSTEM"`, `actor="system"`, summary contains `"Control loop stalled"` and `"missed"`
13. `test_watchdog_audit_emit_failure_swallowed()` — `observability.audit` raises `RuntimeError`; watchdog continues running; structured `audit_emit_failed` log is emitted
14. The 10 existing tests in `tests/unit/services/test_watchdog.py` are updated to inject a `LoopLiveness` and `MagicMock(spec=ObservabilityService)`; the precursor "delayed wake-up" tests (`test_watchdog_task_logs_missed_on_delay`, `test_watchdog_task_no_missed_on_first_heartbeat`) are REPLACED with the new gating-on-`is_alive()` semantics — do not keep both (the old precursor was always best-effort and is now obsolete)

14b. `test_watchdog_skips_sd_notify_when_send_sd_notify_false()` — `send_sd_notify=False` (Docker mode) + alive loop → `sd_notify` is NOT called; but the task still polls and would emit a SYSTEM audit on stall (verified separately by #11)
14c. `test_watchdog_emits_audit_in_docker_mode_on_stall()` — `send_sd_notify=False` + `LoopLiveness` reports not-alive → SYSTEM audit IS emitted (Docker parity for AC11b)

Unit tests in `tests/unit/web/test_health_routes.py` (UPDATE existing file):

15. `test_readiness_503_when_loop_not_alive()` — `is_ready() is True` AND `LoopLiveness.is_alive() is False` → 503 with `{"status": "degraded", "reason": "control_loop_stalled"}`
16. `test_readiness_503_when_in_fail_safe()` — `is_ready() is True`, alive, but `LoopLiveness.in_fail_safe is True` → 503 with `{"status": "degraded", "reason": "fail_safe_active"}`
17. `test_readiness_200_when_ready_alive_not_in_fail_safe()` — all three conditions met → 200 with `{"status": "ready"}`
18. `test_readiness_503_starting_unchanged_when_not_ready()` — `is_ready() is False` returns 503 with `{"status": "starting"}` — preserves Story 1.3 behavior; do not regress
19. `test_liveness_unchanged()` — assert `/health/live` returns 200 unconditionally regardless of LoopLiveness state — adapter pattern: liveness is process-existence, readiness is loop-progress

Unit tests in `tests/unit/engine/test_control_loop.py` (UPDATE existing file):

20. `test_fail_safe_entry_emits_audit_and_skips_dispatch()` — `EvaluationResult.recommended_operating_mode = fail_safe` + non-empty intents in result → `IntentExecutor.translate` is NOT called (or commands list is empty); `RetryPolicy.execute` is NOT called; `ObservabilityService.audit(event_type="SYSTEM")` is called once with summary containing `"Fail-safe entered"`; `LoopLiveness.in_fail_safe` is True
21. `test_fail_safe_consecutive_cycles_no_repeat_audit()` — two ticks both producing `fail_safe` mode → audit is called exactly once (entry only); the second tick does NOT emit
22. `test_fail_safe_summary_lists_degraded_roles()` — snapshot has `DegradedDeviceState` for grid_meter and battery → audit summary includes `"grid_meter"` AND `"battery"`
23. `test_fail_safe_exit_requires_recommendation_and_recovery()` — enter fail-safe with grid_meter degraded; next cycle: `recommended_operating_mode=normal` BUT grid_meter still `DegradedDeviceState` → loop STAYS in fail-safe (no exit audit, no command dispatch); third cycle: grid_meter healthy + mode=normal → exit audit emitted, commands dispatched on this cycle
24. `test_fail_safe_exit_audit_lists_recovered_devices()` — exit summary contains the originally-degraded role names (`grid_meter`, `battery`) and the `cycle_id`
25. `test_fail_safe_does_not_exit_while_engine_still_recommends_fail_safe()` — entry-degraded roles are now healthy BUT engine still returns `mode=fail_safe` → loop stays in fail-safe (the engine's recommendation gates exit, AC5 condition (a))
26. `test_cycle_completion_marked_after_successful_tick()` — successful `_tick()` call → `LoopLiveness.mark_cycle_complete()` is invoked exactly once
27. `test_cycle_completion_marked_for_fail_safe_cycle()` — fail-safe cycle still marks completion (the engine produced a decision; the loop is alive, just suppressing dispatch)
28. `test_cycle_completion_NOT_marked_when_slots_missing()` — `evaluation_skipped_missing_required_slots` path returns from `_tick` early → `mark_cycle_complete` NOT called; this is correct behavior (the engine never ran a full cycle)

Unit tests in `tests/unit/engine/test_retry_policy.py` (UPDATE existing file):

29. `test_retry_policy_emits_device_audit_on_cancellation()` — script `policy_guard.authorize_and_dispatch` to return `failed` then await on a sleep; cancel the outer task during backoff; assert `CancelledError` is raised AND exactly ONE `observability.audit(event_type="DEVICE")` was emitted with summary containing `"cancelled mid-retry"` AND a `retry_cancelled` warning log was emitted
30. `test_retry_policy_audit_failure_during_cancellation_swallowed()` — `observability.audit` itself raises during the cancel path → `CancelledError` still propagates; `audit_emit_failed` is logged; no other exception type is raised

Integration test in `tests/integration/web/test_health_under_loop_states.py` (NEW file):

31. `test_health_ready_reflects_control_loop_progress()` — start a real `ControlLoop` with mocked adapters; tick once successfully → `/health/ready` returns 200; advance simulated monotonic clock past threshold without ticking → `/health/ready` returns 503
32. `test_health_ready_reflects_fail_safe()` — drive a tick that sets `recommended_operating_mode=fail_safe` → `/health/ready` returns 503 with `reason="fail_safe_active"`

Integration test in `tests/integration/engine/test_fail_safe_lifecycle.py` (NEW file):

33. `test_fail_safe_full_lifecycle()` — REAL `ControlLoop`, REAL `RetryPolicy`, REAL `PolicyGuard`, simulated adapters; cycle 1: grid_meter degraded → fail-safe entered, NO `adapter.send_command()` called, ONE `event_log` row with `event_type="SYSTEM"` and summary `"Fail-safe entered"`; cycle 2 (still degraded): no new audit; cycle 3: grid_meter recovered AND engine recommends normal → fail-safe exited, ONE additional `event_log` row with `"Fail-safe exited"`, commands dispatched on this cycle
34. `test_control_loop_crash_emits_audit_and_marks_loop_liveness()` — simulate `_tick` raising `RuntimeError` → `_on_control_loop_done` fires → `LoopLiveness.crashed is True`, `LoopLiveness.in_fail_safe is True`, ONE `event_log` row with `event_type="SYSTEM"` and summary containing `"Control loop crashed"`

**AC13 — Pre/post quality gate**

GIVEN the story is closed
THEN baseline tests passing pre-story (expected: 822 from Story 8.3) → post-story tests passing increases by the new tests (~30+ new), all green
AND `uv run python -m ruff check .` → clean
AND `uv run python -m ruff format --check .` → clean
AND `uv run python -m mypy src/` → clean
AND `python -c "from open_ems.settings import Settings; s = Settings(_env_file=None); print(s.watchdog_missed_cycle_threshold, s.watchdog_cycle_deadline_seconds)"` → `2 60.0`

---

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC13)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — record baseline test count (expected: 822 from Story 8.3)
  - [x] Run `uv run python -m ruff check .` — must be clean
  - [x] Run `uv run python -m ruff format --check .` — must be clean
  - [x] Run `uv run python -m mypy src/` — must be clean

- [x] Task 1: Settings — add watchdog tunables (AC8)
  - [x] Open `src/open_ems/settings.py`
  - [x] Append after `command_retry_backoff_seconds` (line 34):
    ```python
    watchdog_missed_cycle_threshold: int = Field(default=2, ge=1, le=10)
    watchdog_cycle_deadline_seconds: float = Field(default=60.0, gt=0.0, le=300.0)
    ```
  - [x] No env-var aliases needed — pydantic-settings auto-derives `WATCHDOG_MISSED_CYCLE_THRESHOLD` and `WATCHDOG_CYCLE_DEADLINE_SECONDS`

- [x] Task 2: Create `LoopLiveness` (AC9; tests AC12 #1–#8)
  - [x] Create `src/open_ems/services/loop_liveness.py` per the spec in AC9
  - [x] Use `time.monotonic()` everywhere — NOT `datetime.now()` (monotonic is immune to wall-clock jumps; this matters for stall detection across NTP step adjustments)
  - [x] Allow `now_monotonic` injection on `is_alive` and `seconds_since_last_cycle` (test patchability; matches the existing `_monotonic` pattern in `services/watchdog.py:14`)
  - [x] Truncate `crash_reason` to 500 chars to bound memory if a long traceback repr arrives
  - [x] Add module-level docstring referencing the three consumers (ControlLoop writer; watchdog_task and health endpoint readers) and the single-task-asyncio safety guarantee
  - [x] Create `tests/unit/services/test_loop_liveness.py` with tests #1–#8

- [x] Task 3: Refactor `services/watchdog.py` to consume `LoopLiveness` (AC1, AC10, AC11)
  - [x] Update `watchdog_task` signature: `async def watchdog_task(interval: float, *, loop_liveness: LoopLiveness, observability: ObservabilityService) -> None`
  - [x] Inside the loop:
    ```python
    audit_emitted_for_current_stall = False
    while True:
        await asyncio.sleep(interval)
        if loop_liveness.is_alive():
            sd_notify("WATCHDOG=1")
            audit_emitted_for_current_stall = False
        else:
            if not audit_emitted_for_current_stall:
                await _emit_stall_audit(observability, loop_liveness)
                audit_emitted_for_current_stall = True
            # Skip sd_notify so WatchdogSec elapses → systemd restart
    ```
  - [x] Implement `async def _emit_stall_audit(observability, loop_liveness) -> None`:
    - Build summary: `f"Control loop stalled: no successful evaluation cycle in {elapsed_str} (missed {missed_count} consecutive cycles); supervisor restart pending"` where `elapsed_str` uses `seconds_since_last_cycle()` (handle `None` as `"unknown"`); `missed_count = max(1, int(elapsed / cycle_interval))` — derived from `loop_liveness` state
    - Wrap `observability.audit(actor="system", event_type="SYSTEM", summary=summary)` in try/except logging `audit_emit_failed` on failure
  - [x] REMOVE the existing precursor `last_sent`/`elapsed > interval * 1.5` block (lines 43-55) — replaced by `is_alive()` consultation
  - [x] Preserve the module-level `_monotonic = time.monotonic` reference for backward compatibility with existing tests, but it is no longer read by the new logic — tests patch `LoopLiveness.is_alive` directly
  - [x] Update `tests/unit/services/test_watchdog.py`:
    - Update all 10 existing tests to inject `loop_liveness=MagicMock(spec=LoopLiveness)` and `observability=MagicMock(spec=ObservabilityService)`
    - REPLACE `test_watchdog_task_logs_missed_on_delay` and `test_watchdog_task_no_missed_on_first_heartbeat` with the new `is_alive()`-based semantics
    - Add tests #9–#13 from AC12

- [x] Task 4: Update `engine/control_loop.py` (AC4, AC5, AC10; tests AC12 #20–#28)
  - [x] Add new constructor kwargs: `loop_liveness: LoopLiveness` (required), `observability: ObservabilityService` (required); store on `self`
  - [x] Add `from open_ems.services.loop_liveness import LoopLiveness` and `from open_ems.services.audit_log import ObservabilityService`
  - [x] Add new private state: `self._fail_safe_entry_degraded_roles: frozenset[DeviceRole] = frozenset()`
  - [x] Refactor `_handle_evaluation_result` per the AC10 pseudocode — the path is: fail-safe entry → fail-safe exit gate → normal dispatch
  - [x] Helper: `def _degraded_roles_in(self, snapshot: SystemSnapshot) -> frozenset[DeviceRole]:` returns roles whose slot is `DegradedDeviceState`
  - [x] Helper: `def _can_exit_fail_safe(self, snapshot: SystemSnapshot) -> bool:` returns True iff every role in `self._fail_safe_entry_degraded_roles` has a non-degraded, non-None slot in `snapshot`
  - [x] Helper: `async def _emit_fail_safe_entry_audit(self, result: EvaluationResult, snapshot: SystemSnapshot) -> None:` — wraps `observability.audit` in try/except (audit-failure does not block fail-safe entry); summary format per AC4
  - [x] Helper: `async def _emit_fail_safe_exit_audit(self, result: EvaluationResult, snapshot: SystemSnapshot) -> None:` — same pattern; summary lists `self._fail_safe_entry_degraded_roles` (the roles that were degraded at entry, now recovered)
  - [x] In `_tick()`: after `await self._handle_evaluation_result(result, snapshot)` returns, call `self._loop_liveness.mark_cycle_complete()` — this is the ONLY callsite for mark_cycle_complete in the loop
  - [x] Do NOT call `mark_cycle_complete` on the `evaluation_skipped_missing_required_slots` early-return path
  - [x] Update tests/unit/engine/test_control_loop.py per AC12 #20–#28 — the existing `_control_loop()` helper accepts `loop_liveness=None` and `observability=None` kwargs and falls back to `LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)` and `MagicMock(spec=ObservabilityService)`

- [x] Task 5: Update `web/routes/health.py` (AC3; tests AC12 #15–#19)
  - [x] Inject `LoopLiveness` via FastAPI Request: `async def readiness(request: Request) -> JSONResponse:`
  - [x] Read `loop_liveness = request.app.state.loop_liveness`
  - [x] Logic:
    ```python
    if not is_ready():
        return JSONResponse({"status": "starting"}, status_code=503)
    if loop_liveness.crashed or not loop_liveness.is_alive():
        return JSONResponse(
            {"status": "degraded", "reason": "control_loop_stalled"},
            status_code=503,
        )
    if loop_liveness.in_fail_safe:
        return JSONResponse(
            {"status": "degraded", "reason": "fail_safe_active"},
            status_code=503,
        )
    return JSONResponse({"status": "ready"})
    ```
  - [x] `/health/live` is UNCHANGED — assert this in test #19
  - [x] Update `tests/unit/web/test_health_routes.py` per AC12 #15–#19 — note that the existing tests call `await readiness()` directly without a Request; they must be refactored to use FastAPI's `TestClient` OR construct a fake `Request` with `request.app.state.loop_liveness` set; prefer `TestClient` against a minimal `FastAPI()` with `app.state.loop_liveness` for clarity

- [x] Task 6: Update `web/app.py` lifespan (AC9, AC6, AC11b; test AC12 #34)
  - [x] Add imports: `from open_ems.services.loop_liveness import LoopLiveness` and `from open_ems.engine.control_loop import ControlLoop` (already present)
  - [x] In lifespan, BEFORE the watchdog task is started, construct `loop_liveness` AND `observability` (if not already extracted) so both can be shared:
    ```python
    observability = ObservabilityService()  # shared across PolicyGuard, RetryPolicy, watchdog, control loop, _on_control_loop_done
    loop_liveness = LoopLiveness(
        missed_cycle_threshold_seconds=(
            settings.watchdog_missed_cycle_threshold * settings.control_loop_interval_seconds
        ),
        cycle_deadline_seconds=settings.watchdog_cycle_deadline_seconds,
    )
    app.state.loop_liveness = loop_liveness
    ```
  - [x] Construct ONE `observability = ObservabilityService()` BEFORE both PolicyGuard and the watchdog task; the existing `observability` local at line 224 already does this — confirm and reuse it (do NOT instantiate a second `ObservabilityService()` inside the watchdog branch)
  - [x] **Replace** the existing watchdog start block (currently lines 184-197 — gated on `interval is not None`) with the AC11b two-mode start (always runs):
    ```python
    systemd_interval = get_watchdog_interval()
    if systemd_interval is not None:
        watchdog_interval = systemd_interval
        send_sd_notify = True
    else:
        watchdog_interval = settings.control_loop_interval_seconds
        send_sd_notify = False

    def _on_watchdog_done(t: asyncio.Task[None]) -> None:
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error("watchdog_task_died", exc_info=exc, component="watchdog")

    _watchdog_task = asyncio.create_task(
        watchdog_task(
            watchdog_interval,
            loop_liveness=loop_liveness,
            observability=observability,
            send_sd_notify=send_sd_notify,
        )
    )
    _watchdog_task.add_done_callback(_on_watchdog_done)
    logger.info(
        "watchdog_started" if send_sd_notify else "stall_monitor_started",
        interval_seconds=round(watchdog_interval, 3),
        send_sd_notify=send_sd_notify,
        component="startup",
    )
    ```
  - [x] Pass `loop_liveness=loop_liveness, observability=observability` to `ControlLoop(...)` — both as new kwargs
  - [x] Replace `_on_control_loop_done` with the AC6 implementation:
    ```python
    def _on_control_loop_done(t: asyncio.Task[None]) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            return
        logger.error("control_loop_died", exc_info=exc, component="engine")
        loop_liveness.mark_crashed(exc)  # also sets in_fail_safe=True
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop — process is exiting; only the log is recorded
        async def _emit_crash_audit() -> None:
            try:
                await observability.audit(
                    actor="system",
                    event_type="SYSTEM",
                    summary=(
                        f"Control loop crashed: {type(exc).__name__}: {exc!r}; "
                        "entering fail-safe pending supervisor restart"
                    ),
                )
            except Exception as audit_exc:  # noqa: BLE001
                logger.error(
                    "audit_emit_failed",
                    component="engine",
                    error=repr(audit_exc),
                    exc_info=True,
                )
        running.create_task(_emit_crash_audit())
    ```
  - [x] The `loop_liveness` and `observability` references are captured by closure inside `_on_control_loop_done` — this pattern is already used by `_on_watchdog_done` and `_on_cleanup_done`
  - [x] No new database migration is required

- [x] Task 7: Update `engine/retry_policy.py` to handle cancellation cleanly (AC7; tests AC12 #29–#30)
  - [x] Wrap the body of `execute()` in a `try/except asyncio.CancelledError`:
    ```python
    async def execute(self, command: DeviceCommand) -> CommandResult:
        max_attempts = self._settings.command_max_retries + 1
        backoff = self._settings.command_retry_backoff_seconds
        last_result: CommandResult | None = None
        attempt = 0  # track for the cancel path — initialized BEFORE try
        try:
            for attempt in range(1, max_attempts + 1):
                # ... existing body unchanged ...
            assert last_result is not None
            await self._emit_failure_audit(command, last_result, attempts=attempt)
            return last_result
        except asyncio.CancelledError:
            logger.warning(
                "retry_cancelled",
                device_id=command.device_id,
                command_type=type(command).__name__,
                attempts=attempt,
                correlation_id=str(command.correlation_id),
                component="engine",
            )
            try:
                await self._observability.audit(
                    actor="system",
                    event_type="DEVICE",
                    summary=(
                        f"Command {type(command).__name__} for {command.device_id} "
                        f"cancelled mid-retry after {attempt} attempt(s): "
                        "pending result indeterminate"
                    ),
                    device_id=command.device_id,
                )
            except Exception as audit_exc:  # noqa: BLE001
                logger.error(
                    "audit_emit_failed",
                    device_id=command.device_id,
                    command_type=type(command).__name__,
                    error=repr(audit_exc),
                    exc_info=True,
                    component="engine",
                )
            raise
    ```
  - [x] Note: do NOT use `asyncio.shield(...)` around the audit call — letting the audit be cancelled is acceptable; the log line is the fallback
  - [x] The existing `assert last_result is not None` and `_emit_failure_audit` flow are preserved — only the cancel branch is new
  - [x] Add tests #29 and #30 to `tests/unit/engine/test_retry_policy.py`

- [x] Task 8: Integration tests (AC12 #31–#34)
  - [x] Create `tests/integration/web/test_health_under_loop_states.py` with #31, #32 — use `TestClient(create_app())`; mock the control loop so we can drive `LoopLiveness` directly via `app.state.loop_liveness.mark_cycle_complete()` / `mark_fail_safe_entered()`
  - [x] Create `tests/integration/engine/test_fail_safe_lifecycle.py` with #33, #34 — REUSE the `SimulatedBatteryAdapter` / `SimulatedEVChargerAdapter` patterns from `tests/integration/engine/test_command_pipeline.py` and `test_retry_pipeline.py`; for the grid meter, build a `_FailingThenHealthyGridMeter` adapter that returns degraded state for the first N polls then healthy state
  - [x] Mock the `EventLogRepo` underneath `ObservabilityService` (use `AsyncMock(spec=EventLogRepo)`) so we can assert the audit call sequence

- [x] Task 9: Final validation (AC13)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — all tests pass; record final count (~852+ expected)
  - [x] Run `uv run python -m ruff check .` — clean
  - [x] Run `uv run python -m ruff format --check .` — clean
  - [x] Run `uv run python -m mypy src/` — clean
  - [x] Verify Settings smoke test: `python -c "from open_ems.settings import Settings; s = Settings(_env_file=None); print(s.watchdog_missed_cycle_threshold, s.watchdog_cycle_deadline_seconds)"` → `2 60.0`

### Review Findings

_Code review 2026-05-10 — 3-layer adversarial: Blind Hunter, Edge Case Hunter, Acceptance Auditor._

**Patch**

- [x] [Review][Patch][HIGH] `missed_count` arithmetic undercounts by factor of `watchdog_missed_cycle_threshold` [`src/open_ems/services/watchdog.py:87-88`] — `cycle_interval` variable is assigned `loop_liveness.missed_cycle_threshold_seconds` (the threshold WINDOW = `threshold × control_loop_interval_seconds`), not the per-cycle interval. With defaults (threshold=2, interval=10s), at 30s elapsed audit reports `missed=1` when 3 cycles were actually missed. Need to either store `control_loop_interval_seconds` directly on `LoopLiveness` and divide by it, or pass `threshold_count` separately so the math can recover the per-cycle value.
- [x] [Review][Patch][HIGH] Cold-start stall audit + sd_notify suppression before first cycle completes [`src/open_ems/services/watchdog.py:60-68`, `src/open_ems/services/loop_liveness.py:is_alive`] — At process startup, `last_cycle_completed_at_monotonic` is None → `is_alive()` returns False. The watchdog's first tick (at `t=interval`) therefore (a) emits a "Control loop stalled: no successful evaluation cycle in unknown duration" SYSTEM audit at t≈interval seconds, and (b) suppresses `sd_notify("WATCHDOG=1")`, which in systemd mode can directly cause systemd to kill the freshly-started process if the loop's first cycle hasn't completed within `WatchdogSec`. Add a startup grace: if `last_cycle_completed_at_monotonic is None` AND process uptime < (e.g.) `cycle_deadline_seconds`, send sd_notify and skip the audit. After grace expires, treat never-ticked as stalled (per AC3 intent for /health/ready).
- [x] [Review][Patch][HIGH] `_last_mode = new_mode` set BEFORE fail-safe-suppression check causes inconsistent snapshot mode [`src/open_ems/engine/control_loop.py:190-191`] — When `in_fail_safe=True` and engine recommends `normal` but `_can_exit_fail_safe()` returns False, `_handle_evaluation_result` early-returns. But `self._last_mode = new_mode` ran first, so the NEXT `_tick`'s `state_store.publish(..., operating_mode=self._last_mode)` writes `operating_mode=normal` to the snapshot while the loop is still suppressing dispatch (`in_fail_safe=True`). Downstream observers (UI, audit consumers) see contradictory state. Fix: defer `self._last_mode = new_mode` until after the fail-safe-suppression decision, OR explicitly set `self._last_mode = SystemOperatingMode.fail_safe` when staying in fail-safe.
- [x] [Review][Patch][HIGH] Tests #29 and #30 do not verify the `retry_cancelled` warning log [`tests/unit/engine/test_retry_policy.py`] — Spec AC7 explicitly requires `logger.warning("retry_cancelled", ...)` "even if the audit DB write fails — this structured log appears in journald/Docker logs for post-mortem". AC12 #29's docstring also says it asserts the warning. Test #29 only asserts the audit; test #30's `FakeLogger.warning` is a no-op (never recorded). Fix: capture `warning` events in the FakeLogger and assert `"retry_cancelled"` event was emitted in both tests.
- [x] [Review][Patch][HIGH] Integration test #34 does not exercise production `_on_control_loop_done` [`tests/integration/engine/test_fail_safe_lifecycle.py`, `src/open_ems/web/app.py:275-309`] — The test redefines the callback semantics inline rather than importing from `app.py`. A future refactor of the production callback would not be caught. Refactor `_on_control_loop_done` into a module-level factory function in `app.py` (e.g., `make_on_control_loop_done(loop_liveness, observability)` returning the closure), then have the integration test import and invoke it directly.
- [x] [Review][Patch][MEDIUM] Entry audit cancellation can leave state machine in entry-not-marked state [`src/open_ems/engine/control_loop.py` fail-safe entry block] — The order is: capture `_fail_safe_entry_degraded_roles` → `await _emit_fail_safe_entry_audit(...)` → `mark_fail_safe_entered()`. If a `CancelledError` propagates out of the audit await (e.g., shutdown during fail-safe entry), `mark_fail_safe_entered()` never runs → on a subsequent recovery attempt the loop sees `in_fail_safe=False` and re-enters → duplicate entry audit. Fix: call `mark_fail_safe_entered()` BEFORE the audit await, OR wrap with `try/finally`.
- [x] [Review][Patch][MEDIUM] `/health/ready` fails OPEN when `app.state.loop_liveness` is missing [`src/open_ems/web/routes/health.py:20-32`] — Current `getattr(request.app.state, "loop_liveness", None)` returns 200 if absent. Spec AC3 requires ALL three conditions (ready + alive + not-fail_safe) for 200, and AC9 requires lifespan to always wire `loop_liveness`. A misconfiguration that drops `loop_liveness` should fail closed (503). Remove the fallback; delete `test_readiness_200_when_app_state_has_no_loop_liveness`.
- [x] [Review][Patch][MEDIUM] `_SNAPSHOT_SLOT_ATTR` enumeration is not validated against `DeviceRole` [`src/open_ems/engine/control_loop.py`] — A future `DeviceRole` member added without a corresponding entry will silently drop that role from `_degraded_roles_in()` accounting (entry-degraded set wrong; recovery appears instant; audit summary misrepresents). Add a module-load assertion: `assert set(_SNAPSHOT_SLOT_ATTR) == set(DeviceRole)`.
- [x] [Review][Patch][LOW] Stall summary deviates from spec format on never-ticked path [`src/open_ems/services/watchdog.py:78-83`] — Spec AC2 format: `"...in {elapsed_seconds:.1f} s..."`. Impl outputs `"...in unknown duration (missed 1 consecutive cycles)..."` — missing the `s` suffix and grammatically off ("1 consecutive cycles"). Fix: match spec format (treat None elapsed as `0.0` once startup grace is in place per HIGH#2), or pluralize correctly with a singular form for `missed=1`.
- [x] [Review][Patch][LOW] Cancellation audit reports "after 0 attempt(s)" if cancelled before first iteration [`src/open_ems/engine/retry_policy.py`] — `attempt = 0` initialized before the try; if cancellation lands before `for attempt in range(1, ...)` executes its first body, the audit reads "cancelled mid-retry after 0 attempt(s)" — semantically wrong (no retry was in progress). Fix: branch on `attempt == 0` and use distinct phrasing like `"cancelled before first dispatch"` or `max(attempt, 1)` with a slight rewording.
- [x] [Review][Patch][LOW] Test relies on `await asyncio.sleep(0.05)` to "ensure we're inside backoff" — flaky and lies about coverage [`tests/unit/engine/test_retry_policy.py`] — Test #29's name claims it covers cancellation during backoff, but on slow CI the 0.05s wait may not yet have entered `asyncio.sleep(1.0)`. The cancellation could land during dispatch, missing the claimed branch. Replace with a deterministic signal: have the mocked `policy_guard.authorize_and_dispatch` set an `asyncio.Event` after returning the failed result, then `await event.wait()` before cancelling.
- [x] [Review][Patch][LOW] Priority of `crashed` vs `in_fail_safe` 503 reasons is untested [`tests/unit/web/test_health_routes.py`] — Both branches return 503 but with different `reason` payloads, and `mark_crashed()` sets both flags. The check order matters (current impl: stalled-or-crashed wins) but a future re-ordering would silently change the reason. Add `test_readiness_503_crashed_takes_precedence_over_fail_safe`.
- [x] [Review][Patch][LOW] `test_uuid_module_imported` is a tautological dead test [`tests/integration/engine/test_fail_safe_lifecycle.py`] — Asserts `assert uuid is not None` (always True for a successful import). The docstring claims `uuid` is "used by some helpers above" but it does not appear to be used. Either remove the test, or remove the unused `uuid` import if it is genuinely unused.

**Deferred**

- [x] [Review][Defer] `observability.audit` slow DB write blocks heartbeat tick [`src/open_ems/services/watchdog.py:60-69`] — deferred, broader DB-hangs concern. If `await observability.audit(...)` exceeds `interval`, no `WATCHDOG=1` is sent for the whole audit duration. Wrapping with `asyncio.wait_for(..., timeout=interval)` is the obvious mitigation but the underlying DB-hang scenario affects other call sites equally and should be addressed holistically.

---

## Dev Notes

### Why this story exists in this exact shape

Stories 8.1, 8.2, 8.3 implemented: control loop with adapter polling and StateStore publishing (8.1), IntentExecutor + PolicyGuard + command dispatch (8.2), bounded retry with idempotency (8.3). What is **missing** and is this story's job:

1. **No cycle-progress signal** — the existing `services/watchdog.py:watchdog_task` sends sd_notify heartbeats on its own timer regardless of whether the control loop is making progress. A deadlocked or crashed loop can still appear healthy to systemd.
2. **No stalled-loop detection** — there is no audit event when the control loop misses cycles; the only signal today is the precursor "watchdog_missed delayed wake-up" log from Story 1.5, which is best-effort and unrelated to control-loop progress.
3. **No fail-safe transition** — `_handle_evaluation_result` dispatches commands regardless of `recommended_operating_mode`. The decision engine emits `recommended_operating_mode=fail_safe` (from Story 7.1) but no caller acts on it. FR34's "ceases issuing all control commands when device communication is lost" is unimplemented.
4. **No fail-safe exit gate** — there is no state machine to track whether the system is in fail-safe and whether exit conditions are met. A single transient healthy read could flap dispatch on/off.
5. **`_on_control_loop_done` is non-escalating** — it logs `control_loop_died` and stops; the system continues serving requests with stale StateStore (deferred from Story 8.2 review).
6. **`/health/ready` doesn't reflect loop progress** — it only returns 503 during startup (`_ready=False`). After startup, a stalled loop or fail-safe state is invisible to Docker's healthcheck restart policy.
7. **`RetryPolicy.asyncio.sleep` is not cancellation-clean** — control-loop shutdown mid-retry drops the in-flight retry without a DEVICE audit (deferred from Story 8.3 review).

### Design philosophy: prefer supervisor restart, not in-process restart

Both systemd (`WatchdogSec`) and Docker (healthcheck restart policy) have battle-tested process-restart machinery. This story builds on that infrastructure rather than implementing in-process loop restart, because:

- Process restart is the strongest reset: re-runs migrations, rehydrates StateStore, re-establishes adapter connections, clears any leaked task state.
- In-process restart of a single coroutine adds complexity (which subtask? what state was lost? what audit semantics?) for marginal benefit.
- The cross-story rule from epics.md:1861 already says: "Supervisor-managed restart is the primary watchdog recovery path; internal subtask restart is the exception and must emit its own audit event." This story honors that rule by NOT implementing subtask restart.

The implementation reduces to: when the loop is unhealthy, stop sending the systemd heartbeat AND return 503 from `/health/ready`. The supervisor handles restart on its own schedule.

### `LoopLiveness` is a pure state holder, not a service

Three readers consume `LoopLiveness`:
- `ControlLoop` (writer): calls `mark_cycle_complete()`, `mark_fail_safe_entered()`, `mark_fail_safe_exited()`.
- `watchdog_task` (reader): polls `is_alive()`.
- `/health/ready` route handler (reader): polls `is_alive()` and `in_fail_safe`.
- `_on_control_loop_done` callback (writer): calls `mark_crashed(exc)`.

In the current single-task asyncio architecture, the writer (ControlLoop) and readers (watchdog_task, health endpoint) all run in the same event loop. Concurrent reads do not race because the writer's `mark_*` calls are atomic boolean/float assignments, and readers consult one field at a time. There is NO need for a lock, but document this assumption explicitly in the module docstring so a future contributor doesn't introduce concurrent writers.

### Why the cycle-completion mark is in `_tick`, not `_handle_evaluation_result`

A fail-safe cycle is a *successful* evaluation: the engine ran, produced a decision, and the loop honored that decision (even if the decision was "do nothing"). It must count as a completion for liveness purposes — otherwise a system stuck in fail-safe would falsely appear stalled.

The `evaluation_skipped_missing_required_slots` early-return is different: in that case the engine never produced a result, so the cycle is incomplete from a stalled-loop perspective. Do NOT mark completion on that branch.

```python
async def _tick(self) -> None:
    now = datetime.now(UTC)
    device_states = await self._poll_adapters()
    snapshot = await self._state_store.publish(device_states, operating_mode=self._last_mode)
    await self._update_tracker(device_states, now)
    try:
        evaluation_input = self._build_evaluation_input(snapshot, now)
    except ValidationError:
        logger.debug("evaluation_skipped_missing_required_slots", ...)
        return  # NO mark_cycle_complete here
    result = evaluate_cycle(evaluation_input)
    await self._handle_evaluation_result(result, snapshot)
    self._loop_liveness.mark_cycle_complete()  # ONLY here
```

### Fail-safe entry vs. exit — minimum-state machine

The state machine is two booleans plus a frozenset:
- `loop_liveness.in_fail_safe: bool` — globally visible (health endpoint, watchdog).
- `control_loop._fail_safe_entry_degraded_roles: frozenset[DeviceRole]` — the snapshot of degraded roles AT THE MOMENT of entry, used to gate exit.

State transitions happen in exactly one place: `_handle_evaluation_result`. The transitions are:

```
                         engine recommends
                          fail_safe
       not_in_fail_safe ─────────────────────→ in_fail_safe
                                                     │
                          all entry-degraded         │
                          roles healthy AND          │
                          engine recommends          │
                          NOT fail_safe              │
       in_fail_safe ←──────────────────────────  ←┘
```

Two important behaviours:

1. **Entry is one-shot.** Once `in_fail_safe=True`, a subsequent `recommended_operating_mode=fail_safe` does NOT re-emit the entry audit event. The audit fires once per stretch.

2. **Exit gates on entry-time degraded roles, not current snapshot's degraded roles.** This prevents a flapping device that recovered then re-degraded *during fail-safe* from spuriously gating exit. We only care that the things that broke at entry have healed; new failures are addressed by the engine producing a fresh `fail_safe` recommendation, which keeps us in fail-safe naturally.

### Building the fail-safe entry summary

```python
def _degraded_roles_in(self, snapshot: SystemSnapshot) -> frozenset[DeviceRole]:
    roles: set[DeviceRole] = set()
    for role in (DeviceRole.inverter, DeviceRole.battery, DeviceRole.ev_charger, DeviceRole.grid_meter):
        slot = getattr(snapshot, role.value if hasattr(snapshot, role.value) else _slot_attr(role))
        # snapshot fields are: inverter, battery, ev_charger, grid_meter
        if isinstance(slot, DegradedDeviceState):
            roles.add(role)
    return frozenset(roles)
```

Note: `SystemSnapshot` field names are `inverter`, `battery`, `ev_charger`, `grid_meter`. Use a static mapping rather than introspection — see `src/open_ems/core/state.py:154-159`. Build the device summary as a stable, sorted comma-joined list:

```python
def _format_degraded_roles(roles: frozenset[DeviceRole]) -> str:
    return ", ".join(sorted(role.value for role in roles)) or "(no specific role)"
```

### What "recommended_operating_mode" actually means

The decision engine (Story 7.1's `derive_recommended_operating_mode` at `src/open_ems/engine/operating_mode.py:10`) determines `fail_safe` based on the current snapshot's degraded device roles per the architecture's degradation matrix (architecture.md:1080-1106). That matrix says: grid meter + any second critical adapter → fail-safe; all adapters degraded → fail-safe. Story 8.4 does NOT re-implement that logic — it CONSUMES the engine's recommendation and applies the runtime consequences.

This honors the Epic 7 retro decision: "Single-evaluator principle: `evaluate_cycle()` is the sole generator of control intents. Every other Epic 8 component either provides input or enforces safety — no competing decisions." `ControlLoop` enforces fail-safe; `evaluator.py` decides when fail-safe is recommended. They are not allowed to disagree.

### Health endpoint — why three failure modes are surface as different reasons

`/health/ready` is consumed by Docker's healthcheck (which only cares about 200 vs. 503) AND by humans diagnosing why a deployment is unhealthy. Returning a structured `{"status": "degraded", "reason": "<specific>"}` body lets `docker logs` and Kubernetes events surface a useful failure mode without needing to read the application logs.

Three reasons:
- `"starting"` (was here from Story 1.3) — pre-startup, `is_ready()` is False
- `"control_loop_stalled"` (NEW) — startup complete, but `LoopLiveness.is_alive()` is False (or `crashed=True`)
- `"fail_safe_active"` (NEW) — startup complete, loop alive, but `in_fail_safe=True`

The check ORDER matters: `crashed` and `not is_alive()` should be checked together (a crashed loop is "not alive"), and BEFORE `in_fail_safe` (because a crashed loop is *also* in_fail_safe via `mark_crashed`). Returning `"control_loop_stalled"` is more diagnostic than `"fail_safe_active"` for a crash because it tells the operator the loop isn't even running.

### `_on_control_loop_done` audit — why dispatch via `create_task`

`_on_control_loop_done` is a **synchronous** done-callback registered via `task.add_done_callback(...)`. It cannot `await`. To emit an audit event (which IS async), we dispatch a follow-up coroutine via `asyncio.create_task(...)` from inside the callback. This works because:
- A done-callback fires from inside the running event loop (in normal operation), so `get_running_loop()` succeeds.
- If the loop is shutting down (process exit), `get_running_loop()` raises `RuntimeError` — we skip the audit and only log.

There is a subtle race: if the audit task is created but the lifespan completes before it runs, the audit may not be persisted. This is acceptable — the structured `control_loop_died` log is the authoritative record; the audit event is best-effort. The crash log line is sufficient for post-mortem.

### `RetryPolicy` cancellation handling — why log THEN audit THEN re-raise

When `CancelledError` propagates out of `RetryPolicy.execute()`, Python's cancellation semantics require the exception to be re-raised promptly (or the task is treated as having "ignored" cancellation). We have a brief opportunity to do best-effort cleanup — emit a structured log and attempt an audit — but we must NOT block indefinitely.

- `logger.warning("retry_cancelled", ...)` is synchronous — completes in microseconds.
- `await self._observability.audit(...)` is async and writes to SQLite — could take 10-50 ms but is bounded by the underlying repo's connection. If it's interrupted by another cancellation, that's fine; the log line already captured the essentials.

DO NOT use `asyncio.shield(self._observability.audit(...))` here — shielding the audit means subsequent cancellation requests cannot interrupt it, which could delay shutdown beyond the supervisor's grace period. Let the audit be interruptible; the log is the floor.

### Removing the legacy "watchdog_missed" precursor in `services/watchdog.py`

The current `watchdog_task` (lines 43-55) implements a precursor "if my own sleep was delayed > 1.5x interval, log watchdog_missed". This was added in Story 1.5 as best-effort drift detection. It is now obsolete:

- The new design ties heartbeat to control-loop progress, not to the watchdog task's own scheduling.
- A delayed wake-up in the watchdog task no longer correlates with stalled-loop state — the loop could be healthy while the asyncio scheduler delayed our task (e.g., a long blocking call elsewhere).
- Keeping both checks creates duplicate "watchdog_missed" log entries with different semantics → operator confusion.

REMOVE the precursor block. REPLACE the corresponding tests (`test_watchdog_task_logs_missed_on_delay`, `test_watchdog_task_no_missed_on_first_heartbeat`) with the new `is_alive()`-based semantics. Do NOT keep both; partial deprecation is an anti-pattern.

### Common pitfalls — read before coding

1. **Do NOT call `mark_cycle_complete` on the early-return path** (`evaluation_skipped_missing_required_slots`). That path means the engine never ran; counting it as a healthy tick would mask a misconfigured-slots scenario that should trigger stalled-loop detection.
2. **Do NOT add `loop_liveness` to the `RetryPolicy` constructor** — RetryPolicy's cancellation handling is local; it does not need the shared signal. Adding it would bloat the dependency graph for no benefit.
3. **Do NOT compare `monotonic` values across processes** — `time.monotonic()` is process-local. The `LoopLiveness.last_cycle_completed_at_monotonic` is meaningful only within the running process; on restart it resets to `None` (which is correct: a freshly-restarted process has no cycle history yet).
4. **Do NOT use `datetime.now()` for the liveness signal** — wall-clock time can jump backward (NTP step). Use `time.monotonic()` everywhere.
5. **Do NOT add a `lock` to `LoopLiveness`** — the single-task-asyncio guarantee means concurrent writes don't happen in our architecture. Adding a lock would be premature abstraction.
6. **Do NOT skip the audit-emission `try/except` wrapper** — every audit call site in this story (entry, exit, crash, stall, retry-cancellation) MUST be wrapped per the `RetryPolicy._emit_failure_audit` pattern. Audit-write failure must never block, mask, or escalate the underlying event.
7. **Do NOT change `evaluator.py`** — the engine's `recommended_operating_mode` derivation is Story 7.1's responsibility. This story consumes it.
8. **Do NOT change `policy_guard.py`** — the safety pipeline is Story 8.2's responsibility. Fail-safe is enforced by the loop NOT calling PolicyGuard at all (no commands constructed); PolicyGuard itself remains the dispatch path for the post-fail-safe-exit cycle.
9. **Do NOT use `f"watchdog_{state}"` or other dynamic event names** — structured-log aggregation breaks on dynamic event keys (deferred-work.md notes this is a recurring pattern). Use static event names like `"watchdog_missed"`, `"control_loop_died"`, `"retry_cancelled"`.
10. **Do NOT introduce a `time.sleep` anywhere** — architecture forbids it (architecture.md:740). Use `asyncio.sleep`. The `services/watchdog.py:_monotonic` reference is for `time.monotonic` (which is non-blocking); that is fine.
11. **The fail-safe exit cycle CAN dispatch commands** (AC5 last clause). The recovered state has already been validated by the engine producing a non-fail-safe recommendation; suppressing dispatch on the recovery cycle would create artificial latency. The PolicyGuard re-checks every command on dispatch anyway (Story 8.2), so even if the state changed between evaluation and dispatch, the safety gate still fires.
12. **Health endpoint refactor: `Request` injection is required** — the existing `async def readiness() -> JSONResponse` signature has no parameters. Adding `request: Request` is a breaking change for the existing tests. Update `tests/unit/web/test_health_routes.py` to use FastAPI's `TestClient` against a minimal app with `app.state.loop_liveness` populated.

### Project Structure Notes

```
src/open_ems/
├── core/
│   ├── state.py                ← UNCHANGED (SystemOperatingMode.fail_safe is consumed)
│   └── state_store.py          ← UNCHANGED
├── engine/
│   ├── control_loop.py         ← MODIFY: add loop_liveness + observability kwargs;
│   │                             refactor _handle_evaluation_result; new helpers
│   │                             _degraded_roles_in, _can_exit_fail_safe,
│   │                             _emit_fail_safe_entry_audit, _emit_fail_safe_exit_audit;
│   │                             call mark_cycle_complete in _tick after success
│   ├── retry_policy.py         ← MODIFY: wrap execute() body in try/except CancelledError
│   │                             with retry_cancelled log and DEVICE audit on cancel
│   ├── policy_guard.py         ← UNCHANGED
│   ├── intent_executor.py      ← UNCHANGED
│   ├── evaluator.py            ← UNCHANGED
│   └── operating_mode.py       ← UNCHANGED (engine still owns mode derivation)
├── services/
│   ├── loop_liveness.py        ← NEW: LoopLiveness class
│   ├── watchdog.py             ← MODIFY: remove precursor delayed-wake check;
│   │                             add loop_liveness + observability kwargs;
│   │                             skip sd_notify on !is_alive; one-shot SYSTEM audit
│   ├── readiness.py            ← UNCHANGED (mark_ready / is_ready / sd_notify reused)
│   └── audit_log.py            ← UNCHANGED (event_type="SYSTEM" already in VALID_EVENT_TYPES)
├── settings.py                 ← MODIFY: append watchdog_missed_cycle_threshold,
│                                 watchdog_cycle_deadline_seconds
└── web/
    ├── app.py                  ← MODIFY: construct LoopLiveness; pass to ControlLoop and
    │                             watchdog_task; rewrite _on_control_loop_done to escalate
    │                             via mark_crashed + best-effort SYSTEM audit
    └── routes/
        └── health.py           ← MODIFY: readiness consults LoopLiveness via
                                  request.app.state.loop_liveness; three-failure-mode
                                  reason payloads

tests/
├── unit/
│   ├── services/
│   │   ├── test_loop_liveness.py   ← NEW: tests #1–#8
│   │   └── test_watchdog.py        ← UPDATE: replace precursor tests; add #9–#13;
│   │                                 update existing 10 tests to inject deps
│   ├── engine/
│   │   ├── test_control_loop.py    ← UPDATE: helper signature + tests #20–#28
│   │   └── test_retry_policy.py    ← UPDATE: add tests #29, #30
│   └── web/
│       └── test_health_routes.py   ← UPDATE: refactor to TestClient; add #15–#19
└── integration/
    ├── web/
    │   └── test_health_under_loop_states.py  ← NEW: tests #31, #32
    └── engine/
        └── test_fail_safe_lifecycle.py       ← NEW: tests #33, #34
```

**No new database migration.** No schema changes. No alembic file in this story.

### Architecture compliance verification

- architecture.md:318-322 (Decision 5.2 — Watchdog implementation): "A dedicated asyncio task tracks the timestamp of the last completed control loop evaluation. If two consecutive evaluation cycles are missed (configurable threshold, default: 2× the cycle interval), the watchdog logs the stall event to both sinks and triggers recovery: For native installs: sends `sd_notify WATCHDOG=1` heartbeats during normal operation; failure to send triggers systemd's built-in watchdog restart. For Docker: the `/health` FastAPI endpoint returns 503 if the watchdog detects a stall." → AC1, AC2, AC3 implement exactly this.
- architecture.md:1080-1106 (BAD-2 — Conservative Fallback Behavior Matrix): "Fail-safe is defined as: stop issuing new `DeviceCommand`s via `PolicyGuard.authorize_and_dispatch()`; no 'reset to zero' commands are sent (devices handle their own safe defaults)" → AC4 implements by short-circuiting `IntentExecutor.translate()` (no commands constructed → none dispatched). "Recovery from fail-safe is automatic: on the next poll cycle where the previously-degraded device returns a healthy DeviceState, the control loop exits fail-safe and logs a RecoveryEvent." → AC5 implements; the additional gating ("full evaluation cycle with no errors") is the epic AC's specific tightening of the architecture's looser "next poll cycle" wording.
- architecture.md:740 (anti-pattern: `time.sleep`): forbidden — use `asyncio.sleep` everywhere. `time.monotonic()` (non-blocking call returning a clock value) is allowed and is what `LoopLiveness` uses.
- epics.md:1855-1862 (Epic 8 cross-story constraints): "Supervisor-managed restart is the primary watchdog recovery path; internal subtask restart is the exception and must emit its own audit event" → AC2 honors by implementing supervisor restart only; in-process subtask restart is NOT included in this story.

### Deferred items folded into this story (with backreferences)

- `_bmad-output/implementation-artifacts/deferred-work.md:5` (from 8-3 review) — `asyncio.sleep` between attempts not shielded against cancellation → AC7 + tests #29, #30 fold this in.
- `_bmad-output/implementation-artifacts/deferred-work.md:11` (from 8-2 review) — Control loop crash not restarted after unhandled exception → AC6 + test #34 fold this in. We do NOT implement in-process restart per the cross-story rule; we escalate to fail-safe + 503 readiness, supervisor handles restart.
- `_bmad-output/implementation-artifacts/deferred-work.md:30` (from 8-1 review) — Control loop task death not escalated to fail-safe → SAME item as deferred-work.md:11; AC6 covers both phrasings.
- `_bmad-output/implementation-artifacts/deferred-work.md:148` (from 1-5 review) — `WATCHDOG_USEC` read once at startup; dynamic interval extension via systemd not supported (`EXTEND_TIMEOUT_USEC`) → **EXPLICITLY DEFERRED**, see "Deferred items (NOT implemented)" below.
- Memory `project_epic7_retro.md` deferred-findings triage rule for Epic 8 — "If a deferred engine issue can produce ambiguous watchdog behavior → prerequisite fix or explicit Epic 8 AC" → all four items above are now explicit ACs in this story.

### Deferred items (explicitly NOT implemented in this story)

- **Dynamic systemd watchdog timeout extension via `EXTEND_TIMEOUT_USEC`** — systemd allows `sd_notify("EXTEND_TIMEOUT_USEC=...")` to dynamically extend the watchdog deadline at runtime (e.g., during a long migration). Our current static interval (read once at startup) is sufficient because: (a) Alembic migrations run before the watchdog task starts (lifespan ordering — see `app.py:147-151`), so the long-running migration phase is not under watchdog supervision; (b) the runtime evaluation cycle has a hard 60 s ceiling per NFR-P1, so no legitimate operation needs runtime extension. Defer until production observability shows a recurring pattern of legitimate operations exceeding the static window.
- **In-process subtask restart of the control loop** — explicitly out of scope per epic cross-story rule; supervisor restart is the path. If empirical operations show that supervisor restart is too disruptive for transient `_tick` exceptions (e.g., a flaky adapter raises every few cycles), introduce a bounded internal restart with mandatory audit event in a follow-up story.
- **Fail-safe entry/exit hysteresis** — currently entry is one-shot (no flapping spam), but exit is immediate the moment all conditions are met. If empirical observation shows exit-then-immediate-re-entry oscillation, add a minimum-time-in-state hysteresis (e.g., "stay in fail-safe for at least 30 s after entry, even if recovery is detected sooner"). Defer.
- **Per-role recovery thresholds** — exit currently treats all entry-degraded roles equally. A future refinement could weight grid_meter recovery more heavily (it's the most critical per architecture.md:1100). Defer until the engine produces this granularity.
- **Runtime metric counters for stall events** — `LoopLiveness` does not expose Prometheus-style counters (e.g., `total_stall_events`, `total_fail_safe_entries`). The audit log + structured logs are sufficient for v1. Add when a dedicated metrics endpoint is added (Epic 12 or later).
- **Configurable watchdog audit ratelimit** — at most one SYSTEM audit per stall stretch (one-shot). If a deployment experiences flapping stall/recovery, it could produce up to one audit per stretch, which could be noisy. Add a min-time-between-audits cap if production shows this pattern.

### References

- [Source: epics.md — Story 8.4 full AC text, lines 1811-1862](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\epics.md)
- [Source: epics.md — Cross-story constraints, lines 1855-1862](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\epics.md)
- [Source: epics.md — FR32, FR33, FR34 lines 66-68; NFR-P1 line 88; NFR-R5 line 100](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\epics.md)
- [Source: architecture.md — Decision 5.2 Watchdog implementation, lines 318-322](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md)
- [Source: architecture.md — BAD-2 Conservative Fallback Behavior Matrix, lines 1080-1106](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\planning-artifacts\architecture.md)
- [Source: src/open_ems/services/watchdog.py — current watchdog_task with precursor delayed-wake check at lines 43-55](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\services\watchdog.py)
- [Source: src/open_ems/services/readiness.py — sd_notify and mark_ready/is_ready helpers](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\services\readiness.py)
- [Source: src/open_ems/web/routes/health.py — current 2-line readiness/liveness endpoints](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\web\routes\health.py)
- [Source: src/open_ems/engine/control_loop.py — _tick at line 76; _handle_evaluation_result at line 157](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\control_loop.py)
- [Source: src/open_ems/engine/retry_policy.py — execute body at lines 63-110; _emit_failure_audit at lines 112-148 (audit-failure swallow pattern to mirror)](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\retry_policy.py)
- [Source: src/open_ems/web/app.py — lifespan watchdog task block at lines 184-197; _on_control_loop_done at lines 245-249; ControlLoop construction at lines 236-243](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\web\app.py)
- [Source: src/open_ems/services/audit_log.py — VALID_EVENT_TYPES at line 16-18 includes "SYSTEM"](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\services\audit_log.py)
- [Source: src/open_ems/settings.py — Settings class to extend at line 9; existing engine tunables at lines 30-34](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\settings.py)
- [Source: src/open_ems/core/state.py — SystemOperatingMode enum at line 62; SystemSnapshot at line 146-159](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\core\state.py)
- [Source: src/open_ems/core/state_store.py — publish() with operating_mode kwarg at line 65](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\core\state_store.py)
- [Source: src/open_ems/engine/operating_mode.py — derive_recommended_operating_mode (Story 7.1; engine owns the recommendation)](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\src\open_ems\engine\operating_mode.py)
- [Source: tests/unit/services/test_watchdog.py — current 10 tests including the precursor delayed-wake tests being replaced](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\tests\unit\services\test_watchdog.py)
- [Source: tests/unit/web/test_health_routes.py — current 3 tests calling readiness() directly without Request](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\tests\unit\web\test_health_routes.py)
- [Source: tests/unit/engine/test_control_loop.py — _control_loop helper at line 104; _evaluation_result builder at line 60](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\tests\unit\engine\test_control_loop.py)
- [Source: tests/unit/engine/test_retry_policy.py — _observability_spy pattern; _failed_result helper](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\tests\unit\engine\test_retry_policy.py)
- [Source: tests/integration/engine/test_retry_pipeline.py — SimulatedBatteryAdapter / SimulatedEVChargerAdapter patterns to reuse for fail-safe lifecycle integration test](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\tests\integration\engine\test_retry_pipeline.py)
- [Source: _bmad-output/implementation-artifacts/8-3-implement-retry-policy-timeout-handling-and-idempotency-enforcement.md — Story 8.3 dev notes (audit pattern, ClassVar idempotency, settings placement convention)](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\implementation-artifacts\8-3-implement-retry-policy-timeout-handling-and-idempotency-enforcement.md)
- [Source: _bmad-output/implementation-artifacts/deferred-work.md:5, 11, 30, 148 — deferred items folded into this story](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\implementation-artifacts\deferred-work.md)
- [Source: _bmad-output/implementation-artifacts/epic-7-retro-2026-05-05.md — Epic 7 retro: deferred-findings triage rule for Epic 8 (Jordan)](C:\Users\jorda\Documents\VS-Code\OPEN-EMS\_bmad-output\implementation-artifacts\epic-7-retro-2026-05-05.md)

---

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Amelia / bmad-dev-story)

### Debug Log References

- AC13 baseline: 822 tests passing → AC13 final: 857 tests passing (+35 new).
- ruff check, ruff format --check, mypy src/: all clean.
- Settings smoke: `Settings(_env_file=None).watchdog_missed_cycle_threshold == 2`, `watchdog_cycle_deadline_seconds == 60.0`.
- One flaky watchdog test (`test_watchdog_emits_audit_once_per_stall_stretch`) was rewritten to use an `asyncio.Event` sentinel that fires when the second audit is observed, eliminating the wall-clock-based race.

### Completion Notes List

- **AC1, AC10, AC11, AC11b — Watchdog refactor**: `services/watchdog.py:watchdog_task` now consults `LoopLiveness.is_alive()` on every tick. Heartbeat is gated on cycle progress; in Docker mode (`send_sd_notify=False`) the task still polls and emits stall audits but skips `sd_notify`. The legacy precursor delayed-wake check (Story 1.5) is removed.
- **AC2 — Stall audit**: One-shot SYSTEM audit per stall stretch; reset on recovery; emission is best-effort (`audit_emit_failed` log on failure).
- **AC3 — `/health/ready`**: Three failure-mode reasons surfaced — `starting`, `control_loop_stalled`, `fail_safe_active`. `/health/live` unchanged. Backwards-compat fallback when `app.state.loop_liveness` is absent.
- **AC4, AC5 — Fail-safe lifecycle**: `_handle_evaluation_result` short-circuits dispatch on fail-safe entry; entry is one-shot; exit gates on entry-degraded recovery AND engine recommendation. Cycle 3 (exit) falls through to dispatch as designed (PolicyGuard's snapshot-based fail_safe gate may still defer adapter writes by one cycle — acknowledged in Dev Notes #11).
- **AC6 — Crash escalation**: `_on_control_loop_done` now calls `LoopLiveness.mark_crashed()` and dispatches a best-effort SYSTEM crash audit via `asyncio.create_task`. If no running loop, only the structured log is recorded.
- **AC7 — RetryPolicy cancellation**: `execute()` body wrapped in `try/except asyncio.CancelledError`; emits one DEVICE audit + structured `retry_cancelled` warning, then re-raises. No `asyncio.shield` (intentional — log is the floor).
- **AC8 — Settings**: `watchdog_missed_cycle_threshold` (default 2, range 1–10) and `watchdog_cycle_deadline_seconds` (default 60.0, range >0–300.0) added.
- **AC9 — LoopLiveness**: Pure synchronous state holder, asyncio-safe under single-task architecture, `crash_reason` truncated to 500 chars. Single instance constructed in lifespan and shared across ControlLoop, watchdog_task, health endpoint, and `_on_control_loop_done`.
- **AC12 — Tests**: 35 new tests added (8 LoopLiveness, 11 watchdog, 9 control-loop fail-safe, 6 health route, 2 RetryPolicy cancellation, 2 health integration, 2 fail-safe lifecycle integration; 2 sanity asserts). All passing.
- **AC13 — Quality gates**: pytest 857/857 ✓, ruff check ✓, ruff format ✓, mypy ✓, settings smoke ✓.

### File List

**Source (created)**
- `src/open_ems/services/loop_liveness.py`

**Source (modified)**
- `src/open_ems/settings.py`
- `src/open_ems/services/watchdog.py`
- `src/open_ems/engine/control_loop.py`
- `src/open_ems/engine/retry_policy.py`
- `src/open_ems/web/routes/health.py`
- `src/open_ems/web/app.py`

**Tests (created)**
- `tests/unit/services/test_loop_liveness.py`
- `tests/integration/web/__init__.py`
- `tests/integration/web/test_health_under_loop_states.py`
- `tests/integration/engine/test_fail_safe_lifecycle.py`

**Tests (modified)**
- `tests/unit/services/test_watchdog.py`
- `tests/unit/engine/test_control_loop.py`
- `tests/unit/engine/test_retry_policy.py`
- `tests/unit/web/test_health_routes.py`

**Sprint/state (modified)**
- `_bmad-output/implementation-artifacts/sprint-status.yaml`

### Change Log

| Date       | Author        | Change |
|------------|---------------|--------|
| 2026-05-10 | Bob (SM)      | Story 8.4 context engineering complete: comprehensive ACs, dev notes, and pitfall guidance for watchdog heartbeat + stalled-loop detection + fail-safe lifecycle + cancellation-clean retry. Folds 4 deferred items from prior reviews. |
| 2026-05-10 | Amelia (Dev)  | Implemented Story 8.4: LoopLiveness service, watchdog gated on cycle progress (Docker parity via `send_sd_notify` flag), fail-safe lifecycle in ControlLoop, /health/ready three failure modes, RetryPolicy cancellation handling, control-loop crash escalation. 822 → 857 tests (+35), ruff/format/mypy clean. Status → review. |
