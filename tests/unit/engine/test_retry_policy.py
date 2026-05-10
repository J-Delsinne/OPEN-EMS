"""Unit tests for RetryPolicy (Story 8.3 AC2-AC6, AC10 #1-#11; Story 8.5 AC1, AC10, AC12)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_ems.core.commands import (
    CommandOrigin,
    CommandResult,
    CommandStatus,
    DeviceCommand,
    SetBatteryChargeRateCommand,
    StopEVChargingCommand,
)
from open_ems.core.devices import DeviceRole
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.engine.retry_policy import RetryPolicy
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings


def _settings(*, max_retries: int = 2, backoff: float = 0.0) -> Settings:
    """Construct Settings with retry tunables; backoff=0 keeps tests fast (no real sleep)."""
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


def _idempotent_command() -> SetBatteryChargeRateCommand:
    return SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )


def _non_idempotent_command() -> StopEVChargingCommand:
    return StopEVChargingCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
    )


def _success_result(cmd: DeviceCommand) -> CommandResult:
    return CommandResult(
        correlation_id=cmd.correlation_id,
        device_id=cmd.device_id,
        status=CommandStatus.success,
        applied=True,
        reason="ok",
    )


def _failed_result(cmd: DeviceCommand, *, reason: str = "communication_error") -> CommandResult:
    return CommandResult(
        correlation_id=cmd.correlation_id,
        device_id=cmd.device_id,
        status=CommandStatus.failed,
        applied=False,
        reason=reason,
    )


def _timeout_result(cmd: DeviceCommand) -> CommandResult:
    return CommandResult(
        correlation_id=cmd.correlation_id,
        device_id=cmd.device_id,
        status=CommandStatus.timeout,
        applied=False,
        reason="command_timeout",
    )


def _rejected_result(cmd: DeviceCommand, *, reason: str = "fail_safe_mode_active") -> CommandResult:
    return CommandResult(
        correlation_id=cmd.correlation_id,
        device_id=cmd.device_id,
        status=CommandStatus.rejected,
        applied=False,
        reason=reason,
    )


def _correlation_broken_result(cmd: DeviceCommand) -> CommandResult:
    return CommandResult(
        correlation_id=cmd.correlation_id,
        device_id=cmd.device_id,
        status=CommandStatus.correlation_broken,
        applied=False,
        reason="adapter_correlation_id_mismatch",
    )


def _policy_guard_returning(*results: CommandResult) -> MagicMock:
    pg = MagicMock(spec=PolicyGuard)
    pg.authorize_and_dispatch = AsyncMock(side_effect=list(results))
    return pg


# ─── AC10 #1 ───────────────────────────────────────────────────────────────────


async def test_idempotent_command_retried_up_to_max_retries_after_failure() -> None:
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_failed_result(cmd), _failed_result(cmd), _failed_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    result = await rp.execute(cmd)

    assert pg.authorize_and_dispatch.await_count == 3  # 1 + 2 retries
    assert result.status is CommandStatus.failed
    assert result.applied is False
    audit_spy.assert_awaited_once()
    args = audit_spy.await_args
    assert args is not None
    assert args.kwargs["event_type"] == "DEVICE"
    assert args.kwargs["actor"] == "system"
    assert args.kwargs["device_id"] == cmd.device_id
    assert "after 3 attempt(s)" in args.kwargs["summary"]
    assert "communication_error" in args.kwargs["summary"]
    detail = args.kwargs["detail"]
    assert detail["correlation_id"] == str(cmd.correlation_id)
    assert detail["command_type"] == "SetBatteryChargeRateCommand"
    assert detail["command_status"] == "failed"
    assert detail["applied"] is False
    assert detail["attempts"] == 3
    assert detail["final_reason"] == "communication_error"


# ─── Story 8.5 AC11 (renamed from AC10 #2): success-path now emits DECISION audit ───


async def test_idempotent_command_succeeds_on_second_attempt_emits_decision_audit() -> None:
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_failed_result(cmd), _success_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    result = await rp.execute(cmd)

    assert pg.authorize_and_dispatch.await_count == 2
    assert result.status is CommandStatus.success
    assert result.applied is True
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DECISION"
    assert kwargs["actor"] == "system"
    assert kwargs["device_id"] == cmd.device_id
    assert kwargs["detail"]["attempts"] == 2
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)


# ─── Story 8.5 AC11 (renamed from AC10 #3): success-path now emits DECISION audit ───


async def test_idempotent_command_succeeds_on_first_attempt_emits_decision_audit() -> None:
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_success_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    result = await rp.execute(cmd)

    assert pg.authorize_and_dispatch.await_count == 1
    assert result.applied is True
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DECISION"
    assert kwargs["detail"]["attempts"] == 1
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)


# ─── AC10 #4 ───────────────────────────────────────────────────────────────────


async def test_non_idempotent_command_not_retried_on_failure() -> None:
    cmd = _non_idempotent_command()
    pg = _policy_guard_returning(_failed_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    result = await rp.execute(cmd)

    assert pg.authorize_and_dispatch.await_count == 1
    assert result.status is CommandStatus.failed
    assert result.applied is False
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DEVICE"
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)


# ─── AC10 #5 ───────────────────────────────────────────────────────────────────


async def test_non_idempotent_command_not_retried_on_timeout() -> None:
    cmd = _non_idempotent_command()
    pg = _policy_guard_returning(_timeout_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    result = await rp.execute(cmd)

    assert pg.authorize_and_dispatch.await_count == 1
    assert result.status is CommandStatus.timeout
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DEVICE"
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)
    assert kwargs["detail"]["command_status"] == "timeout"


# ─── AC10 #6 ───────────────────────────────────────────────────────────────────


async def test_non_idempotent_command_not_retried_on_rejection() -> None:
    cmd = _non_idempotent_command()
    pg = _policy_guard_returning(_rejected_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    result = await rp.execute(cmd)

    assert pg.authorize_and_dispatch.await_count == 1
    assert result.status is CommandStatus.rejected
    audit_spy.assert_awaited_once()
    # RetryPolicy emits DEVICE; PolicyGuard's CONSTRAINT is independent (from _reject()).
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DEVICE"
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)


# ─── AC10 #7 ───────────────────────────────────────────────────────────────────


async def test_idempotent_command_not_retried_on_correlation_broken() -> None:
    """Story 9.0c D4: ``correlation_broken`` is post-dispatch indeterminate-outcome.

    RetryPolicy must NOT retry: re-issuing an idempotent command that may have
    already succeeded is wasted effort, and re-issuing a non-idempotent command
    after a partial-dispatch is unsafe.
    """
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_correlation_broken_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    result = await rp.execute(cmd)

    assert pg.authorize_and_dispatch.await_count == 1, (
        "correlation_broken must not trigger a retry, even for idempotent commands"
    )
    assert result.status is CommandStatus.correlation_broken
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DEVICE"
    assert kwargs["detail"]["command_status"] == "correlation_broken"


async def test_non_idempotent_command_not_retried_on_correlation_broken() -> None:
    """Story 9.0c D4: ``correlation_broken`` is fatal for non-idempotent commands.

    A double-issue would risk affecting an unrelated session that started between
    the original dispatch and the retry.
    """
    cmd = _non_idempotent_command()
    pg = _policy_guard_returning(_correlation_broken_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    result = await rp.execute(cmd)

    assert pg.authorize_and_dispatch.await_count == 1
    assert result.status is CommandStatus.correlation_broken
    audit_spy.assert_awaited_once()


async def test_retry_stops_immediately_on_rejection_during_retry_cycle() -> None:
    """Idempotent command: failed then rejected on retry → no further retries; final = rejected."""
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_failed_result(cmd), _rejected_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    result = await rp.execute(cmd)

    assert pg.authorize_and_dispatch.await_count == 2  # 1 fail + 1 rejected, then stop
    assert result.status is CommandStatus.rejected
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DEVICE"
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)


# ─── AC10 #8 ───────────────────────────────────────────────────────────────────


async def test_retry_count_zero_disables_retries() -> None:
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_failed_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=0))

    result = await rp.execute(cmd)

    assert pg.authorize_and_dispatch.await_count == 1
    assert result.status is CommandStatus.failed
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DEVICE"
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)


# ─── AC10 #9 ───────────────────────────────────────────────────────────────────


async def test_backoff_sleep_invoked_between_retries() -> None:
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_failed_result(cmd), _failed_result(cmd), _success_result(cmd))
    obs, _ = _observability_spy()
    rp = RetryPolicy(
        policy_guard=pg, observability=obs, settings=_settings(max_retries=2, backoff=0.01)
    )

    sleep_mock = AsyncMock()
    with patch("open_ems.engine.retry_policy.asyncio.sleep", sleep_mock):
        result = await rp.execute(cmd)

    assert result.applied is True
    # Sleep before attempts 2 and 3 only — not before attempt 1, not after attempt 3
    assert sleep_mock.await_count == 2
    for call in sleep_mock.await_args_list:
        assert call.args[0] == 0.01


async def test_backoff_zero_skips_sleep() -> None:
    """backoff=0.0 disables sleep entirely (AC8: 0.0 explicitly allowed)."""
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_failed_result(cmd), _success_result(cmd))
    obs, _ = _observability_spy()
    rp = RetryPolicy(
        policy_guard=pg, observability=obs, settings=_settings(max_retries=2, backoff=0.0)
    )

    sleep_mock = AsyncMock()
    with patch("open_ems.engine.retry_policy.asyncio.sleep", sleep_mock):
        await rp.execute(cmd)

    sleep_mock.assert_not_awaited()


# ─── AC10 #10 ──────────────────────────────────────────────────────────────────


async def test_audit_event_summary_includes_attempt_count_and_final_reason() -> None:
    cmd = _idempotent_command()
    pg = _policy_guard_returning(
        _failed_result(cmd, reason="communication_error"),
        _failed_result(cmd, reason="communication_error"),
        _failed_result(cmd, reason="communication_error"),
    )
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    await rp.execute(cmd)

    audit_spy.assert_awaited_once()
    summary = audit_spy.await_args.kwargs["summary"]
    assert "after 3 attempt(s)" in summary
    assert "communication_error" in summary
    assert "SetBatteryChargeRateCommand" in summary
    assert cmd.device_id in summary


# ─── AC10 #11 ──────────────────────────────────────────────────────────────────


async def test_correlation_id_preserved_across_retries() -> None:
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_failed_result(cmd), _success_result(cmd))
    obs, _ = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    await rp.execute(cmd)

    assert pg.authorize_and_dispatch.await_count == 2
    for call in pg.authorize_and_dispatch.await_args_list:
        passed_command = call.args[0]
        assert passed_command.correlation_id == cmd.correlation_id


# ─── Defensive: audit failure does not mask command failure ────────────────────


async def test_audit_failure_does_not_propagate() -> None:
    """Mirrors PolicyGuard._reject() pattern: audit-emit exception is swallowed."""
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_failed_result(cmd))
    obs, audit_spy = _observability_spy()
    audit_spy.side_effect = RuntimeError("event_log_unavailable")
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=0))

    # Must not raise — command's failed result is what callers must observe
    result = await rp.execute(cmd)

    assert result.status is CommandStatus.failed
    audit_spy.assert_awaited_once()


# ─── Audit phrasing: timeout is indeterminate, not "not applied" ────────────────


async def test_audit_summary_marks_timeout_as_indeterminate() -> None:
    """Timeout means the command may or may not have been applied — phrasing must reflect that."""
    cmd = _non_idempotent_command()
    pg = _policy_guard_returning(_timeout_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    await rp.execute(cmd)

    audit_spy.assert_awaited_once()
    summary = audit_spy.await_args.kwargs["summary"]
    assert "outcome indeterminate" in summary
    assert "(timeout)" in summary
    assert "command_timeout" in summary
    assert "not applied" not in summary


async def test_audit_summary_marks_failed_as_not_applied() -> None:
    """Non-timeout failures keep the existing 'not applied' phrasing."""
    cmd = _non_idempotent_command()
    pg = _policy_guard_returning(_failed_result(cmd, reason="communication_error"))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    await rp.execute(cmd)

    summary = audit_spy.await_args.kwargs["summary"]
    assert "not applied" in summary
    assert "indeterminate" not in summary


# ─── Story 8.4 AC7: cancellation handling (AC12 #29-#30) ───────────────────────


async def test_retry_policy_emits_device_audit_on_cancellation() -> None:
    """AC12 #29: cancel during retry backoff → CancelledError raised AND DEVICE audit emitted.

    Uses an asyncio.Event signalled by the dispatch mock to guarantee the
    cancellation lands while the backoff sleep is in progress (not earlier
    during dispatch), so the test name accurately describes coverage.
    """
    cmd = _idempotent_command()
    obs, audit_spy = _observability_spy()

    in_backoff = asyncio.Event()

    async def dispatch_then_signal(_cmd: DeviceCommand) -> CommandResult:
        # Fire AFTER returning the failed result so the next loop iteration
        # enters `await asyncio.sleep(backoff)` deterministically.
        in_backoff.set()
        return _failed_result(cmd)

    pg = MagicMock(spec=PolicyGuard)
    pg.authorize_and_dispatch = AsyncMock(side_effect=dispatch_then_signal)

    warnings: list[str] = []

    class FakeLogger:
        def debug(self, _event: str, **_kwargs: object) -> None:
            pass

        def warning(self, event: str, **_kwargs: object) -> None:
            warnings.append(event)

        def error(self, _event: str, **_kwargs: object) -> None:
            pass

    rp = RetryPolicy(
        policy_guard=pg,
        observability=obs,
        # backoff long enough that the test cancels during the sleep
        settings=_settings(max_retries=2, backoff=1.0),
    )

    with patch("open_ems.engine.retry_policy.logger", FakeLogger()):
        task = asyncio.create_task(rp.execute(cmd))
        # Wait until the first dispatch has returned (we are now inside the backoff sleep)
        await asyncio.wait_for(in_backoff.wait(), timeout=2.0)
        # Yield once more so the await asyncio.sleep(backoff) is actually entered
        await asyncio.sleep(0)

        task.cancel()
        import pytest

        with pytest.raises(asyncio.CancelledError):
            await task

    # Exactly one cancellation audit emitted, mid-retry
    assert audit_spy.await_count == 1
    call = audit_spy.await_args
    assert call.kwargs["event_type"] == "DEVICE"
    assert call.kwargs["actor"] == "system"
    assert call.kwargs["device_id"] == cmd.device_id
    assert "cancelled mid-retry" in call.kwargs["summary"]
    # Story 8.5 AC10: cancellation audit detail carries correlation_id
    assert call.kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)
    assert call.kwargs["detail"]["command_status"] == "cancelled"
    # AC7 floor: structured `retry_cancelled` warning is the post-mortem record
    assert "retry_cancelled" in warnings


async def test_retry_policy_audit_failure_during_cancellation_swallowed() -> None:
    """AC12 #30: audit raises during cancel path → CancelledError still propagates.

    Also verifies the AC7 floor: even when the audit DB write fails, the
    structured ``retry_cancelled`` warning log is still emitted.
    """
    cmd = _idempotent_command()
    in_backoff = asyncio.Event()

    async def dispatch_then_signal(_cmd: DeviceCommand) -> CommandResult:
        in_backoff.set()
        return _failed_result(cmd)

    pg = MagicMock(spec=PolicyGuard)
    pg.authorize_and_dispatch = AsyncMock(side_effect=dispatch_then_signal)

    audit_spy = AsyncMock(side_effect=RuntimeError("db unavailable"))
    obs = ObservabilityService(repo=MagicMock())
    obs.audit = audit_spy  # type: ignore[method-assign]

    warnings: list[str] = []
    errors: list[str] = []

    class FakeLogger:
        def debug(self, _event: str, **_kwargs: object) -> None:
            pass

        def warning(self, event: str, **_kwargs: object) -> None:
            warnings.append(event)

        def error(self, event: str, **_kwargs: object) -> None:
            errors.append(event)

    rp = RetryPolicy(
        policy_guard=pg,
        observability=obs,
        settings=_settings(max_retries=2, backoff=1.0),
    )

    with patch("open_ems.engine.retry_policy.logger", FakeLogger()):
        task = asyncio.create_task(rp.execute(cmd))
        await asyncio.wait_for(in_backoff.wait(), timeout=2.0)
        await asyncio.sleep(0)

        task.cancel()
        import pytest

        with pytest.raises(asyncio.CancelledError):
            await task

    # Floor: warning log MUST be emitted even though the audit DB write blew up
    assert "retry_cancelled" in warnings
    assert "audit_emit_failed" in errors


# ─── Story 8.5 AC12 #1 — DECISION audit on first-attempt success ───────────────


async def test_decision_audit_emitted_on_first_attempt_success() -> None:
    """AC12 #1: single success result → ONE DECISION audit with full detail payload."""
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_success_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    result = await rp.execute(cmd)

    assert result.status is CommandStatus.success
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DECISION"
    assert kwargs["actor"] == "system"
    assert kwargs["device_id"] == cmd.device_id
    assert "SetBatteryChargeRateCommand" in kwargs["summary"]
    assert "success" in kwargs["summary"]
    detail = kwargs["detail"]
    assert detail["correlation_id"] == str(cmd.correlation_id)
    assert detail["command_type"] == "SetBatteryChargeRateCommand"
    assert detail["command_status"] == "success"
    assert detail["applied"] is True
    assert detail["attempts"] == 1


