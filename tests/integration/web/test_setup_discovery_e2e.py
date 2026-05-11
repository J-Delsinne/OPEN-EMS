"""End-to-end integration tests for the installer discovery wizard.

Story 9.1 AC11 (integration). Exercises the full request → scan → render
pipeline against an in-memory SQLite DB. Simulated Modbus / DSMR / OCPP
behaviour is injected through a custom ``DiscoveryService`` stub — the same
shape used by ``DiscoveryService`` in production, but with deterministic
outcomes.

Cancellation case (per AC11 last sub-bullet) is covered as a stand-alone
test: aborting the scan mid-probe cancels every in-flight task cooperatively
and leaves no half-written ``device_registry`` rows.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.adapters.discovery import DeviceProbeError, DiscoveryService
from open_ems.core.devices import CapabilityStatus, DeviceDiscoveryResult
from open_ems.services.device_discovery import (
    DeviceDiscoveryOrchestrator,
    ModbusScanTarget,
    ScanTargets,
)
from open_ems.services.manual_entry import ManualEntryService
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

_CSRF = "test-csrf-e2e-fixed"

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


class _SimulatedDiscovery(DiscoveryService):
    """Deterministic stand-in for the real DiscoveryService.

    Outcomes are keyed by device_id. A ``DeviceProbeError`` instance is raised;
    a ``DeviceDiscoveryResult`` is returned unchanged; anything else produces
    a default-FULL success.
    """

    def __init__(
        self,
        *,
        modbus_outcomes: dict[str, object] | None = None,
        dsmr_outcomes: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self._modbus = modbus_outcomes or {}
        self._dsmr = dsmr_outcomes or {}

    async def probe_modbus_endpoint(  # type: ignore[override]
        self,
        *,
        device_id: str,
        host: str,
        port: int = 502,
        model: str | None = None,
    ) -> DeviceDiscoveryResult:
        outcome = self._modbus.get(device_id)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, DeviceDiscoveryResult):
            return outcome
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
        outcome = self._dsmr.get(device_id)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, DeviceDiscoveryResult):
            return outcome
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
) -> AsyncGenerator[tuple[FastAPI, _SimulatedDiscovery], None]:
    conn = get_connection()
    await conn.execute(_CREATE_DEVICE_REGISTRY)
    await conn.execute(_CREATE_WIZARD_STATE)
    await conn.commit()

    device_repo = DeviceRepo()
    wizard_state_repo = WizardStateRepo()
    discovery = _SimulatedDiscovery()
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
    yield app, discovery


async def _create_installer_session() -> str:
    user_id = await UserRepo(get_connection()).create(
        username=f"installer_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role="installer",
    )
    raw_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF,
    )
    return raw_token


async def test_scan_renders_reduced_row_for_unknown_model(
    e2e_app: tuple[FastAPI, _SimulatedDiscovery],
) -> None:
    """AC11 #1: a probe that resolves an unknown model produces REDUCED."""
    app, discovery = e2e_app
    discovery._modbus["inv-unknown"] = DeviceDiscoveryResult(  # type: ignore[reportPrivateUsage]
        device_id="inv-unknown",
        protocol="modbus_tcp",
        address="10.0.0.1:502",
        model="unknown_xyz",
        capability_status=CapabilityStatus.reduced,
    )
    await DeviceRepo().upsert_manual(
        ManualDeviceEntryInput(
            device_id="inv-unknown",
            protocol="modbus_tcp",
            address="10.0.0.1:502",
            model="unknown_xyz",
        ),
        first_seen_at=datetime.now(UTC),
    )
    raw_token = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/discovery", cookies={"session": raw_token})
    response = client.post(
        "/installer/setup/discovery/scan",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 200
    assert "REDUCED" in response.text


async def test_registered_absent_produces_unreachable_row_not_duplicated_as_not_found(
    e2e_app: tuple[FastAPI, _SimulatedDiscovery],
) -> None:
    """AC11 #2 + dedup: a registered device whose probe fails surfaces as an
    ``Unreachable`` row with the failure reason. The orchestrator emits both
    classifications per spec AC4 letter, but the template dedupes the NOT FOUND
    row when the device is already shown as unreachable — installer never sees
    the same device twice with competing narratives.
    """
    app, discovery = e2e_app
    discovery._modbus["inv-missing"] = DeviceProbeError("unreachable")  # type: ignore[reportPrivateUsage]
    await DeviceRepo().upsert_manual(
        ManualDeviceEntryInput(
            device_id="inv-missing",
            protocol="modbus_tcp",
            address="10.0.0.99:502",
        ),
        first_seen_at=datetime.now(UTC),
    )
    raw_token = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    client.get("/installer/setup/discovery", cookies={"session": raw_token})
    response = client.post(
        "/installer/setup/discovery/scan",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 200
    # Unreachable rendering: device is shown with the failure reason.
    assert "Unreachable targets" in response.text
    assert "inv-missing" in response.text
    assert "modbus_DeviceProbeError" in response.text
    # NOT FOUND band is suppressed for this device — no duplicate row.
    assert "NOT FOUND" not in response.text


async def test_manual_dsmr_reachable_persists_as_validated(
    e2e_app: tuple[FastAPI, _SimulatedDiscovery],
) -> None:
    """AC11 #3: manual entry pointing at a reachable DSMR persists as validated."""
    app, _ = e2e_app
    raw_token = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/installer/setup/discovery/manual",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "device_id": "meter-001",
            "protocol": "dsmr_p1",
            "address": "/dev/ttyUSB0",
        },
    )
    assert response.status_code == 200
    row = await DeviceRepo().get_by_device_id("meter-001")
    assert row is not None
    assert row.validated is True


