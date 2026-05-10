"""RetryPolicy: bounded retry with safety re-check before every attempt (Story 8.3).

Sits BETWEEN the control loop and PolicyGuard. The control loop calls
``RetryPolicy.execute(command)``; RetryPolicy calls
``PolicyGuard.authorize_and_dispatch(command)`` 1..N times and returns the
final ``CommandResult``. Each retry of an idempotent command re-enters the
full PolicyGuard safety pipeline (operating mode, capability, constraints) —
no separate re-check logic lives here.

Behaviour summary (see story 8.3 ACs for full details):

- ``command.is_idempotent is True`` → on ``failed`` or ``timeout``, retry up to
  ``Settings.command_max_retries`` ADDITIONAL attempts (so total ≤ N+1).
- ``command.is_idempotent is False`` → never retry; fail closed on first
  non-success result (``failed``, ``timeout``, ``rejected``).
- A ``rejected`` result returned mid-retry-cycle stops further retries
  immediately (the safety pipeline says "no, not safe right now").
- After all attempts are exhausted with ``applied is False``, RetryPolicy
  emits exactly ONE ``ObservabilityService.audit(event_type="DEVICE")`` event
  and returns the last result. PolicyGuard's separate ``CONSTRAINT`` audit
  events for rejections are unchanged.
- Successful commands (any attempt count) emit exactly ONE
  ``ObservabilityService.audit(event_type="DECISION")`` event from this layer
  per Story 8.5 AC1 — closing the audit_log gap for control decisions called
  out in architecture.md:312-316. DEVICE and DECISION are mutually exclusive
  per command: a successful command emits ONE DECISION audit, a terminal-
  failure command emits ONE DEVICE audit, never both, never neither. A
  cancellation emits a DEVICE audit per Story 8.4 AC7 (the indeterminate
  outcome) — never a DECISION audit.

Per-attempt timeout is owned by PolicyGuard
(``COMMAND_DISPATCH_TIMEOUT_SECONDS``). RetryPolicy does NOT add another
``asyncio.wait_for`` around ``authorize_and_dispatch()``.

If a future story (Epic 9 installer setup, Epic 10 homeowner overrides) adds
another caller of ``PolicyGuard.authorize_and_dispatch()`` from outside the
engine, that caller MUST also route through ``RetryPolicy`` for consistent
retry / audit behaviour.
"""

from __future__ import annotations

import asyncio

import structlog

from open_ems.core.commands import CommandResult, CommandStatus, DeviceCommand
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings

logger = structlog.get_logger(__name__)


