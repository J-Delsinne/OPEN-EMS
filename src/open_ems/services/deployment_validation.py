"""``DeploymentValidationService`` — single evaluator for Story 9.4 Step 4.

The service owns the full validation lifecycle: ``run()`` dispatches the
six readiness checks concurrently with per-check timeouts, persists
progressive ``checks_json`` so a concurrent poller sees partial results,
finalises the row with the overall verdict, and exposes the ack / revoke /
handoff operations the Step 4 routes call.

Design pillars:

* **Single evaluator** (Epic 7 retro). All callers — Step 4 GET handler,
  POST /run, GET /poll, POST /handoff, future Story 9.5 — read the
  persisted result through this one service surface.
* **Safe-probe invariant** (user guardrail #2). The connectivity and
  control-readiness checks delegate to ``ProtocolAdapterFactory.probe()``
  which never issues ``send_command``.
* **Derived "outdated"** (user guardrail #3). The persisted result row
  has NO ``is_outdated`` column; the derived view compares
  ``result.config_version`` to ``provider.get().config_version`` at read
  time. A constraint activation never triggers a write here.
* **Ack scoped per-result-per-check** (user guardrail #4). Ack rows are
  CASCADE-bound to the parent result row; a fresh ``run()`` clears every
  prior ack via the DELETE-then-INSERT transaction in
  ``DeploymentValidationResultRepo.start_run_locked``.
* **HTMX polling** (user guardrail #5). No SSE — the route handler
  exposes a ``GET /poll`` endpoint that re-renders the same fragment the
  POST /run response returns.
* **Reuse safety pre-check** (user guardrail #6). Check 5 calls
  ``ConstraintsService.evaluate_safety_pre_check`` — the public wrapper
  around the existing 9.3 helper — never reimplementing the rule set.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from open_ems.adapters.capabilities import get_profile
from open_ems.core.constraints import ActiveConstraints
from open_ems.core.deployment_validation import (
    DEPLOYMENT_CHECK_NAMES,
    DeploymentCheckName,
    DeploymentCheckResult,
    DeploymentOverallStatus,
    DeploymentValidationResult,
)
from open_ems.core.devices import (
    CapabilityStatus,
    DeviceCapabilityProfile,
    DeviceRole,
    WriteCapability,
)
from open_ems.core.state import SystemSnapshot
from open_ems.core.state_store import StateStore
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.services.audit_log import ObservabilityService
from open_ems.services.constraints import ConstraintsService
from open_ems.services.protocol_adapter_factory import (
    ProbeOutcome,
    ProtocolAdapterFactory,
)
from open_ems.storage.database import get_write_lock
from open_ems.storage.repositories.deployment_validation_repo import (
    DeploymentValidationResultRepo,
)
from open_ems.storage.repositories.device_repo import DeviceRegistryEntry, DeviceRepo
from open_ems.storage.repositories.wizard_state_repo import WizardStateRepo

logger = structlog.get_logger(__name__)


# Per-check overall deadline. The factory probe inside each check has its
# own (smaller) per-device timeout; this is the upper bound for the whole
# check including any per-device fan-out.
_DEFAULT_CHECK_TIMEOUT_SECONDS: float = 15.0
# Per-device probe timeout inside the connectivity + control_readiness
# checks. Sized below the check timeout so a single slow device cannot
# starve sibling probes on the same check.
_DEFAULT_DEVICE_PROBE_TIMEOUT_SECONDS: float = 5.0


# Roles considered "controllable" by control_readiness. Inverters are
# read-only in v1 (Story 9.0 AC3); grid meters are read-only by protocol.
_CONTROLLABLE_ROLES: frozenset[DeviceRole] = frozenset({DeviceRole.battery, DeviceRole.ev_charger})

# Required write capabilities per controllable role. Used by
# control_readiness to determine whether a reachable device can actually
# be commanded for its assigned role.
_REQUIRED_WRITE_CAPS_BY_ROLE: dict[DeviceRole, frozenset[WriteCapability]] = {
    DeviceRole.battery: frozenset(
        {WriteCapability.set_charge_rate, WriteCapability.set_discharge_rate}
    ),
    DeviceRole.ev_charger: frozenset({WriteCapability.set_ev_charge_current}),
}


# ---------------------------------------------------------------------------
# Public view + exceptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeploymentValidationView:
    """Render-time projection used by Step 4 GET + /poll.

    ``is_outdated`` is derived here (NOT persisted) per user guardrail #3:
    the comparison is ``result.config_version != provider.config_version``
    AND ``result.overall_status != 'running'`` (a running result is
    inherently "current to its own dispatch instant"; outdated only
    applies once it has settled to a terminal state).
    """

    result: DeploymentValidationResult | None
    current_config_version: int | None
    is_outdated: bool


class NoCurrentResultError(Exception):
    """Raised by ack / handoff paths when no validation result is persisted."""


class NotAckEligibleError(Exception):
    """Raised when ack is attempted on a non-WARN overall state.

    Carries the structured ``reason`` per AC8's exact-match contract.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CheckNotWarnableError(Exception):
    """Raised when ack targets a check that is not WARN/TIMEOUT.

    Carries the structured ``reason`` per AC8's exact-match contract.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotHandoffEligibleError(Exception):
    """Raised when the handoff gate rejects the request.

    Carries the structured ``reason`` per AC8's exact-match contract.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DeploymentValidationService:
    def __init__(
        self,
        *,
        validation_repo: DeploymentValidationResultRepo,
        device_repo: DeviceRepo,
        wizard_state_repo: WizardStateRepo,
        active_constraints_provider: ActiveConstraintsProvider,
        constraints_service: ConstraintsService,
        protocol_adapter_factory: ProtocolAdapterFactory,
        state_store: StateStore,
        observability: ObservabilityService,
        check_timeout_seconds: float = _DEFAULT_CHECK_TIMEOUT_SECONDS,
        device_probe_timeout_seconds: float = _DEFAULT_DEVICE_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self._repo = validation_repo
        self._device_repo = device_repo
        self._wizard_repo = wizard_state_repo
        self._provider = active_constraints_provider
        self._constraints = constraints_service
        self._factory = protocol_adapter_factory
        self._state_store = state_store
        self._observability = observability
        self._check_timeout_s = check_timeout_seconds
        self._device_probe_timeout_s = device_probe_timeout_seconds
        # D1 — single per-service lock serialises concurrent `run()` invocations.
        # Step-4 validation is site-wide single-row state; parallel runs offer
        # no value and the second-run race would orphan the first run's row.
        self._run_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    async def get_current_view(self) -> DeploymentValidationView:
        """Read-only render-time helper. Returns the persisted result and the
        derived ``is_outdated`` flag.

        Per user guardrail #3, outdated detection is a derived read-time
        comparison — never a persisted column — so a constraint activation
        landing between runs never has to mutate this row.
        """
        result = await self._repo.get_current()
        current_version = self._current_config_version_safe()
        is_outdated = (
            result is not None
            and result.overall_status != "running"
            and current_version is not None
            and result.config_version != current_version
        )
        return DeploymentValidationView(
            result=result,
            current_config_version=current_version,
            is_outdated=is_outdated,
        )

    async def run(
        self,
        *,
        triggered_by_session_id: str,
        now: datetime,
    ) -> DeploymentValidationResult:
        """Execute one full validation run.

        Flow:
            1. Snapshot ``config_version`` from the provider AT RUN START.
               Per user guardrail #3, the persisted ``result.config_version``
               IS this snapshotted value — every subsequent read computes
               ``is_outdated`` against the current provider state.
            2. Under the write lock: DELETE prior row (CASCADE clears acks),
               INSERT a new ``running`` row. Atomicity provided by the repo's
               ``BEGIN IMMEDIATE`` transaction.
            3. Release the lock; dispatch six checks concurrently via
               ``asyncio.gather``. Each check is wrapped in
               ``asyncio.wait_for(timeout=check_timeout_s)``; a per-check
               timeout resolves as ``status='timeout'`` — NOT ``fail``.
            4. As each check resolves, re-acquire the lock briefly to persist
               the progressive ``checks_json`` so a concurrent poller sees it.
            5. Compute the overall verdict and finalize.

        Cancellation: the outer ``try/finally`` always calls
        ``finalize_run_locked`` if the row was inserted but is still in
        ``running`` state — no row is ever left running beyond the lifetime
        of the task that created it.
        """
        # P23 — strict UTC check (the model uses _require_utc; mirror that
        # contract here so callers cannot smuggle a local-tz datetime past the
        # service-level guard).
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now must be timezone-aware UTC")

        # D1 — serialize concurrent run() invocations. The second caller waits
        # for the first to finish, then performs its own run; this is the
        # site-wide single-row contract working as intended (no parallel runs
        # destroying each other's persisted rows).
        async with self._run_lock:
            config_version = self._snapshot_config_version_or_zero()
            # P21 + D4 — capture StateStore snapshot ONCE at run start. The
            # safety pre-check is then evaluated against this frozen snapshot
            # rather than re-reading StateStore mid-run, which previously
            # surfaced spurious WARNs when state changed during the run.
            state_snapshot = self._state_store.get_snapshot()
            result_id: int | None = None
            try:
                async with get_write_lock():
                    result_id = await self._repo.start_run_locked(
                        started_at=now,
                        config_version=config_version,
                        triggered_by_session_id=triggered_by_session_id,
                    )
                logger.info(
                    "deployment_validation_started",
                    component="installer_setup",
                    triggered_by_session_id=triggered_by_session_id,
                    config_version=config_version,
                    result_id=result_id,
                )

                checks = await self._run_checks(
                    result_id=result_id,
                    now=now,
                    state_snapshot=state_snapshot,
                )
                overall_status = _derive_overall_status(checks)
                summary_text = _summary_text(overall_status)
                completed_at = datetime.now(UTC)

                async with get_write_lock():
                    await self._repo.finalize_run_locked(
                        result_id=result_id,
                        overall_status=overall_status,
                        summary_text=summary_text,
                        checks=checks,
                        completed_at=completed_at,
                    )
                logger.info(
                    "deployment_validation_finalized",
                    component="installer_setup",
                    result_id=result_id,
                    overall_status=overall_status,
                    check_count=len(checks),
                )
            except asyncio.CancelledError:
                # Best-effort terminal-state heal on cancellation.
                if result_id is not None:
                    await self._best_effort_finalize_cancelled(
                        result_id,
                        summary_text="Validation cancelled before completion.",
                    )
                logger.warning(
                    "deployment_validation_cancelled",
                    component="installer_setup",
                    result_id=result_id,
                )
                raise
            except Exception:
                # P1 — non-cancellation exception path. Without this, a check
                # method raising (e.g. evaluate_safety_pre_check propagating)
                # would leave the row in 'running' forever. Heal to
                # complete-FAIL so the UI never polls indefinitely.
                if result_id is not None:
                    await self._best_effort_finalize_cancelled(
                        result_id,
                        summary_text="Validation failed: internal error.",
                    )
                logger.error(
                    "deployment_validation_failed",
                    component="installer_setup",
                    result_id=result_id,
                    exc_info=True,
                )
                raise

            final = await self._repo.get_current()
            if final is None:
                # Defensive — would mean a concurrent run replaced our row.
                # Cannot happen with the run lock in place, but kept for
                # restart hydration safety.
                raise RuntimeError(
                    "validation result vanished between finalize and read"
                )
            return final

    async def acknowledge_warning(
        self,
        *,
        check_name: DeploymentCheckName,
        acknowledged_by_session_id: str,
        now: datetime,
    ) -> DeploymentValidationResult:
        """Record an ack for ``check_name`` on the current persisted result.

        Per user guardrail #4, the ack is scoped to (this result row id, this
        check name) — the underlying repo enforces ``UNIQUE(validation_result_id,
        check_name)`` and CASCADE-clears acks whenever a new run replaces the
        result row.

        Contract reasons (exact-match per AC11):
            - ``not_ack_eligible: status=<overall> check=<name> status=<check>``
            - ``CheckNotWarnableError`` for non-WARN/TIMEOUT checks
            - ``NoCurrentResultError`` for never_run.
        """
        async with get_write_lock():
            current = await self._repo.get_current()
            if current is None:
                raise NoCurrentResultError("no validation result to acknowledge")
            check = _check_by_name(current, check_name)
            # P9 — AC11 exact-match contract: the rejection reason always
            # includes both `status=<overall>` AND `status=<check>` so HTTP
            # clients can match on a single canonical shape. When the check
            # is missing from the result, surface it as `status=missing`
            # (still per the same shape) rather than introducing a
            # `check_not_in_result:` variant the spec does not define.
            check_status = check.status if check is not None else "missing"
            if current.overall_status != "complete-WARN":
                raise NotAckEligibleError(
                    f"not_ack_eligible: status={current.overall_status}"
                    f" check={check_name} status={check_status}"
                )
            if check is None:
                raise CheckNotWarnableError(
                    f"not_ack_eligible: status={current.overall_status}"
                    f" check={check_name} status={check_status}"
                )
            if check.status not in ("warn", "timeout"):
                raise CheckNotWarnableError(
                    f"not_ack_eligible: status={current.overall_status}"
                    f" check={check_name} status={check.status}"
                )
            await self._repo.record_acknowledgment_locked(
                result_id=current.id,
                check_name=check_name,
                acknowledged_at=now,
                acknowledged_by_session_id=acknowledged_by_session_id,
            )
        logger.info(
            "deployment_validation_acknowledged",
            component="installer_setup",
            result_id=current.id,
            check_name=check_name,
            acknowledged_by_session_id=acknowledged_by_session_id,
        )
        refreshed = await self._repo.get_current()
        assert refreshed is not None  # we just held the lock
        return refreshed

    async def revoke_warning(
        self,
        *,
        check_name: DeploymentCheckName,
    ) -> DeploymentValidationResult:
        """Symmetric to ``acknowledge_warning``.

        Always succeeds if the result still exists (idempotent on already-revoked).
        """
        async with get_write_lock():
            current = await self._repo.get_current()
            if current is None:
                raise NoCurrentResultError("no validation result to revoke ack on")
            await self._repo.revoke_acknowledgment_locked(
                result_id=current.id, check_name=check_name
            )
        logger.info(
            "deployment_validation_revoked",
            component="installer_setup",
            result_id=current.id,
            check_name=check_name,
        )
        refreshed = await self._repo.get_current()
        assert refreshed is not None
        return refreshed

    async def mark_step_4_complete(
        self,
        *,
        session_id: str,
        now: datetime,
    ) -> int:
        """Idempotently mark ``wizard_state.step_4_complete=1`` with the
        validation's config_version.

        Re-evaluates the full handoff gate inside the write lock. Raises
        ``NotHandoffEligibleError`` with one of the exact-match reasons:

            - ``handoff_not_eligible: never_run``
            - ``handoff_not_eligible: running``
            - ``handoff_not_eligible: complete-FAIL``
            - ``handoff_not_eligible: outdated``
            - ``handoff_not_eligible: complete-WARN not fully acknowledged``

        Returns the stored ``step_4_completed_config_version`` so the route
        handler can echo it in the success response.
        """
        async with get_write_lock():
            current = await self._repo.get_current()
            if current is None:
                raise NotHandoffEligibleError("handoff_not_eligible: never_run")
            if current.overall_status == "running":
                raise NotHandoffEligibleError("handoff_not_eligible: running")
            if current.overall_status == "complete-FAIL":
                raise NotHandoffEligibleError("handoff_not_eligible: complete-FAIL")
            current_version = self._current_config_version_safe()
            # P24 — distinguish "provider not yet hydrated" from "result is
            # stale". The previous code reported both as `outdated`, which
            # mis-classified a restart-before-hydration race.
            if current_version is None:
                raise NotHandoffEligibleError(
                    "handoff_not_eligible: provider_unavailable"
                )
            if current.config_version != current_version:
                raise NotHandoffEligibleError("handoff_not_eligible: outdated")
            if current.overall_status == "complete-WARN":
                required = _warn_check_names(current)
                missing = required - current.acknowledged_warnings
                if missing:
                    raise NotHandoffEligibleError(
                        "handoff_not_eligible: complete-WARN not fully acknowledged"
                    )
            # P28 — wrap the wizard_state UPDATE so a CASCADE-deleted session
            # row (rowcount==0 → ValueError) surfaces as a structured 400 with
            # the canonical handoff_not_eligible: session_gone reason rather
            # than a raw 500.
            try:
                await self._wizard_repo.set_step_4_complete_locked(
                    session_id,
                    completed_config_version=current.config_version,
                    now=now,
                )
            except ValueError as exc:
                raise NotHandoffEligibleError(
                    "handoff_not_eligible: session_gone"
                ) from exc
            # P20 — read the persisted value back so idempotent re-handoff
            # returns what's actually stored (the wizard helper's CASE WHEN
            # branch preserves the prior `step_4_completed_config_version` on
            # re-handoff; the route must echo what's in the DB, not the
            # candidate value the service tried to set).
            refreshed_wizard = await self._wizard_repo.get(session_id)
        if refreshed_wizard is None or refreshed_wizard.step_4_completed_config_version is None:
            # Defensive — should not happen because set_step_4_complete_locked
            # succeeded above, but covers a hostile CASCADE between write and
            # read.
            stored_version = current.config_version
        else:
            stored_version = refreshed_wizard.step_4_completed_config_version
        logger.info(
            "step_4_completed",
            component="installer_setup",
            session_id=session_id,
            config_version=stored_version,
            overall_status=current.overall_status,
            acknowledged_warnings=sorted(current.acknowledged_warnings),
        )
        return stored_version

    # ------------------------------------------------------------------
    # Internal — run dispatch
    # ------------------------------------------------------------------

    async def _run_checks(
        self,
        *,
        result_id: int,
        now: datetime,
        state_snapshot: SystemSnapshot,
    ) -> tuple[DeploymentCheckResult, ...]:
        """Dispatch the six checks concurrently, persisting progress as each
        resolves. Returns the final tuple in canonical ``DEPLOYMENT_CHECK_NAMES``
        order.

        Snapshot inputs are captured at ``run()`` entry and passed in:
        ``devices`` (read here from ``device_repo.list_all()``), ``active``
        (read here from the provider), and ``state_snapshot`` (passed by
        caller). Mid-run state changes do not surface as spurious WARNs
        because the safety-pre-check is evaluated against the captured
        snapshot, not the live store.
        """
        try:
            devices = tuple(await self._device_repo.list_all())
        except (OSError, RuntimeError, ValueError):
            # P3 — narrow to expected I/O / state failures from the repo. Any
            # other exception is a programmer error and should not be silently
            # downgraded to "no devices".
            logger.error(
                "deployment_validation_device_repo_read_failed",
                component="installer_setup",
                result_id=result_id,
                exc_info=True,
            )
            devices = ()
        try:
            active = self._provider.get()
        except RuntimeError:
            active = None

        # Build one task per check. Each method returns a DeploymentCheckResult
        # — including for timeout cases (the method wraps asyncio.wait_for
        # internally so the outer gather never sees TimeoutError).
        tasks: dict[DeploymentCheckName, asyncio.Task[DeploymentCheckResult]] = {
            "connectivity": asyncio.create_task(
                self._check_connectivity(devices, now=now),
                name="validation_connectivity",
            ),
            "role_completeness": asyncio.create_task(
                self._check_role_completeness(devices, now=now),
                name="validation_role_completeness",
            ),
            "capability_strategy": asyncio.create_task(
                self._check_capability_strategy(devices, active, now=now),
                name="validation_capability_strategy",
            ),
            "constraint_completeness": asyncio.create_task(
                self._check_constraint_completeness(active, now=now),
                name="validation_constraint_completeness",
            ),
            "constraint_safety_pre_check": asyncio.create_task(
                self._check_constraint_safety_pre(
                    active, state_snapshot=state_snapshot, now=now
                ),
                name="validation_constraint_safety_pre",
            ),
            "control_readiness": asyncio.create_task(
                self._check_control_readiness(devices, now=now),
                name="validation_control_readiness",
            ),
        }

        accumulated: dict[DeploymentCheckName, DeploymentCheckResult] = {}
        pending = set(tasks.values())
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    check = task.result()
                    accumulated[check.name] = check
                    progressive = tuple(
                        accumulated[name] for name in DEPLOYMENT_CHECK_NAMES if name in accumulated
                    )
                    try:
                        async with get_write_lock():
                            await self._repo.update_check_progress_locked(
                                result_id=result_id, checks=progressive
                            )
                    except Exception:  # noqa: BLE001 — progress write is best-effort
                        logger.warning(
                            "deployment_validation_progress_write_failed",
                            component="installer_setup",
                            result_id=result_id,
                            exc_info=True,
                        )
        except asyncio.CancelledError:
            # Cancel all in-flight check tasks so we don't leak adapter
            # connections beyond the run.
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise

        return tuple(accumulated[name] for name in DEPLOYMENT_CHECK_NAMES)

    # ------------------------------------------------------------------
    # Internal — six checks
    # ------------------------------------------------------------------

    async def _check_connectivity(
        self,
        devices: tuple[DeviceRegistryEntry, ...],
        *,
        now: datetime,
    ) -> DeploymentCheckResult:
        """Probe every registered device; classify reachable / reduced /
        unreachable / timeout (AC5 check 1)."""
        started_at = now
        outcomes = await self._gather_probes(devices)
        completed_at = datetime.now(UTC)
        if not outcomes:
            return DeploymentCheckResult(
                name="connectivity",
                status="warn",
                summary="No devices are registered to probe.",
                corrective_action=(
                    "Add or discover at least one device in Step 1 before re-running validation."
                ),
                started_at=started_at,
                completed_at=completed_at,
                # P6 — keep evidence_json schema consistent across both
                # branches; the populated branch includes `timed_out`.
                evidence_json={
                    "reachable": [],
                    "reduced": [],
                    "unreachable": [],
                    "timed_out": [],
                },
            )
        reachable = [o.device_id for o in outcomes if o.reachable]
        reduced = [
            o.device_id
            for o in outcomes
            if o.reachable and o.capability_status is CapabilityStatus.reduced
        ]
        unreachable = [o.device_id for o in outcomes if not o.reachable and not o.timed_out]
        timed_out = [o.device_id for o in outcomes if o.timed_out]
        evidence = {
            "reachable": sorted(reachable),
            "reduced": sorted(reduced),
            "unreachable": sorted(unreachable),
            "timed_out": sorted(timed_out),
        }
        if unreachable:
            return DeploymentCheckResult(
                name="connectivity",
                status="fail",
                summary=f"{len(unreachable)} device(s) unreachable.",
                corrective_action=(f"Verify connectivity for: {', '.join(sorted(unreachable))}."),
                started_at=started_at,
                completed_at=completed_at,
                evidence_json=evidence,
            )
        if timed_out:
            return DeploymentCheckResult(
                name="connectivity",
                status="timeout",
                summary=f"{len(timed_out)} device probe(s) did not respond in time.",
                corrective_action=(
                    f"Check connectivity for: {', '.join(sorted(timed_out))}, then retry."
                ),
                started_at=started_at,
                completed_at=completed_at,
                evidence_json=evidence,
            )
        if reduced:
            return DeploymentCheckResult(
                name="connectivity",
                status="warn",
                summary=f"{len(reduced)} device(s) reachable with reduced capability.",
                corrective_action=(
                    f"Review the device firmware or model registry for:"
                    f" {', '.join(sorted(reduced))}."
                ),
                started_at=started_at,
                completed_at=completed_at,
                evidence_json=evidence,
            )
        return DeploymentCheckResult(
            name="connectivity",
            status="pass",
            summary=f"All {len(reachable)} devices responded.",
            started_at=started_at,
            completed_at=completed_at,
            evidence_json=evidence,
        )

    async def _check_role_completeness(
        self,
        devices: tuple[DeviceRegistryEntry, ...],
        *,
        now: datetime,
    ) -> DeploymentCheckResult:
        """Verify ``grid_meter`` assigned + at least one energy source (AC5 check 2)."""
        started_at = now
        assigned = {d.role for d in devices if d.role is not None}
        completed_at = datetime.now(UTC)
        if DeviceRole.grid_meter not in assigned:
            return DeploymentCheckResult(
                name="role_completeness",
                status="fail",
                summary="Grid meter role is not assigned.",
                corrective_action=("Return to Step 2 to assign the grid meter role."),
                started_at=started_at,
                completed_at=completed_at,
                evidence_json={"assigned_roles": sorted(r.value for r in assigned)},
            )
        if DeviceRole.battery not in assigned and DeviceRole.inverter not in assigned:
            return DeploymentCheckResult(
                name="role_completeness",
                status="warn",
                summary="No battery or inverter is assigned — no dispatchable source.",
                corrective_action=(
                    "Assign at least one battery or inverter in Step 2,"
                    " or acknowledge this warning to proceed."
                ),
                started_at=started_at,
                completed_at=completed_at,
                evidence_json={"assigned_roles": sorted(r.value for r in assigned)},
            )
        return DeploymentCheckResult(
            name="role_completeness",
            status="pass",
            summary="All required roles are assigned.",
            started_at=started_at,
            completed_at=completed_at,
            evidence_json={"assigned_roles": sorted(r.value for r in assigned)},
        )

    async def _check_capability_strategy(
        self,
        devices: tuple[DeviceRegistryEntry, ...],
        active: ActiveConstraints | None,
        *,
        now: datetime,
    ) -> DeploymentCheckResult:
        """Verify each configured constraint targets a role that is assigned
        with adequate capability (AC5 check 3)."""
        started_at = now
        completed_at = datetime.now(UTC)
        if active is None:
            return DeploymentCheckResult(
                name="capability_strategy",
                status="warn",
                summary="Active constraints unavailable; capability check skipped.",
                corrective_action=("Re-run validation once the system has finished starting up."),
                started_at=started_at,
                completed_at=completed_at,
            )
        # P7 — full AC5 check 3 coverage. Spec line 235 contracts three
        # pass conditions; the previous implementation tested only two.
        # Build a per-device-id capability profile map so the WARN can
        # surface reduced-profile assigned devices that won't actually be
        # commandable for their constraint surface.
        assigned_by_role: dict[DeviceRole, list[DeviceRegistryEntry]] = {}
        for d in devices:
            if d.role is not None:
                assigned_by_role.setdefault(d.role, []).append(d)
        assigned = set(assigned_by_role.keys())
        gaps: list[str] = []
        # AC5 check 3 condition (a): peak_limit_kw configured but no grid meter.
        if active.peak_limit_kw > 0 and DeviceRole.grid_meter not in assigned:
            gaps.append("peak limit is set but no grid meter is assigned")
        # AC5 check 3 condition (b): battery_reserve_floor_percent > 0 but
        # no battery, OR battery is assigned but its capability profile is
        # REDUCED (does not advertise charge/discharge write capabilities).
        if active.battery_reserve_floor_percent > 0:
            if DeviceRole.battery not in assigned:
                gaps.append("battery_reserve_floor is set but no battery is assigned")
            else:
                for entry in assigned_by_role[DeviceRole.battery]:
                    profile = _capability_profile_for(entry)
                    if profile is None or profile.capability_status is CapabilityStatus.reduced:
                        gaps.append(
                            f"battery '{entry.device_id}' has reduced capability;"
                            " reserve floor enforcement may be limited"
                        )
        # AC5 check 3 condition (c): ev window configured but no ev_charger,
        # or ev_charger present but REDUCED.
        if active.ev_charging_window_start is not None:
            if DeviceRole.ev_charger not in assigned:
                gaps.append("EV charging window is set but no EV charger is assigned")
            else:
                for entry in assigned_by_role[DeviceRole.ev_charger]:
                    profile = _capability_profile_for(entry)
                    if profile is None or profile.capability_status is CapabilityStatus.reduced:
                        gaps.append(
                            f"EV charger '{entry.device_id}' has reduced capability;"
                            " charging window enforcement may be limited"
                        )
        if gaps:
            return DeploymentCheckResult(
                name="capability_strategy",
                status="warn",
                summary=f"{len(gaps)} constraint(s) will have no effect.",
                corrective_action=(
                    "Either assign the missing devices in Step 2 (or replace"
                    " devices with reduced capability) or acknowledge this"
                    " warning to proceed."
                ),
                started_at=started_at,
                completed_at=completed_at,
                evidence_json={"gaps": gaps},
            )
        return DeploymentCheckResult(
            name="capability_strategy",
            status="pass",
            summary="Every constraint maps to an assigned device.",
            started_at=started_at,
            completed_at=completed_at,
        )

    async def _check_constraint_completeness(
        self,
        active: ActiveConstraints | None,
        *,
        now: datetime,
    ) -> DeploymentCheckResult:
        """Defense-in-depth read of the activated constraints (AC5 check 4)."""
        started_at = now
        completed_at = datetime.now(UTC)
        if active is None:
            return DeploymentCheckResult(
                name="constraint_completeness",
                status="fail",
                summary="No active constraints have been loaded.",
                corrective_action="Complete Step 3 (constraint configuration) before validation.",
                started_at=started_at,
                completed_at=completed_at,
            )
        # The Pydantic + DB CHECK layers structurally enforce these invariants.
        # A FAIL here means someone tampered with the DB; defense in depth.
        if active.peak_limit_kw <= 0:
            return DeploymentCheckResult(
                name="constraint_completeness",
                status="fail",
                summary="Peak limit is invalid (must be greater than 0 kW).",
                corrective_action="Return to Step 3 and fix the peak limit value.",
                started_at=started_at,
                completed_at=completed_at,
            )
        if not (0 <= active.battery_reserve_floor_percent <= 100):
            return DeploymentCheckResult(
                name="constraint_completeness",
                status="fail",
                summary="Battery reserve floor is outside the 0–100% range.",
                corrective_action="Return to Step 3 and fix the reserve floor value.",
                started_at=started_at,
                completed_at=completed_at,
            )
        if (active.ev_charging_window_start is None) != (active.ev_charging_window_end is None):
            return DeploymentCheckResult(
                name="constraint_completeness",
                status="fail",
                summary="EV charging window is partially configured.",
                corrective_action=(
                    "Return to Step 3 and either set both EV window endpoints or clear both."
                ),
                started_at=started_at,
                completed_at=completed_at,
            )
        return DeploymentCheckResult(
            name="constraint_completeness",
            status="pass",
            summary=f"Constraints active at config_version {active.config_version}.",
            started_at=started_at,
            completed_at=completed_at,
        )

    async def _check_constraint_safety_pre(
        self,
        active: ActiveConstraints | None,
        *,
        state_snapshot: SystemSnapshot,
        now: datetime,
    ) -> DeploymentCheckResult:
        """Delegate to ``ConstraintsService.evaluate_safety_pre_check`` — the
        public wrapper around the 9.3 helper. Reuse seam per user guardrail #6.

        D4 — the snapshot is captured ONCE at ``run()`` entry and threaded
        through this check, so all six checks observe the same StateStore
        moment. The wrapper signature `(constraints, snapshot)` matches the
        spec contract.
        """
        started_at = now
        completed_at = datetime.now(UTC)
        if active is None:
            # P8 — align messaging with ``_check_constraint_completeness``'s
            # no-active-config path. The completeness check FAILs with
            # "Complete Step 3"; this check WARNs with the same root cause.
            # Use a single canonical reason so the installer is not pulled in
            # two directions.
            return DeploymentCheckResult(
                name="constraint_safety_pre_check",
                status="warn",
                summary="Active constraints unavailable; safety pre-check skipped.",
                corrective_action=(
                    "Complete Step 3 (constraint configuration), or re-run"
                    " validation once the system has finished starting up."
                ),
                started_at=started_at,
                completed_at=completed_at,
            )
        rows = self._constraints.evaluate_safety_pre_check(active, state_snapshot)
        warns = [r for r in rows if r.status == "warn"]
        fails = [r for r in rows if r.status == "fail"]
        if fails:
            messages = [r.message for r in fails if r.message]
            return DeploymentCheckResult(
                name="constraint_safety_pre_check",
                status="fail",
                summary=f"{len(fails)} safety pre-check failure(s) detected.",
                corrective_action=(
                    "Return to Step 3 and adjust the failing constraints"
                    " before re-running validation."
                ),
                started_at=started_at,
                completed_at=completed_at,
                evidence_json={"failures": messages, "warnings": [w.message for w in warns]},
            )
        if warns:
            messages = [r.message for r in warns if r.message]
            return DeploymentCheckResult(
                name="constraint_safety_pre_check",
                status="warn",
                summary=f"{len(warns)} safety pre-check warning(s).",
                corrective_action=(
                    "Acknowledge each warning before handoff, OR adjust constraints in Step 3."
                ),
                started_at=started_at,
                completed_at=completed_at,
                evidence_json={"warnings": messages},
            )
        return DeploymentCheckResult(
            name="constraint_safety_pre_check",
            status="pass",
            summary="Active constraints are safe to enforce against the current device state.",
            started_at=started_at,
            completed_at=completed_at,
        )

    async def _check_control_readiness(
        self,
        devices: tuple[DeviceRegistryEntry, ...],
        *,
        now: datetime,
    ) -> DeploymentCheckResult:
        """For each controllable role, verify probe success AND that the
        capability profile has the required write capabilities (AC5 check 6).

        SAFE-PROBE INVARIANT (user guardrail #2): the probe path is
        ``ProtocolAdapterFactory.probe()`` → ``DiscoveryService.probe_*``
        which are read-only. No ``send_command`` is invoked anywhere.
        """
        started_at = now
        controllable = tuple(d for d in devices if d.role in _CONTROLLABLE_ROLES)
        outcomes = await self._gather_probes(controllable)
        completed_at = datetime.now(UTC)
        if not controllable:
            return DeploymentCheckResult(
                name="control_readiness",
                status="warn",
                summary="No controllable devices assigned.",
                corrective_action=(
                    "Assign a battery or EV charger in Step 2 to enable"
                    " control, or acknowledge this warning to proceed."
                ),
                started_at=started_at,
                completed_at=completed_at,
                evidence_json={"controllable_count": 0},
            )
        unreachable = [o.device_id for o in outcomes if not o.reachable and not o.timed_out]
        timed_out = [o.device_id for o in outcomes if o.timed_out]
        missing_caps: list[str] = []
        for outcome in outcomes:
            if not outcome.reachable or outcome.role is None:
                continue
            required = _REQUIRED_WRITE_CAPS_BY_ROLE.get(outcome.role, frozenset())
            missing = required - outcome.write_capabilities
            if missing:
                missing_caps.append(
                    f"{outcome.device_id}: missing {sorted(c.value for c in missing)}"
                )
        evidence = {
            "controllable_count": len(controllable),
            "unreachable": sorted(unreachable),
            "timed_out": sorted(timed_out),
            "missing_capabilities": missing_caps,
        }
        if unreachable:
            return DeploymentCheckResult(
                name="control_readiness",
                status="fail",
                summary=f"{len(unreachable)} controllable device(s) unreachable.",
                corrective_action=(
                    f"The control pipeline could not reach:"
                    f" {', '.join(sorted(unreachable))}. Verify the connection and re-run."
                ),
                started_at=started_at,
                completed_at=completed_at,
                evidence_json=evidence,
            )
        if timed_out:
            return DeploymentCheckResult(
                name="control_readiness",
                status="timeout",
                summary=f"{len(timed_out)} controllable probe(s) did not respond in time.",
                corrective_action=(
                    f"Verify the connection for: {', '.join(sorted(timed_out))}, then retry."
                ),
                started_at=started_at,
                completed_at=completed_at,
                evidence_json=evidence,
            )
        if missing_caps:
            return DeploymentCheckResult(
                name="control_readiness",
                status="warn",
                summary=(
                    f"{len(missing_caps)} controllable device(s) missing"
                    " required write capabilities."
                ),
                corrective_action=(
                    "Update the device firmware or review the capability"
                    " registry; the listed devices cannot be commanded."
                ),
                started_at=started_at,
                completed_at=completed_at,
                evidence_json=evidence,
            )
        return DeploymentCheckResult(
            name="control_readiness",
            status="pass",
            summary=f"All {len(controllable)} controllable devices are ready.",
            started_at=started_at,
            completed_at=completed_at,
            evidence_json=evidence,
        )

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------

    async def _gather_probes(
        self, devices: tuple[DeviceRegistryEntry, ...]
    ) -> tuple[ProbeOutcome, ...]:
        """Fan out probes concurrently; per-device timeout bounds runtime."""
        if not devices:
            return ()
        probe_tasks = [
            asyncio.create_task(
                self._factory.probe(d, timeout_s=self._device_probe_timeout_s),
                name=f"probe_{d.device_id}",
            )
            for d in devices
        ]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*probe_tasks, return_exceptions=True),
                timeout=self._check_timeout_s,
            )
        except TimeoutError:
            # Cancel any still-running probes and synthesise timeout outcomes
            # for every device that didn't return.
            done_ids: set[str] = set()
            outcomes: list[ProbeOutcome] = []
            for task, device in zip(probe_tasks, devices, strict=True):
                if task.done():
                    res = task.result() if not task.exception() else None
                    if isinstance(res, ProbeOutcome):
                        outcomes.append(res)
                        done_ids.add(device.device_id)
                        continue
                task.cancel()
            await asyncio.gather(*probe_tasks, return_exceptions=True)
            for device in devices:
                if device.device_id in done_ids:
                    continue
                outcomes.append(
                    ProbeOutcome(
                        device_id=device.device_id,
                        role=device.role,
                        protocol=device.protocol,
                        reachable=False,
                        timed_out=True,
                        capability_status=None,
                        write_capabilities=frozenset(),
                        error_reason="check_deadline_exceeded",
                        capability_profile=None,
                    )
                )
            return tuple(outcomes)
        outcomes_out: list[ProbeOutcome] = []
        for gathered, device in zip(results, devices, strict=True):
            if isinstance(gathered, ProbeOutcome):
                outcomes_out.append(gathered)
            else:
                outcomes_out.append(
                    ProbeOutcome(
                        device_id=device.device_id,
                        role=device.role,
                        protocol=device.protocol,
                        reachable=False,
                        timed_out=False,
                        capability_status=None,
                        write_capabilities=frozenset(),
                        error_reason=f"probe_exception:{type(gathered).__name__}",
                        capability_profile=None,
                    )
                )
        return tuple(outcomes_out)

    def _current_config_version_safe(self) -> int | None:
        try:
            return self._provider.get().config_version
        except RuntimeError:
            return None

    def _snapshot_config_version_or_zero(self) -> int:
        version = self._current_config_version_safe()
        return 0 if version is None else version

    async def _best_effort_finalize_cancelled(
        self,
        result_id: int,
        *,
        summary_text: str,
    ) -> None:
        try:
            async with get_write_lock():
                await self._repo.finalize_run_locked(
                    result_id=result_id,
                    overall_status="complete-FAIL",
                    summary_text=summary_text,
                    checks=(),
                    completed_at=datetime.now(UTC),
                )
        except Exception:  # noqa: BLE001 — heal is best-effort
            logger.warning(
                "deployment_validation_cancel_finalize_failed",
                component="installer_setup",
                result_id=result_id,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _derive_overall_status(
    checks: tuple[DeploymentCheckResult, ...],
) -> DeploymentOverallStatus:
    if any(c.status == "fail" for c in checks):
        return "complete-FAIL"
    if any(c.status in ("warn", "timeout") for c in checks):
        return "complete-WARN"
    return "complete-PASS"


def _summary_text(overall_status: DeploymentOverallStatus) -> str:
    """Operator-facing banner text per UX spec Component #10.

    P5 — formerly accepted ``checks`` (unused). Banner copy is keyed on the
    overall status alone, matching the spec UX exactly.
    """
    if overall_status == "complete-PASS":
        return "System is ready. You can complete handoff."
    if overall_status == "complete-WARN":
        return "System is ready with limitations. Review warnings before handoff."
    return "Deployment blocked. Resolve failures before handoff."


def _capability_profile_for(entry: DeviceRegistryEntry) -> DeviceCapabilityProfile | None:
    """Resolve the capability profile for a registered device entry.

    Mirrors the lookup ``ProtocolAdapterFactory._outcome_reachable`` performs.
    Returns ``None`` when the device has no model recorded (treated as
    reduced by the capability-gate contract).
    """
    model = entry.model
    if model is None:
        return None
    return get_profile(
        device_id=entry.device_id,
        model=model,
        firmware_version=entry.firmware_version,
    )


def _check_by_name(
    result: DeploymentValidationResult,
    check_name: DeploymentCheckName,
) -> DeploymentCheckResult | None:
    for c in result.checks:
        if c.name == check_name:
            return c
    return None


def _warn_check_names(
    result: DeploymentValidationResult,
) -> frozenset[DeploymentCheckName]:
    return frozenset(c.name for c in result.checks if c.status in ("warn", "timeout"))


__all__ = [
    "CheckNotWarnableError",
    "DeploymentValidationService",
    "DeploymentValidationView",
    "NoCurrentResultError",
    "NotAckEligibleError",
    "NotHandoffEligibleError",
]
