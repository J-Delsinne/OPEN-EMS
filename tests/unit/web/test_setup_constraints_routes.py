"""Route-level tests for Step 3 — Constraint Configuration (Story 9.3 AC10).

Mirrors the shape of ``test_setup_roles_routes.py`` (Story 9.2) and
``test_setup_routes.py`` (Story 9.1). The fixture wires the real
``ConstraintsService`` against an in-process SQLite. All HTML expectations
target the contract-shape exact-match rejection strings (no substring
assertions per Story 9.0c AC7 / 9.1 AC11 / 9.2 AC9).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core.devices import DeviceRole
from open_ems.core.state import (
    ALL_DEVICE_ROLES,
    ComponentState,
    GlobalState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.services.constraints import ConstraintsService
from open_ems.settings import Settings
from open_ems.storage.database import get_connection
from open_ems.storage.repositories.config_repo import ConfigRepo
from open_ems.storage.repositories.device_repo import DeviceRepo, ManualDeviceEntryInput
from open_ems.storage.repositories.draft_constraints_repo import DraftConstraintsRepo
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.storage.repositories.wizard_state_repo import WizardStateRepo
from open_ems.web.csrf import CsrfMiddleware
from open_ems.web.routes.setup import router as setup_router

_CSRF = "test-csrf-constraints-fixed"

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)


_CREATE_DEVICE_REGISTRY = """
    CREATE TABLE device_registry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL UNIQUE,
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
_CREATE_WIZARD_STATE = """
    CREATE TABLE wizard_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL UNIQUE,
        step_1_complete INTEGER NOT NULL DEFAULT 0,
        step_1_completed_at TEXT,
        last_scan_id TEXT,
        step_2_complete INTEGER NOT NULL DEFAULT 0,
        step_2_completed_at TEXT,
        step_2_acknowledged_gaps TEXT,
        step_3_complete INTEGER NOT NULL DEFAULT 0,
        step_3_completed_at TEXT,
        step_3_activated_config_version INTEGER,
        step_4_complete INTEGER NOT NULL DEFAULT 0,
        step_4_completed_at TEXT,
        step_4_completed_config_version INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    )
"""
_CREATE_ACTIVE_CONSTRAINTS = """
    CREATE TABLE active_constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        peak_limit_kw REAL NOT NULL CHECK (peak_limit_kw > 0),
        battery_reserve_floor_percent REAL NOT NULL,
        config_version INTEGER NOT NULL UNIQUE,
        activated_at TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('system', 'installer')),
        ev_charging_window_start TEXT,
        ev_charging_window_end TEXT
    )
"""
_CREATE_CONFIG_AUDIT_LOG = """
    CREATE TABLE config_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        field TEXT NOT NULL,
        previous_value TEXT NOT NULL,
        new_value TEXT NOT NULL,
        config_version INTEGER NOT NULL
    )
"""
_CREATE_DRAFT_CONSTRAINTS = """
    CREATE TABLE draft_constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL UNIQUE,
        peak_limit_kw REAL NOT NULL,
        battery_reserve_floor_percent REAL NOT NULL,
        ev_charging_window_start TEXT,
        ev_charging_window_end TEXT,
        validation_status TEXT NOT NULL DEFAULT 'pending',
        validation_report TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    )
"""


def _empty_snapshot() -> SystemSnapshot:
    return SystemSnapshot(
        sequence_id=0,
        captured_at=_NOW,
        global_state=GlobalState.degraded,
        operating_mode=SystemOperatingMode.degraded,
        inverter=None,
        battery=None,
        ev_charger=None,
        grid_meter=None,
        component_states=dict.fromkeys(ALL_DEVICE_ROLES, ComponentState.unavailable),
        data_age_seconds=dict.fromkeys(ALL_DEVICE_ROLES),
        system_clock_status="valid",
    )


class _StubStateStore:
    def __init__(self, snapshot: SystemSnapshot) -> None:
        self._snapshot = snapshot

    def get_snapshot(self) -> SystemSnapshot:
        return self._snapshot


