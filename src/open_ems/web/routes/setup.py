"""Installer setup wizard routes (Story 9.1).

Step 1 — Device Discovery. Step 2+ are introduced by subsequent stories; this
router exposes a temporary `/installer/setup/roles` placeholder so the
``advance`` route can redirect there without returning a 404.

All routes require an installer session and CSRF on every state-changing path
(the existing ``CsrfMiddleware`` enforces the latter). Templates extend
``installer/setup_layout.html`` so the persistent 4-step indicator (UX spec
component #14) stays visible across the wizard.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Form, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from open_ems.adapters.capabilities import get_profile
from open_ems.core.constraints import ConstraintDraftInput
from open_ems.core.deployment_validation import DeploymentCheckName
from open_ems.core.devices import CapabilityStatus, DeviceRole
from open_ems.services.constraints import (
    ConstraintActivationError,
    ConstraintsService,
    ProviderNotReadyError,
    reason_to_user_message,
)
from open_ems.services.deployment_validation import (
    CheckNotWarnableError,
    DeploymentValidationService,
    NoCurrentResultError,
    NotAckEligibleError,
    NotHandoffEligibleError,
)
from open_ems.services.device_discovery import (
    DeviceDiscoveryOrchestrator,
    DSMRScanTarget,
    ModbusScanTarget,
    ScanTargets,
)
from open_ems.services.manual_entry import (
    ManualEntryFormError,
    ManualEntryFormRequest,
    ManualEntryService,
)
from open_ems.services.role_assignment import (
    _VALID_GAP_LABELS,
    RoleAssignmentService,
)
from open_ems.services.wizard_gate import WizardGateService
from open_ems.storage.database import get_write_lock
from open_ems.storage.repositories.device_repo import (
    CapabilityStatusLiteral,
    DeviceRegistryEntry,
    DeviceRepo,
)
from open_ems.storage.repositories.wizard_state_repo import WizardStateRepo
from open_ems.web.dependencies import InstallerUser, require_installer

logger = structlog.get_logger(__name__)

router = APIRouter()

_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# R6: the device-registry row template MUST re-resolve the capability profile
# at render time so a code-side capability-registry update is reflected in the
# UI immediately, even if no probe has run since the change. Exposing
# ``get_profile`` as a Jinja global lets the row fragment call it directly
# without each route handler having to pre-compute the resolution.
_templates.env.globals["get_profile"] = get_profile


# ---------------------------------------------------------------------------
# Dependency wiring helpers (app.state-backed)
# ---------------------------------------------------------------------------


def _device_repo(request: Request) -> DeviceRepo:
    repo = getattr(request.app.state, "device_repo", None)
    if not isinstance(repo, DeviceRepo):
        raise HTTPException(status_code=503, detail="Device registry unavailable")
    return repo


def _wizard_state_repo(request: Request) -> WizardStateRepo:
    repo = getattr(request.app.state, "wizard_state_repo", None)
    if not isinstance(repo, WizardStateRepo):
        raise HTTPException(status_code=503, detail="Wizard state unavailable")
    return repo


def _orchestrator(request: Request) -> DeviceDiscoveryOrchestrator:
    orch = getattr(request.app.state, "discovery_orchestrator", None)
    if not isinstance(orch, DeviceDiscoveryOrchestrator):
        raise HTTPException(status_code=503, detail="Discovery orchestrator unavailable")
    return orch


def _capability_status_to_literal(status: CapabilityStatus) -> CapabilityStatusLiteral:
    """Narrow ``CapabilityStatus`` (StrEnum) to the typed Literal accepted by
    ``DeviceRepo.{mark_validated,update_last_seen}``. The enum and the Literal
    carry the same three values; mypy's ``StrEnum.value`` widens to ``str`` so
    we run a runtime guard and narrow back.
    """
    if status is CapabilityStatus.full:
        return "full"
    if status is CapabilityStatus.reduced:
        return "reduced"
    return "unsupported"


def _manual_entry_service(request: Request) -> ManualEntryService:
    svc = getattr(request.app.state, "manual_entry_service", None)
    if not isinstance(svc, ManualEntryService):
        raise HTTPException(status_code=503, detail="Manual entry service unavailable")
    return svc


def _role_assignment_service(request: Request) -> RoleAssignmentService:
    svc = getattr(request.app.state, "role_assignment_service", None)
    if not isinstance(svc, RoleAssignmentService):
        raise HTTPException(status_code=503, detail="Role assignment service unavailable")
    return svc


def _constraints_service(request: Request) -> ConstraintsService:
    svc = getattr(request.app.state, "constraints_service", None)
    if not isinstance(svc, ConstraintsService):
        raise HTTPException(status_code=503, detail="Constraints service unavailable")
    return svc


def _deployment_validation_service(
    request: Request,
) -> DeploymentValidationService:
    svc = getattr(request.app.state, "deployment_validation_service", None)
    if not isinstance(svc, DeploymentValidationService):
        raise HTTPException(status_code=503, detail="Deployment validation service unavailable")
    return svc


_VALID_CHECK_NAMES: frozenset[str] = frozenset(
    {
        "connectivity",
        "role_completeness",
        "capability_strategy",
        "constraint_completeness",
        "constraint_safety_pre_check",
        "control_readiness",
    }
)


def _validate_check_name(raw: str) -> DeploymentCheckName:
    # P27 — return 400 to align with every other validation rejection in this
    # route module. 404 was previously inconsistent (the path is well-known;
    # the check_name is the bad input, which is a 400 concern).
    if raw not in _VALID_CHECK_NAMES:
        raise HTTPException(status_code=400, detail=f"unknown_check_name: {raw}")
    return raw  # type: ignore[return-value]


async def _require_step_3_complete(
    user_session_id: str,
    wizard_repo: WizardStateRepo,
) -> None:
    """P13 — shared step-gate for every mutating Step 4 route.

    Raises HTTPException 403 with exact-match reason when the installer has
    not yet completed Steps 1-3. The `get_validation_page` GET handler
    redirects (its UX gate); the mutation handlers reject hard.
    """
    state = await wizard_repo.get_or_create(user_session_id, now=datetime.now(UTC))
    if not state.step_1_complete:
        raise HTTPException(status_code=403, detail="step_prerequisites_not_met: step_1")
    if not state.step_2_complete:
        raise HTTPException(status_code=403, detail="step_prerequisites_not_met: step_2")
    if not state.step_3_complete:
        raise HTTPException(status_code=403, detail="step_prerequisites_not_met: step_3")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/installer/setup", include_in_schema=False)
async def setup_root(
    _user: InstallerUser = Depends(require_installer),  # noqa: B008
) -> RedirectResponse:
    return RedirectResponse(url="/installer/setup/discovery", status_code=302)


@router.get("/installer/setup/discovery", response_class=HTMLResponse)
async def get_discovery_page(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    device_repo: DeviceRepo = Depends(_device_repo),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
) -> HTMLResponse:
    now = datetime.now(UTC)
    await wizard_repo.get_or_create(user.session_id, now=now)
    rows = await device_repo.list_all()
    gate = await WizardGateService(device_repo).evaluate()
    return _templates.TemplateResponse(
        request,
        "installer/setup_discovery.html",
        {
            "csrf_token": user.csrf_token,
            "title": "Device Discovery",
            "active_step": "discovery",
            "registry_rows": rows,
            "live_results": (),
            "unreachable_targets": (),
            "not_found_devices": (),
            "last_scan_id": None,
            "gate_can_advance": gate.can_advance,
            "gate_blockers": gate.unacknowledged_unvalidated,
        },
    )


@router.post("/installer/setup/discovery/scan", response_class=HTMLResponse)
async def post_run_scan(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    device_repo: DeviceRepo = Depends(_device_repo),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    orchestrator: DeviceDiscoveryOrchestrator = Depends(_orchestrator),  # noqa: B008
) -> HTMLResponse:
    """Run a full scan: every registered Modbus + DSMR target gets probed,
    OCPP self-registered chargers are captured. NOT FOUND classification fires
    for every registered device by passing all device_ids as expected.
    """
    registry = await device_repo.list_all()
    modbus_targets = tuple(
        _to_modbus_target(row) for row in registry if row.protocol == "modbus_tcp"
    )
    dsmr_targets = tuple(_to_dsmr_target(row) for row in registry if row.protocol == "dsmr_p1")
    targets = ScanTargets(
        modbus_targets=tuple(t for t in modbus_targets if t is not None),
        dsmr_targets=tuple(t for t in dsmr_targets if t is not None),
        include_ocpp_self_registered=True,
        expected_device_ids=frozenset(row.device_id for row in registry),
    )
    report = await orchestrator.run_scan(targets)
    await wizard_repo.set_last_scan_id(
        user.session_id, scan_id=str(report.scan_id), now=datetime.now(UTC)
    )
    gate = await WizardGateService(device_repo).evaluate()
    registry_after = await device_repo.list_all()
    return _templates.TemplateResponse(
        request,
        "installer/_scan_results.html",
        {
            "csrf_token": user.csrf_token,
            "registry_rows": registry_after,
            "live_results": report.live_results,
            "unreachable_targets": report.unreachable_targets,
            "not_found_devices": report.not_found_devices,
            "last_scan_id": str(report.scan_id),
            "gate_can_advance": gate.can_advance,
            "gate_blockers": gate.unacknowledged_unvalidated,
        },
    )


@router.post("/installer/setup/discovery/scan/{device_id}/retry", response_class=HTMLResponse)
async def post_retry_single_target(
    request: Request,
    device_id: str,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    device_repo: DeviceRepo = Depends(_device_repo),  # noqa: B008
    orchestrator: DeviceDiscoveryOrchestrator = Depends(_orchestrator),  # noqa: B008
) -> HTMLResponse:
    """Re-run a single-target probe for one registered device.

    The orchestrator is invoked with exactly one expected_device_id so a failed
    probe surfaces as an ``unreachable_targets`` row (NOT FOUND classification
    is suppressed for the same device_id to avoid double-rendering — see
    ``_classify_not_found``). A successful probe writes back to the registry:
    unvalidated rows flip ``validated=0→1`` via ``mark_validated`` (closing the
    AC5 step-5 "Retry validation" contract), validated rows refresh
    ``last_*`` columns via ``update_last_seen``. The gate is re-evaluated so
    the swapped fragment correctly reflects whether Step 2 is now reachable.
    """
    entry = await device_repo.get_by_device_id(device_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="device not registered")
    targets_kw: dict[str, object] = {
        "include_ocpp_self_registered": False,
        "expected_device_ids": frozenset({device_id}),
    }
    if entry.protocol == "modbus_tcp":
        mb = _to_modbus_target(entry)
        if mb is None:
            raise HTTPException(status_code=400, detail="invalid modbus address")
        targets_kw["modbus_targets"] = (mb,)
    elif entry.protocol == "dsmr_p1":
        ds = _to_dsmr_target(entry)
        if ds is None:
            raise HTTPException(status_code=400, detail="invalid dsmr address")
        targets_kw["dsmr_targets"] = (ds,)
    else:  # ocpp_1_6
        targets_kw["include_ocpp_self_registered"] = True
    report = await orchestrator.run_scan(ScanTargets(**targets_kw))  # type: ignore[arg-type]

    # AC5 step 5 / AC7 contract: a successful retry MUST update the registry,
    # otherwise the Step-2 gate continues to block on a row that is now
    # reachable. ``DeviceRepo.mark_validated`` handles the one-way 0→1
    # transition (AC6); ``update_last_seen`` refreshes the cached columns
    # without touching ``validated``.
    for live in report.live_results:
        if live.result.device_id != device_id:
            continue
        capability_literal = _capability_status_to_literal(live.profile.capability_status)
        now_utc = datetime.now(UTC)
        if entry.validated:
            await device_repo.update_last_seen(
                device_id,
                last_capability_status=capability_literal,
                last_limitation_reason=live.profile.limitation_reason,
                last_seen_at=now_utc,
            )
        else:
            await device_repo.mark_validated(
                device_id,
                last_capability_status=capability_literal,
                last_limitation_reason=live.profile.limitation_reason,
                last_seen_at=now_utc,
            )

    gate = await WizardGateService(device_repo).evaluate()
    return _templates.TemplateResponse(
        request,
        "installer/_scan_results.html",
        {
            "csrf_token": user.csrf_token,
            "registry_rows": await device_repo.list_all(),
            "live_results": report.live_results,
            "unreachable_targets": report.unreachable_targets,
            "not_found_devices": report.not_found_devices,
            "last_scan_id": str(report.scan_id),
            "gate_can_advance": gate.can_advance,
            "gate_blockers": gate.unacknowledged_unvalidated,
        },
    )


@router.post("/installer/setup/discovery/manual", response_class=HTMLResponse)
async def post_manual_entry(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    manual_svc: ManualEntryService = Depends(_manual_entry_service),  # noqa: B008
    device_repo: DeviceRepo = Depends(_device_repo),  # noqa: B008
    device_id: str = Form(...),
    protocol: str = Form(...),
    address: str = Form(...),
    model: str | None = Form(default=None),
    firmware_version: str | None = Form(default=None),
    csrf_token: str = Form(default=""),  # consumed by middleware
) -> HTMLResponse:
    _ = csrf_token  # middleware already validated
    form = ManualEntryFormRequest(
        device_id=device_id,
        protocol=protocol,
        address=address,
        model=model,
        firmware_version=firmware_version,
    )
    try:
        validated = manual_svc.validate_form(form)
    except ManualEntryFormError as err:
        return _templates.TemplateResponse(
            request,
            "installer/_setup_discovery_manual_form.html",
            {
                "csrf_token": user.csrf_token,
                "field_errors": {err.field or "_form": err.reason},
                "form": form.model_dump(),
            },
            status_code=400,
        )

    try:
        entry = await manual_svc.persist_unvalidated(validated, now=datetime.now(UTC))
    except ValueError as err:
        return _templates.TemplateResponse(
            request,
            "installer/_setup_discovery_manual_form.html",
            {
                "csrf_token": user.csrf_token,
                "field_errors": {"device_id": str(err)},
                "form": form.model_dump(),
            },
            status_code=409,
        )

    # AC5 step 3-5: probe inline. The UX spec accepts the up-to-10s blocking
    # window for Modbus and 30s for DSMR; the detached variant is documented
    # in R4 but not required.
    probe_outcome = await manual_svc.probe_and_validate(entry)
    refreshed = await device_repo.get_by_device_id(entry.device_id)
    return _templates.TemplateResponse(
        request,
        "installer/_setup_discovery_row.html",
        {
            "csrf_token": user.csrf_token,
            "row": refreshed,
            "probe_failure_reason": probe_outcome.failure_reason,
        },
    )


@router.post(
    "/installer/setup/discovery/manual/{device_id}/acknowledge-unvalidated",
    response_class=HTMLResponse,
)
async def post_acknowledge_unvalidated(
    request: Request,
    device_id: str,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    device_repo: DeviceRepo = Depends(_device_repo),  # noqa: B008
) -> HTMLResponse:
    now = datetime.now(UTC)
    try:
        await device_repo.acknowledge_unvalidated(device_id, acknowledged_at=now)
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    refreshed = await device_repo.get_by_device_id(device_id)
    return _templates.TemplateResponse(
        request,
        "installer/_setup_discovery_row.html",
        {
            "csrf_token": user.csrf_token,
            "row": refreshed,
            "probe_failure_reason": None,
        },
    )


@router.post("/installer/setup/discovery/advance")
async def post_advance_to_step_2(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    device_repo: DeviceRepo = Depends(_device_repo),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    csrf_token: str = Form(default=""),
) -> Response:
    _ = csrf_token
    gate = await WizardGateService(device_repo).evaluate()
    if not gate.can_advance:
        return _templates.TemplateResponse(
            request,
            "installer/_setup_discovery_error_banner.html",
            {
                "csrf_token": user.csrf_token,
                "blockers": gate.unacknowledged_unvalidated,
            },
            status_code=400,
        )
    await wizard_repo.set_step_1_complete(user.session_id, now=datetime.now(UTC))
    return RedirectResponse(url="/installer/setup/roles", status_code=302)


@router.get("/installer/setup/roles", response_class=HTMLResponse)
async def get_roles_page(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    role_svc: RoleAssignmentService = Depends(_role_assignment_service),  # noqa: B008
) -> Response:
    now = datetime.now(UTC)
    state = await wizard_repo.get_or_create(user.session_id, now=now)
    # Step-1 gate: deep-linking past discovery (e.g., bookmark to /roles) must
    # bounce back. The discovery handler is the only writer of step_1_complete=1.
    if not state.step_1_complete:
        return RedirectResponse(url="/installer/setup/discovery", status_code=302)
    gate = await role_svc.evaluate_gate(acknowledged_gaps=state.step_2_acknowledged_gaps)
    snapshot = await role_svc.evaluate_assignments()
    return _templates.TemplateResponse(
        request,
        "installer/setup_roles.html",
        {
            "csrf_token": user.csrf_token,
            "title": "Role Assignment",
            "active_step": "roles",
            "step_2_complete": state.step_2_complete,
            "assignments": snapshot.assignments,
            "conflicts": gate.conflicts,
            "blocking_gaps": gate.blocking_gaps,
            "warning_gaps": gate.unacknowledged_warnings,
            "acknowledged_gaps": gate.effective_acknowledged_gaps,
            "gate_can_advance": gate.can_advance,
        },
    )


@router.post("/installer/setup/roles/{device_id}/assign", response_class=HTMLResponse)
async def post_assign_role(
    request: Request,
    device_id: str,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    device_repo: DeviceRepo = Depends(_device_repo),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    role_svc: RoleAssignmentService = Depends(_role_assignment_service),  # noqa: B008
    role: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> HTMLResponse:
    _ = csrf_token
    resolved: DeviceRole | None
    role_clean = role.strip()
    if role_clean == "":
        resolved = None
    else:
        try:
            resolved = DeviceRole(role_clean)
        except ValueError:
            return _templates.TemplateResponse(
                request,
                "installer/_setup_roles_error.html",
                {
                    "reason": f"role_invalid: {role_clean!r}",
                },
                status_code=400,
            )
    try:
        await device_repo.assign_role(device_id, role=resolved, assigned_at=datetime.now(UTC))
    except ValueError:
        # HTMX-targeted route: keep the error envelope consistent with the
        # invalid-role branch above (HTML fragment, not FastAPI default JSON).
        return _templates.TemplateResponse(
            request,
            "installer/_setup_roles_error.html",
            {"reason": f"device_not_found: {device_id!r}"},
            status_code=404,
        )

    if resolved is None:
        logger.info("role_unassigned", component="installer_setup", device_id=device_id)
    else:
        logger.info(
            "role_assigned",
            component="installer_setup",
            device_id=device_id,
            role=resolved.value,
        )

    state = await wizard_repo.get_or_create(user.session_id, now=datetime.now(UTC))
    gate = await role_svc.evaluate_gate(acknowledged_gaps=state.step_2_acknowledged_gaps)
    snapshot = await role_svc.evaluate_assignments()
    return _templates.TemplateResponse(
        request,
        "installer/_setup_roles_list.html",
        {
            "csrf_token": user.csrf_token,
            "assignments": snapshot.assignments,
            "conflicts": gate.conflicts,
            "blocking_gaps": gate.blocking_gaps,
            "warning_gaps": gate.unacknowledged_warnings,
            "acknowledged_gaps": gate.effective_acknowledged_gaps,
            "gate_can_advance": gate.can_advance,
            "gap_panel_oob": True,
        },
    )


@router.post("/installer/setup/roles/acknowledge-gap", response_class=HTMLResponse)
async def post_acknowledge_gap(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    role_svc: RoleAssignmentService = Depends(_role_assignment_service),  # noqa: B008
    action: str = Form(default="acknowledge"),
    gap_label: str = Form(...),
    csrf_token: str = Form(default=""),
) -> HTMLResponse:
    _ = csrf_token
    label_clean = gap_label.strip()
    action_clean = action.strip().lower()
    if action_clean not in {"acknowledge", "revoke"}:
        return _templates.TemplateResponse(
            request,
            "installer/_setup_roles_error.html",
            {"reason": f"action_invalid: {action_clean!r}"},
            status_code=400,
        )
    if label_clean not in _VALID_GAP_LABELS:
        # AC5: 'grid_meter_missing' is deliberately not in the whitelist —
        # surface the canonical-shape rejection.
        if label_clean == "grid_meter_missing":
            return _templates.TemplateResponse(
                request,
                "installer/_setup_roles_error.html",
                {"reason": "gap_label_not_acknowledgeable: grid_meter_missing"},
                status_code=400,
            )
        return _templates.TemplateResponse(
            request,
            "installer/_setup_roles_error.html",
            {"reason": f"gap_label_invalid: {label_clean!r}"},
            status_code=400,
        )

    state = await wizard_repo.get_or_create(user.session_id, now=datetime.now(UTC))
    current = set(state.step_2_acknowledged_gaps)
    if action_clean == "acknowledge":
        current.add(label_clean)
        logger.info(
            "gap_acknowledged",
            component="installer_setup",
            session_id=user.session_id,
            gap_label=label_clean,
        )
    else:
        current.discard(label_clean)
        logger.info(
            "gap_revoked",
            component="installer_setup",
            session_id=user.session_id,
            gap_label=label_clean,
        )
    await wizard_repo.record_acknowledged_gaps(
        user.session_id,
        gaps=frozenset(current),
        now=datetime.now(UTC),
    )
    gate = await role_svc.evaluate_gate(acknowledged_gaps=frozenset(current))
    return _templates.TemplateResponse(
        request,
        "installer/_setup_roles_gap_panel.html",
        {
            "csrf_token": user.csrf_token,
            "conflicts": gate.conflicts,
            "blocking_gaps": gate.blocking_gaps,
            "warning_gaps": gate.unacknowledged_warnings,
            "acknowledged_gaps": gate.effective_acknowledged_gaps,
            "gate_can_advance": gate.can_advance,
        },
    )


@router.post("/installer/setup/roles/advance")
async def post_advance_to_step_3(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    role_svc: RoleAssignmentService = Depends(_role_assignment_service),  # noqa: B008
    csrf_token: str = Form(default=""),
) -> Response:
    _ = csrf_token
    state = await wizard_repo.get_or_create(user.session_id, now=datetime.now(UTC))
    gate = await role_svc.evaluate_gate(acknowledged_gaps=state.step_2_acknowledged_gaps)
    if not gate.can_advance:
        return _templates.TemplateResponse(
            request,
            "installer/_setup_roles_error_banner.html",
            {
                "csrf_token": user.csrf_token,
                "conflicts": gate.conflicts,
                "blocking_gaps": gate.blocking_gaps,
                "warning_gaps": gate.unacknowledged_warnings,
            },
            status_code=400,
        )
    # TOCTOU close (Story 9.2 review patch): hold the process-wide write
    # lock across the second gate evaluation AND the persistence call.
    # DeviceRepo.assign_role and the acknowledgment write both take the
    # same lock, so a concurrent role change between the optimistic check
    # above and the lock acquisition here cannot land between the
    # re-evaluation and the UPDATE. AC6 step 4: persist the filtered
    # acknowledged-gaps set inside the same write that flips
    # step_2_complete=1 (idempotent on a re-click — the repo preserves the
    # original completed_at).
    async with get_write_lock():
        refreshed = await wizard_repo.get(user.session_id) or state
        fresh_gate = await role_svc.evaluate_gate(
            acknowledged_gaps=refreshed.step_2_acknowledged_gaps
        )
        if not fresh_gate.can_advance:
            return _templates.TemplateResponse(
                request,
                "installer/_setup_roles_error_banner.html",
                {
                    "csrf_token": user.csrf_token,
                    "conflicts": fresh_gate.conflicts,
                    "blocking_gaps": fresh_gate.blocking_gaps,
                    "warning_gaps": fresh_gate.unacknowledged_warnings,
                },
                status_code=400,
            )
        await wizard_repo.set_step_2_complete_locked(
            user.session_id,
            acknowledged_gaps=fresh_gate.effective_acknowledged_gaps,
            now=datetime.now(UTC),
        )
        effective_for_log = fresh_gate.effective_acknowledged_gaps
    logger.info(
        "step_2_completed",
        component="installer_setup",
        session_id=user.session_id,
        acknowledged_gaps=sorted(effective_for_log),
    )
    return RedirectResponse(url="/installer/setup/constraints", status_code=302)


@router.get("/installer/setup/constraints", response_class=HTMLResponse)
async def get_constraints_page(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    svc: ConstraintsService = Depends(_constraints_service),  # noqa: B008
) -> Response:
    """Story 9.3 — Step 3 main page. Step-gate enforcement (mirrors 9.2):
    deep-linking past discovery / roles bounces back. Idempotent short-circuit:
    if Step 3 is already complete, redirect forward to Step 4.
    """
    now = datetime.now(UTC)
    state = await wizard_repo.get_or_create(user.session_id, now=now)
    if not state.step_1_complete:
        return RedirectResponse(url="/installer/setup/discovery", status_code=302)
    if not state.step_2_complete:
        return RedirectResponse(url="/installer/setup/roles", status_code=302)
    if state.step_3_complete:
        return RedirectResponse(url="/installer/setup/validation", status_code=302)
    try:
        view = await svc.get_or_default(user.session_id)
    except ProviderNotReadyError as exc:
        # Story 9.3 P10 — provider has not yet hydrated; report as 503 rather
        # than surfacing the raw RuntimeError as a generic 500.
        raise HTTPException(status_code=503, detail="Constraints provider not ready") from exc
    return _templates.TemplateResponse(
        request,
        "installer/setup_constraints.html",
        {
            "csrf_token": user.csrf_token,
            "title": "Constraints",
            "active_step": "constraints",
            "step_2_complete": state.step_2_complete,
            "step_3_complete": state.step_3_complete,
            "view": view,
        },
    )


@router.post("/installer/setup/constraints/draft", response_class=HTMLResponse)
async def post_upsert_constraint_draft(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    svc: ConstraintsService = Depends(_constraints_service),  # noqa: B008
    peak_limit_kw: str = Form(...),
    battery_reserve_floor_percent: str = Form(...),
    ev_charging_window_start: str = Form(default=""),
    ev_charging_window_end: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> HTMLResponse:
    _ = csrf_token  # validated by middleware
    # Story 9.3 P12 — gate the POST surface too. The GET route's step-gate
    # protects deep-linking but does not stop direct POSTs from clients that
    # bypass the form (HTMX retries, scripted callers, etc.).
    try:
        await svc.assert_step_prerequisites(user.session_id)
    except ConstraintActivationError as exc:
        return _render_constraints_error_banner(request, user, exc, status_code=400)
    field_errors: dict[str, str] = {}
    parsed_peak: float | None = None
    parsed_floor: float | None = None
    try:
        parsed_peak = float(peak_limit_kw)
    except ValueError:
        field_errors["peak_limit_kw"] = "Peak limit must be a number greater than 0 kW."
    try:
        parsed_floor = float(battery_reserve_floor_percent)
    except ValueError:
        field_errors["battery_reserve_floor_percent"] = (
            "Battery reserve floor must be a number between 0 and 100."
        )
    start_clean = ev_charging_window_start.strip() or None
    end_clean = ev_charging_window_end.strip() or None
    if (start_clean is None) != (end_clean is None):
        field_errors["ev_charging_window"] = (
            "EV charging window start and end must both be set or both empty."
        )
    if not field_errors and parsed_peak is not None and parsed_floor is not None:
        try:
            input_model = ConstraintDraftInput(
                peak_limit_kw=parsed_peak,
                battery_reserve_floor_percent=parsed_floor,
                ev_charging_window_start=start_clean,
                ev_charging_window_end=end_clean,
            )
        except ValidationError as exc:
            for err in exc.errors():
                loc = err.get("loc", ())
                # Pydantic model-level errors carry loc=(); route them to
                # the ev_charging_window slot since the only model-validator
                # we ship enforces the EV window paired-NULL invariant.
                field = str(loc[0]) if loc else "ev_charging_window"
                field_errors[field] = str(err.get("msg", "Invalid value."))
        else:
            await svc.upsert_draft(user.session_id, input=input_model, now=datetime.now(UTC))
    if field_errors:
        return _templates.TemplateResponse(
            request,
            "installer/_setup_constraints_form.html",
            {
                "csrf_token": user.csrf_token,
                "view": _form_view_from_request(
                    peak_limit_kw,
                    battery_reserve_floor_percent,
                    parsed_peak,
                    parsed_floor,
                    start_clean,
                    end_clean,
                ),
                "field_errors": field_errors,
            },
            status_code=400,
        )
    view = await svc.get_or_default(user.session_id)
    return _templates.TemplateResponse(
        request,
        "installer/_setup_constraints_form.html",
        {
            "csrf_token": user.csrf_token,
            "view": view,
            "field_errors": {},
        },
    )


@router.post("/installer/setup/constraints/validate", response_class=HTMLResponse)
async def post_validate_constraint_draft(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    svc: ConstraintsService = Depends(_constraints_service),  # noqa: B008
    csrf_token: str = Form(default=""),
) -> HTMLResponse:
    _ = csrf_token
    # Story 9.3 P12 — gate POST.
    try:
        await svc.assert_step_prerequisites(user.session_id)
    except ConstraintActivationError as exc:
        return _render_constraints_error_envelope(request, exc, status_code=400)
    try:
        report = await svc.validate_draft(user.session_id, now=datetime.now(UTC))
    except ConstraintActivationError as exc:
        return _render_constraints_error_envelope(request, exc, status_code=400)
    view = await svc.get_or_default(user.session_id)
    return _templates.TemplateResponse(
        request,
        "installer/_setup_constraints_validation.html",
        {
            "csrf_token": user.csrf_token,
            "report": report,
            "view": view,
            # Out-of-band swap target so the activate button enables/disables
            # in lockstep with the validation result.
            "render_activate_oob": True,
        },
    )


@router.post("/installer/setup/constraints/activate")
async def post_activate_constraints(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    svc: ConstraintsService = Depends(_constraints_service),  # noqa: B008
    csrf_token: str = Form(default=""),
) -> Response:
    _ = csrf_token
    is_htmx = request.headers.get("HX-Request", "").lower() == "true"
    try:
        result = await svc.activate_draft(user.session_id, actor="installer", now=datetime.now(UTC))
    except ConstraintActivationError as exc:
        # R2P5 — A re-click after a successful first activation lands here
        # with ``reason='step_3_already_complete'`` (the service's gate fires
        # on the second pass before any state change). Functionally the user
        # IS in the post-activation state; treat the redundant click as a
        # forward-redirect rather than an error banner so a double-click
        # doesn't turn a successful activation into a 400 in the user's view.
        if exc.reason == "step_3_already_complete":
            return _activate_success_redirect(is_htmx)
        return _render_constraints_error_banner(request, user, exc, status_code=400)
    logger.info(
        "constraints_activated_via_route",
        component="installer_setup",
        session_id=user.session_id,
        config_version=result.config_version,
    )
    return _activate_success_redirect(is_htmx)


def _activate_success_redirect(is_htmx: bool) -> Response:
    """R2P5 — return the correct redirect shape for the client class.

    Plain form POST: HTTP 302 ``Location: …`` — the browser follows.
    HTMX POST (``HX-Request: true``): HTTP 204 + ``HX-Redirect: …`` — HTMX
    does not follow 302 / 303 on ``hx-post`` by default, so the route must
    surface the redirect via the dedicated header. The activate form is
    plain HTML today; the HX-aware branch is a forward-compat guard so a
    future ``hx-post`` migration does not silently leave the user stuck on
    Step 3 with an injected redirect document.
    """
    target = "/installer/setup/validation"
    if is_htmx:
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(url=target, status_code=302)


@router.get("/installer/setup/validation", response_class=HTMLResponse)
async def get_validation_page(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    svc: DeploymentValidationService = Depends(_deployment_validation_service),  # noqa: B008
) -> Response:
    """Story 9.4 — Step 4 main page.

    Step-gate enforcement (mirrors 9.3): deep-linking past discovery / roles /
    constraints bounces back. There is no idempotent forward redirect here
    because Step 4 IS the terminal step of the wizard.
    """
    now = datetime.now(UTC)
    state = await wizard_repo.get_or_create(user.session_id, now=now)
    if not state.step_1_complete:
        return RedirectResponse(url="/installer/setup/discovery", status_code=302)
    if not state.step_2_complete:
        return RedirectResponse(url="/installer/setup/roles", status_code=302)
    if not state.step_3_complete:
        return RedirectResponse(url="/installer/setup/constraints", status_code=302)
    view = await svc.get_current_view()
    return _templates.TemplateResponse(
        request,
        "installer/setup_validation.html",
        {
            "csrf_token": user.csrf_token,
            "title": "Deployment Validation",
            "active_step": "validation",
            "step_2_complete": state.step_2_complete,
            "step_3_complete": state.step_3_complete,
            "step_4_complete": state.step_4_complete,
            "view": view,
        },
    )


@router.post("/installer/setup/validation/run", response_class=HTMLResponse)
async def post_run_validation(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    svc: DeploymentValidationService = Depends(_deployment_validation_service),  # noqa: B008
) -> HTMLResponse:
    """Trigger one validation run. Blocks until completion (validation runs
    are bounded by per-check timeouts; a slow run still returns within
    ~check_timeout_s × 1.x). Returns the full result fragment.

    P29 — CSRF is validated by `CsrfMiddleware` (it reads the header or the
    form body's `csrf_token` field directly). The route handler does not need
    a Form parameter for that, and previously accepting one with a silent
    default obscured where the validation actually happens.

    P13 — explicit step-3 gate so a curl/HTMX caller cannot bypass the
    GET-handler redirect chain.
    """
    await _require_step_3_complete(user.session_id, wizard_repo)
    await svc.run(triggered_by_session_id=user.session_id, now=datetime.now(UTC))
    view = await svc.get_current_view()
    response = _templates.TemplateResponse(
        request,
        "installer/_setup_validation_result.html",
        {
            "csrf_token": user.csrf_token,
            "view": view,
        },
    )
    # Signal HTMX polling clients (if any are subscribed) that the run is done.
    response.headers["HX-Trigger"] = "validation-complete"
    return response


@router.get("/installer/setup/validation/poll", response_class=HTMLResponse)
async def get_validation_poll(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    svc: DeploymentValidationService = Depends(_deployment_validation_service),  # noqa: B008
) -> HTMLResponse:
    """HTMX polling endpoint — read-only re-render of the result fragment.

    The page sets ``hx-trigger="every 2s"`` on this endpoint while
    ``overall_status='running'``. When the persisted state leaves running,
    this handler emits ``HX-Trigger: validation-complete`` so the client
    can stop polling.

    P13 — step gate prevents an authenticated installer who has not yet
    finished Steps 1-3 from polling and observing another session's run.
    """
    await _require_step_3_complete(user.session_id, wizard_repo)
    view = await svc.get_current_view()
    response = _templates.TemplateResponse(
        request,
        "installer/_setup_validation_result.html",
        {
            "csrf_token": user.csrf_token,
            "view": view,
        },
    )
    if view.result is not None and view.result.overall_status != "running":
        response.headers["HX-Trigger"] = "validation-complete"
    return response


@router.post(
    "/installer/setup/validation/acknowledge/{check_name}",
    response_class=HTMLResponse,
)
async def post_acknowledge_warning(
    request: Request,
    check_name: str,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    svc: DeploymentValidationService = Depends(_deployment_validation_service),  # noqa: B008
) -> HTMLResponse:
    await _require_step_3_complete(user.session_id, wizard_repo)
    name = _validate_check_name(check_name)
    try:
        await svc.acknowledge_warning(
            check_name=name,
            acknowledged_by_session_id=user.session_id,
            now=datetime.now(UTC),
        )
    except NoCurrentResultError as exc:
        raise HTTPException(status_code=400, detail="no_current_result") from exc
    except (NotAckEligibleError, CheckNotWarnableError) as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from exc
    view = await svc.get_current_view()
    return _templates.TemplateResponse(
        request,
        "installer/_setup_validation_result.html",
        {
            "csrf_token": user.csrf_token,
            "view": view,
        },
    )


@router.post(
    "/installer/setup/validation/revoke/{check_name}",
    response_class=HTMLResponse,
)
async def post_revoke_warning(
    request: Request,
    check_name: str,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    svc: DeploymentValidationService = Depends(_deployment_validation_service),  # noqa: B008
) -> HTMLResponse:
    await _require_step_3_complete(user.session_id, wizard_repo)
    name = _validate_check_name(check_name)
    try:
        await svc.revoke_warning(check_name=name)
    except NoCurrentResultError as exc:
        raise HTTPException(status_code=400, detail="no_current_result") from exc
    view = await svc.get_current_view()
    return _templates.TemplateResponse(
        request,
        "installer/_setup_validation_result.html",
        {
            "csrf_token": user.csrf_token,
            "view": view,
        },
    )


@router.post("/installer/setup/validation/handoff")
async def post_handoff(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
    svc: DeploymentValidationService = Depends(_deployment_validation_service),  # noqa: B008
) -> Response:
    """Finalize Step 4: gate-evaluator + wizard advance + success redirect.

    Server-side gate enforcement — the client's button state is hint only;
    the route re-evaluates the full handoff gate inside the write lock.
    Exact-match rejection reasons per AC8 / AC11.
    """
    await _require_step_3_complete(user.session_id, wizard_repo)
    is_htmx = request.headers.get("HX-Request", "").lower() == "true"
    try:
        await svc.mark_step_4_complete(session_id=user.session_id, now=datetime.now(UTC))
    except NotHandoffEligibleError as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from exc
    target = "/installer/handoff"
    if is_htmx:
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(url=target, status_code=302)


@router.get("/installer/handoff", response_class=HTMLResponse)
async def get_handoff_success(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
) -> Response:
    """Story 9.4 placeholder — minimal success page after the handoff gate
    passes. Story 9.5 will replace the body with the printable installer
    guide; this route stays put.
    """
    state = await wizard_repo.get_or_create(user.session_id, now=datetime.now(UTC))
    if not state.step_4_complete:
        return RedirectResponse(url="/installer/setup/validation", status_code=302)
    return _templates.TemplateResponse(
        request,
        "installer/_setup_validation_handoff_success.html",
        {
            "csrf_token": user.csrf_token,
            "title": "Handoff Complete",
            "active_step": "validation",
            "step_2_complete": state.step_2_complete,
            "step_3_complete": state.step_3_complete,
            "step_4_complete": state.step_4_complete,
            "step_4_completed_config_version": state.step_4_completed_config_version,
        },
    )


def _form_view_from_request(
    raw_peak: str,
    raw_floor: str,
    parsed_peak: float | None,
    parsed_floor: float | None,
    start: str | None,
    end: str | None,
) -> object:
    """Build a lightweight view preserving the user's submitted values so the
    error-state form fragment re-renders the rejected input verbatim.

    Story 9.3 P3 — previously this synthesized ``0.0`` for unparsable fields,
    which the template then formatted as ``"0.00"`` and showed back to the
    user, destroying their input. Now the raw form strings are carried on
    the view via ``raw_*_text`` and the template prefers them.
    """
    from open_ems.services.constraints import ConstraintDraftView

    return ConstraintDraftView(
        # The float fields remain typed because the template's existing
        # template-tag fallback uses them when no raw text is supplied. They
        # are 0.0 in the unparsable case but the template never renders the
        # 0.0 — it renders ``raw_*_text`` instead.
        peak_limit_kw=parsed_peak if parsed_peak is not None else 0.0,
        battery_reserve_floor_percent=parsed_floor if parsed_floor is not None else 0.0,
        ev_charging_window_start=start,
        ev_charging_window_end=end,
        validation_status="pending",
        validation_report=None,
        has_persisted_draft=False,
        raw_peak_limit_kw_text=raw_peak,
        raw_battery_reserve_floor_percent_text=raw_floor,
    )


def _render_constraints_error_banner(
    request: Request,
    user: InstallerUser,
    exc: ConstraintActivationError,
    *,
    status_code: int,
) -> HTMLResponse:
    """Render the activate-fail banner with operator-facing copy (P6)."""
    return _templates.TemplateResponse(
        request,
        "installer/_setup_constraints_error_banner.html",
        {
            "csrf_token": user.csrf_token,
            "reason": exc.reason,
            "reason_message": reason_to_user_message(exc.reason),
            "report": exc.report,
        },
        status_code=status_code,
    )


def _render_constraints_error_envelope(
    request: Request,
    exc: ConstraintActivationError,
    *,
    status_code: int,
) -> HTMLResponse:
    """Render the inline single-error envelope used by the validate route (P6).

    R2P14 — thread ``exc.report`` into the template context. The original
    envelope discarded the report payload entirely, so a validate call that
    triggered the rejection with a populated ``ConstraintValidationReport``
    showed only the top-level contract reason to the user — every
    check-level error was hidden. The template's ``{% if report is defined
    and report is not none %}`` guard renders nothing when the report is
    absent, so callers that don't have a report continue to render the same
    minimal envelope as before.
    """
    return _templates.TemplateResponse(
        request,
        "installer/_setup_constraints_error.html",
        {
            "reason": exc.reason,
            "reason_message": reason_to_user_message(exc.reason),
            "report": exc.report,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_modbus_target(row: object) -> ModbusScanTarget | None:
    if not isinstance(row, DeviceRegistryEntry):
        return None
    if row.protocol != "modbus_tcp":
        return None
    host_port = row.address.split(":")
    if len(host_port) != 2:
        return None
    host = host_port[0]
    try:
        port = int(host_port[1])
    except ValueError:
        return None
    # ``ModbusScanTarget`` field validators raise ``ValidationError`` (not
    # ``ValueError``) for out-of-range ports / empty hosts. Catching both lets
    # the caller surface a 400 inline-error instead of a 500.
    try:
        return ModbusScanTarget(
            device_id=row.device_id,
            host=host,
            port=port,
            model=row.model,
        )
    except ValidationError:
        return None


def _to_dsmr_target(row: object) -> DSMRScanTarget | None:
    if not isinstance(row, DeviceRegistryEntry):
        return None
    if row.protocol != "dsmr_p1":
        return None
    try:
        if row.address.startswith("/"):
            return DSMRScanTarget(device_id=row.device_id, serial_port=row.address)
        host_port = row.address.split(":")
        if len(host_port) != 2:
            return None
        try:
            port = int(host_port[1])
        except ValueError:
            return None
        return DSMRScanTarget(device_id=row.device_id, tcp_host=host_port[0], tcp_port=port)
    except ValidationError:
        return None
