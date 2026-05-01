from collections.abc import Generator

import pytest
from fastapi.responses import JSONResponse

from open_ems.services.readiness import mark_ready, reset
from open_ems.web.routes.health import liveness, readiness


@pytest.fixture(autouse=True)
def clean_readiness() -> Generator[None, None, None]:
    reset()
    yield
    reset()


async def test_liveness_always_alive() -> None:
    result = await liveness()
    assert result == {"status": "alive"}


async def test_readiness_503_before_ready() -> None:
    result = await readiness()
    assert isinstance(result, JSONResponse)
    assert result.status_code == 503


async def test_readiness_200_when_ready() -> None:
    mark_ready()
    result = await readiness()
    assert isinstance(result, JSONResponse)
    assert result.status_code == 200
