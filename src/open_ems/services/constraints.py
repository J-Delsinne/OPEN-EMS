"""``ConstraintsService`` — single evaluator for staged constraint flow (Story 9.3).

Owns the BAD-3 staged validate→activate semantics: per-session draft upsert,
three-check server-side validation (schema → capability_strategy →
safety_pre_check), atomic activation that re-validates inside the write
lock, persists the new ``active_constraints`` row + ``config_audit_log``
rows + ``wizard_state.step_3_*`` advance + ``draft_constraints`` deletion in
one transaction (P13), then refreshes the in-memory provider snapshot.

The service is the single entry point used by the route handlers, the
templates (via the route handler context), and Story 9.4's deployment
validation. Route handlers do not call ``ConfigRepo.activate`` or
``DraftConstraintsRepo.upsert`` directly; everything goes through here so
the rule set has exactly one implementation (single-evaluator principle —
9.7 retro / Epic 7 retro).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

import aiosqlite
import structlog
from pydantic import ValidationError

from open_ems.core.constraints import (
    ActiveConstraints,
    ActiveConstraintsInput,
    ConstraintActivationResult,
    ConstraintCheckResult,
    ConstraintDraftInput,
    ConstraintOverallStatus,
    ConstraintValidationReport,
)
from open_ems.core.devices import BatteryState, DeviceRole, GridMeterState
from open_ems.core.state import SystemSnapshot
from open_ems.core.state_store import StateStore
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.storage.database import get_write_lock
from open_ems.storage.repositories.config_repo import ConfigRepo, NoChangedFieldsError
from open_ems.storage.repositories.device_repo import DeviceRepo
from open_ems.storage.repositories.draft_constraints_repo import (
    ConstraintDraft,
    DraftConstraintsRepo,
)
from open_ems.storage.repositories.wizard_state_repo import WizardStateRepo

logger = structlog.get_logger(__name__)

# AC7 check 3 — a peak limit below this threshold structurally blocks all
# grid imports indefinitely; documented as a deterministic guard, not a
# heuristic.
_PEAK_LIMIT_FAIL_THRESHOLD_KW: float = 0.5


class ProviderNotReadyError(RuntimeError):
    """Raised by ``get_or_default`` when the provider has not yet hydrated.

    Story 9.3 P10 — route handlers map this to a 503 instead of a generic 500
    so observability can distinguish "provider not yet hydrated" from a real
    application error.
    """


@dataclass(frozen=True)
class ConstraintDraftView:
    """Snapshot returned by ``ConstraintsService.get_or_default``.

    ``has_persisted_draft`` distinguishes "default-seeded from provider"
    (False) from "values came from the persisted draft row" (True). The
    template uses the flag to render the correct page hint.

    Story 9.3 P3 — ``raw_peak_limit_kw_text`` / ``raw_battery_reserve_floor_percent_text``
    carry the user's submitted form values verbatim when a parse failure
    occurred, so the form re-render preserves what the user typed (instead
    of substituting a synthetic 0.0). The template prefers the raw text
    when present, falling back to the float formatting otherwise.
    """

    peak_limit_kw: float
    battery_reserve_floor_percent: float
    ev_charging_window_start: str | None
    ev_charging_window_end: str | None
    validation_status: Literal["pending", "valid", "failed"]
    validation_report: ConstraintValidationReport | None
    has_persisted_draft: bool
    raw_peak_limit_kw_text: str | None = None
    raw_battery_reserve_floor_percent_text: str | None = None


class ConstraintActivationError(Exception):
    """Raised by ``activate_draft`` when the lock-bounded gate rejects.

    Carries the structured reason (exact-match contract string per AC11) and
    optionally the fresh ``ConstraintValidationReport`` that triggered the
    rejection. Route handlers translate this into a 400 + the inline error
    banner fragment.
    """

    def __init__(
        self,
        reason: str,
        *,
        report: ConstraintValidationReport | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.report = report


# Story 9.3 P6 — exact-match contract reasons map to operator-facing copy
# rendered by the inline error banners. Reasons not in this map fall through
# to a generic message so the contract surface can evolve without UI breakage.
_REASON_USER_MESSAGES: dict[str, str] = {
    "draft_not_found_for_session": (
        "No draft to validate yet. Edit a field and save the draft before validating."
    ),
    "activate_called_with_no_changed_fields": (
        "No changes to activate. Edit a field or return to Step 2."
    ),
    "constraints_validation_failed": (
        "Validation failed. Review the errors below and edit the draft."
    ),
    "step_3_already_complete": (
        "Constraints have already been activated for this session. Continue to Step 4."
    ),
    # R2P2 — wizard CASCADE race on happy path. The session was deleted between
    # the prerequisite check and the in-transaction wizard UPDATE; surface as a
    # structured rejection rather than a 500.
    "session_gone_during_activate": (
        "Your session ended before activation could complete. Please sign in again and retry."
    ),
    # R2P13 — SQLite lock contention surfaces as a retryable rejection rather
    # than the raw OperationalError 500. The route handler can render this with
    # an HX-Retry-After header in a future hardening pass.
    "activation_busy": (
        "The system is currently busy applying another change. Please try again in a moment."
    ),
}


def reason_to_user_message(reason: str) -> str:
    """Map an exact-match contract reason to operator-facing copy (P6).

    ``step_prerequisites_not_met:…`` carries dynamic arguments so it is
    matched by prefix. Unknown reasons fall through to a stable generic
    message; the raw reason remains available via the route handler's log
    line and the banner's ``data-reason`` attribute.
    """
    if reason in _REASON_USER_MESSAGES:
        return _REASON_USER_MESSAGES[reason]
    if reason.startswith("step_prerequisites_not_met:"):
        return "Complete the earlier setup steps before configuring constraints."
    return "The request could not be completed. Please review your input and try again."


class ConstraintsService:
    def __init__(
        self,
        *,
        draft_repo: DraftConstraintsRepo,
        config_repo: ConfigRepo,
        active_constraints_provider: ActiveConstraintsProvider,
        wizard_state_repo: WizardStateRepo,
        device_repo: DeviceRepo,
        state_store: StateStore,
    ) -> None:
        self._draft_repo = draft_repo
        self._config_repo = config_repo
        self._provider = active_constraints_provider
        self._wizard_repo = wizard_state_repo
        self._device_repo = device_repo
        self._state_store = state_store

    async def get_or_default(self, session_id: str) -> ConstraintDraftView:
        """Return the persisted draft for this session if one exists, else
        seed the form view from the currently-active provider snapshot.

        Story 9.3 P10 — raises ``ProviderNotReadyError`` if the provider has
        not yet hydrated. The route handler maps that to a 503.
        """
        existing = await self._draft_repo.get(session_id)
        if existing is not None:
            return ConstraintDraftView(
                peak_limit_kw=existing.peak_limit_kw,
                battery_reserve_floor_percent=existing.battery_reserve_floor_percent,
                ev_charging_window_start=existing.ev_charging_window_start,
                ev_charging_window_end=existing.ev_charging_window_end,
                validation_status=existing.validation_status,
                validation_report=existing.validation_report,
                has_persisted_draft=True,
            )
        try:
            active = self._provider.get()
        except RuntimeError as exc:
            raise ProviderNotReadyError(str(exc)) from exc
        return ConstraintDraftView(
            peak_limit_kw=active.peak_limit_kw,
            battery_reserve_floor_percent=active.battery_reserve_floor_percent,
            ev_charging_window_start=active.ev_charging_window_start,
            ev_charging_window_end=active.ev_charging_window_end,
            validation_status="pending",
            validation_report=None,
            has_persisted_draft=False,
        )

    async def assert_step_prerequisites(self, session_id: str) -> None:
        """Story 9.3 P12 — shared gate used by every state-mutating POST.

        Raises ``ConstraintActivationError`` with:
          - ``step_prerequisites_not_met: step_1_complete=<x> step_2_complete=<y>``
            when Steps 1/2 are not finalized; or
          - ``step_3_already_complete`` once Step 3 has been activated for
            this session (prevents post-completion re-activation drift between
            ``active_constraints.config_version`` and
            ``wizard_state.step_3_activated_config_version``).
        """
        wizard = await self._wizard_repo.get(session_id)
        step_1 = 1 if (wizard is not None and wizard.step_1_complete) else 0
        step_2 = 1 if (wizard is not None and wizard.step_2_complete) else 0
        if step_1 != 1 or step_2 != 1:
            raise ConstraintActivationError(
                f"step_prerequisites_not_met: step_1_complete={step_1} step_2_complete={step_2}"
            )
        if wizard is not None and wizard.step_3_complete:
            raise ConstraintActivationError("step_3_already_complete")

    async def upsert_draft(
        self,
        session_id: str,
        *,
        input: ConstraintDraftInput,
        now: datetime,
    ) -> ConstraintDraft:
        """Replace the draft with ``input``; resets ``validation_status='pending'``.

        Schema validation is enforced by the ``ConstraintDraftInput`` Pydantic
        model the caller constructs; this method performs no additional
        validation. The persisted ``validation_status`` is reset because any
        field edit invalidates a prior validation outcome.
        """
        draft = await self._draft_repo.upsert(
            session_id,
            peak_limit_kw=input.peak_limit_kw,
            battery_reserve_floor_percent=input.battery_reserve_floor_percent,
            ev_charging_window_start=input.ev_charging_window_start,
            ev_charging_window_end=input.ev_charging_window_end,
            now=now,
        )
        logger.info(
            "constraints_draft_upserted",
            component="installer_setup",
            session_id=session_id,
            peak_limit_kw=draft.peak_limit_kw,
            battery_reserve_floor_percent=draft.battery_reserve_floor_percent,
            ev_charging_window_start=draft.ev_charging_window_start,
            ev_charging_window_end=draft.ev_charging_window_end,
        )
        return draft

    async def validate_draft(
        self,
        session_id: str,
        *,
        now: datetime,
    ) -> ConstraintValidationReport:
        """Run the three checks in order schema → capability_strategy →
        safety_pre_check; persist + return the report.

        If the draft does not exist, raises ``ConstraintActivationError`` with
        ``reason='draft_not_found_for_session'`` (exact-match per AC11). If
        the schema check FAILs, the downstream checks are SKIPPED (their
        ``ConstraintCheckResult`` is absent from the report).
        """
        existing = await self._draft_repo.get(session_id)
        if existing is None:
            raise ConstraintActivationError("draft_not_found_for_session")
        report = await self._run_validation(existing)
        # Persist the outcome (the persisted state is informational; the
        # activate route re-validates as the authoritative gate).
        await self._draft_repo.record_validation_outcome(
            session_id,
            status=report.overall_status,
            report=report,
            now=now,
        )
        logger.info(
            "constraints_draft_validated",
            component="installer_setup",
            session_id=session_id,
            overall_status=report.overall_status,
            check_count=len(report.checks),
        )
        return report

    async def activate_draft(
        self,
        session_id: str,
        *,
        actor: Literal["installer"],
        now: datetime,
    ) -> ConstraintActivationResult:
        """Atomic activation. Acquires the write lock, re-validates, commits
        if and only if validation passes; refreshes the provider; marks the
        wizard step complete; deletes the draft.

        Raises ``ConstraintActivationError`` (with structured ``reason``) on
        any rejection. The reason vocabulary is exact-match per AC11:

        - ``draft_not_found_for_session``
        - ``step_prerequisites_not_met: step_1_complete=<x> step_2_complete=<y>``
        - ``step_3_already_complete``
        - ``activate_called_with_no_changed_fields``
        - ``constraints_validation_failed`` (with ``report`` populated)

        Story 9.3 P13 — the four-table activation (``active_constraints`` +
        ``config_audit_log`` + ``wizard_state.step_3_*`` + ``draft_constraints``
        delete) runs inside a single ``BEGIN IMMEDIATE`` transaction owned by
        ``ConfigRepo._activate_locked``. If any tabled write raises, the whole
        transaction rolls back; the user retries cleanly without the
        previously-possible permanent ``activate_called_with_no_changed_fields``
        trap.

        The ``provider.reload()`` call sits outside the transaction (and is
        still inside the write lock). A reload failure or a silent
        version-regressed branch (P1) does NOT roll back the activation
        (DB is authoritative; restart re-hydrates correctly) but is logged at
        ERROR so observability surfaces in-memory drift.
        """
        async with get_write_lock():
            # R2P4 — capture the pre-activation provider snapshot INSIDE the
            # write lock. Reading it before the lock allowed a concurrent
            # activator to land between the snapshot read and the lock
            # acquisition, producing a structlog ``changed_fields`` payload
            # that lied about which fields this activation actually changed.
            # The audit log was always correct (it reads ``get_active()``
            # inside the transaction); only the observability line was wrong.
            previous_snapshot = self._snapshot_safe()
            # Step 2 — wizard prerequisite check.
            wizard = await self._wizard_repo.get(session_id)
            step_1 = 1 if (wizard is not None and wizard.step_1_complete) else 0
            step_2 = 1 if (wizard is not None and wizard.step_2_complete) else 0
            if step_1 != 1 or step_2 != 1:
                raise ConstraintActivationError(
                    f"step_prerequisites_not_met: step_1_complete={step_1} step_2_complete={step_2}"
                )
            if wizard is not None and wizard.step_3_complete:
                raise ConstraintActivationError("step_3_already_complete")
            # Step 3 — load the draft.
            draft = await self._draft_repo.get(session_id)
            if draft is None:
                raise ConstraintActivationError("draft_not_found_for_session")
            # Step 4 — re-run validation against live state.
            report = await self._run_validation(draft)
            # Step 5 — abort + persist new outcome on FAIL.
            if report.overall_status == "failed":
                try:
                    await self._draft_repo.record_validation_outcome_locked(
                        session_id, status="failed", report=report, now=now
                    )
                except ValueError:
                    # Story 9.3 P14 — narrow CASCADE race: the session was
                    # concurrently deleted (admin purge or expiry) so the
                    # draft row is already gone. The user is logged out
                    # anyway; the persisted report is moot. Still raise the
                    # contractual rejection so the route returns the
                    # documented 400 instead of a 500.
                    logger.warning(
                        "validation_fail_persist_skipped_session_gone",
                        component="installer_setup",
                        session_id=session_id,
                    )
                raise ConstraintActivationError("constraints_validation_failed", report=report)
            # Step 6 — build the input.
            try:
                input_model = ActiveConstraintsInput(
                    peak_limit_kw=draft.peak_limit_kw,
                    battery_reserve_floor_percent=draft.battery_reserve_floor_percent,
                    ev_charging_window_start=draft.ev_charging_window_start,
                    ev_charging_window_end=draft.ev_charging_window_end,
                )
            except ValidationError as exc:
                # Schema check would have caught this — defensive only.
                raise ConstraintActivationError(
                    "constraints_validation_failed", report=report
                ) from exc

            # Step 7 — atomic four-table commit. The wizard-advance and
            # draft-delete writes share the same BEGIN IMMEDIATE transaction
            # as active_constraints + config_audit_log (P13). If any side
            # fails, all four roll back.
            async def _in_txn_post_writes(new_version: int, _now_utc: datetime) -> None:
                # R2P10 — ``_now_utc`` IS the route's ``now`` thanks to
                # ``_activate_locked(now=now)``; pass it through so the wizard
                # write and the active_constraints write share one timestamp
                # exactly (no sub-millisecond divergence).
                # The repo helpers honor commit=False to defer the COMMIT to
                # ConfigRepo._activate_locked.
                try:
                    await self._wizard_repo.set_step_3_complete_locked(
                        session_id,
                        activated_config_version=new_version,
                        now=_now_utc,
                        commit=False,
                    )
                except ValueError as wizard_exc:
                    # R2P2 — wizard CASCADE race on the happy path: the
                    # session was admin-purged between the prerequisite check
                    # above and this in-transaction UPDATE, so rowcount==0
                    # triggers ``ValueError("No wizard_state row...")``. Map
                    # to a structured rejection so the route renders 400
                    # instead of a 500 with a raw stack trace. Re-raising as
                    # ConstraintActivationError propagates through
                    # _activate_locked's outer ``except Exception`` which
                    # rolls back the transaction atomically — the same wire
                    # contract as any other in-txn failure.
                    raise ConstraintActivationError("session_gone_during_activate") from wizard_exc
                await self._draft_repo.delete_locked(session_id, commit=False)

            try:
                new_version = await self._config_repo._activate_locked(
                    input_model,
                    actor=actor,
                    inside_transaction=_in_txn_post_writes,
                    now=now,
                )
            except NoChangedFieldsError as exc:
                raise ConstraintActivationError("activate_called_with_no_changed_fields") from exc
            except ConstraintActivationError:
                # R2P2 — already-typed rejection bubbled out of the
                # in-transaction callback (e.g. ``session_gone_during_activate``).
                # _activate_locked's ``except Exception`` rolled back the
                # transaction; surface the structured reason to the route.
                raise
            except aiosqlite.OperationalError as exc:
                # R2P13 — SQLite "database is locked" / similar contention. The
                # transaction was rolled back by ``_activate_locked``. Surface
                # as a retryable rejection so the route returns a 400 banner
                # instead of leaking a 500 stack trace.
                raise ConstraintActivationError("activation_busy") from exc

            # Step 8 — refresh the in-memory snapshot. A reload failure is
            # logged but does NOT roll back the activation (DB is the
            # authoritative store; the next process restart re-hydrates
            # correctly). This is the documented narrow drift window from R6.
            try:
                await self._provider.reload()
            except Exception as reload_exc:  # noqa: BLE001
                logger.error(
                    "constraints_reload_after_activate_failed",
                    component="installer_setup",
                    session_id=session_id,
                    config_version=new_version,
                    error=repr(reload_exc),
                    exc_info=True,
                )
            else:
                # Story 9.3 P1 — provider.reload() may take the silent
                # version-regressed branch (no exception, snapshot kept) when
                # MAX(active_constraints.config_version) is < the cached value.
                # Observe that case explicitly so observability surfaces the
                # in-memory drift.
                try:
                    seen_version = self._provider.get().config_version
                except RuntimeError:
                    seen_version = None
                if seen_version != new_version:
                    logger.error(
                        "constraints_reload_after_activate_drift",
                        component="installer_setup",
                        session_id=session_id,
                        config_version=new_version,
                        provider_config_version=seen_version,
                    )
        # Step 9 — log success outside the lock; emit changed-field summary
        # built from the pre-activation snapshot (P4).
        changed_fields = _changed_fields(draft, previous_snapshot)
        logger.info(
            "constraints_activated",
            component="installer_setup",
            session_id=session_id,
            config_version=new_version,
            changed_fields=changed_fields,
        )
        # Story 9.3 P2 — pair ``activated_at`` with ``now`` so the result is
        # deterministic and never depends on the (possibly stale) provider
        # snapshot. The DB row is timestamped by ``_activate_locked`` using
        # its own ``now_utc`` (sub-millisecond apart) — both belong to the
        # same activation moment for all observable purposes.
        return ConstraintActivationResult(
            config_version=new_version,
            activated_at=now,
            report=report,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_validation(self, draft: ConstraintDraft) -> ConstraintValidationReport:
        """Run the three checks in order.

        Schema FAIL skips downstream checks (their results are absent from
        the report) so we never report derived failures on input that
        hasn't yet passed schema validation (AC7 snapshot ordering invariant).

        Story 9.3 P16 — capability_strategy and safety_pre each return a
        tuple of ``ConstraintCheckResult`` rows so multiple WARNs surface
        simultaneously when several gaps coexist (e.g. no battery AND no
        ev_charger). The order within each check is deterministic per the
        sub-check definitions below.
        """
        schema_check = self._check_schema(draft)
        checks: list[ConstraintCheckResult] = [schema_check]
        if schema_check.status != "fail":
            checks.extend(await self._check_capability_strategy(draft))
            checks.extend(self._check_safety_pre(draft))
        overall_status: ConstraintOverallStatus = (
            "failed" if any(c.status == "fail" for c in checks) else "valid"
        )
        return ConstraintValidationReport(
            overall_status=overall_status,
            checks=tuple(checks),
        )

    def _check_schema(self, draft: ConstraintDraft) -> ConstraintCheckResult:
        """Validate the draft through ``ConstraintDraftInput``.

        The repo's CHECK constraints + the model_validator on the Pydantic
        view already block out-of-range values at write time; this check
        re-runs the same validation against the persisted draft so a manually
        tampered DB row is caught in the report.

        Story 9.3 P5 — emit the spec-mandated operator-facing strings rather
        than the raw Pydantic message text. The mapping below covers the
        documented failure modes; unmapped Pydantic errors fall through to a
        stable generic message so a forward-compatible Pydantic change does
        not stop the check from emitting a result.
        """
        try:
            ConstraintDraftInput(
                peak_limit_kw=draft.peak_limit_kw,
                battery_reserve_floor_percent=draft.battery_reserve_floor_percent,
                ev_charging_window_start=draft.ev_charging_window_start,
                ev_charging_window_end=draft.ev_charging_window_end,
            )
        except ValidationError as exc:
            errors = exc.errors()
            first: object = errors[0] if errors else {"msg": "schema validation failed"}
            first_dict = first if isinstance(first, dict) else {}
            field_loc = first_dict.get("loc", ())
            raw_field = (
                str(field_loc[0]) if isinstance(field_loc, tuple | list) and field_loc else None
            )
            message = _spec_schema_message(raw_field, first_dict)
            # R2P7 — AC7 check 1 mandates ``field='ev_charging_window'`` (the
            # unified semantic slot) for any EV-window-related rejection.
            # Pydantic surfaces three different shapes for this concern:
            #   (a) model-level paired-NULL validator → ``loc=()``
            #   (b) format error on ev_charging_window_start → ``loc=('ev_charging_window_start',)``
            #   (c) format error on ev_charging_window_end   → ``loc=('ev_charging_window_end',)``
            # All three should surface the same ``field='ev_charging_window'``
            # contract value so route handlers and tests can match on a single
            # slot regardless of which endpoint the user mis-typed.
            field_str = _normalize_schema_field(raw_field, first_dict)
            return ConstraintCheckResult(
                name="schema",
                status="fail",
                field=field_str,
                message=message,
            )
        return ConstraintCheckResult(name="schema", status="pass")

    async def _check_capability_strategy(
        self, draft: ConstraintDraft
    ) -> tuple[ConstraintCheckResult, ...]:
        """Reads the device registry; produces WARN per gap when a configured
        constraint targets a role that has no assigned device.

        Story 9.3 P16 — each independent gap surfaces its own row so the
        report tells the installer exactly which constraints will be inert.
        Returns a single PASS row when every targeted role is assigned.
        """
        rows = await self._device_repo.list_all()
        assigned_roles = {row.role for row in rows if row.role is not None}
        warns: list[ConstraintCheckResult] = []
        # Defense-in-depth (P9 — the spec-mandated step-gate already fires
        # first; if a tampered wizard_state row let an installer reach Step 3
        # without grid_meter, this WARN tells them the peak limit will be
        # decorative). Copy rewritten to avoid the misleading "Step 2 must
        # complete" claim.
        if DeviceRole.grid_meter not in assigned_roles:
            warns.append(
                ConstraintCheckResult(
                    name="capability_strategy",
                    status="warn",
                    field=None,
                    message=("No grid meter is assigned, so the peak limit will not be enforced."),
                )
            )
        if draft.battery_reserve_floor_percent != 0.0 and DeviceRole.battery not in assigned_roles:
            warns.append(
                ConstraintCheckResult(
                    name="capability_strategy",
                    status="warn",
                    field="battery_reserve_floor_percent",
                    message=(
                        "No battery is assigned, so the reserve floor has no effect"
                        " on system behavior."
                    ),
                )
            )
        if (
            draft.ev_charging_window_start is not None
            and DeviceRole.ev_charger not in assigned_roles
        ):
            warns.append(
                ConstraintCheckResult(
                    name="capability_strategy",
                    status="warn",
                    field="ev_charging_window",
                    message=(
                        "No EV charger is assigned, so the charging window has no"
                        " effect on system behavior."
                    ),
                )
            )
        if warns:
            return tuple(warns)
        return (ConstraintCheckResult(name="capability_strategy", status="pass"),)

    def evaluate_safety_pre_check(
        self,
        constraints: ActiveConstraints,
        snapshot: SystemSnapshot,
    ) -> tuple[ConstraintCheckResult, ...]:
        """Story 9.4 reuse seam — public wrapper around ``_check_safety_pre``.

        Lets ``DeploymentValidationService`` evaluate the safety pre-check
        against the *currently-active* constraints + a snapshot the caller
        already captured (D4 — the deployment-validation run captures one
        StateStore snapshot at run start and threads it through every check
        so the six checks observe a consistent state moment).

        Read-only: no DB writes, no state mutation. Safe to call from any
        thread that holds a reference to this service instance.
        """
        return _check_safety_pre_pure(
            peak_limit_kw=constraints.peak_limit_kw,
            battery_reserve_floor_percent=constraints.battery_reserve_floor_percent,
            snapshot=snapshot,
        )

    def _check_safety_pre(self, draft: ConstraintDraft) -> tuple[ConstraintCheckResult, ...]:
        """Read StateStore and delegate to ``_check_safety_pre_pure``.

        Thin wrapper retained for the internal ``_run_validation`` flow which
        does not have a pre-captured snapshot (each validate-draft pass reads
        the live store at evaluation time, by design — the activate path
        re-runs validation inside the write lock against the current state).
        """
        snapshot = self._state_store.get_snapshot()
        return _check_safety_pre_pure(
            peak_limit_kw=draft.peak_limit_kw,
            battery_reserve_floor_percent=draft.battery_reserve_floor_percent,
            snapshot=snapshot,
        )

    def _snapshot_safe(self) -> _PreviousSnapshot:
        """Capture the pre-activation provider state for the changed-fields
        log (P4). Tolerates a not-yet-hydrated provider — the missing
        snapshot means every input field gets reported as changed, which is
        correct for the first activation.
        """
        try:
            active = self._provider.get()
        except RuntimeError:
            return _PreviousSnapshot(
                peak_limit_kw=None,
                battery_reserve_floor_percent=None,
                ev_charging_window_start=None,
                ev_charging_window_end=None,
            )
        return _PreviousSnapshot(
            peak_limit_kw=active.peak_limit_kw,
            battery_reserve_floor_percent=active.battery_reserve_floor_percent,
            ev_charging_window_start=active.ev_charging_window_start,
            ev_charging_window_end=active.ev_charging_window_end,
        )


@dataclass(frozen=True)
class _PreviousSnapshot:
    """Pre-activation provider snapshot captured for the changed-fields log."""

    peak_limit_kw: float | None
    battery_reserve_floor_percent: float | None
    ev_charging_window_start: str | None
    ev_charging_window_end: str | None


def _check_safety_pre_pure(
    *,
    peak_limit_kw: float,
    battery_reserve_floor_percent: float,
    snapshot: SystemSnapshot,
) -> tuple[ConstraintCheckResult, ...]:
    """Pure evaluator for the safety pre-check rule set.

    Reads only the three inputs it is passed; no I/O, no shared mutable
    state. The two callers (private ``_check_safety_pre`` for validate-draft;
    public ``evaluate_safety_pre_check`` for deployment validation) both
    delegate here so the rule set has exactly one implementation per the
    single-evaluator principle (Epic 7 retro).

    Story 9.3 P16 + R2P6 — multiple safety conditions can co-trip; each
    surfaces as its own row. The structural FAIL (peak_limit_kw < 0.5)
    joins the result tuple alongside any WARNs rather than early-returning.

    R2P11 — ``BatteryState.soc_percent`` can be ``NaN``. ``NaN > x``
    evaluates ``False``, which would suppress the reserve-floor WARN even
    though the soc is effectively unknown. Treat NaN as "state not yet
    known" and surface the unknown-state WARN explicitly.
    """
    results: list[ConstraintCheckResult] = []
    if peak_limit_kw < _PEAK_LIMIT_FAIL_THRESHOLD_KW:
        results.append(
            ConstraintCheckResult(
                name="safety_pre_check",
                status="fail",
                field="peak_limit_kw",
                message=("Peak limit below 0.5 kW would block all grid imports indefinitely."),
            )
        )
    battery_state_known = isinstance(snapshot.battery, BatteryState) and not math.isnan(
        snapshot.battery.soc_percent
    )
    if battery_state_known:
        assert isinstance(snapshot.battery, BatteryState)
        if battery_reserve_floor_percent > snapshot.battery.soc_percent:
            results.append(
                ConstraintCheckResult(
                    name="safety_pre_check",
                    status="warn",
                    field="battery_reserve_floor_percent",
                    message=(
                        f"Battery is currently at {snapshot.battery.soc_percent:.1f}%"
                        f" SoC. Activating a reserve floor of"
                        f" {battery_reserve_floor_percent:.1f}% would"
                        " immediately block discharge until SoC rises above"
                        " the floor."
                    ),
                )
            )
    elif battery_reserve_floor_percent > 0.0:
        results.append(
            ConstraintCheckResult(
                name="safety_pre_check",
                status="warn",
                field="battery_reserve_floor_percent",
                message=(
                    "Battery state is not yet known. The reserve floor will"
                    " take effect once the battery reports SoC; if current"
                    " SoC is below the floor, discharge will be blocked"
                    " immediately."
                ),
            )
        )
    if isinstance(snapshot.grid_meter, GridMeterState) and not math.isnan(
        snapshot.grid_meter.grid_power_kw
    ):
        if snapshot.grid_meter.grid_power_kw > peak_limit_kw:
            results.append(
                ConstraintCheckResult(
                    name="safety_pre_check",
                    status="warn",
                    field="peak_limit_kw",
                    message=(
                        f"Grid is currently importing"
                        f" {snapshot.grid_meter.grid_power_kw:.1f} kW;"
                        f" activating a peak limit of {peak_limit_kw:.1f}"
                        " kW would immediately put the site over limit."
                    ),
                )
            )
    else:
        results.append(
            ConstraintCheckResult(
                name="safety_pre_check",
                status="warn",
                field="peak_limit_kw",
                message=(
                    "Grid meter state is not yet known. The peak limit will"
                    " take effect once telemetry arrives."
                ),
            )
        )
    if results:
        return tuple(results)
    return (ConstraintCheckResult(name="safety_pre_check", status="pass"),)


def _changed_fields(draft: ConstraintDraft, previous: _PreviousSnapshot) -> list[str]:
    """Diff the draft against the captured pre-activation snapshot (P4)."""
    fields: set[str] = set()
    if draft.peak_limit_kw != previous.peak_limit_kw:
        fields.add("peak_limit_kw")
    if draft.battery_reserve_floor_percent != previous.battery_reserve_floor_percent:
        fields.add("battery_reserve_floor_percent")
    if (
        draft.ev_charging_window_start != previous.ev_charging_window_start
        or draft.ev_charging_window_end != previous.ev_charging_window_end
    ):
        fields.add("ev_charging_window")
    return sorted(fields)


def _normalize_schema_field(raw_field: str | None, pydantic_error: object) -> str | None:
    """R2P7 — collapse EV-window-related Pydantic locations to the AC7-mandated
    unified slot ``ev_charging_window``.

    Pydantic surfaces three shapes for the same semantic concern (paired-NULL
    invariant, HH:MM format on start, HH:MM format on end). AC7 check 1
    requires one ``field`` value covering all three. Other fields pass through
    unchanged.
    """
    err = pydantic_error if isinstance(pydantic_error, dict) else {}
    raw_msg = str(err.get("msg", ""))
    if raw_field in ("ev_charging_window_start", "ev_charging_window_end"):
        return "ev_charging_window"
    # Model-level paired-NULL validator: ``loc=()`` → ``raw_field=None``. The
    # ``_ev_window_paired`` validator's ValueError carries the canonical
    # substring; recognising it lets us assign the unified slot even when
    # Pydantic does not surface a ``loc``.
    if raw_field is None and (
        "must both be set or both be NULL" in raw_msg
        or "must both be set or both be None" in raw_msg
    ):
        return "ev_charging_window"
    return raw_field


def _spec_schema_message(field: str | None, pydantic_error: object) -> str:
    """Map a Pydantic ValidationError entry to the AC7-mandated copy (P5).

    The lookup uses ``field`` first, then ``type`` for cases where the error
    is at the model level (paired-NULL invariant). Unmapped combinations
    fall through to the raw message so a forward-compatible Pydantic change
    does not silently empty the WARN.
    """
    err = pydantic_error if isinstance(pydantic_error, dict) else {}
    error_type = str(err.get("type", ""))
    raw_msg = str(err.get("msg", "schema validation failed"))
    if field == "peak_limit_kw":
        if error_type in ("greater_than", "finite_number") or "greater than" in raw_msg:
            return "Peak limit must be greater than 0 kW."
    if field == "battery_reserve_floor_percent":
        if error_type in (
            "greater_than_equal",
            "less_than_equal",
            "finite_number",
        ):
            return "Battery reserve floor must be between 0 and 100 percent."
    if field in ("ev_charging_window_start", "ev_charging_window_end"):
        return "EV charging window times must use 24-hour HH:MM format."
    if (
        "must both be set or both be NULL" in raw_msg
        or "must both be set or both be None" in raw_msg
    ):
        return "EV charging window start and end must both be set or both empty."
    return raw_msg


# Re-export for convenience.
_ = cast  # silences vulture; cast is exported for callers that need it


__all__ = [
    "ConstraintActivationError",
    "ConstraintDraftView",
    "ConstraintsService",
    "ProviderNotReadyError",
    "reason_to_user_message",
]
