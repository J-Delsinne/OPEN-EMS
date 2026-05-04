"""Immutable system state snapshot models and derivation helpers."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator, model_validator

from open_ems.core.devices import (
    BatteryState,
    DegradedDeviceState,
    DeviceRole,
    DeviceState,
    EVChargerState,
    GridMeterState,
    InverterState,
    _require_utc,
)

ClockStatus = Literal["valid", "suspect", "unknown"]

DeviceSlot = DeviceState | DegradedDeviceState | None

REQUIRED_DEVICE_ROLES: frozenset[DeviceRole] = frozenset(
    {DeviceRole.grid_meter, DeviceRole.inverter}
)
OPTIONAL_DEVICE_ROLES: frozenset[DeviceRole] = frozenset(
    {DeviceRole.battery, DeviceRole.ev_charger}
)
ALL_DEVICE_ROLES: tuple[DeviceRole, ...] = (
    DeviceRole.inverter,
    DeviceRole.battery,
    DeviceRole.ev_charger,
    DeviceRole.grid_meter,
)


class GlobalState(enum.StrEnum):
    """Global UI state derived from the current system snapshot."""

    normal = "NORMAL"
    degraded = "DEGRADED"
    stale = "STALE"
    failed = "FAILED"


class ComponentState(enum.StrEnum):
    """Per-component UI state."""

    idle = "IDLE"
    pending = "PENDING"
    active = "ACTIVE"
    error = "ERROR"
    unavailable = "UNAVAILABLE"
    stale = "STALE"


class SystemOperatingMode(enum.StrEnum):
    """Operating mode consumed by later decision-engine and control-loop stories."""

    normal = "normal"
    degraded = "degraded"
    conservative = "conservative"
    fail_safe = "fail_safe"


OPERATING_MODE_GLOBAL_STATE: Mapping[SystemOperatingMode, GlobalState] = MappingProxyType(
    {
        SystemOperatingMode.normal: GlobalState.normal,
        SystemOperatingMode.degraded: GlobalState.degraded,
        SystemOperatingMode.conservative: GlobalState.degraded,
        SystemOperatingMode.fail_safe: GlobalState.failed,
    }
)

_FAILED_DEGRADED_REASONS = frozenset({"device_id_mismatch", "unexpected_raw_state_type"})


def _is_failed_reason(reason: str) -> bool:
    return reason in _FAILED_DEGRADED_REASONS or reason.startswith("validation_error:")


def derive_component_state(state: DeviceSlot) -> ComponentState:
    """Derive a per-role component UI state from a device slot value."""
    if state is None:
        return ComponentState.unavailable
    if isinstance(state, DegradedDeviceState):
        if state.reason == "unavailable":
            return ComponentState.unavailable
        return ComponentState.error
    return ComponentState.active


def derive_global_state(device_slots: Mapping[DeviceRole, DeviceSlot]) -> GlobalState:
    """Derive global state with priority FAILED > DEGRADED > NORMAL."""
    required_slots = {role: device_slots.get(role) for role in REQUIRED_DEVICE_ROLES}

    if any(
        isinstance(state, DegradedDeviceState) and _is_failed_reason(state.reason)
        for state in device_slots.values()
    ):
        return GlobalState.failed

    if any(
        isinstance(state, DegradedDeviceState)
        and (state.role in REQUIRED_DEVICE_ROLES or state.reason != "unavailable")
        for state in device_slots.values()
    ):
        return GlobalState.degraded

    if any(state is None for state in required_slots.values()):
        return GlobalState.degraded

    return GlobalState.normal


def derive_data_age_seconds(state: DeviceSlot, captured_at: datetime) -> int | None:
    """Derive device data age from the timestamp type owned by each state model."""
    measurement_at: datetime | None
    if isinstance(state, InverterState | BatteryState | EVChargerState):
        measurement_at = state.read_at
    elif isinstance(state, GridMeterState):
        measurement_at = state.received_at
    elif isinstance(state, DegradedDeviceState):
        measurement_at = state.occurred_at
    else:
        measurement_at = None

    if measurement_at is None:
        return None
    return max(0, int((captured_at - measurement_at).total_seconds()))


class SystemSnapshot(BaseModel):
    """Immutable point-in-time system state view."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence_id: int
    captured_at: datetime
    global_state: GlobalState
    operating_mode: SystemOperatingMode
    inverter: DeviceSlot
    battery: DeviceSlot
    ev_charger: DeviceSlot
    grid_meter: DeviceSlot
    component_states: Mapping[DeviceRole, ComponentState]
    data_age_seconds: Mapping[DeviceRole, int | None]
    system_clock_status: ClockStatus

    @field_validator("captured_at")
    @classmethod
    def _captured_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "captured_at")

    @model_validator(mode="after")
    def _freeze_nested_mappings(self) -> SystemSnapshot:
        _validate_complete_role_map(self.component_states, "component_states")
        _validate_complete_role_map(self.data_age_seconds, "data_age_seconds")
        object.__setattr__(self, "component_states", MappingProxyType(dict(self.component_states)))
        object.__setattr__(self, "data_age_seconds", MappingProxyType(dict(self.data_age_seconds)))
        return self

    @field_serializer("component_states")
    def _serialize_component_states(
        self, value: Mapping[DeviceRole, ComponentState]
    ) -> dict[DeviceRole, ComponentState]:
        return dict(value)

    @field_serializer("data_age_seconds")
    def _serialize_data_age_seconds(
        self, value: Mapping[DeviceRole, int | None]
    ) -> dict[DeviceRole, int | None]:
        return dict(value)


def _validate_complete_role_map(
    value: Mapping[DeviceRole, object],
    field_name: str,
) -> None:
    missing = set(ALL_DEVICE_ROLES) - set(value)
    extra = set(value) - set(ALL_DEVICE_ROLES)
    if missing or extra:
        missing_values = ", ".join(sorted(role.value for role in missing)) or "none"
        extra_values = ", ".join(sorted(str(role) for role in extra)) or "none"
        raise ValueError(
            f"{field_name} must contain exactly all device roles "
            f"(missing: {missing_values}; extra: {extra_values})"
        )
