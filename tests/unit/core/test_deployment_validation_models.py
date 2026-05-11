"""Unit tests for ``core/deployment_validation.py`` (Story 9.4 AC2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from open_ems.core.deployment_validation import (
    DEPLOYMENT_CHECK_NAMES,
    DeploymentCheckResult,
    DeploymentValidationResult,
)

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 11, 12, 0, 5, tzinfo=UTC)


def test_check_names_are_canonical_six() -> None:
    assert DEPLOYMENT_CHECK_NAMES == (
        "connectivity",
        "role_completeness",
        "capability_strategy",
        "constraint_completeness",
        "constraint_safety_pre_check",
        "control_readiness",
    )


def test_pending_check_omits_terminal_fields() -> None:
    result = DeploymentCheckResult(
        name="connectivity",
        status="pending",
        summary="",
        started_at=_NOW,
    )
    assert result.completed_at is None
    assert result.corrective_action is None
    assert result.is_blocking is False


def test_pass_check_requires_summary_and_no_corrective_action() -> None:
    with pytest.raises(ValidationError):
        DeploymentCheckResult(
            name="connectivity",
            status="pass",
            summary="",
            started_at=_NOW,
            completed_at=_LATER,
        )
    with pytest.raises(ValidationError):
        DeploymentCheckResult(
            name="connectivity",
            status="pass",
            summary="All devices ok",
            corrective_action="should not be set",
            started_at=_NOW,
            completed_at=_LATER,
        )


def test_warn_requires_corrective_action() -> None:
    with pytest.raises(ValidationError):
        DeploymentCheckResult(
            name="connectivity",
            status="warn",
            summary="One device reduced",
            corrective_action=None,
            started_at=_NOW,
            completed_at=_LATER,
        )


def test_fail_is_blocking_derived_true() -> None:
    result = DeploymentCheckResult(
        name="connectivity",
        status="fail",
        summary="Unreachable",
        corrective_action="Reconnect",
        started_at=_NOW,
        completed_at=_LATER,
    )
    assert result.is_blocking is True
    # explicit False on a FAIL is a contract violation
    with pytest.raises(ValidationError):
        DeploymentCheckResult(
            name="connectivity",
            status="fail",
            summary="Unreachable",
            corrective_action="Reconnect",
            is_blocking=False,
            started_at=_NOW,
            completed_at=_LATER,
        )


def test_timeout_is_warn_class_not_blocking() -> None:
    result = DeploymentCheckResult(
        name="connectivity",
        status="timeout",
        summary="Probe did not respond",
        corrective_action="Retry",
        started_at=_NOW,
        completed_at=_LATER,
    )
    assert result.is_blocking is False


def test_pending_is_blocking_derived_false() -> None:
    result = DeploymentCheckResult(
        name="connectivity",
        status="pending",
        summary="",
        started_at=_NOW,
    )
    assert result.is_blocking is False


def test_terminal_status_requires_completed_at() -> None:
    with pytest.raises(ValidationError):
        DeploymentCheckResult(
            name="connectivity",
            status="pass",
            summary="ok",
            started_at=_NOW,
            completed_at=None,
        )


def test_timestamps_must_be_utc() -> None:
    naive = datetime(2026, 5, 11, 12, 0, 0)
    with pytest.raises(ValidationError):
        DeploymentCheckResult(
            name="connectivity",
            status="pending",
            summary="",
            started_at=naive,  # type: ignore[arg-type]
        )


def test_validation_result_running_requires_no_completed_at() -> None:
    with pytest.raises(ValidationError):
        DeploymentValidationResult(
            id=1,
            started_at=_NOW,
            completed_at=_LATER,
            config_version=0,
            overall_status="running",
            checks=(),
            summary_text="x",
        )


def test_validation_result_terminal_requires_completed_at() -> None:
    with pytest.raises(ValidationError):
        DeploymentValidationResult(
            id=1,
            started_at=_NOW,
            completed_at=None,
            config_version=0,
            overall_status="complete-PASS",
            checks=(),
            summary_text="x",
        )


def test_validation_result_summary_text_non_empty() -> None:
    with pytest.raises(ValidationError):
        DeploymentValidationResult(
            id=1,
            started_at=_NOW,
            completed_at=None,
            config_version=0,
            overall_status="running",
            checks=(),
            summary_text="",
        )
