from __future__ import annotations

import os
import socket

import structlog

logger = structlog.get_logger(__name__)

_ready: bool = False


def is_ready() -> bool:
    return _ready


def mark_ready() -> None:
    global _ready
    sd_notify("READY=1")
    _ready = True


def reset() -> None:
    global _ready
    _ready = False


def sd_notify(message: str) -> None:
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    af_unix: int | None = getattr(socket, "AF_UNIX", None)
    if not notify_socket or af_unix is None:
        return
    try:
        abstract = notify_socket.startswith("@")
        addr = ("\0" + notify_socket[1:]) if abstract else notify_socket
        with socket.socket(af_unix, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), addr)
    except OSError as exc:
        logger.warning(
            "sd_notify_failed",
            component="readiness",
            reason=str(exc),
        )
