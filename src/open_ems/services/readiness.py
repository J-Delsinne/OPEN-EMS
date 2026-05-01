from __future__ import annotations

import os
import socket

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
    if not notify_socket:
        return
    try:
        abstract = notify_socket.startswith("@")
        addr = ("\0" + notify_socket[1:]) if abstract else notify_socket
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:  # type: ignore[attr-defined]
            sock.sendto(message.encode(), addr)
    except Exception:  # noqa: BLE001
        pass
