"""Unit tests for ``DeviceDiscoveryOrchestrator`` — Story 9.1 AC2, AC3, AC4."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime

import pytest

from open_ems.adapters.discovery import DeviceProbeError, DiscoveryService
from open_ems.adapters.ocpp.central_system import (
    OCPPCentralSystem,
    OCPPRegisteredCharger,
)
from open_ems.core.devices import CapabilityStatus, DeviceDiscoveryResult
from open_ems.services.device_discovery import (
    DeviceDiscoveryOrchestrator,
    ModbusScanTarget,
    ScanTargets,
    _classify_badge,
)
from open_ems.storage.repositories.device_repo import DeviceRegistryEntry

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------


class _FakeDiscoveryService(DiscoveryService):
    """Lets a test pre-program success/failure per device_id."""

    def __init__(
        self,
        *,
        modbus_outcomes: dict[str, object] | None = None,
        dsmr_outcomes: dict[str, object] | None = None,
        modbus_delay_s: float = 0.0,
    ) -> None:
        super().__init__()
        self._modbus_outcomes: dict[str, object] = modbus_outcomes or {}
        self._dsmr_outcomes: dict[str, object] = dsmr_outcomes or {}
        self._modbus_delay_s = modbus_delay_s
        self.modbus_call_count = 0
        self.dsmr_call_count = 0
        self.concurrent_modbus_peak = 0
        self._in_flight = 0

    async def probe_modbus_endpoint(  # type: ignore[override]
        self,
        *,
        device_id: str,
        host: str,
        port: int = 502,
        model: str | None = None,
    ) -> DeviceDiscoveryResult:
        self.modbus_call_count += 1
        self._in_flight += 1
        self.concurrent_modbus_peak = max(self.concurrent_modbus_peak, self._in_flight)
        try:
            if self._modbus_delay_s > 0:
                await asyncio.sleep(self._modbus_delay_s)
            outcome = self._modbus_outcomes.get(device_id)
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
        finally:
            self._in_flight -= 1

    async def probe_dsmr_endpoint(  # type: ignore[override]
        self,
        *,
        device_id: str,
        serial_port: str | None = None,
        tcp_host: str | None = None,
        tcp_port: int | None = None,
        dsmr_version: str = "5",
    ) -> DeviceDiscoveryResult:
        self.dsmr_call_count += 1
        outcome = self._dsmr_outcomes.get(device_id)
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


class _FakeRegistryProvider:
    def __init__(self, entries: list[DeviceRegistryEntry]) -> None:
        self._entries = entries

    async def list_all(self) -> Iterable[DeviceRegistryEntry]:
        return list(self._entries)


class _FakeOCPP(OCPPCentralSystem):
    def __init__(self, chargers: tuple[OCPPRegisteredCharger, ...]) -> None:
        super().__init__()
        self._chargers = chargers

    def list_registered_chargers(self) -> tuple[OCPPRegisteredCharger, ...]:  # type: ignore[override]
        return self._chargers


def _entry(
    device_id: str,
    protocol: str = "modbus_tcp",
    address: str = "192.168.1.10:502",
    validated: bool = True,
) -> DeviceRegistryEntry:
    # R3 #1: validated=True requires last_capability_status. Default to "full"
    # so the fixture builds a self-consistent row; callers that need REDUCED or
    # unvalidated can override.
    return DeviceRegistryEntry(
        device_id=device_id,
        protocol=protocol,  # type: ignore[arg-type]
        address=address,
        source="manual_entry",
        validated=validated,
        last_capability_status="full" if validated else None,
        first_seen_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Badge classification (AC2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (CapabilityStatus.full, "FULL"),
        (CapabilityStatus.reduced, "REDUCED"),
        (CapabilityStatus.unsupported, "REDUCED"),
    ],
)
def test_badge_classifier_maps_all_three_capability_statuses(
    status: CapabilityStatus, expected: str
) -> None:
    """AC2: ``unsupported`` is rendered as REDUCED (the UI's accepted state)."""
    assert _classify_badge(status) == expected


# ---------------------------------------------------------------------------
# Orchestrator: per-target success + failure (AC2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_returns_live_row_for_successful_modbus_probe() -> None:
    svc = _FakeDiscoveryService()
    orch = DeviceDiscoveryOrchestrator(discovery_service=svc)
    report = await orch.run_scan(
        ScanTargets(
            modbus_targets=(
                ModbusScanTarget(
                    device_id="inv-001",
                    host="192.168.1.10",
                    port=502,
                    model="huawei_sun2000_v3",
                ),
            ),
            include_ocpp_self_registered=False,
        )
    )
    assert len(report.live_results) == 1
    assert len(report.unreachable_targets) == 0
    assert report.live_results[0].result.device_id == "inv-001"


@pytest.mark.asyncio
async def test_orchestrator_failed_modbus_probe_becomes_unreachable_not_not_found() -> None:
    svc = _FakeDiscoveryService(modbus_outcomes={"inv-001": DeviceProbeError("connection refused")})
    orch = DeviceDiscoveryOrchestrator(discovery_service=svc)
    report = await orch.run_scan(
        ScanTargets(
            modbus_targets=(
                ModbusScanTarget(
                    device_id="inv-001",
                    host="192.168.1.10",
                    port=502,
                ),
            ),
            include_ocpp_self_registered=False,
        )
    )
    assert len(report.live_results) == 0
    assert len(report.unreachable_targets) == 1
    assert report.unreachable_targets[0].device_id == "inv-001"
    assert report.unreachable_targets[0].protocol == "modbus_tcp"
    # Exact-match on the format ``modbus_<ExcName>: <message>`` — substring
    # matches would let a prefix-rename of the protocol or exception name slip
    # past the assertion without breaking the test.
    assert report.unreachable_targets[0].reason == "modbus_DeviceProbeError: connection refused"
    # AC4: NOT FOUND is registry-diff only — never applied to fresh probe failures
    assert len(report.not_found_devices) == 0


@pytest.mark.asyncio
async def test_orchestrator_single_failure_does_not_abort_other_targets() -> None:
    svc = _FakeDiscoveryService(modbus_outcomes={"inv-001": DeviceProbeError("connection refused")})
    orch = DeviceDiscoveryOrchestrator(discovery_service=svc)
    report = await orch.run_scan(
        ScanTargets(
            modbus_targets=(
                ModbusScanTarget(device_id="inv-001", host="10.0.0.1"),
                ModbusScanTarget(device_id="inv-002", host="10.0.0.2"),
            ),
            include_ocpp_self_registered=False,
        )
    )
    assert {r.result.device_id for r in report.live_results} == {"inv-002"}
    assert {u.device_id for u in report.unreachable_targets} == {"inv-001"}


@pytest.mark.asyncio
async def test_orchestrator_runs_modbus_probes_concurrently() -> None:
    """AC2: ``asyncio.gather`` over per-target tasks — probes overlap in time."""
    svc = _FakeDiscoveryService(modbus_delay_s=0.05)
    orch = DeviceDiscoveryOrchestrator(discovery_service=svc)
    await orch.run_scan(
        ScanTargets(
            modbus_targets=(
                ModbusScanTarget(device_id="inv-001", host="10.0.0.1"),
                ModbusScanTarget(device_id="inv-002", host="10.0.0.2"),
                ModbusScanTarget(device_id="inv-003", host="10.0.0.3"),
            ),
            include_ocpp_self_registered=False,
        )
    )
    # All three tasks must be in flight concurrently. The previous bound
    # ``>= 2`` would have accepted a regression to "two-at-a-time" scheduling;
    # the AC2 ``asyncio.gather`` contract requires a true peak of 3 over the
    # three submitted Modbus tasks.
    assert svc.concurrent_modbus_peak == 3


# ---------------------------------------------------------------------------
# OCPP self-registered chargers (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_captures_ocpp_self_registered_chargers() -> None:
    svc = _FakeDiscoveryService()
    ocpp = _FakeOCPP(
        chargers=(
            OCPPRegisteredCharger(
                device_id="ev-001",
                charge_point_id="ev-001",
                address="/ev-001",
                model="ocpp_1_6",
            ),
        )
    )
    orch = DeviceDiscoveryOrchestrator(discovery_service=svc, ocpp_central_system=ocpp)
    report = await orch.run_scan(ScanTargets(include_ocpp_self_registered=True))
    assert len(report.live_results) == 1
    assert report.live_results[0].result.protocol == "ocpp_1_6"
    assert report.live_results[0].result.device_id == "ev-001"


@pytest.mark.asyncio
async def test_orchestrator_skips_ocpp_when_flag_is_false() -> None:
    svc = _FakeDiscoveryService()
    ocpp = _FakeOCPP(
        chargers=(
            OCPPRegisteredCharger(
                device_id="ev-001",
                charge_point_id="ev-001",
                address="/ev-001",
                model="ocpp_1_6",
            ),
        )
    )
    orch = DeviceDiscoveryOrchestrator(discovery_service=svc, ocpp_central_system=ocpp)
    report = await orch.run_scan(ScanTargets(include_ocpp_self_registered=False))
    assert report.live_results == ()


@pytest.mark.asyncio
async def test_orchestrator_empty_ocpp_contributes_no_unreachable() -> None:
    """AC3: absence of self-registration is silent — never an unreachable_target."""
    svc = _FakeDiscoveryService()
    ocpp = _FakeOCPP(chargers=())
    orch = DeviceDiscoveryOrchestrator(discovery_service=svc, ocpp_central_system=ocpp)
    report = await orch.run_scan(ScanTargets(include_ocpp_self_registered=True))
    assert report.live_results == ()
    assert report.unreachable_targets == ()


# ---------------------------------------------------------------------------
# NOT FOUND classification (AC4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_found_when_registered_expected_and_absent_from_live_results() -> None:
    """AC4 (strict letter): both classifications fire when a registered + expected
    device produces no live row. The classifier emits both rows; the UI template
    is responsible for deduplicating the rendered output so the installer does
    not see the same device twice (see the ``_scan_results.html`` dedup test).
    """
    svc = _FakeDiscoveryService(modbus_outcomes={"inv-001": DeviceProbeError("connection refused")})
    registry = _FakeRegistryProvider([_entry("inv-001")])
    orch = DeviceDiscoveryOrchestrator(
        discovery_service=svc,
        registry_provider=registry,
    )
    report = await orch.run_scan(
        ScanTargets(
            modbus_targets=(ModbusScanTarget(device_id="inv-001", host="10.0.0.1"),),
            include_ocpp_self_registered=False,
            expected_device_ids=frozenset({"inv-001"}),
        )
    )
    assert len(report.unreachable_targets) == 1
    assert len(report.not_found_devices) == 1
    assert report.not_found_devices[0].device_id == "inv-001"


@pytest.mark.asyncio
async def test_not_found_skipped_when_device_id_not_in_expected_set() -> None:
    """AC4: a partial-scope rescan must NOT brand other registered devices missing."""
    svc = _FakeDiscoveryService()
    registry = _FakeRegistryProvider([_entry("inv-001"), _entry("inv-999")])
    orch = DeviceDiscoveryOrchestrator(
        discovery_service=svc,
        registry_provider=registry,
    )
    report = await orch.run_scan(
        ScanTargets(
            modbus_targets=(ModbusScanTarget(device_id="inv-001", host="10.0.0.1"),),
            include_ocpp_self_registered=False,
            expected_device_ids=frozenset({"inv-001"}),
        )
    )
    assert len(report.live_results) == 1
    # inv-999 is registered but NOT in expected_device_ids → MUST NOT be NOT FOUND
    assert report.not_found_devices == ()


@pytest.mark.asyncio
async def test_not_found_skipped_for_brand_new_probe_target() -> None:
    """AC4: a newly-probed target that failed lands ONLY in unreachable_targets."""
    svc = _FakeDiscoveryService(modbus_outcomes={"inv-new": DeviceProbeError("connection refused")})
    registry = _FakeRegistryProvider([])  # no registry entries at all
    orch = DeviceDiscoveryOrchestrator(
        discovery_service=svc,
        registry_provider=registry,
    )
    report = await orch.run_scan(
        ScanTargets(
            modbus_targets=(ModbusScanTarget(device_id="inv-new", host="10.0.0.1"),),
            include_ocpp_self_registered=False,
            expected_device_ids=frozenset({"inv-new"}),
        )
    )
    assert len(report.unreachable_targets) == 1
    # No registry entry for inv-new → no NOT FOUND row
    assert report.not_found_devices == ()


@pytest.mark.asyncio
async def test_scan_report_carries_scan_id_uuid4_and_utc_timestamps() -> None:
    svc = _FakeDiscoveryService()
    orch = DeviceDiscoveryOrchestrator(discovery_service=svc)
    report = await orch.run_scan(ScanTargets(include_ocpp_self_registered=False))
    assert report.scan_id.version == 4
    assert report.started_at.tzinfo is not None
    assert report.completed_at.tzinfo is not None
    assert report.completed_at >= report.started_at