@pytest_asyncio.fixture
async def app_with_constraints(
    session_repo: SessionRepo,
) -> AsyncGenerator[FastAPI, None]:
    conn = get_connection()
    await conn.execute(_CREATE_DEVICE_REGISTRY)
    await conn.execute(_CREATE_WIZARD_STATE)
    await conn.execute(_CREATE_ACTIVE_CONSTRAINTS)
    await conn.execute(_CREATE_CONFIG_AUDIT_LOG)
    await conn.execute(_CREATE_DRAFT_CONSTRAINTS)
    await conn.commit()

    config_repo = ConfigRepo()
    device_repo = DeviceRepo()
    wizard_state_repo = WizardStateRepo()
    draft_repo = DraftConstraintsRepo()

    # Pre-seed an active row so the route is exercised against a non-cold-start
    # provider. config_version=1 (peak=25, floor=20).
    await config_repo.activate(_make_input(25.0, 20.0), actor="installer")

    provider = ActiveConstraintsProvider(repo=config_repo, settings=Settings(secret_key="x"))
    await provider.hydrate()

    state_store = _StubStateStore(_empty_snapshot())
    constraints_service = ConstraintsService(
        draft_repo=draft_repo,
        config_repo=config_repo,
        active_constraints_provider=provider,
        wizard_state_repo=wizard_state_repo,
        device_repo=device_repo,
        state_store=state_store,
    )

    app = FastAPI()
    app.state.device_repo = device_repo
    app.state.wizard_state_repo = wizard_state_repo
    app.state.constraints_service = constraints_service
    app.state.draft_constraints_repo = draft_repo
    # Other services not needed for these routes but the dependency wiring
    # makes 503 noise less surprising if a test accidentally hits them.
    app.include_router(setup_router)
    app.add_middleware(CsrfMiddleware)
    yield app


def _make_input(peak: float, floor: float):
    from open_ems.core.constraints import ActiveConstraintsInput

    return ActiveConstraintsInput(
        peak_limit_kw=peak,
        battery_reserve_floor_percent=floor,
    )


async def _create_installer_session(
    *,
    step_1_complete: bool = True,
    step_2_complete: bool = True,
) -> tuple[str, str]:
    user_id = await UserRepo(get_connection()).create(
        username=f"installer_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role="installer",
    )
    raw_token = generate_session_token()
    session_id = await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF,
    )
    wizard_repo = WizardStateRepo()
    await wizard_repo.get_or_create(session_id, now=datetime.now(UTC))
    if step_1_complete:
        await wizard_repo.set_step_1_complete(session_id, now=datetime.now(UTC))
    if step_2_complete:
        await wizard_repo.set_step_2_complete(
            session_id, acknowledged_gaps=frozenset(), now=datetime.now(UTC)
        )
    return raw_token, session_id


async def _create_homeowner_session() -> str:
    user_id = await UserRepo(get_connection()).create(
        username=f"homeowner_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role="homeowner",
    )
    raw_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF,
    )
    return raw_token


async def _add_grid_meter() -> None:
    repo = DeviceRepo(get_connection())
    await repo.upsert_manual(
        ManualDeviceEntryInput(
            device_id="meter-1",
            protocol="dsmr_p1",
            address="/dev/ttyUSB0",
        ),
        first_seen_at=datetime.now(UTC),
    )
    await repo.assign_role("meter-1", role=DeviceRole.grid_meter, assigned_at=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Auth + CSRF
# ---------------------------------------------------------------------------


def test_unauthenticated_get_constraints_redirects_to_login(
    app_with_constraints: FastAPI,
) -> None:
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/constraints")
    assert response.status_code == 302
    assert "/login" in response.headers["location"]


async def test_homeowner_rejected_on_constraints_get(
    app_with_constraints: FastAPI,
) -> None:
    raw_token = await _create_homeowner_session()
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/constraints", cookies={"session": raw_token})
    assert response.status_code == 302


async def test_post_draft_without_csrf_returns_403(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw_token},
        data={
            "peak_limit_kw": "30",
            "battery_reserve_floor_percent": "25",
        },
    )
    assert response.status_code == 403


async def test_post_validate_without_csrf_returns_403(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/constraints/validate",
        cookies={"session": raw_token},
    )
    assert response.status_code == 403


