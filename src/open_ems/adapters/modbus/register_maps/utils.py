"""Shared Modbus register decoding utilities.

All register map modules MUST use these helpers exclusively — no inline
bit-twiddling or scaling arithmetic is permitted in the map classes.
"""

from __future__ import annotations


def int16(value: int) -> int:
    """Interpret unsigned 16-bit register value as signed two's complement."""
    return value if value < 32768 else value - 65536


def uint16(value: int) -> int:
    """Pass-through for unsigned 16-bit values (documents intent explicitly)."""
    return value


def scale(value: int, factor: float) -> float:
    """Apply a linear scaling factor to a raw register integer."""
    return value * factor
