from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from open_ems.services.audit_log import ObservabilityService
from open_ems.services.loop_liveness import LoopLiveness
from open_ems.services.watchdog import get_watchdog_interval, watchdog_task

# ── get_watchdog_interval ─────────────────────────────────────────────────


def test_interval_none_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert get_watchdog_interval() is None


def test_interval_half_of_watchdog_sec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")  # 30 s in µs
    assert get_watchdog_interval() == pytest.approx(15.0)


def test_interval_none_on_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "not-a-number")
    assert get_watchdog_interval() is None


def test_interval_none_on_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "")
    assert get_watchdog_interval() is None


def test_interval_small_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "2000000")  # 2 s in µs → 1.0 s interval
    assert get_watchdog_interval() == pytest.approx(1.0)


def test_interval_none_when_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "0")
    assert get_watchdog_interval() is None


def test_interval_none_when_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "-30000000")
    assert get_watchdog_interval() is None


def test_interval_none_and_warns_on_float_string(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []

    class FakeLogger:
        def warning(self, event: str, **_kwargs: object) -> None:
            warnings.append(event)

    monkeypatch.setenv("WATCHDOG_USEC", "30000000.5")
    monkeypatch.setattr("open_ems.services.watchdog.logger", FakeLogger())
    assert get_watchdog_interval() is None
    assert "watchdog_usec_invalid" in warnings


def test_interval_none_when_watchdog_pid_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))
    assert get_watchdog_interval() is None


def test_interval_returned_when_watchdog_pid_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    assert get_watchdog_interval() == pytest.approx(15.0)


# ── watchdog_task helpers ─────────────────────────────────────────────────


def _alive_liveness() -> LoopLiveness:
    """Build a LoopLiveness with `mark_cycle_complete` already called → is_alive=True."""
    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)
    liveness.mark_cycle_complete()
    return liveness


def _stale_liveness() -> LoopLiveness:
    """Build a LoopLiveness that has never reported a completion → is_alive=False."""
    return LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)


async def _run_briefly(coro_factory: object, ticks: int = 1, interval: float = 0.001) -> None:
    """Helper: schedule the task, let it tick once or twice, then cancel."""
    task = asyncio.create_task(coro_factory)  # type: ignore[arg-type]
    await asyncio.sleep(interval * (ticks + 1))
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── watchdog_task: heartbeat sending (AC1, AC10) ──────────────────────────


async def test_watchdog_task_sends_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task sends sd_notify("WATCHDOG=1") after each sleep when alive."""
    sent: list[str] = []
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda msg: sent.append(msg))

    observability = MagicMock(spec=ObservabilityService)
    task = asyncio.create_task(
        watchdog_task(
            0.01,
            loop_liveness=_alive_liveness(),
            observability=observability,
            send_sd_notify=True,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert sent.count("WATCHDOG=1") >= 1


async def test_watchdog_task_cancels_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """CancelledError propagates out without being swallowed."""
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda _msg: None)
    observability = MagicMock(spec=ObservabilityService)
    task = asyncio.create_task(
        watchdog_task(
            0.001,
            loop_liveness=_alive_liveness(),
            observability=observability,
            send_sd_notify=True,
        )
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── watchdog_task: AC1 / AC11 / AC12 #9-#13 — is_alive()-driven semantics ─


async def test_watchdog_skips_sd_notify_when_loop_not_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC12 #9: stale liveness → no sd_notify call."""
    sent: list[str] = []
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda msg: sent.append(msg))

    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)

    task = asyncio.create_task(
        watchdog_task(
            0.005,
            loop_liveness=_stale_liveness(),
            observability=observability,
            send_sd_notify=True,
        )
    )
    await asyncio.sleep(0.03)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert sent == []


async def test_watchdog_sends_sd_notify_when_loop_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC12 #10: fresh mark_cycle_complete → sd_notify is called."""
    sent: list[str] = []
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda msg: sent.append(msg))

    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)

    task = asyncio.create_task(
        watchdog_task(
            0.005,
            loop_liveness=_alive_liveness(),
            observability=observability,
            send_sd_notify=True,
        )
    )
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert sent.count("WATCHDOG=1") >= 1


async def test_watchdog_emits_audit_once_per_stall_stretch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC12 #11: alive → stale → stale → alive → stale ⇒ audit called exactly twice."""
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda _msg: None)

    observability = MagicMock(spec=ObservabilityService)
    audit_call_count = 0
    second_audit_observed = asyncio.Event()

    sequence = [True, False, False, True, False]
    tick_index = 0

    def fake_is_alive(self: LoopLiveness, now_monotonic: float | None = None) -> bool:
        nonlocal tick_index
        result = sequence[tick_index] if tick_index < len(sequence) else False
        tick_index += 1
        return result

    async def fake_audit(**_kwargs: object) -> None:
        nonlocal audit_call_count
        audit_call_count += 1
        if audit_call_count >= 2:
            second_audit_observed.set()

    observability.audit = fake_audit  # type: ignore[method-assign]

    monkeypatch.setattr(LoopLiveness, "is_alive", fake_is_alive)

    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)

    task = asyncio.create_task(
        watchdog_task(
            0.001,
            loop_liveness=liveness,
            observability=observability,
            send_sd_notify=True,
        )
    )

    # Wait until the second audit has been observed (i.e., 5+ ticks done)
    try:
        await asyncio.wait_for(second_audit_observed.wait(), timeout=2.0)
        # Allow a couple more ticks to verify no extra audits
        await asyncio.sleep(0.005)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Two stall stretches → two audit emits (one per onset)
    assert audit_call_count == 2


