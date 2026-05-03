from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from open_ems.core import (
    DeviceRole,
    GridMeterState,
    InverterState,
    StateStore,
)
from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.dependencies import require_stream_user
from open_ems.web.routes.stream import (
    _format_state_event,
    _parse_last_event_id,
    _state_event_generator,
    router,
)

_NOW_UTC = datetime(2026, 5, 3, 21, 30, 0, tzinfo=UTC)


class _FakeRequest:
    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


class _ScriptedClock:
    def __init__(self, *values: float) -> None:
        self._values = list(values)
        self._last = values[-1]

    def __call__(self) -> float:
        if self._values:
            self._last = self._values.pop(0)
        return self._last


def _request_for(app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/stream/state",
            "headers": [],
            "app": app,
        }
    )


def _request_with_session(app: FastAPI, raw_token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/stream/state",
            "headers": [(b"cookie", f"session={raw_token}".encode())],
            "app": app,
        }
    )


def _event_payload(event: str) -> dict[str, Any]:
    data_line = next(line for line in event.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


async def _always_valid(
    _session_token_hash: str,
    _session_id: str,
    _user_id: str,
    _role: str,
) -> bool:
    return True


async def _create_session(role: str, expires_at: datetime | None = None) -> str:
    user_id = await UserRepo(get_connection()).create(
        username=f"{role}_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role=role,
    )
    raw_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=expires_at or datetime.now(UTC) + timedelta(hours=4),
        csrf_token="test-csrf",
    )
    return raw_token


async def _session_details(raw_token: str) -> tuple[str, str]:
    session_row = await SessionRepo(get_connection()).get_by_token_hash(hash_token(raw_token))
    assert session_row is not None
    return str(session_row["id"]), str(session_row["user_id"])


async def _publish_snapshot(store: StateStore) -> None:
    await store.publish(
        {
            DeviceRole.inverter: InverterState(
                device_id="inv-001",
                pv_power_kw=3.2,
                ac_power_kw=3.0,
                operating_mode="normal",
                read_at=_NOW_UTC,
            ),
            DeviceRole.grid_meter: GridMeterState(
                device_id="grid-001",
                grid_power_kw=1.2,
                energy_delivered_kwh=100.0,
                energy_returned_kwh=20.0,
                received_at=_NOW_UTC,
            ),
        }
    )


def test_stream_route_rejects_unauthenticated_before_streaming(session_repo: SessionRepo) -> None:
    app = FastAPI()
    app.state.state_store = StateStore(system_clock_status="valid")
    app.include_router(router)

    response = TestClient(app, base_url="https://test").get("/api/stream/state")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


async def test_authenticated_roles_receive_role_specific_payloads(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)
    installer_token = await _create_session("installer")
    homeowner_token = await _create_session("homeowner")
    app = FastAPI()
    app.state.state_store = store
    app.include_router(router)

    installer_user = await require_stream_user(_request_with_session(app, installer_token))
    homeowner_user = await require_stream_user(_request_with_session(app, homeowner_token))
    installer_stream = _state_event_generator(
        request=_FakeRequest(),  # type: ignore[arg-type]
        store=store,
        role="installer",
        session_token_hash=installer_user.session_token_hash,
        session_id=installer_user.session_id,
        user_id=installer_user.user_id,
        session_expires_at=installer_user.expires_at,
        last_event_id=None,
        poll_interval_seconds=0.01,
    )
    homeowner_stream = _state_event_generator(
        request=_FakeRequest(),  # type: ignore[arg-type]
        store=store,
        role="homeowner",
        session_token_hash=homeowner_user.session_token_hash,
        session_id=homeowner_user.session_id,
        user_id=homeowner_user.user_id,
        session_expires_at=homeowner_user.expires_at,
        last_event_id=None,
        poll_interval_seconds=0.01,
    )
    installer_event = await anext(installer_stream)
    homeowner_event = await anext(homeowner_stream)
    await installer_stream.aclose()
    await homeowner_stream.aclose()

    installer_payload = _event_payload(installer_event)
    homeowner_payload = _event_payload(homeowner_event)
    assert "component_states" in installer_payload
    assert "component_states" not in homeowner_payload
    assert installer_payload["inverter"]["device_id"] == "inv-001"
    assert "device_id" not in json.dumps(homeowner_payload)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12", 12), (" 12 ", 12), ("bad", None), ("-1", None), ("", None)],
)
def test_parse_last_event_id(raw: str, expected: int | None) -> None:
    assert _parse_last_event_id(raw) == expected


