"""Integration tests for /health/ready under loop-liveness states (Story 8.4 #31-#32)."""

from __future__ import annotations

import time
from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.services.loop_liveness import LoopLiveness
from open_ems.services.readiness import mark_ready, reset
from open_ems.web.routes.health import router


@pytest.fixture(autouse=True)
def clean_readiness() -> Generator[None, None, None]:
    reset()
    yield
    reset()


def _build_app(loop_liveness: LoopLiveness) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.loop_liveness = loop_liveness
    return app


def test_health_ready_reflects_control_loop_progress() -> None:
    """AC12 #31: /health/ready transitions 200 → 503 when liveness goes stale."""
    mark_ready()
    liveness = LoopLiveness(missed_cycle_threshold_seconds=0.5, cycle_deadline_seconds=60.0)

    app = _build_app(liveness)
    with TestClient(app) as client:
        # No tick yet → not alive → 503 control_loop_stalled
        before = client.get("/health/ready")
        assert before.status_code == 503
        assert before.json() == {"status": "degraded", "reason": "control_loop_stalled"}

        # Simulate a successful tick — sets last_cycle_completed_at_monotonic now
        liveness.mark_cycle_complete()
        ok = client.get("/health/ready")
        assert ok.status_code == 200
        assert ok.json() == {"status": "ready"}

        # Advance simulated monotonic clock past the threshold by editing the field
        # backwards (no real wait needed — emulates "no tick for >0.5s")
        liveness.last_cycle_completed_at_monotonic = time.monotonic() - 5.0
        stalled = client.get("/health/ready")
        assert stalled.status_code == 503
        assert stalled.json() == {"status": "degraded", "reason": "control_loop_stalled"}


def test_health_ready_reflects_fail_safe() -> None:
    """AC12 #32: a tick that enters fail-safe → /health/ready returns 503 fail_safe_active."""
    mark_ready()
    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)
    liveness.mark_cycle_complete()  # alive

    app = _build_app(liveness)
    with TestClient(app) as client:
        # Not yet in fail-safe
        ok = client.get("/health/ready")
        assert ok.status_code == 200

        # Simulate fail-safe entry
        liveness.mark_fail_safe_entered()
        fs = client.get("/health/ready")
        assert fs.status_code == 503
        assert fs.json() == {"status": "degraded", "reason": "fail_safe_active"}

        # Recovery
        liveness.mark_fail_safe_exited()
        liveness.mark_cycle_complete()
        ok2 = client.get("/health/ready")
        assert ok2.status_code == 200
        assert ok2.json() == {"status": "ready"}
