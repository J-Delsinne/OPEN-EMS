from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from open_ems.services.readiness import is_ready

router = APIRouter()


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    if is_ready():
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "starting"}, status_code=503)
