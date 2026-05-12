"""Tests for EVOverrideState model (Story 10.2 AC1, AC2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from open_ems.core import (
    ComponentState,
    DeviceRole,
    EnergyStrategy,
    EVOverrideState,
    GlobalState,
    SystemOperatingMode,
    SystemSnapshot,
)

_NOW_UTC = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


def _override(
    *,
    correlation_id: uuid.UUID | None = None,
    requested_at: datetime = _NOW_UTC,
    expires_at: datetime | None = None,
    dispatch_status: str = "pending",
    failure_reason: str | None = None,
    session_observed_active: bool = False,
) -> EVOverrideState:
    return EVOverrideState(
        correlation_id=correlation_id or uuid.uuid4(),
        requested_at=requested_at,
        expires_at=expires_at or requested_at + timedelta(hours=2),
        dispatch_status=dispatch_status,  # type: ignore[arg-type]
        failure_reason=failure_reason,
        session_observed_active=session_observed_active,
    )


def _empty_snapshot(active_ev_override: EVOverrideState | None = None) -> SystemSnapshot:
    component_states = dict.fromkeys(DeviceRole, ComponentState.unavailable)
    data_age_seconds: dict[DeviceRole, int | None] = dict.fromkeys(DeviceRole)
    return SystemSnapshot(
        sequence_id=0,
        captured_at=_NOW_UTC,
        global_state=GlobalState.degraded,
        operating_mode=SystemOperatingMode.degraded,
        active_strategy=EnergyStrategy.maximize_self_consumption,
        inverter=None,
        battery=None,
        ev_charger=None,
        grid_meter=None,
        component_states=component_states,
        data_age_seconds=data_age_seconds,
        system_clock_status="valid",
        active_ev_override=active_ev_override,
    )


def test_ev_override_state_constructs_with_required_fields() -> None:
    cid = uuid.uuid4()
    override = EVOverrideState(
        correlation_id=cid,
        requested_at=_NOW_UTC,
        expires_at=_NOW_UTC + timedelta(hours=2),
        dispatch_status="pending",
    )
    assert override.correlation_id == cid
    assert override.dispatch_status == "pending"
    assert override.failure_reason is None
    assert override.session_observed_active is False


def test_ev_override_state_rejects_naive_requested_at() -> None:
    with pytest.raises(ValidationError):
        EVOverrideState(
            correlation_id=uuid.uuid4(),
            requested_at=datetime(2026, 5, 12, 12, 0, 0),  # naive
            expires_at=_NOW_UTC + timedelta(hours=2),
            dispatch_status="pending",
        )


def test_ev_override_state_rejects_non_utc_expires_at() -> None:
    with pytest.raises(ValidationError):
        EVOverrideState(
            correlation_id=uuid.uuid4(),
            requested_at=_NOW_UTC,
            expires_at=datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone(timedelta(hours=1))),
            dispatch_status="pending",
        )


@pytest.mark.parametrize("status", ["failed", "timeout", "rejected"])
def test_ev_override_state_terminal_status_requires_failure_reason(status: str) -> None:
    with pytest.raises(ValidationError, match="requires a non-None failure_reason"):
        EVOverrideState(
            correlation_id=uuid.uuid4(),
            requested_at=_NOW_UTC,
            expires_at=_NOW_UTC + timedelta(hours=2),
            dispatch_status=status,  # type: ignore[arg-type]
            failure_reason=None,
        )


def test_ev_override_state_pending_status_rejects_failure_reason() -> None:
    with pytest.raises(ValidationError, match="requires failure_reason=None"):
        EVOverrideState(
            correlation_id=uuid.uuid4(),
            requested_at=_NOW_UTC,
            expires_at=_NOW_UTC + timedelta(hours=2),
            dispatch_status="pending",
            failure_reason="capability_check_failed",
        )


def test_ev_override_state_rejects_expires_at_before_requested_at() -> None:
    """Review patch: temporal-ordering invariant on the model itself."""
    with pytest.raises(ValidationError, match="expires_at must be strictly greater"):
        EVOverrideState(
            correlation_id=uuid.uuid4(),
            requested_at=_NOW_UTC,
            expires_at=_NOW_UTC - timedelta(seconds=1),
            dispatch_status="pending",
        )


def test_ev_override_state_rejects_expires_at_equal_to_requested_at() -> None:
    """Review patch: equality boundary is rejected — expires must be strictly later."""
    with pytest.raises(ValidationError, match="expires_at must be strictly greater"):
        EVOverrideState(
            correlation_id=uuid.uuid4(),
            requested_at=_NOW_UTC,
            expires_at=_NOW_UTC,
            dispatch_status="pending",
        )


def test_ev_override_state_is_frozen() -> None:
    override = _override()
    with pytest.raises(ValidationError):
        override.dispatch_status = "failed"  # type: ignore[misc]


def test_ev_override_state_model_copy_produces_new_instance() -> None:
    override = _override()
    updated = override.model_copy(
        update={"dispatch_status": "failed", "failure_reason": "capability_check_failed"}
    )
    assert updated.dispatch_status == "failed"
    assert updated.failure_reason == "capability_check_failed"
    assert override.dispatch_status == "pending"  # original unchanged
    assert updated.correlation_id == override.correlation_id  # carry-through


def test_system_snapshot_active_ev_override_defaults_to_none() -> None:
    snapshot = _empty_snapshot()
    assert snapshot.active_ev_override is None


def test_system_snapshot_with_active_ev_override_round_trips_via_model_dump() -> None:
    override = _override()
    snapshot = _empty_snapshot(active_ev_override=override)
    dumped = snapshot.model_dump(mode="json")
    assert dumped["active_ev_override"]["correlation_id"] == str(override.correlation_id)
    assert dumped["active_ev_override"]["dispatch_status"] == "pending"
    assert dumped["active_ev_override"]["session_observed_active"] is False
