"""Integration tests for the homeowner dashboard (Story 10.1 AC12 / AC13 / AC18 / AC19).

Drives the full FastAPI app (homeowner + fragment routers) against a real
in-memory StateStore. Covers:

* Normal-mode shell + headline + 3 cards happy path.
* Degraded-mode headline calm styling.
* AC12 — one device UNAVAILABLE; other cards continue rendering live values.
* AC13 — STALE caption appears with stable layout; body keeps last-known value.
* AC19 — the hand-authored CSS bundle is reachable via the StaticFiles mount.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import open_ems.web.app as app_module
from open_ems.core import (
    BatteryState,
    DeviceRole,
    EnergyStrategy,
    GridMeterState,
    InverterState,
    StateStore,
    SystemOperatingMode,
)
from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.routes.fragments import router as fragments_router
from open_ems.web.routes.homeowner import router as homeowner_router


async def _create_homeowner_session() -> str:
    user_id = await UserRepo(get_connection()).create(
        username=f"hw_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role="homeowner",
    )
    raw_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="test-csrf",
    )
    return raw_token


def _app(store: StateStore) -> FastAPI:
    app = FastAPI()
    app.state.state_store = store
    app.include_router(homeowner_router)
    app.include_router(fragments_router)
    return app


def _inverter(read_at: datetime) -> InverterState:
    return InverterState(
        device_id="inv-001",
        pv_power_kw=3.2,
        ac_power_kw=3.0,
        operating_mode="normal",
        read_at=read_at,
    )


def _battery(read_at: datetime) -> BatteryState:
    return BatteryState(
        device_id="bat-001",
        soc_percent=55.0,
        battery_power_kw=-1.5,
        capacity_kwh=10.0,
        operating_mode="self-use",
        read_at=read_at,
    )


def _grid_meter(received_at: datetime) -> GridMeterState:
    return GridMeterState(
        device_id="grid-001",
        grid_power_kw=1.2,
        energy_delivered_kwh=100.0,
        energy_returned_kwh=20.0,
        received_at=received_at,
    )


async def test_dashboard_shell_then_fetch_each_fragment_normal_mode(
    session_repo: SessionRepo,
) -> None:
    now = datetime.now(UTC)
    store = StateStore(
        system_clock_status="valid",
        operating_mode=SystemOperatingMode.normal,
        active_strategy=EnergyStrategy.minimize_cost,
    )
    await store.publish(
        {
            DeviceRole.inverter: _inverter(now),
            DeviceRole.battery: _battery(now),
            DeviceRole.grid_meter: _grid_meter(now),
        },
        operating_mode=SystemOperatingMode.normal,
    )
    raw = await _create_homeowner_session()
    client = TestClient(_app(store), base_url="https://test", follow_redirects=False)

    # 1. Shell — has all four HTMX-bound slots.
    shell = client.get("/homeowner/dashboard", cookies={"session": raw})
    assert shell.status_code == 200
    assert 'hx-get="/fragments/homeowner/status-headline"' in shell.text

    # 2. Headline — normal mode + Minimize Cost label.
    headline = client.get(
        "/fragments/homeowner/status-headline",
        cookies={"session": raw},
        headers={"HX-Request": "true"},
    )
    assert headline.status_code == 200
    assert 'data-presentation-mode="normal"' in headline.text
    assert "Your home is running on solar · Minimize Cost" in headline.text

    # 3. Each metric card — live primary value.
    battery = client.get(
        "/fragments/homeowner/battery-card",
        cookies={"session": raw},
        headers={"HX-Request": "true"},
    )
    assert battery.status_code == 200
    assert 'data-component-state="ACTIVE"' in battery.text
    assert "55%" in battery.text
    assert "Unavailable" not in battery.text

    solar = client.get(
        "/fragments/homeowner/solar-card",
        cookies={"session": raw},
        headers={"HX-Request": "true"},
    )
    assert solar.status_code == 200
    assert "3.2 kW" in solar.text

    grid = client.get(
        "/fragments/homeowner/grid-card",
        cookies={"session": raw},
        headers={"HX-Request": "true"},
    )
    assert grid.status_code == 200
    # Sign convention: positive grid_power_kw = importing.
    assert "1.2 kW importing" in grid.text


async def test_dashboard_degraded_mode_headline_uses_calm_styling(
    session_repo: SessionRepo,
) -> None:
    """AC7: degraded modes render slate-600 + explanation, no amber."""
    now = datetime.now(UTC)
    store = StateStore(
        system_clock_status="valid",
        operating_mode=SystemOperatingMode.degraded,
    )
    await store.publish(
        {
            DeviceRole.inverter: _inverter(now),
            DeviceRole.battery: _battery(now),
            DeviceRole.grid_meter: _grid_meter(now),
        },
        operating_mode=SystemOperatingMode.degraded,
    )
    raw = await _create_homeowner_session()
    client = TestClient(_app(store), base_url="https://test", follow_redirects=False)

    headline = client.get(
        "/fragments/homeowner/status-headline",
        cookies={"session": raw},
        headers={"HX-Request": "true"},
    )

    assert headline.status_code == 200
    assert 'data-presentation-mode="degraded"' in headline.text
    assert "Running with limited functionality" in headline.text
    assert "Some optimization features are reduced." in headline.text
    assert "status-headline--degraded" in headline.text
    lower = headline.text.lower()
    assert "amber" not in lower
    assert "color-fail" not in lower


async def test_one_device_unavailable_does_not_cascade_to_other_cards(
    session_repo: SessionRepo,
) -> None:
    """AC12: with battery UNAVAILABLE, solar and grid cards still render live values."""
    now = datetime.now(UTC)
    store = StateStore(system_clock_status="valid")
    # Publish without the battery slot — battery resolves to UNAVAILABLE.
    await store.publish(
        {
            DeviceRole.inverter: _inverter(now),
            DeviceRole.grid_meter: _grid_meter(now),
        }
    )
    raw = await _create_homeowner_session()
    client = TestClient(_app(store), base_url="https://test", follow_redirects=False)

    battery = client.get(
        "/fragments/homeowner/battery-card",
        cookies={"session": raw},
        headers={"HX-Request": "true"},
    )
    solar = client.get(
        "/fragments/homeowner/solar-card",
        cookies={"session": raw},
        headers={"HX-Request": "true"},
    )
    grid = client.get(
        "/fragments/homeowner/grid-card",
        cookies={"session": raw},
        headers={"HX-Request": "true"},
    )

    assert battery.status_code == solar.status_code == grid.status_code == 200
    # Battery card — UNAVAILABLE body, no zero fallback.
    assert 'data-component-state="UNAVAILABLE"' in battery.text
    assert "Unavailable" in battery.text
    assert "0%" not in battery.text
    # Solar + grid — fresh primary values.
    assert "3.2 kW" in solar.text
    assert "Unavailable" not in solar.text
    assert "1.2 kW importing" in grid.text
    assert "Unavailable" not in grid.text


async def test_stale_caption_appears_with_stable_layout(session_repo: SessionRepo) -> None:
    """AC13: STALE caption shows in stable bottom slot; body keeps last-known value."""
    now = datetime.now(UTC)
    store = StateStore(system_clock_status="valid", stale_threshold_seconds=30)
    # First publish — fresh battery.
    await store.publish({DeviceRole.battery: _battery(now)})
    # Second publish — stale battery (read 200s ago > 30s threshold).
    await store.publish(
        {
            DeviceRole.battery: _battery(now - timedelta(seconds=200)),
            DeviceRole.inverter: _inverter(now),
        }
    )
    raw = await _create_homeowner_session()
    client = TestClient(_app(store), base_url="https://test", follow_redirects=False)

    battery = client.get(
        "/fragments/homeowner/battery-card",
        cookies={"session": raw},
        headers={"HX-Request": "true"},
    )

    assert battery.status_code == 200
    assert 'data-component-state="STALE"' in battery.text
    assert "55%" in battery.text  # last-known value preserved
    assert "Battery data last updated" in battery.text
    # The stale caption is in the freshness footer; the card shell remains.
    assert "state-card__freshness" in battery.text


def test_static_css_bundle_is_servable_at_expected_url() -> None:
    """AC19 Path B safety check: the hand-authored CSS bundle MUST be present
    on disk and reachable through the StaticFiles mount.

    Catches the failure mode where the file is renamed, moved, or omitted from
    a partial deploy. ``StaticFiles`` does not raise at construction if the
    directory is missing; the failure surfaces only as a 404 at request time,
    which silently un-styles the entire homeowner dashboard.
    """
    static_dir = pathlib.Path(app_module.__file__).parent / "static"
    # On-disk presence — fail fast if the bundle is missing entirely.
    css_path = static_dir / "open-ems.css"
    assert css_path.is_file(), f"open-ems.css missing at {css_path}"

    # End-to-end servability — mount StaticFiles exactly as create_app() does
    # and confirm the asset is reachable with the expected content-type.
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    client = TestClient(app)
    response = client.get("/static/open-ems.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    # The body must contain at least the design-token root block — a sentinel
    # against an empty/truncated file slipping through.
    assert ":root" in response.text
    assert "--color-surface" in response.text