class RetryPolicy:
    """Bounded retry layer wrapping PolicyGuard.authorize_and_dispatch."""

    def __init__(
        self,
        *,
        policy_guard: PolicyGuard,
        observability: ObservabilityService,
        settings: Settings,
    ) -> None:
        self._policy_guard = policy_guard
        self._observability = observability
        self._settings = settings

    async def execute(self, command: DeviceCommand) -> CommandResult:
        """Dispatch ``command`` with retry semantics; return the final result."""
        max_attempts = self._settings.command_max_retries + 1
        backoff = self._settings.command_retry_backoff_seconds
        last_result: CommandResult | None = None
        attempt = 0  # tracked for the cancellation path
        try:
            for attempt in range(1, max_attempts + 1):
                if attempt > 1:
                    assert last_result is not None  # loop invariant: prior attempt ran
                    logger.debug(
                        "command_retry",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        prior_status=last_result.status.value,
                        prior_reason=last_result.reason,
                        device_id=command.device_id,
                        correlation_id=str(command.correlation_id),
                        component="engine",
                    )
                    if backoff > 0:
                        await asyncio.sleep(backoff)
                last_result = await self._policy_guard.authorize_and_dispatch(command)
                if last_result.applied:
                    logger.debug(
                        "command_applied",
                        device_id=command.device_id,
                        command_type=type(command).__name__,
                        attempts=attempt,
                        correlation_id=str(command.correlation_id),
                        component="engine",
                    )
                    # Structured-log floor — emitted BEFORE the audit so the
                    # dispatch is recorded even if the audit DB write fails.
                    logger.info(
                        "command_dispatched",
                        device_id=command.device_id,
                        command_type=type(command).__name__,
                        correlation_id=str(command.correlation_id),
                        attempts=attempt,
                        component="engine",
                    )
                    await self._emit_success_audit(command, last_result, attempts=attempt)
                    return last_result
                if not command.is_idempotent:
                    logger.debug(
                        "non_idempotent_command_not_retried",
                        device_id=command.device_id,
                        command_type=type(command).__name__,
                        status=last_result.status.value,
                        reason=last_result.reason,
                        correlation_id=str(command.correlation_id),
                        component="engine",
                    )
                    break
                if last_result.status is CommandStatus.rejected:
                    # Re-check rejected the command — stop, don't keep hammering an unsafe op.
                    break
            assert last_result is not None  # range(1, N+1) with N>=1 is non-empty
            await self._emit_failure_audit(command, last_result, attempts=attempt)
            return last_result
        except asyncio.CancelledError:
            await self._emit_cancellation_audit(command, attempts=attempt)
            raise

    async def _emit_success_audit(
        self,
        command: DeviceCommand,
        result: CommandResult,
        *,
        attempts: int,
    ) -> None:
        """Emit a DECISION audit event on the success path (Story 8.5 AC1).

        Mirrors the failure-audit shielding pattern: any exception from the
        audit DB write is logged via ``audit_emit_failed`` and swallowed so
        the dispatch return is never blocked by audit-layer faults. The
        ``command_dispatched`` info log emitted upstream is the floor.
        """
        summary = (
            f"Command {type(command).__name__} for {command.device_id} "
            f"dispatched: success (applied={result.applied}) after {attempts} attempt(s)"
        )
        try:
            await self._observability.audit(
                actor="system",
                event_type="DECISION",
                summary=summary,
                device_id=command.device_id,
                detail={
                    "correlation_id": str(command.correlation_id),
                    "command_type": type(command).__name__,
                    "command_status": "success",
                    "applied": result.applied,
                    "attempts": attempts,
                },
            )
        except Exception as exc:  # noqa: BLE001 — audit failure must not mask successful dispatch
            logger.error(
                "audit_emit_failed",
                device_id=command.device_id,
                command_type=type(command).__name__,
                attempts=attempts,
                correlation_id=str(command.correlation_id),
                error=repr(exc),
                exc_info=True,
                component="engine",
            )

    async def _emit_cancellation_audit(
        self,
        command: DeviceCommand,
        *,
        attempts: int,
    ) -> None:
        """Emit a DEVICE audit event when execute() is cancelled mid-retry (AC7).

        The structured warning log is the floor — even if the audit DB write
        fails or is itself cancelled, the cancellation is recorded.
        """
        logger.warning(
            "retry_cancelled",
            device_id=command.device_id,
            command_type=type(command).__name__,
            attempts=attempts,
            correlation_id=str(command.correlation_id),
            component="engine",
        )
        if attempts <= 0:
            audit_summary = (
                f"Command {type(command).__name__} for {command.device_id} "
                "cancelled before first dispatch: not attempted"
            )
        else:
            audit_summary = (
                f"Command {type(command).__name__} for {command.device_id} "
                f"cancelled mid-retry after {attempts} attempt(s): "
                "pending result indeterminate"
            )
        try:
            await self._observability.audit(
                actor="system",
                event_type="DEVICE",
                summary=audit_summary,
                device_id=command.device_id,
                detail={
                    "correlation_id": str(command.correlation_id),
                    "command_type": type(command).__name__,
                    "command_status": "cancelled",
                    "attempts": attempts,
                },
            )
        except Exception as exc:  # noqa: BLE001 — audit failure must not mask cancellation
            logger.error(
                "audit_emit_failed",
                device_id=command.device_id,
                command_type=type(command).__name__,
                error=repr(exc),
                exc_info=True,
                component="engine",
            )

    async def _emit_failure_audit(
        self,
        command: DeviceCommand,
        result: CommandResult,
        *,
        attempts: int,
    ) -> None:
        # Timeout is indeterminate — the command may or may not have been applied.
        # Use distinct phrasing so operators don't assume "not applied" and double-issue.
        if result.status is CommandStatus.timeout:
            summary = (
                f"Command {type(command).__name__} for {command.device_id} "
                f"outcome indeterminate after {attempts} attempt(s) (timeout): {result.reason}"
            )
        else:
            summary = (
                f"Command {type(command).__name__} for {command.device_id} "
                f"not applied after {attempts} attempt(s): {result.reason}"
            )
        try:
            await self._observability.audit(
                actor="system",
                event_type="DEVICE",
                summary=summary,
                device_id=command.device_id,
                detail={
                    "correlation_id": str(command.correlation_id),
                    "command_type": type(command).__name__,
                    "command_status": result.status.value,
                    "applied": result.applied,
                    "attempts": attempts,
                    "final_reason": result.reason,
                },
            )
        except Exception as exc:  # noqa: BLE001 — audit failure must not mask command failure
            logger.error(
                "audit_emit_failed",
                device_id=command.device_id,
                command_type=type(command).__name__,
                attempts=attempts,
                correlation_id=str(command.correlation_id),
                error=repr(exc),
                exc_info=True,
                component="engine",
            )
