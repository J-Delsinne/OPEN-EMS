from __future__ import annotations

import socket
import struct
import time
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)

ClockStatus = Literal["valid", "suspect", "unknown"]

_clock_status: ClockStatus = "unknown"


def get_clock_status() -> ClockStatus:
    return _clock_status


def check_clock(ntp_host: str | None, drift_threshold_seconds: float) -> ClockStatus:
    """Check system clock against an NTP server; stores and returns the result."""
    global _clock_status
    if ntp_host is None:
        _clock_status = "unknown"
        return _clock_status
    try:
        ntp_time = _query_ntp(ntp_host)
        drift = abs(ntp_time - time.time())
        _clock_status = "suspect" if drift > drift_threshold_seconds else "valid"
    except (OSError, ValueError) as exc:
        logger.warning(
            "ntp_check_failed",
            component="time_sync",
            ntp_host=ntp_host,
            reason=str(exc),
        )
        _clock_status = "unknown"
    return _clock_status


def _query_ntp(host: str, timeout: float = 3.0) -> float:
    """Query NTP server over UDP port 123; returns Unix timestamp."""
    data = b"\x1b" + 47 * b"\x00"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(data, (host, 123))
        msg, _ = sock.recvfrom(1024)
    if len(msg) < 48:
        raise ValueError(f"NTP response too short: {len(msg)} bytes")
    if msg[0] & 0x07 != 4:
        raise ValueError(f"NTP response mode not server: {msg[0] & 0x07}")
    ntp_epoch_offset = 2208988800
    seconds = int(struct.unpack("!I", msg[40:44])[0])
    fraction = int(struct.unpack("!I", msg[44:48])[0])
    return float(seconds) + float(fraction) / 2**32 - ntp_epoch_offset
