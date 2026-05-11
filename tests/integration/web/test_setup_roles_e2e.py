"""End-to-end integration tests for Step 2 — Role Assignment (Story 9.2 AC9).

Each test exercises a full request/response chain against an in-memory
SQLite (real ``DeviceRepo`` + ``WizardStateRepo`` + ``RoleAssignmentService``).
The scenarios cover the persistence + restart + drift-cleanup paths that the
unit tests cannot reach.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

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

_CSRF = "test-csrf-roles-e2e"

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
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
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
async def e2e_app(
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
    """Create an installer session; default seeds ``step_1_complete=1`` so
    Step 2 routes are reachable (Story 9.2 review patch P9)."""
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


async def test_e2e_happy_path_assign_all_four_roles_advances(
    e2e_app: FastAPI,
) -> None:
    """Discovery → roles → advance with all four roles assigned."""
    raw_token, session_id = await _create_installer_session()
    for dev in ("inv", "bat", "evc", "gm"):
        await _add_device(dev)

    client = TestClient(e2e_app, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/roles", cookies={"session": raw_token})
    assignments_in_order = (
        ("inv", "inverter"),
        ("bat", "battery"),
        ("evc", "ev_charger"),
        ("gm", "grid_meter"),
    )
    for dev, role in assignments_in_order:
        response = client.post(
            f"/installer/setup/roles/{dev}/assign",
            cookies={"session": raw_token},
            headers={"X-CSRF-Token": _CSRF},
            data={"role": role},
        )
        assert response.status_code == 200

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


async def test_e2e_mixed_path_grid_meter_missing_blocks_then_unblocks(
    e2e_app: FastAPI,
) -> None:
    """Hard block surfaces; assigning a grid meter clears it."""
    raw_token, session_id = await _create_installer_session()
    seeded = (
        ("inv", DeviceRole.inverter),
        ("bat", DeviceRole.battery),
        ("evc", DeviceRole.ev_charger),
    )
    for dev, role in seeded:
        await _add_device(dev, role=role)
    await _add_device("gm")  # unassigned grid_meter candidate

    client = TestClient(e2e_app, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/roles", cookies={"session": raw_token})
    assert "Grid meter is required for peak limiting" in response.text

    # Attempt advance: blocked.
    response = client.post(
        "/installer/setup/roles/advance",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={},
    )
    assert response.status_code == 400

    # Assign grid meter, then advance.
    client.post(
        "/installer/setup/roles/gm/assign",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={"role": "grid_meter"},
    )
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


async def test_e2e_gap_acknowledgment_path_persists_all_three_labels(
    e2e_app: FastAPI,
) -> None:
    """One grid_meter, no others; acknowledge each warn → advance, with the
    three labels stored in step_2_acknowledged_gaps."""
    raw_token, session_id = await _create_installer_session()
    await _add_device("gm", role=DeviceRole.grid_meter)

    client = TestClient(e2e_app, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/roles", cookies={"session": raw_token})
    for label in ("battery_missing", "inverter_missing", "ev_charger_missing"):
        client.post(
            "/installer/setup/roles/acknowledge-gap",
            cookies={"session": raw_token},
            headers={"X-CSRF-Token": _CSRF},
            data={"action": "acknowledge", "gap_label": label},
        )

    response = client.post(
        "/installer/setup/roles/advance",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={},
    )
    assert response.status_code == 302
    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert state.step_2_acknowledged_gaps == frozenset(
        {"battery_missing", "inverter_missing", "ev_charger_missing"}
    )


async def test_e2e_persistence_path_state_survives_session_reload(
    e2e_app: FastAPI,
) -> None:
    """Roles + acknowledgments survive page reload (DB-backed)."""
    raw_token, session_id = await _create_installer_session()
    await _add_device("gm", role=DeviceRole.grid_meter)

    client = TestClient(e2e_app, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/roles", cookies={"session": raw_token})
    client.post(
        "/installer/setup/roles/acknowledge-gap",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={"action": "acknowledge", "gap_label": "battery_missing"},
    )

    # Reload — the gap panel renders with battery_missing acknowledged.
    response = client.get("/installer/setup/roles", cookies={"session": raw_token})
    assert response.status_code == 200
    assert "Revoke acknowledgment" in response.text

    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert "battery_missing" in state.step_2_acknowledged_gaps


async def test_e2e_conflict_and_resolve_path_clears_indicator(
    e2e_app: FastAPI,
) -> None:
    """Duplicate role yields a conflict indicator; reassigning clears it."""
    raw_token, _ = await _create_installer_session()
    await _add_device("inv-a", role=DeviceRole.inverter)
    await _add_device("inv-b")
    client = TestClient(e2e_app, base_url="https://test", follow_redirects=False)

    # Create the conflict.
    response = client.post(
        "/installer/setup/roles/inv-b/assign",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={"role": "inverter"},
    )
    assert "Another device is already assigned this role" in response.text

    # Reassign inv-b → battery; conflict clears.
    response = client.post(
        "/installer/setup/roles/inv-b/assign",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={"role": "battery"},
    )
    assert "Another device is already assigned this role" not in response.text


async def test_e2e_session_expiry_loses_acknowledgments_preserves_role_assignments(
    e2e_app: FastAPI,
) -> None:
    """When the session is destroyed (CASCADE), wizard_state goes with it, but
    device_registry.role persists. The next session has to re-acknowledge.
    """
    raw_token_a, session_id_a = await _create_installer_session()
    await _add_device("gm", role=DeviceRole.grid_meter)

    client = TestClient(e2e_app, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/roles", cookies={"session": raw_token_a})
    client.post(
        "/installer/setup/roles/acknowledge-gap",
        cookies={"session": raw_token_a},
        headers={"X-CSRF-Token": _CSRF},
        data={"action": "acknowledge", "gap_label": "battery_missing"},
    )

    # Simulate "session expired" by deleting it. CASCADE removes wizard_state.
    await SessionRepo(get_connection()).delete_by_id(session_id_a)
    state = await WizardStateRepo().get(session_id_a)
    assert state is None

    # Role assignment persists.
    repo = DeviceRepo(get_connection())
    entry = await repo.get_by_device_id("gm")
    assert entry is not None
    assert entry.role is DeviceRole.grid_meter

    # New session must re-acknowledge.
    raw_token_b, session_id_b = await _create_installer_session()
    client.get("/installer/setup/roles", cookies={"session": raw_token_b})
    state_b = await WizardStateRepo().get(session_id_b)
    assert state_b is not None
    assert state_b.step_2_acknowledged_gaps == frozenset()


async def test_e2e_advance_partial_state_recovery(
    e2e_app: FastAPI,
) -> None:
    """R1 #3: after the role assignments succeed and the advance write fails
    mid-execution, the next click recovers cleanly. We simulate the partial
    state by assigning roles but not advancing, then advancing in the next
    request — the gate state must be identical.
    """
    raw_token, session_id = await _create_installer_session()
    for dev, role in (
        ("inv", DeviceRole.inverter),
        ("bat", DeviceRole.battery),
        ("evc", DeviceRole.ev_charger),
        ("gm", DeviceRole.grid_meter),
    ):
        await _add_device(dev, role=role)
    client = TestClient(e2e_app, base_url="https://test", follow_redirects=False)
    # First GET: the gate should already report can_advance=True.
    response = client.get("/installer/setup/roles", cookies={"session": raw_token})
    assert response.status_code == 200
    assert "Continue to Step 3" in response.text
    # Now advance — the same outcome.
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
