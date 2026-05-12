from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

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


# ---------------------------------------------------------------------------
# Story 10.1 — status-headline route (AC6 / AC7 / AC15)
# ---------------------------------------------------------------------------


async def test_status_headline_route_rejects_unauthenticated_request(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get(
        "/fragments/homeowner/status-headline",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 401


async def test_status_headline_route_rejects_installer_role(
    session_repo: SessionRepo,
) -> None:
    store = StateStore(system_clock_status="valid")
    raw_token = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get(
        "/fragments/homeowner/status-headline",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 403


async def test_status_headline_normal_mode_renders_strategy_label(
    session_repo: SessionRepo,
) -> None:
    """AC5 / AC7: normal mode → 'Your home is running on solar · <label>'."""
    store = StateStore(
        system_clock_status="valid",
        operating_mode=SystemOperatingMode.normal,
        active_strategy=EnergyStrategy.minimize_cost,
    )
    raw_token = await _create_session("homeowner")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get(
        "/fragments/homeowner/status-headline",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'data-presentation-mode="normal"' in response.text
    assert 'data-active-strategy="minimize_cost"' in response.text
    assert "Your home is running on solar · Minimize Cost" in response.text
    assert 'aria-live="polite"' in response.text
    assert 'aria-label="System status"' in response.text


async def test_status_headline_degraded_mode_renders_calm_styling(
    session_repo: SessionRepo,
) -> None:
    """AC7: degraded mode uses slate-600 (degraded class) with NO amber CSS class."""
    store = StateStore(
        system_clock_status="valid",
        operating_mode=SystemOperatingMode.degraded,
    )
    raw_token = await _create_session("homeowner")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get(
        "/fragments/homeowner/status-headline",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'data-presentation-mode="degraded"' in response.text
    assert "Running with limited functionality" in response.text
    assert "status-headline--degraded" in response.text
    # AC7 design rule — NO amber, red, or warn CSS class fragments.
    lower = response.text.lower()
    assert "amber" not in lower
    assert "warn" not in lower
    # Allow tokens that contain "fail" as part of "fail_safe" data attribute,
    # but explicit fail-style classes (.fail, fail-, color-fail) must not appear.
    assert "color-fail" not in lower
    assert "state-card--fail" not in lower


# ---------------------------------------------------------------------------
# Story 10.3 — status-headline fragment includes the inline strategy selector
# ---------------------------------------------------------------------------


async def test_status_headline_fragment_includes_strategy_selector_panel_with_three_options(
    session_repo: SessionRepo,
) -> None:
    """AC8: headline fragment renders three strategy option buttons in enum order."""
    store = StateStore(
        system_clock_status="valid",
        operating_mode=SystemOperatingMode.normal,
        active_strategy=EnergyStrategy.maximize_self_consumption,
    )
    raw_token = await _create_session("homeowner")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get(
        "/fragments/homeowner/status-headline",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    body = response.text
    # Three option buttons in role=option with the EXACT enum-declaration order.
    minimize_idx = body.index('hx-vals=\'{"strategy": "minimize_cost"}\'')
    maximize_idx = body.index('hx-vals=\'{"strategy": "maximize_self_consumption"}\'')
    prioritize_idx = body.index('hx-vals=\'{"strategy": "prioritize_ev"}\'')
    assert minimize_idx < maximize_idx < prioritize_idx
    # Selector panel structure.
    assert 'id="strategy-selector-panel"' in body
    assert 'role="listbox"' in body
    assert body.count('role="option"') == 3
    # Failure slot present in DOM (visibility gated by Alpine x-show).
    assert 'id="strategy-failure-slot"' in body
    # Alpine factory bound.
    assert "strategySelector(" in body
    # CSRF token per-button hx-headers (survives outerHTML swap).
    assert "X-CSRF-Token" in body


async def test_status_headline_fragment_active_strategy_uses_no_pass_green_fragments(
    session_repo: SessionRepo,
) -> None:
    """AC13 + AC14 load-bearing design rule: active option uses accent left-border,
    NOT pass-green/amber/red/warn class fragments. Pairs with the 10.2 EV-card
    no-pass-green test as the second instance of the design-rule enforcement.
    """
    store = StateStore(
        system_clock_status="valid",
        operating_mode=SystemOperatingMode.normal,
        active_strategy=EnergyStrategy.prioritize_ev,
    )
    raw_token = await _create_session("homeowner")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get(
        "/fragments/homeowner/status-headline",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    body = response.text
    # Exactly one option has the --active modifier class.
    assert body.count("strategy-selector__option--active") == 1
    # The active option is prioritize_ev.
    active_match = re.search(
        r'<button[^>]*strategy-selector__option--active[^>]*hx-vals=\'\{"strategy": "([^"]+)"\}\'',
        body,
        re.DOTALL,
    )
    assert active_match is not None
    assert active_match.group(1) == "prioritize_ev"
    # No pass-green / amber / red / warn class fragments anywhere in the selector panel.
    lower = body.lower()
    assert "pass-green" not in lower
    assert "color-pass" not in lower
    assert "--color-pass" not in lower
    # 'amber' was already asserted absent above for degraded mode; reassert here
    # for the selector's own DOM tree just to pin the design rule on this surface.
    assert "amber" not in lower
    assert "color-warn" not in lower
