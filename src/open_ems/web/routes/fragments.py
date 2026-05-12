from __future__ import annotations

import pathlib

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from open_ems.core import StateStore
from open_ems.core.constraints import ActiveConstraints
from open_ems.settings import Settings
from open_ems.web.dependencies import (
    HomeownerUser,
    get_active_constraints_optional,
    get_settings_dep,
    get_state_store,
    require_homeowner,
)
from open_ems.web.state_serialization import (
    HomeownerCard,
    build_homeowner_card_context,
    build_homeowner_ev_card_context,
    build_homeowner_headline_context,
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
    _user: HomeownerUser = Depends(require_homeowner),  # noqa: B008
    store: StateStore = Depends(get_state_store),  # noqa: B008
) -> HTMLResponse:
    snapshot = store.get_snapshot()
    context = build_homeowner_headline_context(snapshot)
    return _templates.TemplateResponse(
        request,
        "fragments/homeowner/status-headline.html",
        context,
    )
