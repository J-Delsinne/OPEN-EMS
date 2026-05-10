from __future__ import annotations

import asyncio
import os
import time

import structlog

from open_ems.services.audit_log import ObservabilityService
from open_ems.services.loop_liveness import LoopLiveness
from open_ems.services.readiness import sd_notify

logger = structlog.get_logger(__name__)

# Module-level reference allows test patching without disturbing asyncio's clock
_monotonic = time.monotonic


def get_watchdog_interval() -> float | None:
    """Return heartbeat interval in seconds (half of WatchdogSec), or None if not configured."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    # Only the process nominated by systemd should send heartbeats
    watchdog_pid = os.environ.get("WATCHDOG_PID")
    if watchdog_pid not in (None, str(os.getpid())):
        return None
    try:
        usec_int = int(usec)
    except ValueError:
        logger.warning("watchdog_usec_invalid", raw_value=usec, component="watchdog")
        return None
    if usec_int <= 0:
        logger.warning("watchdog_usec_invalid", raw_value=usec, component="watchdog")
        return None
    return usec_int / 2_000_000  # microseconds → seconds, halved


async def watchdog_task(
    interval: float,
    *,
    loop_liveness: LoopLiveness,
    observability: ObservabilityService,
    send_sd_notify: bool,
    startup_grace_seconds: float | None = None,
) -> None:
    """Poll loop liveness; send sd_notify only while loop is alive.

    Behaviour (Story 8.4 AC1, AC2, AC10, AC11, AC11b):

    * On every tick, consult ``loop_liveness.is_alive()``.
    * If alive: in native systemd mode (``send_sd_notify=True``) emit
      ``WATCHDOG=1``. In Docker mode (``send_sd_notify=False``) do nothing —
      the supervisor restart trigger is the 503 from ``/health/ready``.
    * If NOT alive: skip the heartbeat (so systemd's ``WatchdogSec`` elapses
      and triggers a restart) AND emit a SYSTEM audit event ONCE per stall
      stretch (one-shot — no flapping spam). The flag resets when the loop
      recovers.

    Cold-start grace: until either the first alive tick is observed OR
    ``startup_grace_seconds`` elapses (defaults to
    ``loop_liveness.cycle_deadline_seconds``), a never-ticked liveness is
    treated as "warming up": both the stall audit and the heartbeat are
    suppressed so we neither flood the audit log on every startup nor
    falsely report a stall before the loop has had a chance to run its
    first cycle. Once warm-up ends, normal stall semantics resume.
    """
    audit_emitted_for_current_stall = False
    grace_seconds = (
        startup_grace_seconds
        if startup_grace_seconds is not None
        else loop_liveness.cycle_deadline_seconds
    )
    started_at = _monotonic()
    grace_active = grace_seconds > 0
    while True:
        await asyncio.sleep(interval)
        if loop_liveness.is_alive():
            if send_sd_notify:
                sd_notify("WATCHDOG=1")
            audit_emitted_for_current_stall = False
            grace_active = False  # any healthy tick ends cold-start grace
        else:
            if (
                grace_active
                and loop_liveness.last_cycle_completed_at_monotonic is None
                and (_monotonic() - started_at) < grace_seconds
            ):
                # Cold-start: loop has not yet completed its first cycle and the
                # grace window has not yet expired. Suppress both the audit
                # (no startup flood) and sd_notify (per AC1: not alive → no
                # heartbeat); systemd's WatchdogSec absorbs the warm-up window.
                continue
            grace_active = False
            if not audit_emitted_for_current_stall:
                await _emit_stall_audit(observability, loop_liveness)
                audit_emitted_for_current_stall = True
            # Skip sd_notify so WatchdogSec elapses → systemd restart


async def _emit_stall_audit(
    observability: ObservabilityService,
    loop_liveness: LoopLiveness,
) -> None:
    """Emit one SYSTEM audit event for the current stall stretch (AC2)."""
    elapsed = loop_liveness.seconds_since_last_cycle()
    cycle_interval = max(loop_liveness.cycle_interval_seconds, 1.0)
    if elapsed is None:
        elapsed_str = "0.0 s"
        missed_count = 1
    else:
        elapsed_str = f"{elapsed:.1f} s"
        missed_count = max(1, int(elapsed / cycle_interval))

    cycle_word = "cycle" if missed_count == 1 else "cycles"
    summary = (
        f"Control loop stalled: no successful evaluation cycle in {elapsed_str} "
        f"(missed {missed_count} consecutive {cycle_word}); supervisor restart pending"
    )
    try:
        await observability.audit(
            actor="system",
            event_type="SYSTEM",
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 — audit failure must not mask stall recovery
        logger.error(
            "audit_emit_failed",
            component="watchdog",
            error=repr(exc),
            exc_info=True,
        )
