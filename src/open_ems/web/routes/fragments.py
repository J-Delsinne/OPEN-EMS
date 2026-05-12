from __future__ import annotations

import pathlib

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from open_ems.core import StateStore
from open_ems.core.constraints import ActiveConstraints
from open_ems.services.installer_anomaly import get_dismissed_signature
from open_ems.settings import Settings
from open_ems.storage.repositories.device_repo import DeviceRepo
from open_ems.storage.repositories.energy_repo import EnergyRepo
from open_ems.storage.repositories.event_log_repo import EventLogRepo
from open_ems.web.dependencies import (
    HomeownerUser,
    InstallerUser,
    get_active_constraints_optional,
    get_device_repo,
    get_energy_repo,
    get_event_log_repo,
    get_settings_dep,
    get_state_store,
    require_homeowner,
    require_installer,
)
from open_ems.web.state_serialization import (
    HomeownerCard,
    build_homeowner_card_context,
    build_homeowner_ev_card_context,
    build_homeowner_headline_context,
    build_homeowner_weekly_summary_context,
    build_installer_anomaly_notice_context,
    build_installer_device_row_context,
    build_installer_event_log_preview_context,
    build_installer_health_indicator_context,
    build_installer_peak_tracker_context,
)

router = APIRouter()

_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _homeowner_card_response(
    request: Request,
    store: StateStore,
    card: HomeownerCard,
) -> HTMLResponse:
    snapshot = store.get_snapshot()
    context = build_homeowner_card_context(snapshot, card)
    return _templates.TemplateResponse(
        request,
        f"fragments/homeowner/{card}-card.html",
        context,
    )


@router.get("/fragments/homeowner/battery-card", response_class=HTMLResponse)
async def homeowner_battery_card(
    request: Request,
    _user: HomeownerUser = Depends(require_homeowner),  # noqa: B008
    store: StateStore = Depends(get_state_store),  # noqa: B008
) -> HTMLResponse:
    return _homeowner_card_response(request, store, "battery")


@router.get("/fragments/homeowner/solar-card", response_class=HTMLResponse)
async def homeowner_solar_card(
    request: Request,
    _user: HomeownerUser = Depends(require_homeowner),  # noqa: B008
    store: StateStore = Depends(get_state_store),  # noqa: B008
) -> HTMLResponse:
    return _homeowner_card_response(request, store, "solar")


@router.get("/fragments/homeowner/grid-card", response_class=HTMLResponse)
async def homeowner_grid_card(
    request: Request,
    _user: HomeownerUser = Depends(require_homeowner),  # noqa: B008
    store: StateStore = Depends(get_state_store),  # noqa: B008
) -> HTMLResponse:
    return _homeowner_card_response(request, store, "grid")


@router.get("/fragments/homeowner/ev-card", response_class=HTMLResponse)
async def homeowner_ev_card(
    request: Request,
    user: HomeownerUser = Depends(require_homeowner),  # noqa: B008
    store: StateStore = Depends(get_state_store),  # noqa: B008
    constraints: ActiveConstraints | None = Depends(get_active_constraints_optional),  # noqa: B008
    settings: Settings = Depends(get_settings_dep),  # noqa: B008
) -> HTMLResponse:
    """Story 10.2 AC13: EV card fragment with 4-state override model."""
    snapshot = store.get_snapshot()
    context = build_homeowner_ev_card_context(
        snapshot,
        constraints,
        confirmation_timeout_seconds=settings.ev_override_confirmation_timeout_seconds,
    )
    context["csrf_token"] = user.csrf_token
    return _templates.TemplateResponse(
        request,
        "fragments/homeowner/ev-card.html",
        context,
    )


@router.get("/fragments/homeowner/status-headline", response_class=HTMLResponse)
async def homeowner_status_headline(
    request: Request,
    user: HomeownerUser = Depends(require_homeowner),  # noqa: B008
    store: StateStore = Depends(get_state_store),  # noqa: B008
    settings: Settings = Depends(get_settings_dep),  # noqa: B008
) -> HTMLResponse:
    """Story 10.3 AC8: headline fragment + inline strategy selector.

    The route injects ``csrf_token``, ``strategy_update_confirmation_timeout_seconds``,
    and ``failure_message`` at the context level (NOT through
    ``build_homeowner_headline_context``) so the builder stays pure-functional
    over ``SystemSnapshot`` only. ``failure_message`` is the single source of
    truth for the calm-notice copy used by both server-rendered failure
    fragments and Alpine-side fail paths.
    """
    # Local import avoids a circular dependency between fragments.py and
    # actions.py (actions.py imports build_homeowner_headline_context from
    # state_serialization, which fragments.py also uses). Importing here keeps
    # the failure-message constant single-sourced in actions.py without
    # introducing a top-level cycle.
    from open_ems.web.routes.actions import _STRATEGY_FAILURE_MESSAGE

    snapshot = store.get_snapshot()
    context = build_homeowner_headline_context(snapshot)
    context["csrf_token"] = user.csrf_token
    context["strategy_update_confirmation_timeout_seconds"] = (
        settings.strategy_update_confirmation_timeout_seconds
    )
    context["failure_message"] = _STRATEGY_FAILURE_MESSAGE
    return _templates.TemplateResponse(
        request,
        "fragments/homeowner/status-headline.html",
        context,
    )


