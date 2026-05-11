"""Unit tests for the registry-diff NOT FOUND classification (Story 9.1 AC4).

These cases are also covered in ``test_device_discovery_orchestrator.py``;
this file factors the contract clauses into their own explicitly-named tests
so AC4 traceability is direct.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

import pytest

from open_ems.adapters.discovery import DeviceProbeError, DiscoveryService
from open_ems.core.devices import CapabilityStatus, DeviceDiscoveryResult
from open_ems.services.device_discovery import (
    DeviceDiscoveryOrchestrator,
    ModbusScanTarget,
    ScanTargets,
)
from open_ems.storage.repositories.device_repo import DeviceRegistryEntry

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)


class _StubDiscovery(DiscoveryService):
    def __init__(self, *, outcomes: dict[str, object] | None = None) -> None:
        super().__init__()
        self._outcomes = outcomes or {}

    async def probe_modbus_endpoint(  # type: ignore[override]
        self,
        *,
        device_id: str,
        host: str,
        port: int = 502,
        model: str | None = None,
    ) -> DeviceDiscoveryResult:
        outcome = self._outcomes.get(device_id)
        if isinstance(outcome, BaseException):
            raise outcome
        return DeviceDiscoveryResult(
            device_id=device_id,
            protocol="modbus_tcp",
            address=f"{host}:{port}",
            model=model,
            capability_status=CapabilityStatus.full,
        )


class _Registry:
    def __init__(self, entries: list[DeviceRegistryEntry]) -> None:
        self._entries = entries

    async def list_all(self) -> Iterable[DeviceRegistryEntry]:
        return list(self._entries)


def _entry(device_id: str) -> DeviceRegistryEntry:
    # R3 #1: validated=True requires last_capability_status to be set.
    return DeviceRegistryEntry(
        device_id=device_id,
        protocol="modbus_tcp",
        address="10.0.0.5:502",
        source="manual_entry",
        validated=True,
        last_capability_status="full",
        first_seen_at=_NOW,
    )


@pytest.mark.asyncio
async def test_registered_expected_absent_classified_as_not_found() -> None:
    svc = _StubDiscovery(outcomes={"inv-001": DeviceProbeError("down")})
    orch = DeviceDiscoveryOrchestrator(
        discovery_service=svc,
        registry_provider=_Registry([_entry("inv-001")]),
    )
    report = await orch.run_scan(
        ScanTargets(
            modbus_targets=(ModbusScanTarget(device_id="inv-001", host="10.0.0.5"),),
            include_ocpp_self_registered=False,
            expected_device_ids=frozenset({"inv-001"}),
        )
    )
    # Both fire per spec AC4 letter; the UI deduplicates at render time.
    assert [n.device_id for n in report.not_found_devices] == ["inv-001"]
    assert [u.device_id for u in report.unreachable_targets] == ["inv-001"]


@pytest.mark.asyncio
async def test_registered_but_not_expected_not_classified_as_not_found() -> None:
    svc = _StubDiscovery()
    orch = DeviceDiscoveryOrchestrator(
        discovery_service=svc,
        registry_provider=_Registry([_entry("inv-001"), _entry("inv-999")]),
    )
    report = await orch.run_scan(
        ScanTargets(
            modbus_targets=(ModbusScanTarget(device_id="inv-001", host="10.0.0.5"),),
            include_ocpp_self_registered=False,
            expected_device_ids=frozenset({"inv-001"}),
        )
    )
    assert report.not_found_devices == ()


@pytest.mark.asyncio
async def test_newly_probed_unregistered_target_never_classified_as_not_found() -> None:
    svc = _StubDiscovery(outcomes={"inv-new": DeviceProbeError("down")})
    orch = DeviceDiscoveryOrchestrator(
        discovery_service=svc,
        registry_provider=_Registry([]),
    )
    report = await orch.run_scan(
        ScanTargets(
            modbus_targets=(ModbusScanTarget(device_id="inv-new", host="10.0.0.5"),),
            include_ocpp_self_registered=False,
            expected_device_ids=frozenset({"inv-new"}),
        )
    )
    assert report.not_found_devices == ()
    assert len(report.unreachable_targets) == 1


@pytest.mark.asyncio
async def test_orchestrator_without_registry_provider_returns_no_not_found() -> None:
    svc = _StubDiscovery()
    orch = DeviceDiscoveryOrchestrator(discovery_service=svc)
    report = await orch.run_scan(
        ScanTargets(
            modbus_targets=(ModbusScanTarget(device_id="inv-001", host="10.0.0.5"),),
            include_ocpp_self_registered=False,
            expected_device_ids=frozenset({"inv-001"}),
        )
    )
    assert report.not_found_devices == ()
