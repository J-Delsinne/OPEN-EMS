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

Capability Enumerations
=======================
``ReadCapability`` — validated read operations a device supports:
    - ``state``  : typed domain state via ``get_state()``
    - ``power``  : real-time power measurement (kW)
    - ``energy`` : cumulative energy counters (kWh)
    - ``soc``    : state-of-charge percentage (batteries only)

``WriteCapability`` — validated write/control operations a device supports:
    - ``set_charge_rate``       : set battery/EV charge power
    - ``set_discharge_rate``    : set battery discharge power
    - ``set_operating_mode``    : set inverter/battery operating mode
    - ``set_ev_charge_current`` : set EV charger current limit

``CapabilityStatus`` — overall profile completeness:
    - ``full``        : all capabilities for the device type are validated
    - ``reduced``     : partial support; one or more capabilities absent
    - ``unsupported`` : capability explicitly not supported for this model

``DeviceDiscoveryResult`` — typed result from a discovery probe or OCPP registration:
    Returned by ``DiscoveryService`` for each discovered device. Used by the
    installer wizard (Epic 9) to present a typed list of probed or self-registered
    devices with their protocol, address, optional model, and capability status.
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


class ReadCapability(enum.StrEnum):
    """Validated read operations that a device model supports."""

    state = "state"
    power = "power"
    energy = "energy"
    soc = "soc"


class WriteCapability(enum.StrEnum):
    """Validated write/control operations that a device model supports."""

    set_charge_rate = "set_charge_rate"
    set_discharge_rate = "set_discharge_rate"
    set_operating_mode = "set_operating_mode"
    set_ev_charge_current = "set_ev_charge_current"


class CapabilityStatus(enum.StrEnum):
    """Overall completeness of a device capability profile."""

    full = "full"
    reduced = "reduced"
    unsupported = "unsupported"


class DeviceCapabilityProfile(BaseModel):
    """Per-device capability profile with validated read/write capability sets.

    Profiles are declared in ``adapters/capabilities/`` per device model and are
    keyed by model string in the capability registry. The ``device_id`` is
    substituted at lookup time; profiles in the registry use ``"__placeholder__"``
    as a sentinel. The ``firmware_version`` parameter is accepted but firmware
    range matching is deferred to a future story — all known profiles accept any
    firmware value in Epic 4.

    ``capability_status`` reflects overall completeness:
    - ``CapabilityStatus.full``        — all validated capabilities present
    - ``CapabilityStatus.reduced``     — partial support (e.g., unknown model)
    - ``CapabilityStatus.unsupported`` — capability not supported for this model

    Higher layers (Epic 7 decision engine, Epic 8 PolicyGuard) must only act on
    capabilities present in ``read_capabilities`` / ``write_capabilities``.
    Absent capabilities are never exposed (FR6b).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    model: str
    firmware_version: str | None = None
    capability_status: CapabilityStatus = CapabilityStatus.unsupported
    read_capabilities: frozenset[ReadCapability] = frozenset()
    write_capabilities: frozenset[WriteCapability] = frozenset()
    known_limitations: tuple[str, ...] = ()
    limitation_reason: str | None = None


class DeviceDiscoveryResult(BaseModel):
    """Typed result returned by DiscoveryService for a single discovered device.

    Used by the installer wizard (Epic 9) to present a typed list of probed or
    self-registered devices. Immutable once created.

    ``capability_status`` is derived from the capability registry if a model string
    is available; defaults to ``CapabilityStatus.reduced`` when the model is unknown.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    protocol: Literal["modbus_tcp", "ocpp_1_6", "dsmr_p1"]
    address: NonEmptyStr
    model: NonEmptyStr | None = None
    capability_status: CapabilityStatus = CapabilityStatus.reduced


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
