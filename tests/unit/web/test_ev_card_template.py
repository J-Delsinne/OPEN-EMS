"""Story 10.2 — EV card fragment template tests (AC18 #28–#33)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core import (
    DegradedDeviceState,
    DeviceRole,
    EVChargerState,
    EVOverrideState,
    StateStore,
)
from open_ems.core.constraints import ActiveConstraints
from open_ems.services.active_constraints import ActiveConstraintsProvider
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


def _active_constraints() -> ActiveConstraints:
    return ActiveConstraints(
        peak_limit_kw=5.0,
        battery_reserve_floor_percent=20.0,
        config_version=1,
        activated_at=datetime.now(UTC),
        ev_charging_window_start="22:30",
        ev_charging_window_end="06:00",
    )


def _app_with_store(
    store: StateStore,
    constraints: ActiveConstraints | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.state_store = store
    if constraints is not None:
        provider = ActiveConstraintsProvider.from_snapshot(constraints)
        app.state.active_constraints_provider = provider
    app.include_router(router)
    return app


def _ev_state(*, session_active: bool = False) -> EVChargerState:
    now = datetime.now(UTC)
    return EVChargerState(
        device_id="ev-001",
        status="charging" if session_active else "available",
        session_active=session_active,
        current_power_kw=7.0 if session_active else 0.0,
        power_source="meter_values",
        power_measured_at=now,
        read_at=now,
    )


def _pending_override() -> EVOverrideState:
    now = datetime.now(UTC)
    return EVOverrideState(
        correlation_id=uuid.uuid4(),
        requested_at=now,
        expires_at=now + timedelta(hours=2),
        dispatch_status="pending",
    )


def _failed_override(reason: str = "capability_check_failed") -> EVOverrideState:
    now = datetime.now(UTC)
    return EVOverrideState(
        correlation_id=uuid.uuid4(),
        requested_at=now,
        expires_at=now + timedelta(hours=2),
        dispatch_status="failed",
        failure_reason=reason,
    )


async def test_ev_card_renders_button_in_idle_state(session_repo: SessionRepo) -> None:
    """AC18 #28 — idle state has the right data-attr, aria-disabled, button label."""
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state()})
    raw_token = await _create_session("homeowner")
    client = TestClient(
        _app_with_store(store, _active_constraints()),
        base_url="https://test",
        follow_redirects=False,
    )
    resp = client.get(
        "/fragments/homeowner/ev-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert 'data-override-state="idle"' in resp.text
    assert "Charge EV now" in resp.text
    assert 'aria-disabled="false"' in resp.text
    assert "22:30" in resp.text  # next session summary


async def test_ev_card_renders_optimistic_state_when_pending_and_not_session_active(
    session_repo: SessionRepo,
) -> None:
    """AC11 — optimistic state for pending override + session_active=False."""
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state(session_active=False)})
    await store.set_ev_override(_pending_override())
    raw_token = await _create_session("homeowner")
    client = TestClient(
        _app_with_store(store, _active_constraints()),
        base_url="https://test",
        follow_redirects=False,
    )
    resp = client.get(
        "/fragments/homeowner/ev-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert 'data-override-state="optimistic"' in resp.text
    assert "Starting charging" in resp.text
    assert 'aria-disabled="true"' in resp.text


async def test_ev_card_renders_no_pass_green_class_in_confirmed_state(
    session_repo: SessionRepo,
) -> None:
    """AC18 #29 + AC16 load-bearing rule — Confirmed uses --color-accent-hover, not pass-green."""
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state(session_active=True)})
    await store.set_ev_override(_pending_override())
    raw_token = await _create_session("homeowner")
    client = TestClient(
        _app_with_store(store, _active_constraints()),
        base_url="https://test",
        follow_redirects=False,
    )
    resp = client.get(
        "/fragments/homeowner/ev-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert 'data-override-state="confirmed"' in resp.text
    assert "Charging now" in resp.text
    # Critical: NO pass-green or amber/red CSS class fragment.
    body_lower = resp.text.lower()
    for forbidden in ("pass-green", "color-pass", "amber", "color-fail", "warn"):
        assert forbidden not in body_lower, f"Forbidden class fragment {forbidden!r} appears"
    assert "state-card__ev-button--confirmed" in resp.text


async def test_ev_card_renders_retry_link_only_in_fallback_state(session_repo: SessionRepo) -> None:
    """AC18 #30 — retry visible only when dispatch_status terminal."""
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state()})
    await store.set_ev_override(_failed_override())
    raw_token = await _create_session("homeowner")
    client = TestClient(
        _app_with_store(store, _active_constraints()),
        base_url="https://test",
        follow_redirects=False,
    )
    resp = client.get(
        "/fragments/homeowner/ev-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert 'data-override-state="fallback"' in resp.text
    assert "Could not start charging" in resp.text
    assert "Try again" in resp.text
    assert "did not respond" in resp.text  # plain-language reason


async def test_ev_card_idle_state_omits_retry_link(session_repo: SessionRepo) -> None:
    """AC18 #30 (parametrized — non-fallback) — no retry link in idle state."""
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state()})
    raw_token = await _create_session("homeowner")
    client = TestClient(
        _app_with_store(store, _active_constraints()),
        base_url="https://test",
        follow_redirects=False,
    )
    resp = client.get(
        "/fragments/homeowner/ev-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert "Try again" not in resp.text


async def test_ev_card_unavailable_when_ev_charger_is_none(session_repo: SessionRepo) -> None:
    """AC18 #31 — no EV charger published → unavailable branch (UX spec line 1503)."""
    store = StateStore(system_clock_status="valid")
    raw_token = await _create_session("homeowner")
    client = TestClient(
        _app_with_store(store, _active_constraints()),
        base_url="https://test",
        follow_redirects=False,
    )
    resp = client.get(
        "/fragments/homeowner/ev-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "No EV charger connected" in resp.text
    assert "Charge EV now" not in resp.text  # button hidden in unavailable branch


async def test_ev_card_unavailable_when_ev_charger_is_degraded(
    session_repo: SessionRepo,
) -> None:
    """AC11 — DegradedDeviceState also routes to unavailable branch."""
    store = StateStore(system_clock_status="valid")
    await store.publish(
        {
            DeviceRole.ev_charger: DegradedDeviceState(
                device_id="ev-001",
                role=DeviceRole.ev_charger,
                reason="ocpp_charger_not_connected",
                occurred_at=datetime.now(UTC),
            )
        }
    )
    raw_token = await _create_session("homeowner")
    client = TestClient(
        _app_with_store(store, _active_constraints()),
        base_url="https://test",
        follow_redirects=False,
    )
    resp = client.get(
        "/fragments/homeowner/ev-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "No EV charger connected" in resp.text


def test_ev_card_unauth_request_returns_401_for_htmx(session_repo: SessionRepo) -> None:
    """AC18 #32 — existing role contract."""
    store = StateStore(system_clock_status="valid")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)
    resp = client.get(
        "/fragments/homeowner/ev-card",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 401


async def test_ev_card_installer_request_returns_403_for_htmx(session_repo: SessionRepo) -> None:
    """AC18 #33 — existing role contract."""
    store = StateStore(system_clock_status="valid")
    raw_token = await _create_session("installer")
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)
    resp = client.get(
        "/fragments/homeowner/ev-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 403


async def test_ev_card_idle_renders_no_charging_session_when_no_window(
    session_repo: SessionRepo,
) -> None:
    """AC11 — no EV charging window → "No charging session scheduled"."""
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state()})
    constraints = ActiveConstraints(
        peak_limit_kw=5.0,
        battery_reserve_floor_percent=20.0,
        config_version=1,
        activated_at=datetime.now(UTC),
        ev_charging_window_start=None,
        ev_charging_window_end=None,
    )
    raw_token = await _create_session("homeowner")
    client = TestClient(
        _app_with_store(store, constraints),
        base_url="https://test",
        follow_redirects=False,
    )
    resp = client.get(
        "/fragments/homeowner/ev-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "No charging session scheduled" in resp.text


async def test_ev_card_idle_works_without_active_constraints_provider(
    session_repo: SessionRepo,
) -> None:
    """AC13 / get_active_constraints_optional — fragment still renders pre-wizard."""
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state()})
    raw_token = await _create_session("homeowner")
    # No active_constraints_provider on app.state.
    client = TestClient(_app_with_store(store), base_url="https://test", follow_redirects=False)
    resp = client.get(
        "/fragments/homeowner/ev-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "No charging session scheduled" in resp.text


async def test_ev_card_renders_correlation_id_data_attr_for_pending_override(
    session_repo: SessionRepo,
) -> None:
    """Review patch: ``data-correlation-id`` must reflect the override's UUID.

    Jinja2's attribute-access fallback on dict context (``active_ev_override.correlation_id``)
    is fragile — if the serializer key is renamed, the attribute renders empty
    silently. This test pins the rendered attribute value to the override's UUID.
    """
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state(session_active=False)})
    cid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    now = datetime.now(UTC)
    override = EVOverrideState(
        correlation_id=cid,
        requested_at=now,
        expires_at=now + timedelta(hours=2),
        dispatch_status="pending",
    )
    await store.set_ev_override(override)
    raw_token = await _create_session("homeowner")
    client = TestClient(
        _app_with_store(store, _active_constraints()),
        base_url="https://test",
        follow_redirects=False,
    )
    resp = client.get(
        "/fragments/homeowner/ev-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert f'data-correlation-id="{cid}"' in resp.text


async def test_ev_card_renders_confirmation_timeout_data_attr(
    session_repo: SessionRepo,
) -> None:
    """Review patch: ``data-confirmation-timeout-seconds`` is wired through.

    Catches the regression where the ``ev_override_confirmation_timeout_seconds``
    Setting was defined but never injected to the client, leaving the Alpine
    optimistic-layer with no timeout enforcement (R3 invariant violation).
    """
    store = StateStore(system_clock_status="valid")
    await store.publish({DeviceRole.ev_charger: _ev_state()})
    raw_token = await _create_session("homeowner")
    client = TestClient(
        _app_with_store(store, _active_constraints()),
        base_url="https://test",
        follow_redirects=False,
    )
    resp = client.get(
        "/fragments/homeowner/ev-card",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    # Default Settings value is 30s; the attribute must be present so the
    # Alpine factory can pick it up via x-data config.
    assert "data-confirmation-timeout-seconds=" in resp.text
    assert "confirmationTimeoutSeconds" in resp.text
