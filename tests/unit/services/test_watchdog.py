from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

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


# ── watchdog_task ─────────────────────────────────────────────────────────


async def test_watchdog_task_sends_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task sends sd_notify("WATCHDOG=1") after each sleep."""
    sent: list[str] = []
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda msg: sent.append(msg))

    task = asyncio.create_task(watchdog_task(0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert sent.count("WATCHDOG=1") >= 1


async def test_watchdog_task_cancels_cleanly() -> None:
    """CancelledError propagates out without being swallowed."""
    with patch("open_ems.services.watchdog.sd_notify"):
        task = asyncio.create_task(watchdog_task(0.001))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_watchdog_task_logs_missed_on_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task calls logger.warning when wake-up is delayed beyond 1.5× interval."""
    warnings: list[str] = []

    class FakeLogger:
        def warning(self, event: str, **_kwargs: object) -> None:
            warnings.append(event)

    import time as time_module

    call_count = 0
    base_time = time_module.monotonic()

    def fake_monotonic() -> float:
        nonlocal call_count
        call_count += 1
        # calls: 1=now after first sleep (establishes last_sent=base_time),
        #        2=now after second sleep (simulates 100 s delay → warns)
        if call_count == 1:
            return base_time
        return base_time + 100.0

    monkeypatch.setattr("open_ems.services.watchdog._monotonic", fake_monotonic)
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", lambda _: None)
    monkeypatch.setattr("open_ems.services.watchdog.logger", FakeLogger())

    task = asyncio.create_task(watchdog_task(0.001))
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert "watchdog_missed" in warnings


async def test_watchdog_task_no_missed_on_first_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup latency before the first heartbeat must not trigger watchdog_missed.

    last_sent is None on the first iteration, so the missed-interval check is skipped
    regardless of how large the elapsed value would be if last_sent were pre-initialised.
    """
    warnings: list[str] = []
    heartbeats: list[str] = []

    class FakeLogger:
        def warning(self, event: str, **_kwargs: object) -> None:
            warnings.append(event)

    def fake_send(msg: str) -> None:
        heartbeats.append(msg)
        if len(heartbeats) == 1:
            task.cancel()

    # _monotonic always returns a huge value — would trigger missed if last_sent were
    # pre-initialised to a small value, but last_sent starts as None so check is skipped.
    monkeypatch.setattr("open_ems.services.watchdog._monotonic", lambda: 9_999_999.0)
    monkeypatch.setattr("open_ems.services.watchdog.sd_notify", fake_send)
    monkeypatch.setattr("open_ems.services.watchdog.logger", FakeLogger())

    task = asyncio.create_task(watchdog_task(0.001))
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert heartbeats == ["WATCHDOG=1"]
    assert "watchdog_missed" not in warnings
