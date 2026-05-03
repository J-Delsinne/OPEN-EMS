from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import HTTPException
from starlette.requests import Request

from open_ems.core import StateStore
from open_ems.web.dependencies import get_state_store


def _request_for(app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "app": app,
        }
    )


def test_get_state_store_returns_app_state_store() -> None:
    app = FastAPI()
    store = StateStore(system_clock_status="valid")
    app.state.state_store = store

    assert get_state_store(_request_for(app)) is store


def test_get_state_store_fails_clearly_when_missing() -> None:
    app = FastAPI()

    try:
        get_state_store(_request_for(app))
    except HTTPException as exc:
        assert exc.status_code == 503
        assert exc.detail == "State store unavailable"
    else:
        raise AssertionError("Expected missing StateStore to raise HTTPException")
