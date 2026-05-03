"""Domain device state models, DeviceAdapter protocol, and energy sign conventions.

Sign Conventions
================
All energy values use physics/IEC 60050 polarity conventions:

    grid_power_kw > 0    : import from grid (site is consuming)
    grid_power_kw < 0    : export to grid (site is producing)

    battery_power_kw > 0 : battery charging (consuming power)
    battery_power_kw < 0 : battery discharging (providing power)

    pv_power_kw >= 0     : PV production only; negative values are physically impossible

    energy_delivered_kwh >= 0 : cumulative energy imported from grid (monotonically increasing)
    energy_returned_kwh  >= 0 : cumulative energy exported to grid (monotonically increasing)

All ``read_at`` / ``received_at`` timestamps are timezone-aware UTC.
Units are always SI with suffix in field name (``_kw``, ``_kwh``, ``_percent``).
"""

from __future__ import annotations

import enum
from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

SocPercent = Annotated[float, Field(ge=0.0, le=100.0)]
PvPowerKw = Annotated[float, Field(ge=0.0)]
EnergyKwh = Annotated[float, Field(ge=0.0)]


def _require_utc(value: datetime, field_name: str) -> datetime:
    """Validate that a datetime is timezone-aware UTC.

    Copied from adapters/protocol.py; core must not import from adapters.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be in UTC")
    return value


class DeviceRole(enum.StrEnum):
    """Energy system device role classification."""

    inverter = "inverter"
    battery = "battery"
    ev_charger = "ev_charger"
    grid_meter = "grid_meter"


class InverterState(BaseModel):
    """Normalized domain state for a PV inverter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    pv_power_kw: PvPowerKw
    ac_power_kw: float
    operating_mode: str
    fault_code: str | None = None
    read_at: datetime

    @field_validator("read_at")
    @classmethod
    def _read_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "read_at")


class BatteryState(BaseModel):
    """Normalized domain state for a battery storage system."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    soc_percent: SocPercent
    battery_power_kw: float
    capacity_kwh: float
    operating_mode: str
    read_at: datetime

    @field_validator("read_at")
    @classmethod
    def _read_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "read_at")


class EVChargerState(BaseModel):
    """Normalized domain state for an EV charger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    status: Literal["available", "charging", "faulted", "unavailable"]
    session_active: bool
    current_power_kw: float | None = None
    power_source: Literal["meter_values"] | None = None
    power_measured_at: datetime | None = None
    read_at: datetime

    @field_validator("read_at")
    @classmethod
    def _read_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "read_at")

    @field_validator("power_measured_at")
    @classmethod
    def _power_measured_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value, "power_measured_at")


class GridMeterState(BaseModel):
    """Normalized domain state for a grid energy meter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    grid_power_kw: float
    energy_delivered_kwh: EnergyKwh
    energy_returned_kwh: EnergyKwh
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def _received_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "received_at")


DeviceState = InverterState | BatteryState | EVChargerState | GridMeterState


class DegradedDeviceState(BaseModel):
    """Domain-level recoverable device failure state.

    Distinct from ProtocolDegradedState (Epic 3). Carries domain role context.
    Consumers of open_ems.core must never import ProtocolDegradedState for domain use.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    role: DeviceRole
    reason: NonEmptyStr
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "occurred_at")


class DeviceCapabilityProfile(BaseModel):
    """Per-device capability profile. Expanded in Story 4.4."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    model: str
    firmware_version: str | None = None
    capability_status: Literal["full", "reduced", "unknown"] = "unknown"


@runtime_checkable
class DeviceAdapter(Protocol):
    """Structural contract for all domain-level device adapters.

    Epic 4 scope: get_state() and get_capabilities() only.
    send_command() is added in Epic 8 via PolicyGuard.
    """

    device_id: str

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_state(self) -> DeviceState | DegradedDeviceState: ...

    async def get_capabilities(self) -> DeviceCapabilityProfile: ...
