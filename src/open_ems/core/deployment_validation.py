"""Deployment-validation value types (Story 9.4).

Pure typed values used by:

* ``services/deployment_validation.py`` — the ``DeploymentValidationService``
  that owns the run / acknowledge / handoff lifecycle.
* ``storage/repositories/deployment_validation_repo.py`` — the persistence
  surface that serialises ``DeploymentCheckResult`` rows into the
  ``deployment_validation_results.checks_json`` column.
* ``web/routes/setup.py`` + ``web/templates/installer/`` — the Step 4 UI.

Why these live in ``core/`` (the same placement rationale Story 9.3 used for
``ConstraintCheckResult``): the storage layer encodes them as JSON and the
service layer produces them; if either layer owned the types, the other would
need to import across the storage⇄services boundary. Keeping pure value
models in ``core/`` keeps the import graph clean.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Story 9.4 AC2 — the six checks, in their canonical dispatch / display order.
DeploymentCheckName = Literal[
    "connectivity",
    "role_completeness",
    "capability_strategy",
    "constraint_completeness",
    "constraint_safety_pre_check",
    "control_readiness",
]

# Per-check status. ``timeout`` is distinct from ``fail`` — the UX spec
# (Component #9) classifies TIMEOUT as WARN-class with a connectivity-focused
# corrective action; it is never a structural FAIL.
DeploymentCheckStatus = Literal["pending", "pass", "warn", "fail", "timeout"]

# Persisted overall status. The ``outdated`` UX state is DERIVED at render
# time by comparing ``result.config_version`` to
# ``ActiveConstraintsProvider.get().config_version`` — it is NOT a persisted
# status. This deliberate decision (story dev-notes "Why outdated is a DERIVED
# state") avoids a write-side coupling between Story 9.3's activate path and
# Story 9.4's persistence.
DeploymentOverallStatus = Literal[
    "running",
    "complete-PASS",
    "complete-WARN",
    "complete-FAIL",
]

# Canonical iteration order used by the service for concurrent dispatch and
# by the UI for stable check-row rendering.
DEPLOYMENT_CHECK_NAMES: tuple[DeploymentCheckName, ...] = (
    "connectivity",
    "role_completeness",
    "capability_strategy",
    "constraint_completeness",
    "constraint_safety_pre_check",
    "control_readiness",
)


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be in UTC")
    return value


class DeploymentCheckResult(BaseModel):
    """One readiness-check outcome.

    ``summary`` is the operator-facing one-liner rendered next to the status
    badge. ``corrective_action`` is the WARN/FAIL/TIMEOUT remediation hint;
    it MUST be ``None`` for ``pass`` / ``pending``. ``is_blocking`` is the
    SSE-contract wire flag (UX spec); it is **derived** from ``status`` (True
    iff ``status='fail'``) at construction time — callers do not pass it.

    ``evidence_json`` carries structured per-device data (e.g. the set of
    unreachable device_ids) used by tests and a future detail-expander UI;
    it is NEVER rendered raw to the installer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: DeploymentCheckName
    status: DeploymentCheckStatus
    summary: str
    corrective_action: str | None = None
    # ``is_blocking`` is derived in ``_enforce_status_invariants``; callers
    # may pass it explicitly (the model rejects a value that disagrees with
    # ``status``) but the convention is to leave it unset.
    is_blocking: bool | None = None
    started_at: datetime
    completed_at: datetime | None = None
    evidence_json: dict[str, Any] | None = None

    @field_validator("started_at")
    @classmethod
    def _started_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "started_at")

    @field_validator("completed_at")
    @classmethod
    def _completed_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value, "completed_at")

    @model_validator(mode="after")
    def _enforce_status_invariants(self) -> DeploymentCheckResult:
        if self.status == "pending":
            if self.completed_at is not None:
                raise ValueError("pending DeploymentCheckResult must have completed_at=None")
            if self.corrective_action is not None:
                raise ValueError("pending DeploymentCheckResult must have corrective_action=None")
        else:
            if self.completed_at is None:
                raise ValueError(
                    f"DeploymentCheckResult.status={self.status!r} requires completed_at to be set"
                )
            if not self.summary:
                raise ValueError(
                    f"DeploymentCheckResult.status={self.status!r} requires a non-empty summary"
                )
        if self.status in ("warn", "fail", "timeout"):
            if not self.corrective_action:
                raise ValueError(
                    f"DeploymentCheckResult.status={self.status!r} requires"
                    " a non-empty corrective_action"
                )
        elif self.status == "pass":
            if self.corrective_action is not None:
                raise ValueError("pass DeploymentCheckResult must have corrective_action=None")
        expected_blocking = self.status == "fail"
        if self.is_blocking is None:
            object.__setattr__(self, "is_blocking", expected_blocking)
        elif self.is_blocking != expected_blocking:
            raise ValueError(
                f"DeploymentCheckResult.is_blocking must be {expected_blocking} when"
                f" status={self.status!r} (got {self.is_blocking})"
            )
        return self


class DeploymentValidationResult(BaseModel):
    """Persisted view of one ``deployment_validation_results`` row.

    Single-row contract: at most one persisted row at a time. A new
    ``DeploymentValidationService.run`` DELETEs the prior row + INSERTs a new
    one inside one ``BEGIN IMMEDIATE`` transaction (Story 9.4 AC3 +
    Story 9.0b precedent).

    ``acknowledged_warnings`` is populated by JOINing the
    ``deployment_validation_acks`` table at read time — it is NOT a
    column on this row. The JOIN keeps the (typically small) ack write
    path independent of the (larger, immutable post-completion)
    ``checks_json`` blob.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    started_at: datetime
    completed_at: datetime | None = None
    config_version: Annotated[int, Field(ge=0)]
    overall_status: DeploymentOverallStatus
    checks: tuple[DeploymentCheckResult, ...]
    triggered_by_session_id: str | None = None
    summary_text: str
    acknowledged_warnings: frozenset[DeploymentCheckName] = frozenset()

    @field_validator("started_at")
    @classmethod
    def _started_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "started_at")

    @field_validator("completed_at")
    @classmethod
    def _completed_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value, "completed_at")

    @model_validator(mode="after")
    def _completed_paired_with_terminal_status(self) -> DeploymentValidationResult:
        terminal = self.overall_status != "running"
        if terminal and self.completed_at is None:
            raise ValueError(
                "DeploymentValidationResult.completed_at must be set"
                " when overall_status is not 'running'"
            )
        if not terminal and self.completed_at is not None:
            raise ValueError(
                "DeploymentValidationResult.completed_at must be None"
                " when overall_status is 'running'"
            )
        if not self.summary_text:
            raise ValueError("DeploymentValidationResult.summary_text must be non-empty")
        return self


__all__ = [
    "DEPLOYMENT_CHECK_NAMES",
    "DeploymentCheckName",
    "DeploymentCheckResult",
    "DeploymentCheckStatus",
    "DeploymentOverallStatus",
    "DeploymentValidationResult",
]
