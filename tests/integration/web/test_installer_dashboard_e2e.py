"""Story 11.1 — installer dashboard integration tests.

Covers AC1 + AC7 + AC8 + AC9 end-to-end:
- Normal snapshot → no anomaly notice
- FAIL snapshot → anomaly notice with pre-filtered event-log link
- Dismiss + elevation round-trip
- Sidebar Setup link is wired
- A11y placeholder (xfail+skipif per Story 9.x precedent)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core import (
    DeviceRole,
    GridMeterState,
    InverterState,
    StateStore,
    SystemOperatingMode,
)
from open_ems.core.constraints import ActiveConstraints
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.services.installer_anomaly import _reset_dismiss_state_for_tests
from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.routes.actions import router as actions_router
from open_ems.web.routes.fragments import router as fragments_router
from open_ems.web.routes.installer import router as installer_router

_CREATE_PEAK_INTERVALS = """
    CREATE TABLE IF NOT EXISTS peak_intervals (
        interval_start_utc TEXT PRIMARY KEY,
        avg_power_kw REAL NOT NULL,
        sample_count INTEGER NOT NULL,
        data_quality TEXT NOT NULL DEFAULT 'complete'
    )
"""
_CREATE_EVENT_LOG = """
    CREATE TABLE IF NOT EXISTS event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schema_version INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        actor TEXT NOT NULL,
        event_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        detail TEXT,
        device_id TEXT,
        config_version TEXT
    )
"""
_CREATE_DEVICE_REGISTRY = """
    CREATE TABLE IF NOT EXISTS device_registry (
        device_id TEXT PRIMARY KEY,
        protocol TEXT NOT NULL,
        address TEXT NOT NULL,
        model TEXT,
        firmware_version TEXT,
        source TEXT NOT NULL,
        validated INTEGER NOT NULL DEFAULT 0,
        last_capability_status TEXT,
        last_limitation_reason TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT,
        installer_acknowledged_unvalidated_at TEXT,
        role TEXT,
        role_assigned_at TEXT
    )
