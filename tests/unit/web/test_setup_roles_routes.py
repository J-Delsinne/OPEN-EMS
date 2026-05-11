"""Route-level tests for Step 2 — Role Assignment (Story 9.2 AC9).

Mirrors the shape of ``test_setup_routes.py`` (Story 9.1's AC9 suite). The
fixture wires the real ``RoleAssignmentService`` against an in-process
SQLite. All HTML expectations target the contract-shape exact-match
rejection strings (no substring assertions per Story 9.0c AC7 / 9.1 AC11).
"""

from __future__ import annotations

import html
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.adapters.discovery import DiscoveryService
from open_ems.core.devices import CapabilityStatus, DeviceDiscoveryResult, DeviceRole
from open_ems.services.device_discovery import DeviceDiscoveryOrchestrator
from open_ems.services.manual_entry import ManualEntryService
from open_ems.services.role_assignment import RoleAssignmentService
from open_ems.storage.database import get_connection
from open_ems.storage.repositories.device_repo import DeviceRepo, ManualDeviceEntryInput
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.storage.repositories.wizard_state_repo import WizardStateRepo
from open_ems.web.csrf import CsrfMiddleware
from open_ems.web.routes.setup import router as setup_router

_CSRF = "test-csrf-roles-fixed"


_CREATE_DEVICE_REGISTRY = """
    CREATE TABLE device_registry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL UNIQUE,
        protocol TEXT NOT NULL
            CHECK (protocol IN ('modbus_tcp', 'ocpp_1_6', 'dsmr_p1')),
        address TEXT NOT NULL,
        model TEXT,
        firmware_version TEXT,
        source TEXT NOT NULL
            CHECK (source IN ('manual_entry', 'ocpp_self_registration')),
        validated INTEGER NOT NULL DEFAULT 0
            CHECK (validated IN (0, 1)),
        last_capability_status TEXT,
        last_limitation_reason TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT,
        installer_acknowledged_unvalidated_at TEXT,
        role TEXT
            CHECK (role IS NULL
                   OR role IN ('inverter', 'battery', 'ev_charger', 'grid_meter')),
        role_assigned_at TEXT,
        CHECK ((role IS NULL AND role_assigned_at IS NULL)
               OR (role IS NOT NULL AND role_assigned_at IS NOT NULL))
    )
"""

_CREATE_WIZARD_STATE = """
    CREATE TABLE wizard_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL UNIQUE,
        step_1_complete INTEGER NOT NULL DEFAULT 0
            CHECK (step_1_complete IN (0, 1)),
        step_1_completed_at TEXT,
        last_scan_id TEXT,
        step_2_complete INTEGER NOT NULL DEFAULT 0
            CHECK (step_2_complete IN (0, 1)),
        step_2_completed_at TEXT,
        step_2_acknowledged_gaps TEXT,
        step_3_complete INTEGER NOT NULL DEFAULT 0
            CHECK (step_3_complete IN (0, 1)),
        step_3_completed_at TEXT,
        step_3_activated_config_version INTEGER,
        step_4_complete INTEGER NOT NULL DEFAULT 0
            CHECK (step_4_complete IN (0, 1)),
        step_4_completed_at TEXT,
        step_4_completed_config_version INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
        CHECK (
            (step_3_complete = 0
             AND step_3_completed_at IS NULL
             AND step_3_activated_config_version IS NULL)
            OR (step_3_complete = 1
                AND step_3_completed_at IS NOT NULL
                AND step_3_activated_config_version IS NOT NULL)
        ),
        CHECK (
            (step_4_complete = 0
             AND step_4_completed_at IS NULL
             AND step_4_completed_config_version IS NULL)
            OR (step_4_complete = 1
                AND step_4_completed_at IS NOT NULL
                AND step_4_completed_config_version IS NOT NULL)
        )
    )
"""


class _NoOpDiscovery(DiscoveryService):
    async def probe_modbus_endpoint(  # type: ignore[override]
        self,
        *,
        device_id: str,
        host: str,
        port: int = 502,
        model: str | None = None,
    ) -> DeviceDiscoveryResult:
        return DeviceDiscoveryResult(
            device_id=device_id,
            protocol="modbus_tcp",
            address=f"{host}:{port}",
            model=model,
            capability_status=CapabilityStatus.full,
        )

    async def probe_dsmr_endpoint(  # type: ignore[override]
        self,
        *,
        device_id: str,
        serial_port: str | None = None,
        tcp_host: str | None = None,
        tcp_port: int | None = None,
        dsmr_version: str = "5",
    ) -> DeviceDiscoveryResult:
        return DeviceDiscoveryResult(
            device_id=device_id,
            protocol="dsmr_p1",
            address=serial_port or f"{tcp_host}:{tcp_port}",
            model="dsmr_p1",
            capability_status=CapabilityStatus.full,
        )


