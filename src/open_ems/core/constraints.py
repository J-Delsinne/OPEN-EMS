"""Active-constraints typed models — Story 9.0b + 9.3.

The runtime values for the safety-critical site constraints
(``peak_limit_kw``, ``battery_reserve_floor_percent``) are owned by
``ActiveConstraintsProvider`` (``open_ems.services.active_constraints``) and
read on the hot path by ``PolicyGuard`` and ``ControlLoop``. Story 9.3 adds
the optional EV-charging-window preference (``ev_charging_window_start`` /
``ev_charging_window_end``); it is stored on ``active_constraints`` so the
single-``config_version``-per-activation invariant covers it too. The window
is paired-NULL (either both fields set or both NULL) and uses an ``HH:MM``
24-hour string representation. The models below are the wire/storage shape —
pure typed values with no I/O — and live under ``core/`` so they can be
imported from both ``engine/`` and ``services/`` without violating the
engine→core import boundary.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Story 9.3: validation-check / report value types are kept in ``core/``
# (alongside ``ActiveConstraints``) so the storage layer (which persists the
# JSON-encoded report on the draft row) and the services layer (which
# produces and consumes the report) can both import them without one
# layer reaching across into the other.
ConstraintCheckName = Literal["schema", "capability_strategy", "safety_pre_check"]
ConstraintCheckStatus = Literal["pass", "warn", "fail"]
ConstraintOverallStatus = Literal["valid", "failed"]
ConstraintDraftValidationStatus = Literal["pending", "valid", "failed"]

# 24-hour HH:MM. 00:00–23:59. Used for EV charging window endpoints.
_HH_MM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_hh_mm(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not _HH_MM_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a 24-hour HH:MM string (got {value!r})")
    return value


class ActiveConstraintsInput(BaseModel):
    """Input shape for ``ConfigRepo.activate()``.

    ``config_version`` and ``activated_at`` are server-assigned; callers must
    not provide them. ``ev_charging_window_*`` are optional and paired-NULL.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    peak_limit_kw: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    battery_reserve_floor_percent: Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]
    ev_charging_window_start: str | None = None
    ev_charging_window_end: str | None = None

    @field_validator("ev_charging_window_start")
    @classmethod
    def _ev_window_start_format(cls, value: str | None) -> str | None:
        return _validate_hh_mm(value, "ev_charging_window_start")

    @field_validator("ev_charging_window_end")
    @classmethod
    def _ev_window_end_format(cls, value: str | None) -> str | None:
        return _validate_hh_mm(value, "ev_charging_window_end")

    @model_validator(mode="after")
    def _ev_window_paired(self) -> ActiveConstraintsInput:
        if (self.ev_charging_window_start is None) != (self.ev_charging_window_end is None):
            raise ValueError(
                "ev_charging_window_start and ev_charging_window_end"
                " must both be set or both be NULL"
            )
        return self


class ActiveConstraints(BaseModel):
    """The currently-active site safety constraints.

    Returned by ``ConfigRepo.get_active()`` and cached in
    ``ActiveConstraintsProvider``. Treated as an immutable snapshot — readers
    receive whichever instance is current at the moment of ``provider.get()``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    peak_limit_kw: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    battery_reserve_floor_percent: Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]
    ev_charging_window_start: str | None = None
    ev_charging_window_end: str | None = None
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

    @field_validator("ev_charging_window_start")
    @classmethod
    def _ev_window_start_format(cls, value: str | None) -> str | None:
        return _validate_hh_mm(value, "ev_charging_window_start")

    @field_validator("ev_charging_window_end")
    @classmethod
    def _ev_window_end_format(cls, value: str | None) -> str | None:
        return _validate_hh_mm(value, "ev_charging_window_end")

    @model_validator(mode="after")
    def _ev_window_paired(self) -> ActiveConstraints:
        if (self.ev_charging_window_start is None) != (self.ev_charging_window_end is None):
            raise ValueError(
                "ev_charging_window_start and ev_charging_window_end"
                " must both be set or both be NULL"
            )
        return self


class ConstraintCheckResult(BaseModel):
    """One validation-check outcome (Story 9.3 AC7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ConstraintCheckName
    status: ConstraintCheckStatus
    field: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def _pass_must_have_no_message(self) -> ConstraintCheckResult:
        if self.status == "pass" and self.message is not None:
            raise ValueError("ConstraintCheckResult with status='pass' must have message=None")
        return self


class ConstraintValidationReport(BaseModel):
    """Outcome of one ``ConstraintsService.validate_draft`` call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overall_status: ConstraintOverallStatus
    checks: tuple[ConstraintCheckResult, ...]


class ConstraintDraftInput(BaseModel):
    """Validated input for ``ConstraintsService.upsert_draft``.

    Mirrors ``ActiveConstraintsInput`` field-for-field. Carrying a separate
    type lets routes parse form data into a frozen value model before the
    service runs schema validation, and lets tests construct fixtures without
    going through ``ActiveConstraintsInput``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    peak_limit_kw: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    battery_reserve_floor_percent: Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]
    ev_charging_window_start: str | None = None
    ev_charging_window_end: str | None = None

    @field_validator("ev_charging_window_start")
    @classmethod
    def _ev_window_start_format(cls, value: str | None) -> str | None:
        return _validate_hh_mm(value, "ev_charging_window_start")

    @field_validator("ev_charging_window_end")
    @classmethod
    def _ev_window_end_format(cls, value: str | None) -> str | None:
        return _validate_hh_mm(value, "ev_charging_window_end")

    @model_validator(mode="after")
    def _ev_window_paired(self) -> ConstraintDraftInput:
        if (self.ev_charging_window_start is None) != (self.ev_charging_window_end is None):
            raise ValueError(
                "ev_charging_window_start and ev_charging_window_end"
                " must both be set or both be NULL"
            )
        return self


class ConstraintActivationResult(BaseModel):
    """Successful return from ``ConstraintsService.activate_draft``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_version: Annotated[int, Field(gt=0)]
    activated_at: datetime
    report: ConstraintValidationReport

    @field_validator("activated_at")
    @classmethod
    def _activated_at_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("activated_at must be timezone-aware UTC")
        if value.utcoffset() != timedelta(0):
            raise ValueError("activated_at must be in UTC")
        return value
