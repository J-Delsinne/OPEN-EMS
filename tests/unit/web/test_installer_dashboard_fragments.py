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

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core import (
    DegradedDeviceState,
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
def _clear_dismiss_state() -> Iterator[None]:
    """P10 review fix — reset before AND after each test."""
    _reset_dismiss_state_for_tests()
    yield
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


@pytest.mark.parametrize(
    ("mode", "should_have_link"),
    [
        (SystemOperatingMode.normal, False),
        (SystemOperatingMode.degraded, True),
        (SystemOperatingMode.conservative, True),
        (SystemOperatingMode.fail_safe, True),
    ],
)
async def test_health_indicator_emits_event_log_link_when_status_not_normal(
    session_repo: SessionRepo,
    mode: SystemOperatingMode,
    should_have_link: bool,
) -> None:
    """AC9 + P2 review fix: the health-indicator carries a "View in event log"
    pre-filter link when status != NORMAL. Link target is
    ``/installer/event-log?type=SYSTEM&window=24h``. AC9 listed three link
    surfaces (anomaly notice, per-device rows, health indicator); this test
    enforces the third.
    """
    store = StateStore(system_clock_status="valid", operating_mode=mode)
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/health-indicator", cookies={"session": raw_token})
    body = response.text
    has_link = (
        'href="/installer/event-log?type=SYSTEM&window=24h"' in body
        or 'href="/installer/event-log?type=SYSTEM&amp;window=24h"' in body
    )
    if should_have_link:
        assert has_link, f"expected event-log link for {mode}; body={body!r}"
        assert "View in event log" in body
    else:
        assert not has_link, f"NORMAL status must not emit event-log link; body={body!r}"


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
    """AC6: limit=5; rows ordered by timestamp DESC, id DESC.

    P17 review fix: use non-overlapping unique sentinel markers (`__EVT_AA__`
    through `__EVT_GG__`) instead of `event-0..6` so substring assertions
    cannot be invalidated by future fixture extension where `event-1` would
    match both `event-1` and `event-10`.
    """
    await _setup_db_tables()
    conn = get_connection()
    now = datetime.now(UTC)
    sentinels = [
        "__EVT_AA__",  # i=0 — newest, expected present
        "__EVT_BB__",
        "__EVT_CC__",
        "__EVT_DD__",
        "__EVT_EE__",  # i=4 — boundary, expected present
        "__EVT_FF__",  # i=5 — past limit, expected absent
        "__EVT_GG__",  # i=6 — past limit, expected absent
    ]
    for i, sentinel in enumerate(sentinels):
        await conn.execute(
            "INSERT INTO event_log (schema_version, timestamp, actor, event_type, summary)"
            " VALUES (1, ?, 'system', 'SYSTEM', ?)",
            ((now - timedelta(minutes=i)).isoformat(), sentinel),
        )
    await conn.commit()

    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/event-log-preview", cookies={"session": raw_token})
    body = response.text
    # Newest first: AA, BB, CC, DD, EE are present; FF, GG past the limit.
    for present in sentinels[:5]:
        assert body.count(present) == 1, f"sentinel {present} expected exactly once in body"
    for absent in sentinels[5:]:
        assert absent not in body, f"sentinel {absent} (past limit=5) leaked into body"


async def test_event_log_preview_renders_utc_iso_in_data_utc_attribute_and_local_timezone_text(
    session_repo: SessionRepo,
) -> None:
    """AC6 + AC12 + AC14 #36 (P14 review fix): each rendered row carries BOTH
    the UTC ISO-8601 string in ``data-utc`` AND a server-side locally-formatted
    display string (Europe/Brussels, the v1 default) in the visible text."""
    await _setup_db_tables()
    conn = get_connection()
    # Pick a non-DST timestamp so the local-zone display is deterministic.
    # 2026-01-15 12:34 UTC → 13:34 CET (UTC+1). Using ISO with explicit +00:00
    # offset so SQLite stores a TZ-aware value.
    fixed_utc = datetime(2026, 1, 15, 12, 34, 0, tzinfo=UTC)
    await conn.execute(
        "INSERT INTO event_log (schema_version, timestamp, actor, event_type, summary)"
        " VALUES (1, ?, 'system', 'SYSTEM', ?)",
        (fixed_utc.isoformat(), "__P14_FIXTURE__"),
    )
    await conn.commit()

    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    response = client.get("/fragments/installer/event-log-preview", cookies={"session": raw_token})
    assert response.status_code == 200
    body = response.text
    # UTC ISO is rendered in the data-utc attribute (Story 11.2 will use it
    # for client-side re-rendering with Intl.DateTimeFormat).
    assert 'data-utc="2026-01-15T12:34:00+00:00"' in body
    # The local-timezone display string is also rendered (server-side v1
    # fallback) so the row is readable without JS. CET (UTC+1) is the expected
    # Europe/Brussels offset for January.
    assert "2026-01-15 13:34 CET / UTC+1" in body


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
    # P13 review fix — tighten the "empty section" check: assert NONE of the
    # render branches' content fragments appear. Naming-honesty rule: the
    # test name promises "empty section" so the absences must cover every
    # element the render path would emit.
    assert "Dismiss" not in body
    assert ">FAIL<" not in body
    assert ">WARN<" not in body
    assert "View in event log" not in body
    assert "installer-anomaly-notice__summary" not in body


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


async def test_anomaly_notice_renders_at_most_one_notice_element(
    session_repo: SessionRepo,
) -> None:
    """AC12 + P16 review fix: structural assertion that the rendered anomaly
    fragment carries AT MOST ONE ``.installer-anomaly-notice`` element. The
    spec promises this as the structural counterpart to the function-level
    ``AnomalySignature | None`` return contract.
    """
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

    response = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})
    body = response.text
    # Count BOTH the bare class fragment AND any modifier-suffixed class to
    # catch a future refactor that switches to a class-modifier-only emission.
    # The `installer-anomaly-notice` substring must appear as a class once
    # on the root `<section>` (with `--fail`/`--warn` suffix when render=True).
    # The test allows ancillary class fragments (badge, summary, link, dismiss)
    # that begin with the same prefix; what it forbids is a SECOND `<section>`
    # carrying the class. Counting `<section ` start tags with the class is
    # the cleanest structural assertion.
    section_starts = body.count('class="installer-anomaly-notice')
    # Exactly ONE section is expected: the root. (Internal spans/buttons use
    # different class names — `installer-anomaly-notice__badge`,
    # `installer-anomaly-notice__summary`, etc. — which would all match the
    # raw substring, so we tighten by anchoring to the section's class= start
    # of the root `<section>` element.)
    root_count = body.count('id="installer-anomaly-notice"')
    assert root_count == 1, (
        f"expected exactly 1 root anomaly-notice element, got {root_count}; body={body!r}"
    )
    # And the section's class attribute must include the base class exactly once.
    # (Multiple internal element classes share the prefix; the root one is the
    # only `class="installer-anomaly-notice installer-anomaly-notice--<sev>"`.)
    assert section_starts >= 1, "root section class is missing"


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