@pytest_asyncio.fixture
async def app_with_services(
    session_repo: SessionRepo,
) -> AsyncGenerator[FastAPI, None]:
    conn = get_connection()
    await conn.execute(_CREATE_DEVICE_REGISTRY)
    await conn.execute(_CREATE_WIZARD_STATE)
    await conn.commit()

    device_repo = DeviceRepo()
    wizard_state_repo = WizardStateRepo()
    discovery = _NoOpDiscovery()
    orchestrator = DeviceDiscoveryOrchestrator(
        discovery_service=discovery,
        registry_provider=device_repo,
    )
    manual_entry = ManualEntryService(
        device_repo=device_repo,
        discovery_service=discovery,
    )
    role_assignment = RoleAssignmentService(device_repo=device_repo)

    app = FastAPI()
    app.state.device_repo = device_repo
    app.state.wizard_state_repo = wizard_state_repo
    app.state.discovery_orchestrator = orchestrator
    app.state.manual_entry_service = manual_entry
    app.state.role_assignment_service = role_assignment
    app.include_router(setup_router)
    app.add_middleware(CsrfMiddleware)
    yield app


async def _create_installer_session(*, step_1_complete: bool = True) -> tuple[str, str]:
    """Create an installer session and seed wizard_state.

    Defaults to ``step_1_complete=True`` because every test in this module
    exercises Step 2 routes, which (per Story 9.2 review patch P9) redirect
    to discovery when Step 1 is not complete. Tests for the new redirect
    pass ``step_1_complete=False``.
    """
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
    if step_1_complete:
        wizard_repo = WizardStateRepo()
        await wizard_repo.get_or_create(session_id, now=datetime.now(UTC))
        await wizard_repo.set_step_1_complete(session_id, now=datetime.now(UTC))
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


async def _add_device(device_id: str, *, role: DeviceRole | None = None) -> None:
    repo = DeviceRepo(get_connection())
    await repo.upsert_manual(
        ManualDeviceEntryInput(
            device_id=device_id,
            protocol="modbus_tcp",
            address="10.0.0.1:502",
        ),
        first_seen_at=datetime.now(UTC),
    )
    if role is not None:
        await repo.assign_role(device_id, role=role, assigned_at=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Auth + CSRF
# ---------------------------------------------------------------------------


def test_unauthenticated_get_roles_redirects_to_login(app_with_services: FastAPI) -> None:
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/roles")
    assert response.status_code == 302
    assert "/login" in response.headers["location"]


async def test_homeowner_rejected_on_roles_get(app_with_services: FastAPI) -> None:
    raw_token = await _create_homeowner_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/roles", cookies={"session": raw_token})
    assert response.status_code == 302


async def test_get_roles_redirects_to_discovery_when_step_1_incomplete(
    app_with_services: FastAPI,
) -> None:
    """Story 9.2 review patch P9: deep-linking to Step 2 before Step 1 is
    complete must bounce back to discovery, not silently render the page
    (which would let the installer assign roles and acknowledge gaps over a
    not-yet-discovered registry).
    """
    raw_token, _ = await _create_installer_session(step_1_complete=False)
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/roles", cookies={"session": raw_token})
    assert response.status_code == 302
    assert response.headers["location"] == "/installer/setup/discovery"


async def test_assign_without_csrf_returns_403(app_with_services: FastAPI) -> None:
    raw_token, _ = await _create_installer_session()
    await _add_device("inv-1")
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/roles/inv-1/assign",
        cookies={"session": raw_token},
        data={"role": "inverter"},  # no csrf_token
    )
    assert response.status_code == 403


async def test_acknowledge_without_csrf_returns_403(app_with_services: FastAPI) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/roles/acknowledge-gap",
        cookies={"session": raw_token},
        data={"action": "acknowledge", "gap_label": "battery_missing"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# AC4 — Assign route
# ---------------------------------------------------------------------------


async def test_assign_valid_role_swaps_list_region(app_with_services: FastAPI) -> None:
    raw_token, _ = await _create_installer_session()
    await _add_device("inv-1")
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/roles/inv-1/assign",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={"role": "inverter"},
    )
    assert response.status_code == 200
    assert 'id="role-rows"' in response.text