async def test_post_activate_without_csrf_returns_403(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/constraints/activate",
        cookies={"session": raw_token},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Step-gate enforcement
# ---------------------------------------------------------------------------


async def test_get_constraints_redirects_to_discovery_when_step_1_incomplete(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session(step_1_complete=False, step_2_complete=False)
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/constraints", cookies={"session": raw_token})
    assert response.status_code == 302
    assert response.headers["location"] == "/installer/setup/discovery"


async def test_get_constraints_redirects_to_roles_when_step_2_incomplete(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session(step_1_complete=True, step_2_complete=False)
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/constraints", cookies={"session": raw_token})
    assert response.status_code == 302
    assert response.headers["location"] == "/installer/setup/roles"


async def test_get_constraints_short_circuits_to_step_4_when_step_3_complete(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, session_id = await _create_installer_session()
    wizard_repo = WizardStateRepo()
    await wizard_repo.set_step_3_complete(
        session_id, activated_config_version=1, now=datetime.now(UTC)
    )
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/constraints", cookies={"session": raw_token})
    assert response.status_code == 302
    assert response.headers["location"] == "/installer/setup/validation"


# R2P18 — Round-2 review patch: the service-layer ``assert_step_prerequisites``
# gate is invoked from every state-mutating POST (P12 from round 1), but only
# the GET path's step_3_already_complete short-circuit had a route-level test.
# The two tests below assert the contract that POST /draft and POST /validate
# both surface the exact-match ``step_3_already_complete`` rejection after the
# wizard has been advanced — protecting against scripted callers (or HTMX
# retries) that bypass the GET handler's redirect.


async def test_post_draft_rejected_when_step_3_already_complete(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, session_id = await _create_installer_session()
    wizard_repo = WizardStateRepo()
    await wizard_repo.set_step_3_complete(
        session_id, activated_config_version=1, now=datetime.now(UTC)
    )
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "30",
            "battery_reserve_floor_percent": "25",
            "ev_charging_window_start": "",
            "ev_charging_window_end": "",
        },
    )
    assert response.status_code == 400
    # Exact-match contract reason per AC11 — surfaced via the banner's
    # ``data-reason`` attribute so tests / scripted callers can match.
    assert 'data-reason="step_3_already_complete"' in response.text


async def test_post_validate_rejected_when_step_3_already_complete(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, session_id = await _create_installer_session()
    wizard_repo = WizardStateRepo()
    await wizard_repo.set_step_3_complete(
        session_id, activated_config_version=1, now=datetime.now(UTC)
    )
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/constraints/validate",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 400
    assert 'data-reason="step_3_already_complete"' in response.text


async def test_post_activate_after_success_redirects_instead_of_400(
    app_with_constraints: FastAPI,
) -> None:
    """R2P5 — a double-click on Activate (or a scripted retry) lands here once
    Step 3 is already complete. The service-layer gate fires
    ``step_3_already_complete`` BEFORE any state change; the route maps the
    user-visible response to a forward-redirect rather than a 400 banner so
    the second click does not turn a successful activation into an error.
    """
    raw_token, session_id = await _create_installer_session()
    wizard_repo = WizardStateRepo()
    await wizard_repo.set_step_3_complete(
        session_id, activated_config_version=1, now=datetime.now(UTC)
    )
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/constraints/activate",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/installer/setup/validation"


async def test_post_activate_with_hx_request_returns_204_and_hx_redirect(
    app_with_constraints: FastAPI,
) -> None:
    """R2P5 — HTMX clients (``HX-Request: true``) receive 204 + HX-Redirect
    instead of a 302 redirect document so the activate response is handled by
    HTMX's redirect mechanism rather than swapped into the activate region.
    The activate form is plain HTML today; this test pins the forward-compat
    HX behaviour so a future ``hx-post`` migration does not regress.
    """
    raw_token, session_id = await _create_installer_session()
    wizard_repo = WizardStateRepo()
    await wizard_repo.set_step_3_complete(
        session_id, activated_config_version=1, now=datetime.now(UTC)
    )
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/constraints/activate",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF, "HX-Request": "true"},
    )
    assert response.status_code == 204
    assert response.headers.get("HX-Redirect") == "/installer/setup/validation"


# ---------------------------------------------------------------------------
# GET render
# ---------------------------------------------------------------------------


async def test_get_constraints_renders_seeded_form_when_no_draft(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/constraints", cookies={"session": raw_token})
    assert response.status_code == 200
    # Provider seed values render into the inputs.
    assert 'value="25.00"' in response.text  # peak_limit_kw
    assert 'value="20.0"' in response.text  # battery_reserve_floor_percent
    # Activate button is disabled (validation_status defaults to 'pending').
    assert "Activate (validation required)" in response.text


# ---------------------------------------------------------------------------
# Draft upsert
# ---------------------------------------------------------------------------


async def test_post_draft_persists_and_returns_form_fragment(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, session_id = await _create_installer_session()
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "30",
            "battery_reserve_floor_percent": "25",
            "ev_charging_window_start": "",
            "ev_charging_window_end": "",
        },
    )
    assert response.status_code == 200
    assert 'value="30.00"' in response.text
    draft = await DraftConstraintsRepo().get(session_id)
    assert draft is not None
    assert draft.peak_limit_kw == 30.0
    assert draft.validation_status == "pending"


async def test_post_draft_rejects_unpaired_ev_window_with_400(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "30",
            "battery_reserve_floor_percent": "25",
            "ev_charging_window_start": "09:00",
            "ev_charging_window_end": "",
        },
    )
    assert response.status_code == 400
    assert "EV charging window start and end" in response.text


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


async def test_post_validate_without_draft_returns_400_exact_match(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/constraints/validate",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 400
    assert "draft_not_found_for_session" in response.text


async def test_post_validate_pass_renders_validation_fragment(
    app_with_constraints: FastAPI,
) -> None:
    await _add_grid_meter()
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    # Upsert a valid draft first.
    client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "30",
            "battery_reserve_floor_percent": "25",
            "ev_charging_window_start": "",
            "ev_charging_window_end": "",
        },
    )
    response = client.post(
        "/installer/setup/constraints/validate",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 200
    assert "Overall: VALID" in response.text


# ---------------------------------------------------------------------------
# Activate
# ---------------------------------------------------------------------------


async def test_post_activate_happy_path_redirects_to_step_4(
    app_with_constraints: FastAPI,
) -> None:
    await _add_grid_meter()
    raw_token, session_id = await _create_installer_session()
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "30",
            "battery_reserve_floor_percent": "25",
            "ev_charging_window_start": "",
            "ev_charging_window_end": "",
        },
    )
    response = client.post(
        "/installer/setup/constraints/activate",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/installer/setup/validation"
    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert state.step_3_complete is True
    assert state.step_3_activated_config_version == 2  # 1 was the seeded row


async def test_post_activate_no_changes_returns_400_exact_match(
    app_with_constraints: FastAPI,
) -> None:
    await _add_grid_meter()
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    # Upsert with the seeded values — activation will be a no-op.
    client.post(
        "/installer/setup/constraints/draft",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "peak_limit_kw": "25",
            "battery_reserve_floor_percent": "20",
            "ev_charging_window_start": "",
            "ev_charging_window_end": "",
        },
    )
    response = client.post(
        "/installer/setup/constraints/activate",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 400
    assert "activate_called_with_no_changed_fields" in response.text


async def test_post_activate_step_prereq_not_met_returns_400_exact_match(
    app_with_constraints: FastAPI,
) -> None:
    raw_token, session_id = await _create_installer_session(
        step_1_complete=True, step_2_complete=False
    )
    # Pre-seed a draft via the repo (the route would have redirected from GET).
    await DraftConstraintsRepo().upsert(
        session_id,
        peak_limit_kw=30.0,
        battery_reserve_floor_percent=25.0,
        ev_charging_window_start=None,
        ev_charging_window_end=None,
        now=datetime.now(UTC),
    )
    client = TestClient(app_with_constraints, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/constraints/activate",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 400
    assert "step_prerequisites_not_met: step_1_complete=1 step_2_complete=0" in response.text


# Story 9.4 NOTE: the prior ``test_get_validation_placeholder_renders`` was
# removed — Story 9.4 replaces ``/installer/setup/validation`` with the real
# Step 4 page (``setup_validation.html``). The 9.4 route tests in
# ``tests/unit/web/test_setup_validation_routes.py`` cover the new route's
# step-gate + render with the full service fixture.