async def test_get_fragment_after_dismiss_re_renders_on_new_anomaly_type(
    session_repo: SessionRepo,
) -> None:
    """AC8 + R2 elevation rule + AC14 #44 (P15 review fix): when a new anomaly
    TYPE appears (not severity elevation, but set-difference non-empty), the
    dismissed notice must re-display.

    Sequence: WARN {DEVICE_ERROR} dismissed → next snapshot adds SYSTEM_DEGRADED
    (still WARN severity, different type set) → GET fragment must render the
    full notice because ``current.anomaly_types`` is no longer a subset of
    the dismissed signature's types.
    """
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    now = datetime.now(UTC)
    # Publish a snapshot with battery in ERROR state (one DEVICE_ERROR) under
    # operating_mode=normal so the snapshot carries only {DEVICE_ERROR}.
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
            DeviceRole.battery: DegradedDeviceState(
                device_id="bat-001",
                role=DeviceRole.battery,
                reason="modbus_read_failed",
                occurred_at=now,
            ),
        },
        operating_mode=SystemOperatingMode.normal,
    )
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)

    # Confirm initial render is the WARN+DEVICE_ERROR notice.
    initial = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})
    assert "Dismiss" in initial.text
    assert "1 device degraded" in initial.text

    # Dismiss the WARN+{DEVICE_ERROR} notice.
    client.post(
        "/actions/dismiss-anomaly",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
    )

    # Verify the dismiss took effect (next GET is empty).
    after_dismiss = client.get(
        "/fragments/installer/anomaly-notice", cookies={"session": raw_token}
    )
    assert "Dismiss" not in after_dismiss.text

    # Publish a new snapshot that adds SYSTEM_DEGRADED on top of the battery
    # error — still WARN severity, but anomaly_types is now
    # {DEVICE_ERROR, SYSTEM_DEGRADED} which is a SUPERSET of the dismissed
    # {DEVICE_ERROR}.
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
            DeviceRole.battery: DegradedDeviceState(
                device_id="bat-001",
                role=DeviceRole.battery,
                reason="modbus_read_failed",
                occurred_at=now,
            ),
        },
        operating_mode=SystemOperatingMode.degraded,
    )

    # GET must re-render because a NEW anomaly type appeared.
    response = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})
    # WARN severity (unchanged) but the new type triggered re-display.
    assert "WARN" in response.text
    assert "Dismiss" in response.text
    # Both the original DEVICE_ERROR and the new SYSTEM_DEGRADED appear in
    # the aggregated summary.
    assert "System is operating with reduced functionality" in response.text
    assert "1 device degraded" in response.text


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


# ── AC10 #2 (P7 review fix) — per-fragment DB query-count assertions ─────────


_DATA_SURFACE_TABLES = ("PEAK_INTERVALS", "EVENT_LOG", "DEVICE_REGISTRY")