async def test_watchdog_audit_event_type_is_SYSTEM(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC12 #12: audit fields — actor=system, event_type=SYSTEM, summary contains
    'Control loop stalled' and 'missed'.
    """
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda _msg: None)

    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)

    task = asyncio.create_task(
        watchdog_task(
            0.001,
            loop_liveness=_stale_liveness(),
            observability=observability,
            send_sd_notify=True,
            startup_grace_seconds=0.0,  # bypass cold-start grace for this audit-shape test
        )
    )
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert observability.audit.await_count >= 1
    call = observability.audit.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs["actor"] == "system"
    assert kwargs["event_type"] == "SYSTEM"
    assert "Control loop stalled" in kwargs["summary"]
    assert "missed" in kwargs["summary"]


async def test_watchdog_audit_emit_failure_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC12 #13: audit raises → watchdog continues running and logs audit_emit_failed."""
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda _msg: None)

    errors: list[str] = []

    class FakeLogger:
        def warning(self, event: str, **_kwargs: object) -> None:
            pass

        def error(self, event: str, **_kwargs: object) -> None:
            errors.append(event)

    monkeypatch.setattr("open_ems.services.watchdog.logger", FakeLogger())

    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(side_effect=RuntimeError("db unavailable"))

    task = asyncio.create_task(
        watchdog_task(
            0.001,
            loop_liveness=_stale_liveness(),
            observability=observability,
            send_sd_notify=True,
            startup_grace_seconds=0.0,
        )
    )
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert "audit_emit_failed" in errors
    # Task must NOT have died — cancellation came from us, not from inside.
    assert task.cancelled() is True


# ── Cold-start grace (Story 8.4 review patch) ─────────────────────────────


async def test_watchdog_skips_audit_during_cold_start_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never-ticked loop within startup_grace_seconds → no audit, no sd_notify.

    Prevents an audit flood at every process startup when the watchdog runs
    its first tick before the control loop has completed its first cycle.
    """
    sent: list[str] = []
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda msg: sent.append(msg))

    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)

    task = asyncio.create_task(
        watchdog_task(
            0.001,
            loop_liveness=_stale_liveness(),
            observability=observability,
            send_sd_notify=True,
            startup_grace_seconds=5.0,  # well beyond test runtime
        )
    )
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert observability.audit.await_count == 0
    assert sent == []


async def test_watchdog_emits_audit_after_cold_start_grace_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When grace elapses without a single alive tick, normal stall audit resumes."""
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda _msg: None)

    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)

    task = asyncio.create_task(
        watchdog_task(
            0.001,
            loop_liveness=_stale_liveness(),
            observability=observability,
            send_sd_notify=True,
            startup_grace_seconds=0.005,  # short grace so the test can wait it out
        )
    )
    # Grace expires within ~5 ms; let the task tick a few more times after that.
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Exactly one stall audit per stretch — once grace expired and the loop is
    # still never-alive, we are in one continuous stall stretch.
    assert observability.audit.await_count == 1
    kwargs = observability.audit.await_args.kwargs
    assert kwargs["event_type"] == "SYSTEM"
    assert "Control loop stalled" in kwargs["summary"]


# ── AC11b — Docker parity ──────────────────────────────────────────────────


async def test_watchdog_skips_sd_notify_when_send_sd_notify_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC12 #14b: send_sd_notify=False + alive loop → sd_notify NOT called."""
    sent: list[str] = []
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda msg: sent.append(msg))

    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)

    task = asyncio.create_task(
        watchdog_task(
            0.005,
            loop_liveness=_alive_liveness(),
            observability=observability,
            send_sd_notify=False,
        )
    )
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert sent == []


async def test_watchdog_emits_audit_in_docker_mode_on_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC12 #14c: send_sd_notify=False + stale → SYSTEM audit IS emitted (Docker parity)."""
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda _msg: None)

    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)

    task = asyncio.create_task(
        watchdog_task(
            0.001,
            loop_liveness=_stale_liveness(),
            observability=observability,
            send_sd_notify=False,
            startup_grace_seconds=0.0,
        )
    )
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert observability.audit.await_count >= 1
    call = observability.audit.await_args
    assert call is not None
    assert call.kwargs["event_type"] == "SYSTEM"
