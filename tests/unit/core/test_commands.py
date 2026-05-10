"""Tests for device command idempotency classification (Story 8.3 AC1, AC10 #12, #13)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from open_ems.core.commands import (
    CommandOrigin,
    CommandResult,
    CommandStatus,
    DeviceCommandBase,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.core.devices import DeviceRole


def test_command_subtypes_idempotency_classification() -> None:
    """AC10 #12: exact classification on every concrete command subtype."""
    assert SetBatteryChargeRateCommand.is_idempotent is True
    assert SetBatteryDischargeRateCommand.is_idempotent is True
    assert SetEVChargingRateCommand.is_idempotent is True
    assert StopEVChargingCommand.is_idempotent is False


def test_is_idempotent_is_classvar_not_instance_field() -> None:
    """AC10 #13: ``is_idempotent`` must be ClassVar, not a Pydantic field.

    A Pydantic field with ``extra="forbid"`` would reject existing call sites
    that don't pass ``is_idempotent=...``. Verify it does NOT appear in
    ``model_fields`` AND that passing it as a kwarg raises ValidationError.
    """
    for cls in (
        SetBatteryChargeRateCommand,
        SetBatteryDischargeRateCommand,
        SetEVChargingRateCommand,
        StopEVChargingCommand,
    ):
        assert "is_idempotent" not in cls.model_fields, (
            f"{cls.__name__}.is_idempotent must be ClassVar, not a Pydantic field"
        )

    with pytest.raises(ValidationError):
        SetBatteryChargeRateCommand(  # type: ignore[call-arg]
            device_id="bat-001",
            device_role=DeviceRole.battery,
            origin=CommandOrigin.decision_engine,
            rate_kw=1.0,
            is_idempotent=False,
        )


def test_is_idempotent_accessible_via_instance() -> None:
    """Sanity check: ClassVar is readable via instance (not just class)."""
    cmd = StopEVChargingCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
    )
    assert cmd.is_idempotent is False


def test_subclass_must_declare_is_idempotent() -> None:
    """A subclass that omits ``is_idempotent`` raises at class creation, not at runtime.

    Inheriting silently from a sibling concrete subclass would let a future
    "stop-like" command auto-classify as idempotent. Force every subtype to choose.
    """
    with pytest.raises(TypeError, match="must declare"):

        class _MissingIdempotency(DeviceCommandBase):
            pass


def test_command_result_rejects_applied_true_with_non_success_status() -> None:
    """Malformed adapter result (applied=True, status=failed) must be rejected at construction."""
    with pytest.raises(ValidationError, match="applied=True requires status=success"):
        CommandResult(
            correlation_id=uuid.uuid4(),
            device_id="bat-001",
            status=CommandStatus.failed,
            applied=True,
            reason="bug",
        )


def test_command_result_rejects_status_success_with_applied_false() -> None:
    """The inverse: status=success with applied=False is also incoherent."""
    with pytest.raises(ValidationError, match="status=success requires applied=True"):
        CommandResult(
            correlation_id=uuid.uuid4(),
            device_id="bat-001",
            status=CommandStatus.success,
            applied=False,
            reason="bug",
        )