async def test_assign_invalid_role_rejected_with_exact_match(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    await _add_device("inv-1")
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/roles/inv-1/assign",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={"role": "banana"},
    )
    assert response.status_code == 400
    # AC9 precedent (Story 9.0c AC7 / 9.1 AC11): exact-match on the rendered
    # _setup_roles_error.html body. The template wraps the contract string
    # in a single error-banner div with no surrounding whitespace inside the
    # wrapper, so strip() + unescape gives us a deterministic comparand.
    body = html.unescape(response.text).strip()
    assert body == '<div class="error-banner" role="alert">\n  role_invalid: \'banana\'\n</div>'


async def test_assign_creates_conflict_indicator_on_both_rows(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    await _add_device("inv-a", role=DeviceRole.inverter)
    await _add_device("inv-b")
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/roles/inv-b/assign",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={"role": "inverter"},
    )
    assert response.status_code == 200
    # Both rows must list each other in the rendered fragment.
    assert "inv-a" in response.text
    assert "inv-b" in response.text
    assert "Another device is already assigned this role" in response.text


async def test_assign_route_returns_oob_gap_panel(app_with_services: FastAPI) -> None:
    """R1 #4: the assign-route fragment carries an out-of-band swap so the
    sibling gap panel refreshes atomically with the list region.
    """
    raw_token, _ = await _create_installer_session()
    await _add_device("gm-1")
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/roles/gm-1/assign",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={"role": "grid_meter"},
    )
    assert response.status_code == 200
    assert 'id="gap-panel-region"' in response.text
    assert 'hx-swap-oob="innerHTML"' in response.text


# ---------------------------------------------------------------------------
# AC5 — Acknowledge-gap route
# ---------------------------------------------------------------------------


async def test_acknowledge_battery_missing_returns_panel(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    # GET creates wizard_state lazily
    client.get("/installer/setup/roles", cookies={"session": raw_token})
    response = client.post(
        "/installer/setup/roles/acknowledge-gap",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "action": "acknowledge",
            "gap_label": "battery_missing",
        },
    )
    assert response.status_code == 200
    assert "Acknowledged" in response.text or "Revoke" in response.text


async def test_acknowledge_grid_meter_missing_rejected_with_exact_match(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/roles", cookies={"session": raw_token})
    response = client.post(
        "/installer/setup/roles/acknowledge-gap",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "action": "acknowledge",
            "gap_label": "grid_meter_missing",
        },
    )
    assert response.status_code == 400
    body = html.unescape(response.text).strip()
    expected = (
        '<div class="error-banner" role="alert">\n'
        "  gap_label_not_acknowledgeable: grid_meter_missing\n"
        "</div>"
    )
    assert body == expected


async def test_acknowledge_unknown_label_rejected_with_exact_match(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/roles", cookies={"session": raw_token})
    response = client.post(
        "/installer/setup/roles/acknowledge-gap",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "action": "acknowledge",
            "gap_label": "delete_everything",
        },
    )
    assert response.status_code == 400
    body = html.unescape(response.text).strip()
    expected = (
        '<div class="error-banner" role="alert">\n'
        "  gap_label_invalid: 'delete_everything'\n"
        "</div>"
    )
    assert body == expected


async def test_revoke_removes_acknowledgment(app_with_services: FastAPI) -> None:
    raw_token, session_id = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/roles", cookies={"session": raw_token})
    # Acknowledge
    client.post(
        "/installer/setup/roles/acknowledge-gap",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "action": "acknowledge",
            "gap_label": "battery_missing",
        },
    )
    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert "battery_missing" in state.step_2_acknowledged_gaps
    # Revoke
    client.post(
        "/installer/setup/roles/acknowledge-gap",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "action": "revoke",
            "gap_label": "battery_missing",
        },
    )
    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert "battery_missing" not in state.step_2_acknowledged_gaps


# ---------------------------------------------------------------------------
# AC6 — Advance route
# ---------------------------------------------------------------------------


