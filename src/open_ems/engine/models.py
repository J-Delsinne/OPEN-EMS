"""Typed inputs consumed by pure decision-engine rules."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from open_ems.core.state import DeviceSlot, SystemSnapshot


class EvaluationInput(BaseModel):
    """Snapshot-derived input for one decision-engine evaluation cycle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inverter: DeviceSlot
    battery: DeviceSlot
    ev_charger: DeviceSlot
    grid_meter: DeviceSlot

    @model_validator(mode="after")
    def _required_slots_present(self) -> EvaluationInput:
        if self.inverter is None:
            raise ValueError("inverter is a required device slot and must not be None")
        if self.grid_meter is None:
            raise ValueError("grid_meter is a required device slot and must not be None")
        return self

    @classmethod
    def from_snapshot(cls, snapshot: SystemSnapshot) -> EvaluationInput:
        """Build engine input from a StateStore snapshot without retaining the store."""
        return cls(
            inverter=snapshot.inverter,
            battery=snapshot.battery,
            ev_charger=snapshot.ev_charger,
            grid_meter=snapshot.grid_meter,
        )
