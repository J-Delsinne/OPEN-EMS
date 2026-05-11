"""Route-level tests for the installer setup wizard — Story 9.1 AC9.

Covers: auth (non-installer rejected), CSRF enforcement on every POST,
form validation, OCPP manual-entry rejection with exact message, Step 2 gate
behaviour (pass → 303 to roles; fail → 400 + banner).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.adapters.discovery import DiscoveryService
from open_ems.core.devices import CapabilityStatus, DeviceDiscoveryResult
from open_ems.services.device_discovery import DeviceDiscoveryOrchestrator
from open_ems.services.manual_entry import (
    OCPP_MANUAL_ENTRY_REJECTION_REASON,
    ManualEntryService,
)
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

_CSRF = "test-csrf-token-fixed"


# Required schema additions on top of the conftest users + sessions tables.
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
        installer_acknowledged_unvalidated_at TEXT
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
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    )
"""


class _NoOpDiscovery(DiscoveryService):
    """Always-succeed probe stub; suitable for route tests that don't care
    about transport behaviour.
    """

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
    """Real FastAPI app wired with the Story 9.1 services + CSRF middleware.

    Reuses the conftest ``session_repo`` fixture which creates users + sessions
    tables in a real on-disk SQLite via ``init_database``; we add the
    device_registry + wizard_state tables on top.
    """
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

    app = FastAPI()
    app.state.device_repo = device_repo
    app.state.wizard_state_repo = wizard_state_repo
    app.state.discovery_orchestrator = orchestrator
    app.state.manual_entry_service = manual_entry
    app.include_router(setup_router)
    app.add_middleware(CsrfMiddleware)
    yield app


async def _create_installer_session() -> tuple[str, str]:
    """Returns (raw_session_token, session_id)."""
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


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_unauthenticated_get_redirects_to_login(app_with_services: FastAPI) -> None:
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/discovery")
    assert response.status_code == 302
    assert "/login" in response.headers["location"]


async def test_homeowner_role_rejected_on_setup_get(app_with_services: FastAPI) -> None:
    raw_token = await _create_homeowner_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.get(
        "/installer/setup/discovery",
        cookies={"session": raw_token},
    )
    # Homeowner is redirected to their dashboard (302 per require_installer)
    assert response.status_code == 302


async def test_installer_get_discovery_returns_200_and_persists_wizard_state(
    app_with_services: FastAPI,
) -> None:
    raw_token, session_id = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.get(
        "/installer/setup/discovery",
        cookies={"session": raw_token},
    )
    assert response.status_code == 200
    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert state.step_1_complete is False


async def test_setup_root_redirects_to_discovery(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup", cookies={"session": raw_token})
    assert response.status_code == 302
    assert response.headers["location"] == "/installer/setup/discovery"


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


async def test_post_without_csrf_token_returns_403(app_with_services: FastAPI) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/discovery/scan",
        cookies={"session": raw_token},
        # No CSRF header AND no form body — middleware rejects with 403.
    )
    assert response.status_code == 403


async def test_post_with_wrong_csrf_token_returns_403(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/discovery/scan",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "wrong-token"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Manual entry form validation
# ---------------------------------------------------------------------------


async def test_manual_entry_with_ocpp_rejected_with_exact_message(
    app_with_services: FastAPI,
) -> None:
    """AC5 clause 3: rejection is server-enforced with the exact reason code."""
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/discovery/manual",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "device_id": "ev-001",
            "protocol": "ocpp_1_6",
            "address": "/ev-001",
        },
    )
    assert response.status_code == 400
    assert OCPP_MANUAL_ENTRY_REJECTION_REASON in response.text
    # AC5 clause 3 exact-match guarantee
    assert "manual_entry_unsupported_for_protocol: ocpp_1_6" in response.text


async def test_manual_entry_blank_device_id_returns_400(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/discovery/manual",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "device_id": "   ",
            "protocol": "modbus_tcp",
            "address": "10.0.0.1:502",
        },
    )
    assert response.status_code == 400
    assert "device_id_required" in response.text


async def test_manual_entry_duplicate_device_id_returns_409(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    await DeviceRepo().upsert_manual(
        ManualDeviceEntryInput(
            device_id="inv-001",
            protocol="modbus_tcp",
            address="10.0.0.1:502",
        ),
        first_seen_at=datetime.now(UTC),
    )
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/discovery/manual",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "device_id": "inv-001",
            "protocol": "modbus_tcp",
            "address": "10.0.0.2:502",
        },
    )
    assert response.status_code == 409


async def test_manual_entry_modbus_success_persists_and_validates(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/discovery/manual",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "device_id": "inv-001",
            "protocol": "modbus_tcp",
            "address": "10.0.0.1:502",
            "model": "huawei_sun2000_v3",
        },
    )
    assert response.status_code == 200
    row = await DeviceRepo().get_by_device_id("inv-001")
    assert row is not None
    assert row.validated is True
    assert row.last_capability_status == "full"


# ---------------------------------------------------------------------------
# Step 2 advance gate
# ---------------------------------------------------------------------------


