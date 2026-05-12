from __future__ import annotations

import pathlib
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from open_ems.storage.repositories.event_log_repo import EventLogRepo
from open_ems.web.dependencies import (
    InstallerUser,
    get_event_log_repo,
    require_installer,
)
from open_ems.web.event_log_filters import (
    EVENT_LOG_PAGE_SIZE,
    _parse_event_log_filters,
)
from open_ems.web.state_serialization import build_installer_event_log_page_context

router = APIRouter()

_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("/installer/dashboard", response_class=HTMLResponse)
async def installer_dashboard(
    request: Request,
    _user: InstallerUser = Depends(require_installer),  # noqa: B008
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "csrf_token": _user.csrf_token,
            "dashboard_role": "installer",
            "title": "Installer Dashboard",
        },
    )


@router.get("/installer/event-log", response_class=HTMLResponse)
async def installer_event_log(
    request: Request,
    user: InstallerUser = Depends(require_installer),  # noqa: B008
    event_log_repo: EventLogRepo = Depends(get_event_log_repo),  # noqa: B008
    type: str | None = None,
    device_id: str | None = None,
    window: str | None = None,
    from_date: Annotated[str | None, Query(alias="from")] = None,
    to_date: Annotated[str | None, Query(alias="to")] = None,
    q: str | None = None,
    offset: int = 0,
) -> HTMLResponse:
    """Story 11.2: full installer event-log page.

    Filter state is parsed from query params (silently drops invalid values
    for URL-tampering-resilience — see ``_parse_event_log_filters``). The
    page renders 50 rows + the "Load more" button when more rows match.

    Date interpretation: ``from``/``to`` are parsed as ``YYYY-MM-DD`` at
    midnight UTC (``to`` is advanced to next-day-midnight for exclusive
    upper-bound semantics). Installers in non-UTC locales should expect
    boundary events near local midnight to be filtered by UTC rather than
    local-clock — the page's filter-bar hint surfaces this to the user.
    """
    filt = _parse_event_log_filters(
        type=type,
        device_id=device_id,
        window=window,
        from_date=from_date,
        to_date=to_date,
        q=q,
    )
    safe_offset = max(0, offset)
    rows = await event_log_repo.list_filtered(
        event_types=filt.event_types,
        device_id=filt.device_id,
        since=filt.since,
        until=filt.until,
        keyword=filt.keyword,
        limit=EVENT_LOG_PAGE_SIZE,
        offset=safe_offset,
    )
    # AC8: count is computed only when a filter is active. Otherwise the
    # "Showing N of M" caption is not rendered; skipping the COUNT(*) avoids
    # the extra DB query in the common unfiltered case.
    total_count = (
        await event_log_repo.count_filtered(
            event_types=filt.event_types,
            device_id=filt.device_id,
            since=filt.since,
            until=filt.until,
            keyword=filt.keyword,
        )
        if filt.is_filtered()
        else len(rows)
    )
    context = build_installer_event_log_page_context(
        rows=rows,
        total_count=total_count,
        current_offset=safe_offset,
        filter=filt,
        page_size=EVENT_LOG_PAGE_SIZE,
        csrf_token=user.csrf_token,
    )
    context["title"] = "Event Log"
    return _templates.TemplateResponse(
        request,
        "installer/event_log.html",
        context,
    )
