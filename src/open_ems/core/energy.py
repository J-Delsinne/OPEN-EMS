"""Domain types for the energy-flow tracking + weekly summary surface (Story 10.4 / FR30).

``CompletedEnergyFlowInterval`` is emitted by ``EnergyFlowIntervalTracker`` on rollover and
persisted via ``EnergyRepo.write_energy_flow_interval``. ``EnergyFlowIntervalRow`` is the
read-path shape (structurally identical; named separately so callers do not confuse a
tracker output with a persisted read). ``WeeklyEnergySummaryRow`` is the single-row
pre-aggregated summary read by the homeowner endpoint and written by
``WeeklyEnergySummaryService``.

The ``insufficient_history=False ⇒ all three metrics set + ratio bounded [0, 1]``
invariant is enforced at TWO layers: the Pydantic model-validator below AND the SQLite
``CHECK`` constraint in migration ``0013``. Defense-in-depth, same class as
``EVOverrideState._failure_reason_iff_terminal`` (Story 10.2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")
    if value.utcoffset().total_seconds() != 0:  # type: ignore[union-attr]
        raise ValueError(f"{name} must be UTC (offset 0)")
    return value.astimezone(UTC)


class CompletedEnergyFlowInterval(BaseModel):
    """Finalized record of a single 15-minute clock-aligned energy-flow interval.

    Emitted by ``EnergyFlowIntervalTracker`` on rollover; consumed by
    ``EnergyRepo.write_energy_flow_interval``. All ``_kwh`` fields are ``>= 0`` —
    energy flow magnitudes; sign information is captured by the SPLIT of battery
    into ``battery_charged_kwh`` / ``battery_discharged_kwh`` and grid into
    ``grid_imported_kwh`` / ``grid_exported_kwh``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_start_utc: datetime
    pv_kwh: float = Field(ge=0.0)
    battery_charged_kwh: float = Field(ge=0.0)
    battery_discharged_kwh: float = Field(ge=0.0)
    grid_imported_kwh: float = Field(ge=0.0)
    grid_exported_kwh: float = Field(ge=0.0)
    ev_charged_kwh: float = Field(ge=0.0)
    sample_count: int = Field(ge=0)
    data_quality: Literal["complete", "incomplete"]

    @field_validator("interval_start_utc")
    @classmethod
    def _interval_start_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "interval_start_utc")


class EnergyFlowIntervalRow(BaseModel):
    """Read-path shape for ``energy_flow_intervals`` rows. Structurally identical to
    ``CompletedEnergyFlowInterval`` but named separately so callers do not confuse a
    tracker output with a persisted read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_start_utc: datetime
    pv_kwh: float = Field(ge=0.0)
    battery_charged_kwh: float = Field(ge=0.0)
    battery_discharged_kwh: float = Field(ge=0.0)
    grid_imported_kwh: float = Field(ge=0.0)
    grid_exported_kwh: float = Field(ge=0.0)
    ev_charged_kwh: float = Field(ge=0.0)
    sample_count: int = Field(ge=0)
    data_quality: Literal["complete", "incomplete"]

    @field_validator("interval_start_utc")
    @classmethod
    def _interval_start_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "interval_start_utc")


class WeeklyEnergySummaryRow(BaseModel):
    """Single-row pre-aggregated weekly summary read by the homeowner endpoint.

    Story 10.4 / FR30. The model-validator enforces:
        insufficient_history=False ⇒ all three metric fields are set + ratio in [0, 1]

    This mirrors ``EVOverrideState._failure_reason_iff_terminal`` (Story 10.2). The DB
    ``CHECK`` constraint in migration ``0013`` enforces the same invariant at the
    storage boundary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start_utc: datetime
    window_end_utc: datetime
    peaks_avoided_count: int | None = None
    self_consumption_ratio: float | None = None
    estimated_cost_savings_eur: float | None = None
    data_complete_days_count: int = Field(ge=0)
    insufficient_history: bool
    computed_at: datetime

    @field_validator("window_start_utc", "window_end_utc", "computed_at")
    @classmethod
    def _must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "weekly_energy_summary timestamp")

    @model_validator(mode="after")
    def _terminal_fields_iff_history_sufficient(self) -> WeeklyEnergySummaryRow:
        if not self.insufficient_history:
            if (
                self.peaks_avoided_count is None
                or self.self_consumption_ratio is None
                or self.estimated_cost_savings_eur is None
            ):
                raise ValueError(
                    "insufficient_history=False requires "
                    "peaks_avoided_count, self_consumption_ratio, and "
                    "estimated_cost_savings_eur to all be non-None"
                )
            if not 0.0 <= self.self_consumption_ratio <= 1.0:
                raise ValueError(
                    f"self_consumption_ratio must be in [0.0, 1.0] when "
                    f"insufficient_history=False; got {self.self_consumption_ratio!r}"
                )
        return self
