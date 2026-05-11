"""Address parsing helpers shared by validation-time and runtime adapter wiring.

Story 9.X: previously the only consumer of these parsers was
``ProtocolAdapterFactory`` (validation surface). The runtime adapter wiring
introduced in 9.X reuses them via this shared module — duplication would
silently drift between the two surfaces.

Both helpers enforce P14 (review of Story 9.4): IPv6 bracketed-host form
``[<ipv6>]:port`` is accepted, port must be in ``(0, 65535]``.
"""

from __future__ import annotations


def parse_host_port(address: str) -> tuple[str | None, int | None]:
    """Parse ``host:port`` for Modbus TCP addresses.

    Accepts the IPv6 bracketed-host form ``[::1]:502`` and validates the port
    range. Returns ``(None, None)`` if the address is unparsable or the port
    is out of range; downstream callers raise their own structured error.
    """
    if not address:
        return None, None
    if address.startswith("["):
        end = address.find("]")
        if end == -1 or end + 1 >= len(address) or address[end + 1] != ":":
            return None, None
        host = address[1:end]
        port_str = address[end + 2 :]
    else:
        if address.count(":") != 1:
            return None, None
        host, port_str = address.rsplit(":", 1)
    if not host:
        return None, None
    try:
        port = int(port_str)
    except ValueError:
        return None, None
    if not (0 < port <= 65535):
        return None, None
    return host, port


def parse_dsmr_address(
    address: str,
) -> tuple[str | None, str | None, int | None]:
    """Parse a DSMR address: serial-port path OR ``host:port`` for TCP DSMR.

    Returns ``(serial_port, tcp_host, tcp_port)``; exactly one transport
    triple is populated on success. ``(None, None, None)`` on parse failure.
    """
    if not address:
        return None, None, None
    if address.startswith("/"):
        return address, None, None
    if address.count(":") != 1:
        return None, None, None
    host, port_str = address.rsplit(":", 1)
    if not host:
        return None, None, None
    try:
        port = int(port_str)
    except ValueError:
        return None, None, None
    if not (0 < port <= 65535):
        return None, None, None
    return None, host, port


__all__ = ["parse_dsmr_address", "parse_host_port"]
