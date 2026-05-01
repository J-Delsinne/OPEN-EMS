from __future__ import annotations

import asyncio
import os
import time

import structlog

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


async def watchdog_task(interval: float) -> None:
    """Send WATCHDOG=1 heartbeat to systemd every `interval` seconds.

    Logs watchdog_missed if the event loop delays the wake-up beyond 1.5× the expected
    interval. This is precursor detection only; stall-recovery logic is Epic 8 scope.
    """
    last_sent: float | None = None
    while True:
        await asyncio.sleep(interval)
        now = _monotonic()
        if last_sent is not None:
            elapsed = now - last_sent
            if elapsed > interval * 1.5:
                logger.warning(
                    "watchdog_missed",
                    expected_interval=round(interval, 3),
                    actual_interval=round(elapsed, 3),
                    component="watchdog",
                )
        sd_notify("WATCHDOG=1")
        last_sent = now
