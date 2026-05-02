from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from open_ems.web.dependencies import InstallerUser, require_installer

router = APIRouter()


@router.get("/installer/dashboard", response_class=HTMLResponse)
async def installer_dashboard(
    _user: InstallerUser = Depends(require_installer),
) -> HTMLResponse:
    # Placeholder — replaced in Epic 10/11
    return HTMLResponse("<h1>Installer Dashboard</h1>")
