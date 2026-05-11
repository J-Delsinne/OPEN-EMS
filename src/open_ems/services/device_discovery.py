"""``DeviceDiscoveryOrchestrator`` — multi-protocol concurrent discovery scan.

Story 9.1 AC2 / AC3 / AC4. Runs Modbus + DSMR probes concurrently via
``DiscoveryService`` and atomically captures the current set of self-registered
OCPP chargers from ``OCPPCentralSystem``. Per-target failures NEVER abort the
scan — they land in ``unreachable_targets``. ``NOT FOUND`` is a registry-diff
classification (AC4): a device is NOT FOUND if and only if a
``DeviceRegistryEntry`` exists, the scan was configured to expect it, and the
scan produced no ``LiveDiscoveryRow`` for that ``device_id``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol, runtime_checkable

import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from open_ems.adapters.capabilities import get_profile
from open_ems.adapters.discovery import DeviceProbeError, DiscoveryService
from open_ems.adapters.ocpp.central_system import OCPPCentralSystem, OCPPRegisteredCharger
from open_ems.core.devices import (
    CapabilityStatus,
    DeviceCapabilityProfile,
    DeviceDiscoveryResult,
)
from open_ems.storage.repositories.device_repo import DeviceRegistryEntry

logger = structlog.get_logger(__name__)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

BadgeClassification = Literal["FULL", "REDUCED"]


def _classify_badge(status: CapabilityStatus) -> BadgeClassification:
    """Story 9.1 AC2: ``unsupported`` renders as REDUCED in the UI.

    The underlying enum value is preserved on the row for audit; only the
    rendered badge is squashed to two outputs.
    """
    if status is CapabilityStatus.full:
        return "FULL"
    return "REDUCED"


class ModbusScanTarget(BaseModel):
    """One Modbus probe target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    host: NonEmptyStr
    port: int = Field(default=502, ge=1, le=65535)
    model: str | None = Field(default=None, min_length=1)


class DSMRScanTarget(BaseModel):
    """One DSMR P1 probe target. Exactly one of ``serial_port`` or ``tcp_host``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    serial_port: str | None = Field(default=None, min_length=1)
    tcp_host: str | None = Field(default=None, min_length=1)
    tcp_port: int | None = Field(default=None, ge=1, le=65535)
    dsmr_version: Literal["4", "5"] = "5"

    @model_validator(mode="after")
    def _exactly_one_of_serial_or_tcp(self) -> DSMRScanTarget:
        # Cross-field invariants surface as ``ValidationError`` like every other
        # Pydantic model in this file (the prior custom ``__init__`` raised bare
        # ``ValueError`` and was inconsistent with the rest of the contract).
        if self.serial_port is None and self.tcp_host is None:
            raise ValueError("DSMRScanTarget requires serial_port or tcp_host")
        if self.serial_port is not None and self.tcp_host is not None:
            raise ValueError("DSMRScanTarget cannot have both serial_port and tcp_host")
        if self.tcp_host is not None and self.tcp_port is None:
            raise ValueError("DSMRScanTarget with tcp_host requires tcp_port")
        return self


class ScanTargets(BaseModel):
    """Inputs for ``DeviceDiscoveryOrchestrator.run_scan``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    modbus_targets: tuple[ModbusScanTarget, ...] = ()
    dsmr_targets: tuple[DSMRScanTarget, ...] = ()
    include_ocpp_self_registered: bool = True
    # AC4: the registry-diff requires the caller to declare which device_ids are
    # expected. A partial-scope rescan supplies a subset; a full rescan supplies
    # every registered device_id. Omitting a registered device_id excludes it
    # from NOT FOUND classification for this scan.
    expected_device_ids: frozenset[str] = frozenset()


class LiveDiscoveryRow(BaseModel):
    """One reached device in a scan, with its resolved capability profile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: DeviceDiscoveryResult
    profile: DeviceCapabilityProfile
    badge: BadgeClassification


class UnreachableTarget(BaseModel):
    """A newly-probed target that did not respond. NOT the same as NOT FOUND.

    NOT FOUND is registry-diff only (AC4); this row represents a freshly
    requested target whose probe failed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    protocol: Literal["modbus_tcp", "ocpp_1_6", "dsmr_p1"]
    address: NonEmptyStr
    reason: NonEmptyStr