async def test_manual_unreachable_stays_unvalidated_and_blocks_advance(
    e2e_app: tuple[FastAPI, _SimulatedDiscovery],
) -> None:
    """AC11 #4: unreachable manual entry stays validated=0; advance button gated."""
    app, discovery = e2e_app
    discovery._modbus["inv-down"] = DeviceProbeError("connection refused")  # type: ignore[reportPrivateUsage]
    raw_token = await _create_installer_session()
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    add_response = client.post(
        "/installer/setup/discovery/manual",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
        data={
            "device_id": "inv-down",
            "protocol": "modbus_tcp",
            "address": "10.0.0.50:502",
        },
    )
    assert add_response.status_code == 200
    row = await DeviceRepo().get_by_device_id("inv-down")
    assert row is not None
    assert row.validated is False

    # Discovery page now renders the disabled Continue button.
    page = client.get("/installer/setup/discovery", cookies={"session": raw_token})
    assert page.status_code == 200
    assert "Continue to Step 2" in page.text
    assert "disabled" in page.text

    # POST advance fails with the inline banner (AC7).
    advance = client.post(
        "/installer/setup/discovery/advance",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert advance.status_code == 400
    assert "inv-down" in advance.text


async def test_scan_cancellation_releases_resources(
    e2e_app: tuple[FastAPI, _SimulatedDiscovery],
) -> None:
    """AC11 #5 + R3 #6: cancelling a scan task propagates to per-target probes.

    Direct orchestrator-level cancellation test (the route-handler cancel path
    is exercised by the framework around it). Asserts the in-flight probe
    receives ``CancelledError`` and the orchestrator does not produce a
    partial DiscoveryScanReport.
    """
    app, _ = e2e_app
    orchestrator = app.state.discovery_orchestrator
    assert isinstance(orchestrator, DeviceDiscoveryOrchestrator)

    cancelled_flag: list[bool] = []

    class _SlowDiscovery(DiscoveryService):
        async def probe_modbus_endpoint(  # type: ignore[override]
            self,
            *,
            device_id: str,
            host: str,
            port: int = 502,
            model: str | None = None,
        ) -> DeviceDiscoveryResult:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled_flag.append(True)
                raise
            return DeviceDiscoveryResult(
                device_id=device_id,
                protocol="modbus_tcp",
                address=f"{host}:{port}",
                capability_status=CapabilityStatus.full,
            )

    slow_orch = DeviceDiscoveryOrchestrator(
        discovery_service=_SlowDiscovery(),
        registry_provider=app.state.device_repo,
    )
    scan_task = asyncio.create_task(
        slow_orch.run_scan(
            ScanTargets(
                modbus_targets=(ModbusScanTarget(device_id="x", host="10.0.0.1"),),
                include_ocpp_self_registered=False,
            )
        )
    )
    # Give the probe time to start, then cancel.
    await asyncio.sleep(0.05)
    scan_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await scan_task
    assert cancelled_flag == [True], "cancellation must propagate to the probe coroutine"
    assert len(await DeviceRepo().list_all()) == 0  # no half-written rows