"""


class _StubProvider(ActiveConstraintsProvider):
    def __init__(self, constraints: ActiveConstraints | None) -> None:
        self._constraints = constraints

    def get(self) -> ActiveConstraints:  # type: ignore[override]
        if self._constraints is None:
            raise RuntimeError("constraints unavailable")
        return self._constraints


@pytest.fixture(autouse=True)
def _clear_dismiss_state() -> None:
    _reset_dismiss_state_for_tests()


async def _setup_db_tables() -> None:
    conn = get_connection()
    await conn.execute(_CREATE_PEAK_INTERVALS)
    await conn.execute(_CREATE_EVENT_LOG)
    await conn.execute(_CREATE_DEVICE_REGISTRY)
    await conn.commit()


async def _create_installer_session() -> str:
    user_id = await UserRepo(get_connection()).create(
        username=f"inst_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role="installer",
    )
    raw_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="test-csrf",
    )
    return raw_token


def _app(store: StateStore, *, constraints: ActiveConstraints | None = None) -> FastAPI:
    app = FastAPI()
    app.state.state_store = store
    app.state.active_constraints_provider = _StubProvider(constraints)
    app.include_router(installer_router)
    app.include_router(fragments_router)
    app.include_router(actions_router)
    return app


async def _publish_normal_snapshot(store: StateStore) -> None:
    now = datetime.now(UTC)
    await store.publish(
        {
            DeviceRole.inverter: InverterState(
                device_id="inv-001",
                pv_power_kw=3.0,
                ac_power_kw=3.0,
                operating_mode="normal",
                read_at=now,
            ),
            DeviceRole.grid_meter: GridMeterState(
                device_id="grid-001",
                grid_power_kw=1.0,
                energy_delivered_kwh=100.0,
                energy_returned_kwh=20.0,
                received_at=now,
            ),
        },
        operating_mode=SystemOperatingMode.normal,
    )


async def _publish_fail_safe_snapshot(store: StateStore) -> None:
    now = datetime.now(UTC)
    await store.publish(
        {
            DeviceRole.inverter: InverterState(
                device_id="inv-001",
                pv_power_kw=3.0,
                ac_power_kw=3.0,
                operating_mode="normal",
                read_at=now,
            ),
            DeviceRole.grid_meter: GridMeterState(
                device_id="grid-001",
                grid_power_kw=1.0,
                energy_delivered_kwh=100.0,
                energy_returned_kwh=20.0,
                received_at=now,
            ),
        },
        operating_mode=SystemOperatingMode.fail_safe,
    )


# ── AC1 / AC7 — normal snapshot has no anomaly notice ────────────────────────


async def test_e2e_normal_snapshot_no_anomaly_notice_health_normal(
    session_repo: SessionRepo,
) -> None:
    await _setup_db_tables()
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    await _publish_normal_snapshot(store)
    raw_token = await _create_installer_session()
    client = TestClient(_app(store), base_url="https://test", follow_redirects=False)

    # Dashboard shell loads.
    response = client.get("/installer/dashboard", cookies={"session": raw_token})
    assert response.status_code == 200

    # Anomaly notice is empty (no Dismiss button).
    anomaly_response = client.get(
        "/fragments/installer/anomaly-notice", cookies={"session": raw_token}
    )
    assert "Dismiss" not in anomaly_response.text

    # Health indicator says NORMAL.
    health_response = client.get(
        "/fragments/installer/health-indicator", cookies={"session": raw_token}
    )
    assert ">NORMAL<" in health_response.text


# ── AC7 / AC9 — fail_safe snapshot renders FAIL notice + pre-filtered link ───


async def test_e2e_failed_snapshot_renders_fail_anomaly_with_event_log_link(
    session_repo: SessionRepo,
) -> None:
    await _setup_db_tables()
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    await _publish_fail_safe_snapshot(store)
    raw_token = await _create_installer_session()
    client = TestClient(_app(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})
    body = response.text
    assert "FAIL" in body
    assert "safe mode" in body
    # AC9: pre-filter link points at SYSTEM type + 24h window.
    assert "/installer/event-log?type=SYSTEM" in body
    assert "window=24h" in body


# ── AC8 — dismiss + elevation round-trip ─────────────────────────────────────


async def test_e2e_dismiss_and_elevate_round_trip(
    session_repo: SessionRepo,
) -> None:
    await _setup_db_tables()
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    raw_token = await _create_installer_session()
    client = TestClient(_app(store), base_url="https://test", follow_redirects=False)

    # T0: publish WARN-level snapshot.
    now = datetime.now(UTC)
    await store.publish(
        {
            DeviceRole.inverter: InverterState(
                device_id="inv-001",
                pv_power_kw=3.0,
                ac_power_kw=3.0,
                operating_mode="normal",
                read_at=now,
            ),
            DeviceRole.grid_meter: GridMeterState(
                device_id="grid-001",
                grid_power_kw=1.0,
                energy_delivered_kwh=100.0,
                energy_returned_kwh=20.0,
                received_at=now,
            ),
        },
        operating_mode=SystemOperatingMode.degraded,
    )

    # WARN notice renders.
    response = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})
    assert "WARN" in response.text
    assert "Dismiss" in response.text

    # T1: dismiss.
    response = client.post(
        "/actions/dismiss-anomaly",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert response.status_code == 200
    assert "Dismiss" not in response.text

    # T2: re-fetch — still suppressed.
    response = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})
    assert "Dismiss" not in response.text

    # T3: elevate to FAIL.
    await _publish_fail_safe_snapshot(store)

    # T4: re-fetch — FAIL notice re-renders (elevation rule).
    response = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})
    assert "FAIL" in response.text
    assert "Dismiss" in response.text


# ── AC2 — sidebar Setup link is wired ────────────────────────────────────────


async def test_e2e_sidebar_setup_link_present_in_shell(
    session_repo: SessionRepo,
) -> None:
    """AC2: the Setup sidebar item links to the existing Story 9.1 setup page."""
    store = StateStore(system_clock_status="valid")
    raw_token = await _create_installer_session()
    client = TestClient(_app(store), base_url="https://test", follow_redirects=False)

    response = client.get("/installer/dashboard", cookies={"session": raw_token})
    assert response.status_code == 200
    body = response.text
    # Setup link present.
    assert 'href="/installer/setup/discovery"' in body
    # Disabled-but-present placeholder links.
    assert 'aria-disabled="true"' in body


# ── AC14 #55 — a11y placeholder (xfail+skipif) ───────────────────────────────


@pytest.mark.xfail(reason="Story 11.1 AC14 #55 — placeholder for future a11y audit pass")
@pytest.mark.skipif(True, reason="Story 11.1 — a11y audit pass deferred to future story")
async def test_installer_dashboard_a11y_placeholder(session_repo: SessionRepo) -> None:
    """A11y audit placeholder — Story 9.x precedent. Will assert axe-clean
    rendering when the test infrastructure for accessibility is added."""
    raise AssertionError("a11y audit not yet implemented")
