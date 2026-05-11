"""Unit tests for ``ManualEntryService`` — Story 9.1 AC5, AC6."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import aiosqlite
import pytest
import pytest_asyncio

from open_ems.adapters.discovery import DeviceProbeError, DiscoveryService
from open_ems.core.devices import CapabilityStatus, DeviceDiscoveryResult
from open_ems.services.manual_entry import (
    OCPP_MANUAL_ENTRY_REJECTION_REASON,
    ManualEntryFormError,
    ManualEntryFormRequest,
    ManualEntryService,
)
from open_ems.storage.repositories.device_repo import DeviceRepo

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
        last_capability_status TEXT
            CHECK (last_capability_status IS NULL
                   OR last_capability_status IN ('full', 'reduced', 'unsupported')),
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

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)


class _StubDiscovery(DiscoveryService):
    def __init__(self, *, modbus: object = None, dsmr: object = None) -> None:
        super().__init__()
        self._modbus = modbus
        self._dsmr = dsmr

    async def probe_modbus_endpoint(  # type: ignore[override]
        self,
        *,
        device_id: str,
        host: str,
        port: int = 502,
        model: str | None = None,
    ) -> DeviceDiscoveryResult:
        outcome = self._modbus
        if isinstance(outcome, BaseException):
            raise outcome
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
        outcome = self._dsmr
        if isinstance(outcome, BaseException):
            raise outcome
        return DeviceDiscoveryResult(
            device_id=device_id,
            protocol="dsmr_p1",
            address=serial_port or f"{tcp_host}:{tcp_port}",
            model="dsmr_p1",
            capability_status=CapabilityStatus.full,
        )


@pytest_asyncio.fixture
async def repo() -> AsyncGenerator[DeviceRepo, None]:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_CREATE_DEVICE_REGISTRY)
        await conn.commit()
        yield DeviceRepo(conn)


# ---------------------------------------------------------------------------
# Form validation (AC5 step 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_form_rejects_blank_device_id(repo: DeviceRepo) -> None:
    svc = ManualEntryService(device_repo=repo, discovery_service=_StubDiscovery())
    with pytest.raises(ManualEntryFormError) as exc:
        svc.validate_form(
            ManualEntryFormRequest(
                device_id="   ",
                protocol="modbus_tcp",
                address="192.168.1.10:502",
            )
        )
    assert exc.value.field == "device_id"
    assert exc.value.reason == "device_id_required"


@pytest.mark.asyncio
async def test_validate_form_rejects_device_id_over_64_chars(repo: DeviceRepo) -> None:
    svc = ManualEntryService(device_repo=repo, discovery_service=_StubDiscovery())
    with pytest.raises(ManualEntryFormError) as exc:
        svc.validate_form(
            ManualEntryFormRequest(
                device_id="x" * 65,
                protocol="modbus_tcp",
                address="192.168.1.10:502",
            )
        )
    assert exc.value.reason == "device_id_too_long"


@pytest.mark.asyncio
async def test_validate_form_rejects_ocpp_with_exact_message(repo: DeviceRepo) -> None:
    """AC5 clause 3 + R2 / D5: exact-match rejection reason."""
    svc = ManualEntryService(device_repo=repo, discovery_service=_StubDiscovery())
    with pytest.raises(ManualEntryFormError) as exc:
        svc.validate_form(
            ManualEntryFormRequest(
                device_id="ev-001",
                protocol="ocpp_1_6",
                address="/ev-001",
            )
        )
    assert exc.value.field == "protocol"
    assert exc.value.reason == OCPP_MANUAL_ENTRY_REJECTION_REASON
    assert exc.value.reason == "manual_entry_unsupported_for_protocol: ocpp_1_6"


@pytest.mark.asyncio
async def test_validate_form_rejects_unknown_protocol(repo: DeviceRepo) -> None:
    svc = ManualEntryService(device_repo=repo, discovery_service=_StubDiscovery())
    with pytest.raises(ManualEntryFormError) as exc:
        svc.validate_form(
            ManualEntryFormRequest(
                device_id="inv-001",
                protocol="carrier_pigeon",
                address="x",
            )
        )
    assert "protocol_invalid" in exc.value.reason


@pytest.mark.asyncio
async def test_validate_form_rejects_modbus_address_without_port(repo: DeviceRepo) -> None:
    svc = ManualEntryService(device_repo=repo, discovery_service=_StubDiscovery())
    with pytest.raises(ManualEntryFormError) as exc:
        svc.validate_form(
            ManualEntryFormRequest(
                device_id="inv-001",
                protocol="modbus_tcp",
                address="192.168.1.10",  # missing :port
            )
        )
    assert exc.value.reason == "modbus_address_must_be_host_port"


@pytest.mark.asyncio
async def test_validate_form_accepts_dsmr_serial_path(repo: DeviceRepo) -> None:
    svc = ManualEntryService(device_repo=repo, discovery_service=_StubDiscovery())
    entry = svc.validate_form(
        ManualEntryFormRequest(
            device_id="meter-001",
            protocol="dsmr_p1",
            address="/dev/ttyUSB0",
        )
    )
    assert entry.protocol == "dsmr_p1"
    assert entry.address == "/dev/ttyUSB0"


@pytest.mark.asyncio
async def test_validate_form_accepts_dsmr_host_port(repo: DeviceRepo) -> None:
    svc = ManualEntryService(device_repo=repo, discovery_service=_StubDiscovery())
    entry = svc.validate_form(
        ManualEntryFormRequest(
            device_id="meter-001",
            protocol="dsmr_p1",
            address="192.168.1.20:2404",
        )
    )
    assert entry.address == "192.168.1.20:2404"


# ---------------------------------------------------------------------------
# Persist + probe lifecycle (AC5 steps 2-5, AC6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_unvalidated_writes_row_with_validated_zero(repo: DeviceRepo) -> None:
    svc = ManualEntryService(device_repo=repo, discovery_service=_StubDiscovery())
    entry = svc.validate_form(
        ManualEntryFormRequest(
            device_id="inv-001",
            protocol="modbus_tcp",
            address="10.0.0.1:502",
        )
    )
    persisted = await svc.persist_unvalidated(entry, now=_NOW)
    assert persisted.validated is False
    assert persisted.last_capability_status is None
    assert persisted.last_seen_at is None


@pytest.mark.asyncio
async def test_probe_success_flips_validated_to_one(repo: DeviceRepo) -> None:
    svc = ManualEntryService(device_repo=repo, discovery_service=_StubDiscovery())
    entry = svc.validate_form(
        ManualEntryFormRequest(
            device_id="inv-001",
            protocol="modbus_tcp",
            address="10.0.0.1:502",
            model="huawei_sun2000_v3",
        )
    )
    persisted = await svc.persist_unvalidated(entry, now=_NOW)
    outcome = await svc.probe_and_validate(persisted)
    assert outcome.status == "validated"
    assert outcome.failure_reason is None
    row = await repo.get_by_device_id("inv-001")
    assert row is not None
    assert row.validated is True
    assert row.last_capability_status == "full"
    assert row.last_seen_at is not None


@pytest.mark.asyncio
async def test_probe_failure_keeps_validated_zero_with_failure_reason(
    repo: DeviceRepo,
) -> None:
    svc = ManualEntryService(
        device_repo=repo,
        discovery_service=_StubDiscovery(modbus=DeviceProbeError("connection refused")),
    )
    entry = svc.validate_form(
        ManualEntryFormRequest(
            device_id="inv-001",
            protocol="modbus_tcp",
            address="10.0.0.1:502",
        )
    )
    persisted = await svc.persist_unvalidated(entry, now=_NOW)
    outcome = await svc.probe_and_validate(persisted)
    assert outcome.status == "failed"
    assert outcome.failure_reason is not None
    assert "connection refused" in outcome.failure_reason
    row = await repo.get_by_device_id("inv-001")
    assert row is not None
    assert row.validated is False  # AC6: stays unvalidated on probe failure
    assert row.last_capability_status is None


@pytest.mark.asyncio
async def test_probe_success_unknown_model_becomes_reduced_not_full(
    repo: DeviceRepo,
) -> None:
    """AC6: unknown model resolves to REDUCED via get_profile(); never FULL.

    The stub probe always returns CapabilityStatus.full — but the contract is
    that the registry-resolved status is what gets persisted. Here we instead
    rely on ``mark_validated`` taking the result's status; the probe service
    is responsible for the resolution. This test pins the contract at the
    service layer.
    """

    # Stub returns CapabilityStatus.reduced from the registry path for unknown.
    class _ReducedStub(DiscoveryService):
        async def probe_modbus_endpoint(  # type: ignore[override]
            self, *, device_id: str, host: str, port: int = 502, model: str | None = None
        ) -> DeviceDiscoveryResult:
            return DeviceDiscoveryResult(
                device_id=device_id,
                protocol="modbus_tcp",
                address=f"{host}:{port}",
                model=model,
                capability_status=CapabilityStatus.reduced,
            )

    svc = ManualEntryService(device_repo=repo, discovery_service=_ReducedStub())
    entry = svc.validate_form(
        ManualEntryFormRequest(
            device_id="inv-001",
            protocol="modbus_tcp",
            address="10.0.0.1:502",
            model="unknown_model_xyz",
        )
    )
    persisted = await svc.persist_unvalidated(entry, now=_NOW)
    await svc.probe_and_validate(persisted)
    row = await repo.get_by_device_id("inv-001")
    assert row is not None
    assert row.last_capability_status == "reduced"


@pytest.mark.asyncio
async def test_probe_refuses_ocpp_entries_loudly(repo: DeviceRepo) -> None:
    """The route handler rejects OCPP at validation; the service refuses too."""
    svc = ManualEntryService(device_repo=repo, discovery_service=_StubDiscovery())
    # Manually construct an OCPP entry bypassing form validation
    from open_ems.storage.repositories.device_repo import DeviceRegistryEntry

    entry = DeviceRegistryEntry(
        device_id="ev-001",
        protocol="ocpp_1_6",
        address="/ev-001",
        source="manual_entry",
        validated=False,
        first_seen_at=_NOW,
    )
    with pytest.raises(RuntimeError, match="refuses ocpp_1_6"):
        await svc.probe_and_validate(entry)
