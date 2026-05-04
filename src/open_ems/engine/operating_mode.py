"""System operating mode recommendation for pure decision-engine rules."""

from __future__ import annotations

from open_ems.core import DegradedDeviceState, DeviceRole, SystemOperatingMode
from open_ems.core.state import DeviceSlot
from open_ems.engine.models import EvaluationInput


def derive_recommended_operating_mode(
    evaluation_input: EvaluationInput,
) -> SystemOperatingMode:
    """Derive the deterministic engine-side operating mode recommendation."""
    degraded_roles = _degraded_roles(
        (
            evaluation_input.inverter,
            evaluation_input.battery,
            evaluation_input.ev_charger,
            evaluation_input.grid_meter,
        )
    )

    if not degraded_roles:
        return SystemOperatingMode.normal
    if DeviceRole.grid_meter in degraded_roles and len(degraded_roles) > 1:
        return SystemOperatingMode.fail_safe
    if degraded_roles == frozenset({DeviceRole.grid_meter}):
        return SystemOperatingMode.conservative
    return SystemOperatingMode.degraded


def _degraded_roles(slots: tuple[DeviceSlot, ...]) -> frozenset[DeviceRole]:
    return frozenset(state.role for state in slots if isinstance(state, DegradedDeviceState))
