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
from open_ems.core.devices import CapabilityStatus, DeviceRole
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
async def get_constraints_placeholder(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    wizard_repo: WizardStateRepo = Depends(_wizard_state_repo),  # noqa: B008
) -> HTMLResponse:
    """Story 9.3 placeholder. Replaced by the real Step 3 page in 9.3."""
    state = await wizard_repo.get_or_create(user.session_id, now=datetime.now(UTC))
    return _templates.TemplateResponse(
        request,
        "installer/setup_constraints_placeholder.html",
        {
            "csrf_token": user.csrf_token,
            "title": "Constraints",
            "active_step": "constraints",
            # Surface step_2_complete so the layout's back-link logic
            # (active_step != "discovery" and not step_2_complete) sees the
            # truthy value and correctly hides the back-link once Step 2 is
            # finalized. Without this the back-link incorrectly renders.
            "step_2_complete": state.step_2_complete,
        },
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
