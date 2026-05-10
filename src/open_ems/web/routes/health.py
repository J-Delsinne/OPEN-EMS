from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from open_ems.services.readiness import is_ready

router = APIRouter()


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    if not is_ready():
        return JSONResponse({"status": "starting"}, status_code=503)
    # AC9 requires the lifespan to always populate app.state.loop_liveness; a
    # missing value is a misconfiguration that must fail closed (503), not open.
    loop_liveness = request.app.state.loop_liveness
    if loop_liveness.crashed or not loop_liveness.is_alive():
        return JSONResponse(
            {"status": "degraded", "reason": "control_loop_stalled"},
            status_code=503,
        )
    if loop_liveness.in_fail_safe:
        return JSONResponse(
            {"status": "degraded", "reason": "fail_safe_active"},
            status_code=503,
        )
    return JSONResponse({"status": "ready"})