async def test_advance_pass_redirects_to_constraints_and_flips_step_2_complete(
    app_with_services: FastAPI,
) -> None:
    raw_token, session_id = await _create_installer_session()
    await _add_device("inv", role=DeviceRole.inverter)
    await _add_device("bat", role=DeviceRole.battery)
    await _add_device("evc", role=DeviceRole.ev_charger)
    await _add_device("gm", role=DeviceRole.grid_meter)
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/roles", cookies={"session": raw_token})
    response = client.post(
        "/installer/setup/roles/advance",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={},
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/installer/setup/constraints"
    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert state.step_2_complete is True
    assert state.step_2_completed_at is not None


async def test_advance_fails_when_grid_meter_unassigned(
    app_with_services: FastAPI,
) -> None:
    raw_token, session_id = await _create_installer_session()
    await _add_device("inv", role=DeviceRole.inverter)
    await _add_device("bat", role=DeviceRole.battery)
    await _add_device("evc", role=DeviceRole.ev_charger)
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/roles", cookies={"session": raw_token})
    response = client.post(
        "/installer/setup/roles/advance",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={},
    )
    assert response.status_code == 400
    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert state.step_2_complete is False


async def test_advance_fails_on_role_conflict(app_with_services: FastAPI) -> None:
    raw_token, session_id = await _create_installer_session()
    await _add_device("inv-a", role=DeviceRole.inverter)
    await _add_device("inv-b", role=DeviceRole.inverter)
    await _add_device("bat", role=DeviceRole.battery)
    await _add_device("evc", role=DeviceRole.ev_charger)
    await _add_device("gm", role=DeviceRole.grid_meter)
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/roles", cookies={"session": raw_token})
    response = client.post(
        "/installer/setup/roles/advance",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={},
    )
    assert response.status_code == 400
    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert state.step_2_complete is False


async def test_advance_filters_stale_acknowledgments_during_persistence(
    app_with_services: FastAPI,
) -> None:
    """AC6 step 4: a passing advance prunes the persisted ack-set to the
    currently-open gaps. After the installer acknowledges battery_missing
    and then assigns a battery, the persisted set MUST drop battery_missing.
    """
    raw_token, session_id = await _create_installer_session()
    await _add_device("gm", role=DeviceRole.grid_meter)
    await _add_device("inv", role=DeviceRole.inverter)
    await _add_device("evc", role=DeviceRole.ev_charger)
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/roles", cookies={"session": raw_token})
    # Acknowledge battery_missing (battery is currently absent).
    client.post(
        "/installer/setup/roles/acknowledge-gap",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "action": "acknowledge",
            "gap_label": "battery_missing",
        },
    )
    # Now assign a battery — gap closes but the ack persists until advance.
    await _add_device("bat", role=DeviceRole.battery)
    response = client.post(
        "/installer/setup/roles/advance",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={},
    )
    assert response.status_code == 302
    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert state.step_2_complete is True
    # The stale ack is pruned.
    assert "battery_missing" not in state.step_2_acknowledged_gaps


async def test_advance_idempotent_preserves_completed_at(
    app_with_services: FastAPI,
) -> None:
    raw_token, session_id = await _create_installer_session()
    await _add_device("inv", role=DeviceRole.inverter)
    await _add_device("bat", role=DeviceRole.battery)
    await _add_device("evc", role=DeviceRole.ev_charger)
    await _add_device("gm", role=DeviceRole.grid_meter)
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/roles", cookies={"session": raw_token})
    client.post(
        "/installer/setup/roles/advance",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={},
    )
    first = await WizardStateRepo().get(session_id)
    assert first is not None
    first_at = first.step_2_completed_at

    client.post(
        "/installer/setup/roles/advance",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={},
    )
    second = await WizardStateRepo().get(session_id)
    assert second is not None
    assert second.step_2_completed_at == first_at


# ---------------------------------------------------------------------------
# Roles GET (constraints placeholder removed by Story 9.3)
# ---------------------------------------------------------------------------


async def test_get_roles_renders_real_page_not_placeholder(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/roles", cookies={"session": raw_token})
    assert response.status_code == 200
    assert "Story 9.2" not in response.text
    assert 'id="roles-list-region"' in response.text
    assert 'id="gap-panel-region"' in response.text


@pytest.mark.parametrize(
    "label",
    sorted({"battery_missing", "inverter_missing", "ev_charger_missing"}),
)
async def test_warn_gap_acknowledge_buttons_render_for_each_acknowledgeable_role(
    app_with_services: FastAPI, label: str
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/roles", cookies={"session": raw_token})
    assert response.status_code == 200
    assert f'value="{label}"' in response.text
