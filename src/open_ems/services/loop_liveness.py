"""LoopLiveness: shared in-process liveness signal for the runtime control loop.

This module exposes a single class :class:`LoopLiveness` that is the single
source of truth for whether the control loop is currently making progress and
whether it is in fail-safe mode. Three readers consume it (``watchdog_task``,
``/health/ready`` route, ``_on_control_loop_done`` callback) and one writer
mutates it (``ControlLoop`` writes ``mark_cycle_complete`` /
``mark_fail_safe_entered`` / ``mark_fail_safe_exited``;
``_on_control_loop_done`` writes ``mark_crashed``).

Asyncio-safety guarantee
------------------------
``LoopLiveness`` is NOT thread-safe. It IS asyncio-safe under the project's
single-task-asyncio architecture: every mutator runs to completion inside a
single asyncio task before yielding, and readers consult one field at a time.
A future contributor who introduces a second writer task MUST add a lock or
re-architect this object.

Time source
-----------
All timestamps use :func:`time.monotonic`, NOT :func:`datetime.now`. Wall-clock
time can jump backward across NTP step adjustments; ``time.monotonic`` is
guaranteed to be non-decreasing within a process. Stall detection across an
NTP step would silently regress under wall-clock time.
"""

from __future__ import annotations

import time

_MAX_CRASH_REASON_LEN = 500


class LoopLiveness:
    """Pure state holder for control-loop liveness and fail-safe flags."""

    def __init__(
        self,
        *,
        missed_cycle_threshold_seconds: float,
        cycle_deadline_seconds: float,
        cycle_interval_seconds: float | None = None,
    ) -> None:
        self.missed_cycle_threshold_seconds = missed_cycle_threshold_seconds
        self.cycle_deadline_seconds = cycle_deadline_seconds
        # Per-cycle interval is needed by the watchdog to compute the missed-cycle
        # count accurately. When omitted (e.g. some unit tests), recover it from the
        # threshold window so behaviour stays sensible: missed_cycle_threshold_seconds
        # is `threshold_count × cycle_interval`, so dividing by the threshold count of
        # 1 gives us a safe floor.
        self.cycle_interval_seconds: float = (
            cycle_interval_seconds
            if cycle_interval_seconds is not None
            else missed_cycle_threshold_seconds
        )
        self.last_cycle_completed_at_monotonic: float | None = None
        self.in_fail_safe: bool = False
        self.crashed: bool = False
        self.crash_reason: str | None = None

    def mark_cycle_complete(self) -> None:
        self.last_cycle_completed_at_monotonic = time.monotonic()

    def mark_fail_safe_entered(self) -> None:
        self.in_fail_safe = True

    def mark_fail_safe_exited(self) -> None:
        self.in_fail_safe = False

    def mark_crashed(self, exc: BaseException) -> None:
        self.crashed = True
        self.crash_reason = f"{type(exc).__name__}: {exc!r}"[:_MAX_CRASH_REASON_LEN]
        self.in_fail_safe = True

    def is_alive(self, now_monotonic: float | None = None) -> bool:
        if self.crashed:
            return False
        if self.last_cycle_completed_at_monotonic is None:
            return False
        now = time.monotonic() if now_monotonic is None else now_monotonic
        elapsed = now - self.last_cycle_completed_at_monotonic
        freshness_window = min(self.missed_cycle_threshold_seconds, self.cycle_deadline_seconds)
        return elapsed < freshness_window

    def seconds_since_last_cycle(self, now_monotonic: float | None = None) -> float | None:
        if self.last_cycle_completed_at_monotonic is None:
            return None
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return now - self.last_cycle_completed_at_monotonic
