"""Story 11.1 — installer dashboard fragment endpoint tests.

Covers:
- AC3 health-indicator route + exhaustive operating_mode mapping
- AC4 device-rows distinct-DOM-element invariant
- AC5 peak-tracker label branches
- AC6 event-log-preview ordering, truncation, view-all link
- AC7+AC8 anomaly-notice render + dismiss flow
- AC9 link-query format
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


@pytest.fixture(autouse=True)
def _clear_dismiss_state() -> None:
    _reset_dismiss_state_for_tests()


async def _create_session(role: str) -> tuple[str, str]:
    """Returns (raw_token, session_id) for the new session."""
    user_id = await UserRepo(get_connection()).create(
        username=f"{role}_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role=role,
    )
    raw_token = generate_session_token()
    session_id = await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="test-csrf",
    )
    return raw_token, session_id


class _StubProvider(ActiveConstraintsProvider):
    """Test-only ActiveConstraintsProvider that returns a fixed payload.

    Subclasses the real provider so the dependency's isinstance check passes;
    overrides only ``get()``.
    """

    def __init__(self, constraints: ActiveConstraints | None) -> None:
        # Skip the parent init — we don't need its lock/state.
        self._constraints = constraints

    def get(self) -> ActiveConstraints:  # type: ignore[override]
        if self._constraints is None:
            raise RuntimeError("constraints unavailable")
        return self._constraints


def _app_with_store(
    store: StateStore,
    *,
    constraints: ActiveConstraints | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.state_store = store
    # Tests that exercise the peak-tracker fragment need a provider; for
    # other fragment tests the provider is unused.
    app.state.active_constraints_provider = _StubProvider(constraints)
    app.include_router(fragments_router)
    app.include_router(actions_router)
    return app


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


async def _setup_db_tables() -> None:
    conn = get_connection()
    await conn.execute(_CREATE_PEAK_INTERVALS)
    await conn.execute(_CREATE_EVENT_LOG)
    await conn.execute(_CREATE_DEVICE_REGISTRY)
    await conn.commit()


# ── AC3 health indicator ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "expected_display"),
    [
        (SystemOperatingMode.normal, "NORMAL"),
        (SystemOperatingMode.degraded, "DEGRADED"),
        (SystemOperatingMode.conservative, "DEGRADED"),
        (SystemOperatingMode.fail_safe, "FAILED"),
    ],
)
async def test_health_indicator_exhaustive_over_system_operating_mode_enum(
    session_repo: SessionRepo,
    mode: SystemOperatingMode,
    expected_display: str,
) -> None:
    """AC3: every SystemOperatingMode member maps to exactly one display string.

    Exhaustive parametrize guards against future enum members shipping
    without an entry in _INSTALLER_HEALTH_DISPLAY.
    """
    store = StateStore(system_clock_status="valid", operating_mode=mode)
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/health-indicator", cookies={"session": raw_token})
    assert response.status_code == 200
    assert f">{expected_display}<" in response.text


async def test_health_indicator_section_carries_self_refresh_binding(
    session_repo: SessionRepo,
) -> None:
    """AC9 / 10.4 pattern: the swapped-in fragment must carry hx-get +
    hx-trigger on its root <section> so polling continues after the outerHTML
    swap (closes 10-3 deferred D1 on the new installer surface)."""
    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/health-indicator", cookies={"session": raw_token})
    body = response.text
    assert 'hx-get="/fragments/installer/health-indicator"' in body
    assert 'hx-trigger="every 10s"' in body


# ── AC4 device rows: DISTINCT DOM elements ───────────────────────────────────


async def test_device_rows_render_component_state_and_capability_badge_as_distinct_dom_elements(
    session_repo: SessionRepo,
) -> None:
    """AC4 structural invariant: per-device-row's component_state and
    capability_badge MUST render as TWO DISTINCT DOM elements with TWO
    DISTINCT CSS class names. The two values are independently combinable;
    no class merges them."""
    await _setup_db_tables()
    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/device-rows", cookies={"session": raw_token})
    assert response.status_code == 200
    body = response.text
    # Each role appears with BOTH distinct CSS classes.
    assert "device-row__component-state" in body
    assert "device-row__capability-badge" in body
    # They must NOT appear as a merged class name.
    assert "device-row__component-state-capability" not in body
    assert "component-and-capability" not in body


async def test_device_rows_render_in_canonical_order(
    session_repo: SessionRepo,
) -> None:
    """AC4: rows render grid_meter → inverter → battery → ev_charger order."""
    await _setup_db_tables()
    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/device-rows", cookies={"session": raw_token})
    body = response.text
    grid_pos = body.find('data-role="grid_meter"')
    inverter_pos = body.find('data-role="inverter"')
    battery_pos = body.find('data-role="battery"')
    ev_pos = body.find('data-role="ev_charger"')
    assert 0 <= grid_pos < inverter_pos < battery_pos < ev_pos


async def test_device_rows_unknown_capability_when_no_registry_entry(
    session_repo: SessionRepo,
) -> None:
    """AC4: empty device_registry → all roles render UNKNOWN capability badge."""
    await _setup_db_tables()
    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/device-rows", cookies={"session": raw_token})
    body = response.text
    assert body.count(">UNKNOWN<") == 4  # one per role


# ── AC5 peak tracker label branches ──────────────────────────────────────────


async def test_peak_tracker_under_limit_renders_simple_label(
    session_repo: SessionRepo,
) -> None:
    """AC5: peak below 90% of limit → simple "Peak / Limit" label, no suffix."""
    await _setup_db_tables()
    conn = get_connection()
    # Insert a peak well below the limit.
    now = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    await conn.execute(
        "INSERT INTO peak_intervals (interval_start_utc, avg_power_kw, sample_count)"
        " VALUES (?, 12.4, 100)",
        (now.isoformat(),),
    )
    await conn.commit()

    store = StateStore(system_clock_status="valid")
    constraints = ActiveConstraints(
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        config_version=1,
        activated_at=datetime.now(UTC),
    )
    raw_token, _ = await _create_session("installer")
    client = TestClient(
        _app_with_store(store, constraints=constraints),
        base_url="https://test",
        follow_redirects=False,
    )

    response = client.get("/fragments/installer/peak-tracker", cookies={"session": raw_token})
    assert response.status_code == 200
    body = response.text
    assert "12.4 kW" in body
    assert "25.0 kW" in body
    assert "(approaching)" not in body
    assert "(exceeded)" not in body


async def test_peak_tracker_approaching_at_90_percent(
    session_repo: SessionRepo,
) -> None:
    """AC5: ratio >= 0.9 → "(approaching)" suffix."""
    await _setup_db_tables()
    conn = get_connection()
    now = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    await conn.execute(
        "INSERT INTO peak_intervals (interval_start_utc, avg_power_kw, sample_count)"
        " VALUES (?, 22.5, 100)",
        (now.isoformat(),),
    )
    await conn.commit()

    store = StateStore(system_clock_status="valid")
    constraints = ActiveConstraints(
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        config_version=1,
        activated_at=datetime.now(UTC),
    )
    raw_token, _ = await _create_session("installer")
    client = TestClient(
        _app_with_store(store, constraints=constraints),
        base_url="https://test",
        follow_redirects=False,
    )

    response = client.get("/fragments/installer/peak-tracker", cookies={"session": raw_token})
    assert "(approaching)" in response.text


async def test_peak_tracker_exceeded_when_over_limit(
    session_repo: SessionRepo,
) -> None:
    """AC5: peak >= limit → "(exceeded)" suffix."""
    await _setup_db_tables()
    conn = get_connection()
    now = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    await conn.execute(
        "INSERT INTO peak_intervals (interval_start_utc, avg_power_kw, sample_count)"
        " VALUES (?, 28.0, 100)",
        (now.isoformat(),),
    )
    await conn.commit()

    store = StateStore(system_clock_status="valid")
    constraints = ActiveConstraints(
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        config_version=1,
        activated_at=datetime.now(UTC),
    )
    raw_token, _ = await _create_session("installer")
    client = TestClient(
        _app_with_store(store, constraints=constraints),
        base_url="https://test",
        follow_redirects=False,
    )

    response = client.get("/fragments/installer/peak-tracker", cookies={"session": raw_token})
    assert "(exceeded)" in response.text


async def test_peak_tracker_no_data_message(
    session_repo: SessionRepo,
) -> None:
    """AC5: empty peak_intervals → "No data yet" message."""
    await _setup_db_tables()
    store = StateStore(system_clock_status="valid")
    constraints = ActiveConstraints(
        peak_limit_kw=25.0,
        battery_reserve_floor_percent=20.0,
        config_version=1,
        activated_at=datetime.now(UTC),
    )
    raw_token, _ = await _create_session("installer")
    client = TestClient(
        _app_with_store(store, constraints=constraints),
        base_url="https://test",
        follow_redirects=False,
    )

    response = client.get("/fragments/installer/peak-tracker", cookies={"session": raw_token})
    assert "No data yet" in response.text


async def test_peak_tracker_not_configured_when_constraints_unavailable(
    session_repo: SessionRepo,
) -> None:
    """AC5: constraints provider returns None → "Limit: not configured"."""
    await _setup_db_tables()
    conn = get_connection()
    now = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    await conn.execute(
        "INSERT INTO peak_intervals (interval_start_utc, avg_power_kw, sample_count)"
        " VALUES (?, 5.0, 100)",
        (now.isoformat(),),
    )
    await conn.commit()

    store = StateStore(system_clock_status="valid")
    # constraints=None — provider raises in .get(), get_active_constraints_optional returns None
    raw_token, _ = await _create_session("installer")
    client = TestClient(
        _app_with_store(store, constraints=None),
        base_url="https://test",
        follow_redirects=False,
    )

    response = client.get("/fragments/installer/peak-tracker", cookies={"session": raw_token})
    assert "not configured" in response.text


# ── AC6 event log preview ────────────────────────────────────────────────────


async def test_event_log_preview_empty_renders_placeholder(
    session_repo: SessionRepo,
) -> None:
    """AC6: empty event_log → calm placeholder text."""
    await _setup_db_tables()
    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/event-log-preview", cookies={"session": raw_token})
    assert response.status_code == 200
    assert "system is starting up" in response.text


async def test_event_log_preview_renders_5_most_recent(
    session_repo: SessionRepo,
) -> None:
    """AC6: limit=5; rows ordered by timestamp DESC, id DESC."""
    await _setup_db_tables()
    conn = get_connection()
    now = datetime.now(UTC)
    # Insert 7 entries; expect only the 5 newest.
    for i in range(7):
        await conn.execute(
            "INSERT INTO event_log (schema_version, timestamp, actor, event_type, summary)"
            " VALUES (1, ?, 'system', 'SYSTEM', ?)",
            ((now - timedelta(minutes=i)).isoformat(), f"event-{i}"),
        )
    await conn.commit()

    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/event-log-preview", cookies={"session": raw_token})
    body = response.text
    # Newest first: event-0, event-1, event-2, event-3, event-4
    assert body.count("event-0") == 1
    assert body.count("event-4") == 1
    # event-5 and event-6 should NOT appear (limit=5)
    assert "event-5" not in body
    assert "event-6" not in body


async def test_event_log_preview_summary_truncated_to_80_chars(
    session_repo: SessionRepo,
) -> None:
    """AC6: summary > 80 chars truncated with ellipsis."""
    await _setup_db_tables()
    conn = get_connection()
    long_summary = "A" * 100  # 100 chars > 80 budget
    await conn.execute(
        "INSERT INTO event_log (schema_version, timestamp, actor, event_type, summary)"
        " VALUES (1, ?, 'system', 'SYSTEM', ?)",
        (datetime.now(UTC).isoformat(), long_summary),
    )
    await conn.commit()

    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/event-log-preview", cookies={"session": raw_token})
    body = response.text
    # Truncated string: 79 A's + ellipsis = 80 chars total visible.
    assert "A" * 79 + "…" in body
    # The full 100-char string must not appear.
    assert "A" * 100 not in body


async def test_event_log_preview_includes_view_all_link(
    session_repo: SessionRepo,
) -> None:
    """AC6: "View all events" link is present with no pre-filter."""
    await _setup_db_tables()
    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/event-log-preview", cookies={"session": raw_token})
    body = response.text
    assert 'href="/installer/event-log"' in body
    # No query string on the View all link.
    assert 'href="/installer/event-log?' not in body


# ── AC7 + AC8 anomaly notice + dismiss flow ──────────────────────────────────


async def test_anomaly_notice_returns_empty_section_when_normal(
    session_repo: SessionRepo,
) -> None:
    """AC7: a published-normal snapshot has no anomaly → empty section but
    HTMX poll binding retained (10.4 pattern)."""
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    # Publish a real snapshot so sequence_id > 0 (cold-start path is separate test).
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
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})
    assert response.status_code == 200
    body = response.text
    # Section exists and retains its HTMX binding.
    assert 'id="installer-anomaly-notice"' in body
    assert 'hx-get="/fragments/installer/anomaly-notice"' in body
    # But no Dismiss button or summary rendered.
    assert "Dismiss" not in body


async def test_anomaly_notice_renders_full_notice_when_fail_safe(
    session_repo: SessionRepo,
) -> None:
    """AC7: fail_safe snapshot produces a FAIL anomaly with dismiss button."""
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    now = datetime.now(UTC)
    # Publish a fail_safe snapshot.
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
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})
    body = response.text
    assert "FAIL" in body
    assert "safe mode" in body
    assert "Dismiss" in body
    # Jinja autoescape encodes & as &amp; in href attributes — both forms accepted.
    assert (
        'href="/installer/event-log?type=SYSTEM&window=24h"' in body
        or 'href="/installer/event-log?type=SYSTEM&amp;window=24h"' in body
    )


async def test_anomaly_dismiss_and_re_render_round_trip(
    session_repo: SessionRepo,
) -> None:
    """AC8 + R2: POST dismiss → next GET returns empty section."""
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
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
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    # Pre-dismiss: full notice with Dismiss button.
    response = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})
    assert "Dismiss" in response.text

    # POST dismiss.
    response = client.post(
        "/actions/dismiss-anomaly",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert response.status_code == 200
    # The dismiss response is the empty section.
    assert "Dismiss" not in response.text
    assert 'id="installer-anomaly-notice"' in response.text

    # Next GET: still suppressed (same signature).
    response = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})
    assert "Dismiss" not in response.text


async def test_anomaly_dismiss_re_renders_on_severity_elevation(
    session_repo: SessionRepo,
) -> None:
    """AC8 + R2 elevation rule: WARN dismissed, then FAIL appears → re-render."""
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    now = datetime.now(UTC)
    # Publish a WARN-level snapshot first.
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
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    # Dismiss the WARN.
    client.post(
        "/actions/dismiss-anomaly",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
    )

    # Elevate to FAIL.
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

    response = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})
    # The FAIL elevation must re-render.
    assert "FAIL" in response.text
    assert "Dismiss" in response.text


async def test_anomaly_dismiss_idempotent_when_no_anomaly_active(
    session_repo: SessionRepo,
) -> None:
    """AC8: POST dismiss with no active anomaly is a no-op that returns empty."""
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
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
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.post(
        "/actions/dismiss-anomaly",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert response.status_code == 200
    assert "Dismiss" not in response.text
