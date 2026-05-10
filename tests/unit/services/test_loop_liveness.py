"""Unit tests for LoopLiveness (Story 8.4 AC9, AC12 #1-#8)."""

from __future__ import annotations

import pytest

from open_ems.services.loop_liveness import LoopLiveness


@pytest.fixture
def liveness() -> LoopLiveness:
    return LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)


def test_initial_state_is_not_alive_and_not_in_fail_safe(liveness: LoopLiveness) -> None:
    assert liveness.is_alive() is False
    assert liveness.in_fail_safe is False
    assert liveness.crashed is False
    assert liveness.last_cycle_completed_at_monotonic is None
    assert liveness.crash_reason is None


def test_is_alive_true_after_recent_mark_cycle_complete(liveness: LoopLiveness) -> None:
    liveness.mark_cycle_complete()
    assert liveness.is_alive() is True


def test_is_alive_false_when_last_cycle_older_than_threshold(liveness: LoopLiveness) -> None:
    liveness.last_cycle_completed_at_monotonic = 1000.0
    # threshold = min(20.0, 60.0) = 20.0; elapsed = 30.0 > 20.0 → not alive
    assert liveness.is_alive(now_monotonic=1030.0) is False


def test_is_alive_false_when_crashed(liveness: LoopLiveness) -> None:
    liveness.mark_cycle_complete()  # would otherwise be alive
    liveness.mark_crashed(RuntimeError("boom"))
    assert liveness.is_alive() is False


def test_mark_crashed_sets_in_fail_safe(liveness: LoopLiveness) -> None:
    assert liveness.in_fail_safe is False
    liveness.mark_crashed(RuntimeError("boom"))
    assert liveness.in_fail_safe is True
    assert liveness.crashed is True
    assert liveness.crash_reason is not None
    assert "RuntimeError" in liveness.crash_reason


def test_mark_crashed_truncates_long_repr(liveness: LoopLiveness) -> None:
    very_long_message = "x" * 5000
    liveness.mark_crashed(RuntimeError(very_long_message))
    assert liveness.crash_reason is not None
    assert len(liveness.crash_reason) <= 500


def test_seconds_since_last_cycle_returns_none_when_never_ticked(
    liveness: LoopLiveness,
) -> None:
    assert liveness.seconds_since_last_cycle() is None


def test_seconds_since_last_cycle_returns_elapsed(liveness: LoopLiveness) -> None:
    liveness.last_cycle_completed_at_monotonic = 1000.0
    assert liveness.seconds_since_last_cycle(now_monotonic=1042.5) == pytest.approx(42.5)
