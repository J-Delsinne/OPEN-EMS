from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core import BatteryState, DeviceRole, GridMeterState, InverterState, StateStore
from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.routes.fragments import router


async def _create_session(role: str) -> str:
    user_id = await UserRepo(get_connection()).create(
        username=f"{role}_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role=role,
    )
    raw_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="test-csrf",
    )
    return raw_token


def _app_with_store(store: StateStore) -> FastAPI:
    app = FastAPI()
    app.state.state_store = store
    app.include_router(router)
    return app


async def _publish_snapshot(store: StateStore, *, stale_battery: bool = False) -> None:
    now = datetime.now(UTC)
    battery_read_at = now - timedelta(seconds=185) if stale_battery else now
    await store.publish(
        {
            DeviceRole.inverter: InverterState(
                device_id="inv-001",
                pv_power_kw=3.2,
                ac_power_kw=3.0,
                operating_mode="normal",
                read_at=now,
            ),
            DeviceRole.battery: BatteryState(
                device_id="bat-001",
                soc_percent=55.0,
                battery_power_kw=-1.5,
                capacity_kwh=10.0,
                operating_mode="self-use",
                read_at=battery_read_at,
            ),
            DeviceRole.grid_meter: GridMeterState(
                device_id="grid-001",
                grid_power_kw=1.2,
                energy_delivered_kwh=100.0,
                energy_returned_kwh=20.0,
                received_at=now,
            ),
        }
    )


def test_fragment_route_rejects_unauthenticated_htmx_request(session_repo: SessionRepo) -> None:
    store = StateStore(system_clock_status="valid")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get(
        "/fragments/homeowner/battery-card",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 401


async def test_fragment_route_rejects_installer_role(session_repo: SessionRepo) -> None:
    store = StateStore(system_clock_status="valid")
    raw_token = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get(
        "/fragments/homeowner/battery-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 403


async def test_stale_fragment_uses_homeowner_safe_values_and_caption(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="suspect", stale_threshold_seconds=30)
    await _publish_snapshot(store, stale_battery=True)
    raw_token = await _create_session("homeowner")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get(
        "/fragments/homeowner/battery-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'data-component-state="STALE"' in response.text
    assert 'data-clock-status="suspect"' in response.text
    assert "55%" in response.text
    assert "Battery data last updated 3 min ago" in response.text
    assert "device_id" not in response.text
    assert "reason" not in response.text


async def test_fresh_fragment_omits_stale_caption(session_repo: SessionRepo) -> None:
    store = StateStore(system_clock_status="valid", stale_threshold_seconds=30)
    await _publish_snapshot(store)
    raw_token = await _create_session("homeowner")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get(
        "/fragments/homeowner/battery-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'data-component-state="ACTIVE"' in response.text
    assert "last updated" not in response.text


async def test_unavailable_fragment_renders_explicit_text_not_zero(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    await store.publish(
        {
            DeviceRole.inverter: InverterState(
                device_id="inv-001",
                pv_power_kw=3.2,
                ac_power_kw=3.0,
                operating_mode="normal",
                read_at=datetime.now(UTC),
            ),
            DeviceRole.grid_meter: GridMeterState(
                device_id="grid-001",
                grid_power_kw=1.2,
                energy_delivered_kwh=100.0,
                energy_returned_kwh=20.0,
                received_at=datetime.now(UTC),
            ),
        }
    )
    raw_token = await _create_session("homeowner")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get(
        "/fragments/homeowner/battery-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'data-component-state="UNAVAILABLE"' in response.text
    assert "Unavailable" in response.text
    assert "0 kW" not in response.text
    assert "0%" not in response.text
