"""Active-constraints typed models — Story 9.0b.

The runtime values for the two safety-critical site constraints
(``peak_limit_kw`` and ``battery_reserve_floor_percent``) are owned by
``ActiveConstraintsProvider`` (``open_ems.services.active_constraints``) and
read on the hot path by ``PolicyGuard`` and ``ControlLoop``. The models below
are the wire/storage shape — pure typed values with no I/O — and live under
``core/`` so they can be imported from both ``engine/`` and ``services/``
without violating the engine→core import boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ActiveConstraintsInput(BaseModel):
    """Input shape for ``ConfigRepo.activate()`` — only the two value fields.

    ``config_version`` and ``activated_at`` are server-assigned; callers must
    not provide them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    peak_limit_kw: Annotated[float, Field(gt=0.0)]
    battery_reserve_floor_percent: Annotated[float, Field(ge=0.0, le=100.0)]


class ActiveConstraints(BaseModel):
    """The currently-active site safety constraints.

    Returned by ``ConfigRepo.get_active()`` and cached in
    ``ActiveConstraintsProvider``. Treated as an immutable snapshot — readers
    receive whichever instance is current at the moment of ``provider.get()``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    peak_limit_kw: Annotated[float, Field(gt=0.0)]
    battery_reserve_floor_percent: Annotated[float, Field(ge=0.0, le=100.0)]
    config_version: Annotated[int, Field(ge=0)]
    activated_at: datetime

    @field_validator("activated_at")
    @classmethod
    def _activated_at_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("activated_at must be timezone-aware UTC")
        if value.utcoffset() != timedelta(0):
            raise ValueError("activated_at must be in UTC")
        return value
