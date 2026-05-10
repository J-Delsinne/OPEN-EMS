"""Unit tests for ``open_ems.core.constraints`` — Story 9.0b AC3.

Covers field validators, ``frozen=True`` immutability, ``extra="forbid"``,
and UTC timezone-awareness for ``activated_at``.

Note on ``config_version`` lower bound: the story header AC3 reads
``config_version: int, ge=1`` but AC4/AC10/R3/R6 explicitly require seed-from-
Settings to produce an in-memory ``ActiveConstraints`` instance with
``config_version=0`` (DB has no row yet). The single ``ActiveConstraints``
shape covers BOTH the seed and the DB-row case, so the lower bound is
relaxed to ``ge=0``; DB-row values are guaranteed ``>= 1`` by the schema's
``AUTOINCREMENT + UNIQUE`` and the seed value of 0 is in-memory only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from open_ems.core.constraints import ActiveConstraints, ActiveConstraintsInput

_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


def test_active_constraints_input_accepts_valid_values() -> None:
    inp = ActiveConstraintsInput(peak_limit_kw=25.0, battery_reserve_floor_percent=20.0)
    assert inp.peak_limit_kw == 25.0
    assert inp.battery_reserve_floor_percent == 20.0


def test_active_constraints_input_rejects_zero_peak_limit() -> None:
    with pytest.raises(ValidationError):
        ActiveConstraintsInput(peak_limit_kw=0.0, battery_reserve_floor_percent=20.0)


def test_active_constraints_input_rejects_negative_peak_limit() -> None:
    with pytest.raises(ValidationError):
        ActiveConstraintsInput(peak_limit_kw=-1.0, battery_reserve_floor_percent=20.0)


def test_active_constraints_input_rejects_reserve_floor_below_range() -> None:
    with pytest.raises(ValidationError):
        ActiveConstraintsInput(peak_limit_kw=25.0, battery_reserve_floor_percent=-0.1)


def test_active_constraints_input_rejects_reserve_floor_above_range() -> None:
    with pytest.raises(ValidationError):
        ActiveConstraintsInput(peak_limit_kw=25.0, battery_reserve_floor_percent=100.01)


def test_active_constraints_input_accepts_boundary_values() -> None:
    a = ActiveConstraintsInput(peak_limit_kw=0.0001, battery_reserve_floor_percent=0.0)
    b = ActiveConstraintsInput(peak_limit_kw=1000.0, battery_reserve_floor_percent=100.0)
    assert a.battery_reserve_floor_percent == 0.0
    assert b.battery_reserve_floor_percent == 100.0


def test_active_constraints_input_is_frozen() -> None:
    inp = ActiveConstraintsInput(peak_limit_kw=25.0, battery_reserve_floor_percent=20.0)
    with pytest.raises(ValidationError):
        inp.peak_limit_kw = 30.0  # type: ignore[misc]


def test_active_constraints_input_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ActiveConstraintsInput(  # type: ignore[call-arg]
            peak_limit_kw=25.0,
            battery_reserve_floor_percent=20.0,
            config_version=1,
        )


def test_active_constraints_full_model_constructs() -> None:
    c = ActiveConstraints(
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        config_version=1,
        activated_at=_NOW,
    )
    assert c.config_version == 1
    assert c.activated_at == _NOW


def test_active_constraints_allows_seed_config_version_zero() -> None:
    """Seed-from-Settings path produces config_version=0 (AC4); model must accept it."""
    c = ActiveConstraints(
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        config_version=0,
        activated_at=_NOW,
    )
    assert c.config_version == 0


def test_active_constraints_rejects_negative_config_version() -> None:
    with pytest.raises(ValidationError):
        ActiveConstraints(
            peak_limit_kw=25.0,
            battery_reserve_floor_percent=20.0,
            config_version=-1,
            activated_at=_NOW,
        )


def test_active_constraints_is_frozen() -> None:
    c = ActiveConstraints(
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        config_version=1,
        activated_at=_NOW,
    )
    with pytest.raises(ValidationError):
        c.peak_limit_kw = 30.0  # type: ignore[misc]


def test_active_constraints_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ActiveConstraints(  # type: ignore[call-arg]
            peak_limit_kw=25.0,
            battery_reserve_floor_percent=20.0,
            config_version=1,
            activated_at=_NOW,
            actor="installer",
        )


def test_active_constraints_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        ActiveConstraints(
            peak_limit_kw=25.0,
            battery_reserve_floor_percent=20.0,
            config_version=1,
            activated_at=datetime(2026, 5, 10, 12, 0, 0),  # naive
        )


def test_active_constraints_rejects_non_utc_timezone() -> None:
    paris = timezone(timedelta(hours=2))
    with pytest.raises(ValidationError):
        ActiveConstraints(
            peak_limit_kw=25.0,
            battery_reserve_floor_percent=20.0,
            config_version=1,
            activated_at=datetime(2026, 5, 10, 14, 0, 0, tzinfo=paris),
        )