# ─── Story 8.5 AC12 #2 — DECISION audit after retry recovery ───────────────────


async def test_decision_audit_emitted_on_retry_recovery_success() -> None:
    """AC12 #2: failed-then-success → ONE DECISION audit with attempts=2; ZERO DEVICE audits."""
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_failed_result(cmd), _success_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=2))

    result = await rp.execute(cmd)

    assert result.status is CommandStatus.success
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DECISION"
    assert kwargs["detail"]["attempts"] == 2
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)


# ─── Story 8.5 AC12 #3 — DECISION audit-emit failure is swallowed ──────────────


async def test_decision_audit_emit_failure_swallowed() -> None:
    """AC12 #3: observability.audit raises on success → return value still success;
    structured `audit_emit_failed` log is the floor.
    """
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_success_result(cmd))
    obs, audit_spy = _observability_spy()
    audit_spy.side_effect = RuntimeError("event_log_unavailable")
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=0))

    errors: list[str] = []
    info_events: list[str] = []

    class FakeLogger:
        def debug(self, _event: str, **_kwargs: object) -> None:
            pass

        def info(self, event: str, **_kwargs: object) -> None:
            info_events.append(event)

        def warning(self, _event: str, **_kwargs: object) -> None:
            pass

        def error(self, event: str, **_kwargs: object) -> None:
            errors.append(event)

    with patch("open_ems.engine.retry_policy.logger", FakeLogger()):
        result = await rp.execute(cmd)

    assert result.status is CommandStatus.success
    assert result.applied is True
    audit_spy.assert_awaited_once()
    # Floor: structured info log emitted BEFORE the audit attempt
    assert "command_dispatched" in info_events
    # Audit failure was logged via structlog and swallowed
    assert "audit_emit_failed" in errors