async def test_advance_with_empty_registry_redirects_to_roles(
    app_with_services: FastAPI,
) -> None:
    """AC7 cold-start branch: empty registry trivially passes the gate."""
    raw_token, session_id = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    # Seed wizard_state row by issuing the GET first.
    client.get("/installer/setup/discovery", cookies={"session": raw_token})
    response = client.post(
        "/installer/setup/discovery/advance",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    # AC9 + R2 state-table: POST /advance returns 302 to /installer/setup/roles.
    assert response.status_code == 302
    assert response.headers["location"] == "/installer/setup/roles"
    # The 302 alone does not prove the gate-pass side effect — assert that
    # ``step_1_complete`` was actually flipped on the wizard_state row so a
    # future regression that returns 302 without setting state would fail.
    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert state.step_1_complete is True
    assert state.step_1_completed_at is not None


async def test_advance_with_unacknowledged_unvalidated_returns_400_banner(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    await DeviceRepo().upsert_manual(
        ManualDeviceEntryInput(
            device_id="inv-001",
            protocol="modbus_tcp",
            address="10.0.0.1:502",
        ),
        first_seen_at=datetime.now(UTC),
    )
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/discovery", cookies={"session": raw_token})
    response = client.post(
        "/installer/setup/discovery/advance",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 400
    assert "inv-001" in response.text


async def test_advance_passing_gate_sets_step_1_complete(
    app_with_services: FastAPI,
) -> None:
    raw_token, session_id = await _create_installer_session()
    await DeviceRepo().upsert_manual(
        ManualDeviceEntryInput(
            device_id="inv-001",
            protocol="modbus_tcp",
            address="10.0.0.1:502",
        ),
        first_seen_at=datetime.now(UTC),
    )
    await DeviceRepo().acknowledge_unvalidated("inv-001", acknowledged_at=datetime.now(UTC))
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/discovery", cookies={"session": raw_token})
    response = client.post(
        "/installer/setup/discovery/advance",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 302
    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert state.step_1_complete is True
    assert state.step_1_completed_at is not None


async def test_roles_placeholder_renders_step_2_pending_message(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/roles", cookies={"session": raw_token})
    assert response.status_code == 200
    assert "Story 9.2" in response.text


# ---------------------------------------------------------------------------
# Scan + retry
# ---------------------------------------------------------------------------


async def test_post_scan_records_last_scan_id_on_wizard_state(
    app_with_services: FastAPI,
) -> None:
    raw_token, session_id = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/discovery", cookies={"session": raw_token})
    response = client.post(
        "/installer/setup/discovery/scan",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 200
    state = await WizardStateRepo().get(session_id)
    assert state is not None
    assert state.last_scan_id is not None


async def test_retry_unknown_device_returns_404(app_with_services: FastAPI) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/discovery/scan/nope/retry",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 404


async def test_acknowledge_unknown_device_returns_404(
    app_with_services: FastAPI,
) -> None:
    raw_token, _ = await _create_installer_session()
    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/discovery/manual/nope/acknowledge-unvalidated",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 404


async def test_render_reresolves_capability_profile(
    app_with_services: FastAPI,
) -> None:
    """R6: the row template MUST call ``get_profile`` at render time. A row
    whose cached ``last_capability_status`` says FULL but whose ``model`` no
    longer resolves to a known FULL profile (e.g. the capability registry was
    updated post-probe) MUST render as REDUCED with the current limitation
    reason — not as stale FULL from the cached column.
    """
    raw_token, _ = await _create_installer_session()
    # Seed a row whose cached state claims FULL but whose ``model=None`` makes
    # ``get_profile`` return REDUCED with ``capability_profile_unknown`` at
    # render time. The model-validator on DeviceRegistryEntry permits this
    # combination (validated=True requires last_capability_status to be set
    # which it is — to "full"). The cached value diverges from the current
    # registry resolution, which is the drift R6 exists to absorb.
    async with get_connection().execute(
        "INSERT INTO device_registry"
        " (device_id, protocol, address, model, source, validated,"
        "  last_capability_status, last_limitation_reason, first_seen_at, last_seen_at)"
        " VALUES (?, 'modbus_tcp', '10.0.0.50:502', NULL, 'manual_entry', 1,"
        " 'full', NULL, ?, ?)",
        ("inv-drift", datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
    ):
        pass
    await get_connection().commit()

    client = TestClient(app_with_services, base_url="https://test", follow_redirects=False)
    response = client.get("/installer/setup/discovery", cookies={"session": raw_token})
    assert response.status_code == 200
    body = response.text
    # Row should render REDUCED (current resolution) and surface the unknown-model
    # limitation reason — NOT the cached "FULL" badge.
    assert "REDUCED" in body
    assert "capability_profile_unknown" in body
    # The rendered span must NOT carry the badge-full class. Matching on the
    # full attribute literal avoids false hits on the CSS ``.badge-full { ... }``
    # rule that lives in the layout stylesheet.
    assert 'class="badge badge-full"' not in body