# ── Story 11.1 — installer dashboard fragment endpoints ─────────────────────


@router.get("/fragments/installer/health-indicator", response_class=HTMLResponse)
async def installer_health_indicator(
    request: Request,
    _user: InstallerUser = Depends(require_installer),  # noqa: B008
    store: StateStore = Depends(get_state_store),  # noqa: B008
) -> HTMLResponse:
    """Story 11.1 AC3: system health indicator with direct SystemOperatingMode mapping."""
    snapshot = store.get_snapshot()
    context = build_installer_health_indicator_context(snapshot)
    return _templates.TemplateResponse(
        request,
        "fragments/installer/health-indicator.html",
        context,
    )


@router.get("/fragments/installer/device-rows", response_class=HTMLResponse)
async def installer_device_rows(
    request: Request,
    _user: InstallerUser = Depends(require_installer),  # noqa: B008
    store: StateStore = Depends(get_state_store),  # noqa: B008
    device_repo: DeviceRepo = Depends(get_device_repo),  # noqa: B008
) -> HTMLResponse:
    """Story 11.1 AC4: per-device rows with distinct component_state + capability_badge."""
    snapshot = store.get_snapshot()
    registry_entries = await device_repo.list_all()
    context = build_installer_device_row_context(snapshot, registry_entries)
    return _templates.TemplateResponse(
        request,
        "fragments/installer/device-rows.html",
        context,
    )


@router.get("/fragments/installer/peak-tracker", response_class=HTMLResponse)
async def installer_peak_tracker(
    request: Request,
    _user: InstallerUser = Depends(require_installer),  # noqa: B008
    energy_repo: EnergyRepo = Depends(get_energy_repo),  # noqa: B008
    constraints: ActiveConstraints | None = Depends(get_active_constraints_optional),  # noqa: B008
) -> HTMLResponse:
    """Story 11.1 AC5: current-month peak vs configured limit."""
    peak_kw = await energy_repo.get_current_monthly_peak_kw()
    peak_limit_kw = constraints.peak_limit_kw if constraints is not None else None
    context = build_installer_peak_tracker_context(peak_kw, peak_limit_kw)
    return _templates.TemplateResponse(
        request,
        "fragments/installer/peak-tracker.html",
        context,
    )


@router.get("/fragments/installer/event-log-preview", response_class=HTMLResponse)
async def installer_event_log_preview(
    request: Request,
    _user: InstallerUser = Depends(require_installer),  # noqa: B008
    event_log_repo: EventLogRepo = Depends(get_event_log_repo),  # noqa: B008
) -> HTMLResponse:
    """Story 11.1 AC6: 5 most recent event_log rows with "View all" link."""
    entries = await event_log_repo.list_recent(limit=5)
    context = build_installer_event_log_preview_context(entries)
    return _templates.TemplateResponse(
        request,
        "fragments/installer/event-log-preview.html",
        context,
    )


@router.get("/fragments/installer/anomaly-notice", response_class=HTMLResponse)
async def installer_anomaly_notice(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    store: StateStore = Depends(get_state_store),  # noqa: B008
) -> HTMLResponse:
    """Story 11.1 AC7 + AC8: single deterministic anomaly notice with dismiss/elevation."""
    snapshot = store.get_snapshot()
    dismissed = get_dismissed_signature(user.session_id)
    context = build_installer_anomaly_notice_context(snapshot, dismissed)
    context["csrf_token"] = user.csrf_token
    return _templates.TemplateResponse(
        request,
        "fragments/installer/anomaly-notice.html",
        context,
    )


@router.get("/fragments/homeowner/weekly-summary", response_class=HTMLResponse)
async def homeowner_weekly_summary(
    request: Request,
    _user: HomeownerUser = Depends(require_homeowner),  # noqa: B008
    energy_repo: EnergyRepo = Depends(get_energy_repo),  # noqa: B008
) -> HTMLResponse:
    """Story 10.4 AC8: pre-aggregated weekly energy summary.

    Single indexed read from ``weekly_energy_summary`` + template render. NO
    live computation, NO heavy on-demand query — the aggregator pre-computes the
    row periodically (``weekly_summary_aggregation_interval_seconds`` cadence).

    The 200ms-on-Pi-4 budget is the AC's hard performance contract; the
    implementation path here is one SELECT WHERE id=1 + Jinja render.
    """
    row = await energy_repo.read_weekly_energy_summary()
    context = build_homeowner_weekly_summary_context(row)
    return _templates.TemplateResponse(
        request,
        "fragments/homeowner/weekly-summary.html",
        context,
    )