class NotFoundDevice(BaseModel):
    """A previously-registered device that the scan was configured to expect
    but did not appear in ``live_results``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    protocol: Literal["modbus_tcp", "ocpp_1_6", "dsmr_p1"]
    address: NonEmptyStr


class DiscoveryScanReport(BaseModel):
    """The typed result of one ``DeviceDiscoveryOrchestrator.run_scan``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scan_id: uuid.UUID
    started_at: datetime
    completed_at: datetime
    live_results: tuple[LiveDiscoveryRow, ...]
    unreachable_targets: tuple[UnreachableTarget, ...]
    not_found_devices: tuple[NotFoundDevice, ...]

    @field_validator("started_at", "completed_at")
    @classmethod
    def _timestamps_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware UTC")
        return value


class DeviceDiscoveryOrchestrator:
    """Concurrent multi-protocol scan orchestrator.

    Stateless across calls: it carries no per-scan state on the instance, so
    it is safe to construct once at startup and share across all installer
    requests. Concurrency for a single scan is handled by ``asyncio.gather``
    inside ``run_scan``.
    """

    def __init__(
        self,
        *,
        discovery_service: DiscoveryService,
        ocpp_central_system: OCPPCentralSystem | None = None,
        registry_provider: DeviceRegistryProvider | None = None,
    ) -> None:
        self._discovery_service = discovery_service
        self._ocpp_central_system = ocpp_central_system
        self._registry_provider = registry_provider

    async def run_scan(self, scan_targets: ScanTargets) -> DiscoveryScanReport:
        """Run all probes concurrently; return a typed report.

        ``CancelledError`` is propagated cooperatively (R4 contract): every
        per-target task is cancelled, each adapter's ``close()`` / ``stop()``
        runs in its ``finally`` block, and no rows are written to
        ``device_registry`` (this orchestrator never writes; the manual-entry
        path is the sole writer).
        """
        scan_id = uuid.uuid4()
        started_at = datetime.now(UTC)
        logger.info(
            "scan_started",
            component="discovery",
            scan_id=str(scan_id),
            modbus_target_count=len(scan_targets.modbus_targets),
            dsmr_target_count=len(scan_targets.dsmr_targets),
            include_ocpp=scan_targets.include_ocpp_self_registered,
            expected_device_id_count=len(scan_targets.expected_device_ids),
        )

        try:
            tasks: list[asyncio.Task[_ProbeOutcome]] = []
            for mb in scan_targets.modbus_targets:
                tasks.append(asyncio.create_task(self._probe_modbus(mb, scan_id)))
            for ds in scan_targets.dsmr_targets:
                tasks.append(asyncio.create_task(self._probe_dsmr(ds, scan_id)))

            outcomes: list[_ProbeOutcome] = []
            if tasks:
                # return_exceptions=False per AC2: each task individually wraps
                # its own DeviceProbeError into an UnreachableTarget outcome, so
                # gather never raises a ProbeError. CancelledError is allowed to
                # propagate so client-disconnect mid-scan cooperatively cancels
                # every in-flight probe task (R4 contract).
                outcomes = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            # R4 audit-owner: scan cancellation is silent on the event_log surface
            # (this is a configuration flow, not a control decision) but emits a
            # structlog event so operators can correlate aborted scans with the
            # client-disconnect window.
            logger.info(
                "scan_cancelled",
                component="discovery",
                scan_id=str(scan_id),
            )
            raise

        live_results: list[LiveDiscoveryRow] = []
        unreachable: list[UnreachableTarget] = []
        for outcome in outcomes:
            if outcome.kind == "live":
                assert outcome.row is not None
                live_results.append(outcome.row)
            else:
                assert outcome.unreachable is not None
                unreachable.append(outcome.unreachable)

        if scan_targets.include_ocpp_self_registered and self._ocpp_central_system is not None:
            chargers = self._ocpp_central_system.list_registered_chargers()
            for charger in chargers:
                live_results.append(_ocpp_to_live_row(charger))

        not_found = await self._classify_not_found(
            live_results=live_results,
            expected_device_ids=scan_targets.expected_device_ids,
        )

        completed_at = datetime.now(UTC)
        logger.info(
            "scan_completed",
            component="discovery",
            scan_id=str(scan_id),
            live_result_count=len(live_results),
            unreachable_count=len(unreachable),
            not_found_count=len(not_found),
        )
        return DiscoveryScanReport(
            scan_id=scan_id,
            started_at=started_at,
            completed_at=completed_at,
            live_results=tuple(live_results),
            unreachable_targets=tuple(unreachable),
            not_found_devices=tuple(not_found),
        )

    async def _probe_modbus(self, target: ModbusScanTarget, scan_id: uuid.UUID) -> _ProbeOutcome:
        address = f"{target.host}:{target.port}"
        try:
            result = await self._discovery_service.probe_modbus_endpoint(
                device_id=target.device_id,
                host=target.host,
                port=target.port,
                model=target.model,
            )
        except DeviceProbeError as exc:
            return _ProbeOutcome(
                kind="unreachable",
                unreachable=UnreachableTarget(
                    device_id=target.device_id,
                    protocol="modbus_tcp",
                    address=address,
                    reason=f"modbus_{type(exc).__name__}: {exc}",
                ),
            )
        profile = get_profile(target.device_id, target.model or "")
        return _ProbeOutcome(
            kind="live",
            row=LiveDiscoveryRow(
                result=result,
                profile=profile,
                badge=_classify_badge(profile.capability_status),
            ),
        )

    async def _probe_dsmr(self, target: DSMRScanTarget, scan_id: uuid.UUID) -> _ProbeOutcome:
        address = target.serial_port or f"{target.tcp_host}:{target.tcp_port}"
        try:
            result = await self._discovery_service.probe_dsmr_endpoint(
                device_id=target.device_id,
                serial_port=target.serial_port,
                tcp_host=target.tcp_host,
                tcp_port=target.tcp_port,
                dsmr_version=target.dsmr_version,
            )
        except DeviceProbeError as exc:
            return _ProbeOutcome(
                kind="unreachable",
                unreachable=UnreachableTarget(
                    device_id=target.device_id,
                    protocol="dsmr_p1",
                    address=address,
                    reason=f"dsmr_{type(exc).__name__}: {exc}",
                ),
            )
        # DSMR resolves a fixed model identifier — get_profile() returns the
        # registry profile for dsmr_p1 (FULL) or REDUCED for unknown firmware.
        profile = get_profile(target.device_id, result.model or "")
        return _ProbeOutcome(
            kind="live",
            row=LiveDiscoveryRow(
                result=result,
                profile=profile,
                badge=_classify_badge(profile.capability_status),
            ),
        )

    async def _classify_not_found(
        self,
        *,
        live_results: list[LiveDiscoveryRow],
        expected_device_ids: frozenset[str],
    ) -> list[NotFoundDevice]:
        if not expected_device_ids or self._registry_provider is None:
            return []
        seen: set[str] = {row.result.device_id for row in live_results}
        registry = await self._registry_provider.list_all()
        not_found: list[NotFoundDevice] = []
        for entry in registry:
            if entry.device_id not in expected_device_ids:
                continue
            if entry.device_id in seen:
                continue
            not_found.append(
                NotFoundDevice(
                    device_id=entry.device_id,
                    protocol=entry.protocol,
                    address=entry.address,
                )
            )
        return not_found


@runtime_checkable
class DeviceRegistryProvider(Protocol):
    """Minimal read protocol used by the orchestrator for the AC4 registry diff.

    Structural — ``DeviceRepo.list_all()`` satisfies it without explicit
    inheritance. Decouples the orchestrator from the SQLite repo so it stays
    unit-testable with an in-memory fake.
    """

    async def list_all(self) -> Iterable[DeviceRegistryEntry]: ...


def _ocpp_to_live_row(charger: OCPPRegisteredCharger) -> LiveDiscoveryRow:
    profile = get_profile(charger.device_id, charger.model or "")
    result = DeviceDiscoveryResult(
        device_id=charger.device_id,
        protocol="ocpp_1_6",
        address=charger.address,
        model=charger.model,
        capability_status=profile.capability_status,
    )
    return LiveDiscoveryRow(
        result=result,
        profile=profile,
        badge=_classify_badge(profile.capability_status),
    )


class _ProbeOutcome(BaseModel):
    """Internal sum-type returned by per-target probe tasks."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    kind: Literal["live", "unreachable"]
    row: LiveDiscoveryRow | None = None
    unreachable: UnreachableTarget | None = None