async def test_sse_frame_contains_event_id_and_json_data() -> None:
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)
    frame = _format_state_event(store.get_snapshot(), "homeowner")

    assert frame.startswith("event: state_update\nid: 1\n")
    assert frame.endswith("\n\n")
    assert _event_payload(frame)["sequence_id"] == 1


async def test_generator_sends_current_snapshot_without_last_event_id() -> None:
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)

    generator = _state_event_generator(
        request=_FakeRequest(),  # type: ignore[arg-type]
        store=store,
        role="homeowner",
        session_token_hash="unused",
        session_id="unused",
        user_id="unused",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        last_event_id=None,
        poll_interval_seconds=0.01,
        session_validator=_always_valid,
    )

    event = await anext(generator)
    await generator.aclose()

    assert _event_payload(event)["sequence_id"] == 1
    assert "reason" not in event


async def test_generator_waits_when_last_event_id_matches_latest_then_sends_newer() -> None:
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)
    request = _FakeRequest()
    generator = _state_event_generator(
        request=request,  # type: ignore[arg-type]
        store=store,
        role="installer",
        session_token_hash="unused",
        session_id="unused",
        user_id="unused",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        last_event_id=1,
        poll_interval_seconds=0.01,
        session_validator=_always_valid,
    )

    pending = asyncio.create_task(anext(generator))
    await asyncio.sleep(0.02)
    assert not pending.done()

    await _publish_snapshot(store)
    event = await asyncio.wait_for(pending, timeout=1)
    await generator.aclose()

    assert _event_payload(event)["sequence_id"] == 2


async def test_generator_sends_latest_immediately_when_last_event_id_is_older() -> None:
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)
    await _publish_snapshot(store)
    generator = _state_event_generator(
        request=_FakeRequest(),  # type: ignore[arg-type]
        store=store,
        role="installer",
        session_token_hash="unused",
        session_id="unused",
        user_id="unused",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        last_event_id=1,
        poll_interval_seconds=0.01,
        session_validator=_always_valid,
    )

    event = await anext(generator)
    await generator.aclose()

    assert _event_payload(event)["sequence_id"] == 2


async def test_generator_treats_future_last_event_id_as_reset() -> None:
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)
    generator = _state_event_generator(
        request=_FakeRequest(),  # type: ignore[arg-type]
        store=store,
        role="homeowner",
        session_token_hash="unused",
        session_id="unused",
        user_id="unused",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        last_event_id=500,
        poll_interval_seconds=0.01,
        session_validator=_always_valid,
    )

    event = await anext(generator)
    await generator.aclose()

    assert _event_payload(event)["sequence_id"] == 1


@pytest.mark.parametrize("parsed_last_event_id", [None])
async def test_generator_ignores_malformed_or_negative_last_event_id(
    parsed_last_event_id: int | None,
) -> None:
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)
    generator = _state_event_generator(
        request=_FakeRequest(),  # type: ignore[arg-type]
        store=store,
        role="homeowner",
        session_token_hash="unused",
        session_id="unused",
        user_id="unused",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        last_event_id=parsed_last_event_id,
        poll_interval_seconds=0.01,
        session_validator=_always_valid,
    )

    event = await anext(generator)
    await generator.aclose()

    assert _event_payload(event)["sequence_id"] == 1


async def test_generator_does_not_emit_same_snapshot_twice() -> None:
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)
    generator = _state_event_generator(
        request=_FakeRequest(),  # type: ignore[arg-type]
        store=store,
        role="installer",
        session_token_hash="unused",
        session_id="unused",
        user_id="unused",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        last_event_id=None,
        poll_interval_seconds=0.01,
        session_validator=_always_valid,
    )

    first = await anext(generator)
    pending = asyncio.create_task(anext(generator))
    await asyncio.sleep(0.02)
    assert not pending.done()
    await _publish_snapshot(store)
    second = await asyncio.wait_for(pending, timeout=1)
    await generator.aclose()

    assert _event_payload(first)["sequence_id"] == 1
    assert _event_payload(second)["sequence_id"] == 2


async def test_keepalive_is_suppressed_until_interval_after_last_state_event() -> None:
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)
    clock = _ScriptedClock(0.0, 10.0, 24.9, 25.0)
    generator = _state_event_generator(
        request=_FakeRequest(),  # type: ignore[arg-type]
        store=store,
        role="homeowner",
        session_token_hash="unused",
        session_id="unused",
        user_id="unused",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        last_event_id=None,
        poll_interval_seconds=0.01,
        keepalive_seconds=15.0,
        session_validator=_always_valid,
        now=clock,
    )

    first = await anext(generator)
    pending = asyncio.create_task(anext(generator))
    await asyncio.sleep(0.05)
    keepalive = await asyncio.wait_for(pending, timeout=1)
    await generator.aclose()

    assert first.startswith("event: state_update")
    assert keepalive == ": keepalive\n\n"