# ─── Story 8.5 AC12 #4 — terminal failure audit detail.correlation_id ──────────


async def test_failure_audit_includes_correlation_id_in_detail() -> None:
    """AC12 #4: terminal failure path carries correlation_id in audit detail."""
    cmd = _idempotent_command()
    pg = _policy_guard_returning(_failed_result(cmd), _failed_result(cmd))
    obs, audit_spy = _observability_spy()
    rp = RetryPolicy(policy_guard=pg, observability=obs, settings=_settings(max_retries=1))

    await rp.execute(cmd)

    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DEVICE"
    detail = kwargs["detail"]
    assert detail["correlation_id"] == str(cmd.correlation_id)
    assert detail["command_type"] == "SetBatteryChargeRateCommand"
    assert detail["command_status"] == "failed"
    assert detail["applied"] is False
    assert detail["attempts"] == 2
    assert detail["final_reason"] == "communication_error"


# ─── Story 8.5 AC12 #5 — cancellation audit detail.correlation_id ──────────────


async def test_cancellation_audit_includes_correlation_id_in_detail() -> None:
    """AC12 #5: cancellation path carries correlation_id in audit detail.

    Drives the same cancellation path as Story 8.4 AC7's existing test, but
    asserts only the new AC10 detail-field contract.
    """
    cmd = _idempotent_command()
    obs, audit_spy = _observability_spy()
    in_backoff = asyncio.Event()

    async def dispatch_then_signal(_cmd: DeviceCommand) -> CommandResult:
        in_backoff.set()
        return _failed_result(cmd)

    pg = MagicMock(spec=PolicyGuard)
    pg.authorize_and_dispatch = AsyncMock(side_effect=dispatch_then_signal)
    rp = RetryPolicy(
        policy_guard=pg, observability=obs, settings=_settings(max_retries=2, backoff=1.0)
    )

    task = asyncio.create_task(rp.execute(cmd))
    await asyncio.wait_for(in_backoff.wait(), timeout=2.0)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DEVICE"
    detail = kwargs["detail"]
    assert detail["correlation_id"] == str(cmd.correlation_id)
    assert detail["command_type"] == "SetBatteryChargeRateCommand"
    assert detail["command_status"] == "cancelled"
    assert "attempts" in detail
