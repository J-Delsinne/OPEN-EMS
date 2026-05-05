"""Typed inputs consumed by pure decision-engine rules."""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from open_ems.core.devices import DeviceCapabilityProfile, NonEmptyStr
from open_ems.core.state import DeviceSlot, EnergyStrategy, SystemSnapshot

PositiveKw = Annotated[float, Field(gt=0.0)]
NonNegativeKw = Annotated[float, Field(ge=0.0)]
Percent = Annotated[float, Field(ge=0.0, le=100.0)]
IntervalElapsedSeconds = Annotated[int, Field(ge=0, le=900)]


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be in UTC")
    return value


class PeakContext(BaseModel):
    """Pure decision-engine peak signal for the active clock-aligned interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    current_partial_window_projection_kw: float
    configured_peak_limit_kw: PositiveKw

    @field_validator("current_partial_window_projection_kw")
    @classmethod
    def _projection_must_be_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("current_partial_window_projection_kw must be a finite number")
        return value

    current_monthly_recorded_peak_kw: NonNegativeKw
    current_interval_start: datetime
    current_interval_elapsed_seconds: IntervalElapsedSeconds

    @field_validator("current_interval_start")
    @classmethod
    def _interval_start_must_be_clock_aligned_utc(cls, value: datetime) -> datetime:
        value = _require_utc(value, "current_interval_start")
        if value.minute % 15 != 0 or value.second != 0 or value.microsecond != 0:
            raise ValueError("current_interval_start must be aligned to a 15-minute boundary")
        return value


class BatteryControlContext(BaseModel):
    """Pure battery-control context supplied by the evaluation caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reserve_floor_percent: Percent
    capability_profile: DeviceCapabilityProfile | None = None


class EVChargingWindow(BaseModel):
    """Local-time EV charging preference window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_local_time: time
    end_local_time: time
    timezone_name: NonEmptyStr

    @field_validator("timezone_name")
    @classmethod
    def _timezone_name_must_resolve(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone_name must be accepted by zoneinfo.ZoneInfo") from exc
        return value

    @model_validator(mode="after")
    def _start_and_end_must_differ(self) -> EVChargingWindow:
        if self.start_local_time == self.end_local_time:
            raise ValueError("start_local_time and end_local_time must not be equal")
        return self


class EVSchedulingContext(BaseModel):
    """Pure EV scheduling context supplied by the evaluation caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability_profile: DeviceCapabilityProfile | None = None
    charging_window: EVChargingWindow | None = None
    homeowner_override_active: bool
    evaluated_at: datetime
    target_charge_rate_kw: PositiveKw | None = None

    @field_validator("evaluated_at")
    @classmethod
    def _evaluated_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "evaluated_at")


class EvaluationInput(BaseModel):
    """Snapshot-derived input for one decision-engine evaluation cycle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inverter: DeviceSlot
    battery: DeviceSlot
    ev_charger: DeviceSlot
    grid_meter: DeviceSlot
    peak_context: PeakContext
    strategy: EnergyStrategy
    battery_control: BatteryControlContext
    ev_scheduling: EVSchedulingContext

    @model_validator(mode="after")
    def _required_slots_present(self) -> EvaluationInput:
        if self.inverter is None:
            raise ValueError("inverter is a required device slot and must not be None")
        if self.grid_meter is None:
            raise ValueError("grid_meter is a required device slot and must not be None")
        return self

    @classmethod
    def from_snapshot(
        cls,
        snapshot: SystemSnapshot,
        *,
        peak_context: PeakContext,
        strategy: EnergyStrategy,
        battery_control: BatteryControlContext,
        ev_scheduling: EVSchedulingContext,
    ) -> EvaluationInput:
        """Build engine input from a StateStore snapshot without retaining the store."""
        return cls(
            inverter=snapshot.inverter,
            battery=snapshot.battery,
            ev_charger=snapshot.ev_charger,
            grid_meter=snapshot.grid_meter,
            peak_context=peak_context,
            strategy=strategy,
            battery_control=battery_control,
            ev_scheduling=ev_scheduling,
        )
