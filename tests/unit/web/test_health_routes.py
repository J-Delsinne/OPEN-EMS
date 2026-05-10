from __future__ import annotations

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


def _make_app(loop_liveness: LoopLiveness | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    if loop_liveness is not None:
        app.state.loop_liveness = loop_liveness
    return app


def _alive_liveness() -> LoopLiveness:
    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)
    liveness.mark_cycle_complete()
    return liveness


def _stale_liveness() -> LoopLiveness:
    return LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)


def test_liveness_always_alive() -> None:
    app = _make_app()
    with TestClient(app) as client:
        resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_readiness_503_before_ready() -> None:
    """Story 1.3 behaviour preserved: pre-mark_ready returns starting/503."""
    app = _make_app()
    with TestClient(app) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "starting"}


def test_readiness_503_starting_unchanged_when_not_ready() -> None:
    """AC12 #18: is_ready() False returns 503 starting — preserves Story 1.3."""
    app = _make_app(_alive_liveness())  # alive but not ready
    with TestClient(app) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "starting"}


def test_readiness_503_when_loop_not_alive() -> None:
    """AC12 #15: ready=True, alive=False → 503 control_loop_stalled."""
    mark_ready()
    app = _make_app(_stale_liveness())
    with TestClient(app) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "reason": "control_loop_stalled"}


def test_readiness_503_when_in_fail_safe() -> None:
    """AC12 #16: ready=True, alive=True, in_fail_safe=True → 503 fail_safe_active."""
    mark_ready()
    liveness = _alive_liveness()
    liveness.mark_fail_safe_entered()
    app = _make_app(liveness)
    with TestClient(app) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "reason": "fail_safe_active"}


def test_readiness_200_when_ready_alive_not_in_fail_safe() -> None:
    """AC12 #17: all three conditions met → 200 ready."""
    mark_ready()
    app = _make_app(_alive_liveness())
    with TestClient(app) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_readiness_503_when_loop_crashed() -> None:
    """Crashed liveness → 503 control_loop_stalled (crashed dominates is_alive)."""
    mark_ready()
    liveness = _alive_liveness()
    liveness.mark_crashed(RuntimeError("boom"))
    app = _make_app(liveness)
    with TestClient(app) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "reason": "control_loop_stalled"}


def test_liveness_unchanged_under_loop_state() -> None:
    """AC12 #19: /health/live always 200 regardless of LoopLiveness state."""
    mark_ready()
    liveness = _stale_liveness()  # not alive
    liveness.mark_crashed(RuntimeError("boom"))
    app = _make_app(liveness)
    with TestClient(app) as client:
        resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_readiness_503_crashed_takes_precedence_over_fail_safe() -> None:
    """When both crashed and in_fail_safe are True, the reason is control_loop_stalled.

    Pins the check ordering so a future re-ordering would surface as a test
    failure rather than a silent payload change.
    """
    mark_ready()
    liveness = _alive_liveness()
    liveness.mark_fail_safe_entered()
    liveness.mark_crashed(RuntimeError("boom"))  # also sets in_fail_safe=True
    app = _make_app(liveness)
    with TestClient(app) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "reason": "control_loop_stalled"}
