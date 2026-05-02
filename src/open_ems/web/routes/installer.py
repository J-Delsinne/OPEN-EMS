from __future__ import annotations

import pathlib

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from open_ems.web.dependencies import InstallerUser, require_installer

router = APIRouter()

_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("/installer/dashboard", response_class=HTMLResponse)
async def installer_dashboard(
    request: Request,
    _user: InstallerUser = Depends(require_installer),
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        "dashboard.html",
        {"csrf_token": _user.csrf_token, "title": "Installer Dashboard"},
    )