async def test_deleted_session_closes_stream_before_next_event(session_repo: SessionRepo) -> None:
    raw_token = await _create_session("installer")
    repo = SessionRepo(get_connection())
    session_id, user_id = await _session_details(raw_token)
    await repo.delete_by_id(session_id)
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)

    generator = _state_event_generator(
        request=_FakeRequest(),  # type: ignore[arg-type]
        store=store,
        role="installer",
        session_token_hash=hash_token(raw_token),
        session_id=session_id,
        user_id=user_id,
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        last_event_id=0,
        poll_interval_seconds=0.01,
    )

    with pytest.raises(StopAsyncIteration):
        await anext(generator)


async def test_role_revocation_closes_installer_stream_before_next_event(
    session_repo: SessionRepo,
) -> None:
    raw_token = await _create_session("installer")
    session_id, user_id = await _session_details(raw_token)
    await get_connection().execute(
        "UPDATE users SET role = ? WHERE id = ?",
        ("homeowner", user_id),
    )
    await get_connection().commit()
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)

    generator = _state_event_generator(
        request=_FakeRequest(),  # type: ignore[arg-type]
        store=store,
        role="installer",
        session_token_hash=hash_token(raw_token),
        session_id=session_id,
        user_id=user_id,
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        last_event_id=0,
        poll_interval_seconds=0.01,
    )

    with pytest.raises(StopAsyncIteration):
        await anext(generator)


async def test_exact_expiry_boundary_is_invalid(session_repo: SessionRepo) -> None:
    raw_token = await _create_session("homeowner", expires_at=datetime.now(UTC))
    session_id, user_id = await _session_details(raw_token)
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)

    generator = _state_event_generator(
        request=_FakeRequest(),  # type: ignore[arg-type]
        store=store,
        role="homeowner",
        session_token_hash=hash_token(raw_token),
        session_id=session_id,
        user_id=user_id,
        session_expires_at=datetime.now(UTC),
        last_event_id=0,
        poll_interval_seconds=0.01,
    )

    with pytest.raises(StopAsyncIteration):
        await anext(generator)


async def test_multiple_generators_receive_later_snapshot_independently() -> None:
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)
    kwargs = {
        "store": store,
        "session_token_hash": "unused",
        "session_id": "unused",
        "user_id": "unused",
        "session_expires_at": datetime.now(UTC) + timedelta(hours=1),
        "last_event_id": 1,
        "poll_interval_seconds": 0.01,
        "session_validator": _always_valid,
    }
    first = _state_event_generator(request=_FakeRequest(), role="installer", **kwargs)  # type: ignore[arg-type]
    second = _state_event_generator(request=_FakeRequest(), role="homeowner", **kwargs)  # type: ignore[arg-type]

    first_pending = asyncio.create_task(anext(first))
    second_pending = asyncio.create_task(anext(second))
    await asyncio.sleep(0.02)
    await _publish_snapshot(store)

    events = await asyncio.wait_for(asyncio.gather(first_pending, second_pending), timeout=1)
    await first.aclose()
    await second.aclose()

    assert [_event_payload(event)["sequence_id"] for event in events] == [2, 2]


async def test_disconnected_client_does_not_block_active_stream_or_publish() -> None:
    store = StateStore(system_clock_status="valid")
    await _publish_snapshot(store)
    disconnected_request = _FakeRequest()
    disconnected_request.disconnected = True
    disconnected = _state_event_generator(
        request=disconnected_request,  # type: ignore[arg-type]
        store=store,
        role="installer",
        session_token_hash="unused",
        session_id="unused",
        user_id="unused",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        last_event_id=1,
        poll_interval_seconds=0.01,
        session_validator=_always_valid,
    )
    active = _state_event_generator(
        request=_FakeRequest(),  # type: ignore[arg-type]
        store=store,
        role="homeowner",
        session_token_hash="unused",
        session_id="unused",
        user_id="unused",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        last_event_id=1,
        poll_interval_seconds=0.01,
        session_validator=_always_valid,
    )

    with pytest.raises(StopAsyncIteration):
        await anext(disconnected)

    pending = asyncio.create_task(anext(active))
    await asyncio.sleep(0.02)
    await asyncio.wait_for(_publish_snapshot(store), timeout=1)
    event = await asyncio.wait_for(pending, timeout=1)
    await active.aclose()

    assert _event_payload(event)["sequence_id"] == 2