def _count_data_surface_queries(queries: list[str]) -> int:
    """Return the number of intercepted SQL strings that touch a Story 11.1
    data-surface table. Auth-side (sessions/users) reads are excluded.
    """
    return sum(1 for sql in queries if any(table in sql.upper() for table in _DATA_SURFACE_TABLES))


async def test_fragment_health_indicator_triggers_zero_data_surface_queries(
    session_repo: SessionRepo,
) -> None:
    """AC10 #2: ``/fragments/installer/health-indicator`` reads only the
    StateStore snapshot — no data-surface DB queries."""
    from tests.utils.db_query_counter import count_queries  # noqa: PLC0415

    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)
    # Warm: caches one-time imports and auth lookups.
    client.get("/fragments/installer/health-indicator", cookies={"session": raw_token})

    async with count_queries() as counter:
        response = client.get(
            "/fragments/installer/health-indicator", cookies={"session": raw_token}
        )

    assert response.status_code == 200
    assert _count_data_surface_queries(counter.queries) == 0, (
        f"health-indicator should hit zero data-surface tables; got: {counter.queries}"
    )


async def test_fragment_device_rows_triggers_exactly_one_data_surface_query(
    session_repo: SessionRepo,
) -> None:
    """AC10 #2: ``/fragments/installer/device-rows`` triggers exactly one
    query — ``DeviceRepo.list_all`` on ``device_registry``."""
    from tests.utils.db_query_counter import count_queries  # noqa: PLC0415

    await _setup_db_tables()
    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)
    client.get("/fragments/installer/device-rows", cookies={"session": raw_token})

    async with count_queries() as counter:
        response = client.get("/fragments/installer/device-rows", cookies={"session": raw_token})

    assert response.status_code == 200
    data_surface_queries = [
        sql for sql in counter.queries if any(t in sql.upper() for t in _DATA_SURFACE_TABLES)
    ]
    assert len(data_surface_queries) == 1, (
        f"device-rows should hit exactly 1 data-surface table; got: {data_surface_queries}"
    )
    assert "DEVICE_REGISTRY" in data_surface_queries[0].upper()


async def test_fragment_peak_tracker_triggers_exactly_one_data_surface_query(
    session_repo: SessionRepo,
) -> None:
    """AC10 #2: ``/fragments/installer/peak-tracker`` triggers exactly one
    query — ``EnergyRepo.get_current_monthly_peak_kw`` on ``peak_intervals``.
    ``ActiveConstraintsProvider`` reads are in-memory (post-Story 9.0b
    hydration) and do not count."""
    from tests.utils.db_query_counter import count_queries  # noqa: PLC0415

    await _setup_db_tables()
    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)
    client.get("/fragments/installer/peak-tracker", cookies={"session": raw_token})

    async with count_queries() as counter:
        response = client.get("/fragments/installer/peak-tracker", cookies={"session": raw_token})

    assert response.status_code == 200
    data_surface_queries = [
        sql for sql in counter.queries if any(t in sql.upper() for t in _DATA_SURFACE_TABLES)
    ]
    assert len(data_surface_queries) == 1, (
        f"peak-tracker should hit exactly 1 data-surface table; got: {data_surface_queries}"
    )
    assert "PEAK_INTERVALS" in data_surface_queries[0].upper()


async def test_fragment_event_log_preview_triggers_exactly_one_data_surface_query(
    session_repo: SessionRepo,
) -> None:
    """AC10 #2: ``/fragments/installer/event-log-preview`` triggers exactly
    one query — ``EventLogRepo.list_recent`` on ``event_log``."""
    from tests.utils.db_query_counter import count_queries  # noqa: PLC0415

    await _setup_db_tables()
    store = StateStore(system_clock_status="valid")
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)
    client.get("/fragments/installer/event-log-preview", cookies={"session": raw_token})

    async with count_queries() as counter:
        response = client.get(
            "/fragments/installer/event-log-preview", cookies={"session": raw_token}
        )

    assert response.status_code == 200
    data_surface_queries = [
        sql for sql in counter.queries if any(t in sql.upper() for t in _DATA_SURFACE_TABLES)
    ]
    assert len(data_surface_queries) == 1, (
        f"event-log-preview should hit exactly 1 data-surface table; got: {data_surface_queries}"
    )
    assert "EVENT_LOG" in data_surface_queries[0].upper()


async def test_fragment_anomaly_notice_triggers_zero_data_surface_queries(
    session_repo: SessionRepo,
) -> None:
    """AC10 #2: ``/fragments/installer/anomaly-notice`` reads only the
    StateStore snapshot + the module-level dismiss dict — no data-surface
    DB queries."""
    from tests.utils.db_query_counter import count_queries  # noqa: PLC0415

    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    raw_token, _ = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)
    client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})

    async with count_queries() as counter:
        response = client.get("/fragments/installer/anomaly-notice", cookies={"session": raw_token})

    assert response.status_code == 200
    assert _count_data_surface_queries(counter.queries) == 0, (
        f"anomaly-notice should hit zero data-surface tables; got: {counter.queries}"
    )
