from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from open_ems.web.dependencies import HomeownerUser, require_homeowner

router = APIRouter()


@router.get("/homeowner/dashboard", response_class=HTMLResponse)
async def homeowner_dashboard(
    _user: HomeownerUser = Depends(require_homeowner),
) -> HTMLResponse:
    # Placeholder — replaced in Epic 10/11
    return HTMLResponse("<h1>Homeowner Dashboard</h1>")
