"""``ProtocolAdapterFactory`` — Story 9.4 validation-only probe coordinator.

The factory is invoked **only** by ``DeploymentValidationService``'s
connectivity + control-readiness checks. It opens a transient adapter,
runs a read-only protocol-layer probe (the same primitives Story 9.1's
``DiscoveryService`` uses for the wizard's Step 1 scan), resolves the
device's capability profile from the registry, and tears the adapter down.

**Validation-only:** the factory is NOT part of the runtime control surface.
Probes go through ``DiscoveryService`` read-only primitives; the factory
does not issue ``send_command``. The runtime control surface
(``PolicyGuard.adapters`` and ``ControlLoop.adapters``) is wired separately
by ``runtime_adapter_wiring.build_runtime_adapter_map`` (Story 9.X — closes
the Story 8-2 deferred finding at ``[app.py:223-236]``).

**Safe-probe invariant (user guardrail #2):** the probe path goes through
``DiscoveryService.probe_modbus_endpoint`` / ``probe_dsmr_endpoint`` — both
of which are read-only by construction (single ``read_holding_registers``
for Modbus; ``wait_for_telegram`` for DSMR). No ``send_command`` call is
issued anywhere in this module. For OCPP, the "probe" is a synchronous
check of the central system's registered-chargers list (the charger
initiates the connection; we cannot probe a charger that has not
self-announced).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

from open_ems.adapters.address_parsing import parse_dsmr_address, parse_host_port
from open_ems.adapters.capabilities import get_profile
from open_ems.adapters.discovery import DeviceProbeError, DiscoveryService
from open_ems.adapters.ocpp.central_system import OCPPCentralSystem
from open_ems.core.devices import (
    CapabilityStatus,
    DeviceCapabilityProfile,
    DeviceRole,
    WriteCapability,
)
from open_ems.storage.repositories.device_repo import DeviceRegistryEntry

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ProbeOutcome:
    """Typed result of one device's safe readiness probe.

    Consumed by both the connectivity check (cares about ``reachable`` +
    ``capability_status``) and the control-readiness check (cares about
    ``write_capabilities`` for the controllable roles).

    ``error_reason`` is populated when ``reachable=False`` so the per-check
    summary text can include the device-level cause.
    """

    device_id: str
    role: DeviceRole | None
    protocol: str  # "modbus_tcp" | "ocpp_1_6" | "dsmr_p1"
    reachable: bool
    timed_out: bool
    capability_status: CapabilityStatus | None
    write_capabilities: frozenset[WriteCapability]
    error_reason: str | None
    capability_profile: DeviceCapabilityProfile | None


class ProtocolAdapterFactory:
    """Construct, probe, and tear down per-device transient adapters.

    The factory is a service-layer dependency consumed by
    ``DeploymentValidationService``. Production wiring (in ``web/app.py``
    lifespan step 4g) injects the real ``DiscoveryService`` and the
    application's ``OCPPCentralSystem`` (or ``None`` if OCPP wiring is
    not yet in place — same fallback the 9.1 discovery orchestrator uses).
    Test fixtures inject fakes that satisfy the same surface.
    """

    def __init__(
        self,
        *,
        discovery: DiscoveryService | None = None,
        ocpp_central_system: OCPPCentralSystem | None = None,
    ) -> None:
        self._discovery = discovery if discovery is not None else DiscoveryService()
        self._ocpp_central_system = ocpp_central_system

    async def probe(
        self,
        entry: DeviceRegistryEntry,
        *,
        timeout_s: float,
    ) -> ProbeOutcome:
        """Run one safe probe for ``entry``.

        Returns a ``ProbeOutcome`` describing protocol-layer reachability and
        the resolved capability profile. NEVER issues ``send_command``.

        The underlying ``DiscoveryService.probe_*`` methods own their own
        transient connection (open → probe → close inside one async call), so
        cancellation safety reduces to the per-probe ``asyncio.wait_for``
        guarding the network call itself. There is no long-lived adapter
        handle kept by this factory to shield around — review finding P2 was
        previously overstated in this docstring; corrected to match reality.

        ``timeout_s`` is the upper bound for the network probe itself; if the
        probe exceeds it, ``timed_out=True`` is set and ``reachable=False``.
        """
        if entry.protocol == "modbus_tcp":
            return await self._probe_modbus(entry, timeout_s=timeout_s)
        if entry.protocol == "dsmr_p1":
            return await self._probe_dsmr(entry, timeout_s=timeout_s)
        if entry.protocol == "ocpp_1_6":
            return self._probe_ocpp(entry)
        return ProbeOutcome(
            device_id=entry.device_id,
            role=entry.role,
            protocol=entry.protocol,
            reachable=False,
            timed_out=False,
            capability_status=None,
            write_capabilities=frozenset(),
            error_reason=f"unknown_protocol:{entry.protocol}",
            capability_profile=None,
        )

    async def _probe_modbus(
        self,
        entry: DeviceRegistryEntry,
        *,
        timeout_s: float,
    ) -> ProbeOutcome:
        host, port = parse_host_port(entry.address)
        if host is None or port is None:
            return _outcome_unreachable(entry, "modbus_address_unparsable")
        try:
            await asyncio.wait_for(
                self._discovery.probe_modbus_endpoint(
                    device_id=entry.device_id,
                    host=host,
                    port=port,
                    model=entry.model,
                ),
                timeout=timeout_s,
            )
        except TimeoutError:
            return ProbeOutcome(
                device_id=entry.device_id,
                role=entry.role,
                protocol="modbus_tcp",
                reachable=False,
                timed_out=True,
                capability_status=None,
                write_capabilities=frozenset(),
                error_reason="probe_timeout",
                capability_profile=None,
            )
        except DeviceProbeError as exc:
            return _outcome_unreachable(entry, str(exc))
        # Probe succeeded → resolve capabilities from the registry.
        return _outcome_reachable(entry)

    async def _probe_dsmr(
        self,
        entry: DeviceRegistryEntry,
        *,
        timeout_s: float,
    ) -> ProbeOutcome:
        serial_port, tcp_host, tcp_port = parse_dsmr_address(entry.address)
        if serial_port is None and tcp_host is None:
            return _outcome_unreachable(entry, "dsmr_address_unparsable")
        try:
            await asyncio.wait_for(
                self._discovery.probe_dsmr_endpoint(
                    device_id=entry.device_id,
                    serial_port=serial_port,
                    tcp_host=tcp_host,
                    tcp_port=tcp_port,
                ),
                timeout=timeout_s,
            )
        except TimeoutError:
            return ProbeOutcome(
                device_id=entry.device_id,
                role=entry.role,
                protocol="dsmr_p1",
                reachable=False,
                timed_out=True,
                capability_status=None,
                write_capabilities=frozenset(),
                error_reason="probe_timeout",
                capability_profile=None,
            )
        except DeviceProbeError as exc:
            return _outcome_unreachable(entry, str(exc))
        return _outcome_reachable(entry)

    def _probe_ocpp(self, entry: DeviceRegistryEntry) -> ProbeOutcome:
        """OCPP "probe" — consult the central system's registered chargers.

        The OCPP central system is the only authoritative source for charger
        reachability (the charger initiates the WebSocket; we cannot probe
        it from the server side). If no central system is wired (the Story
        9.1 deferred-finding state), every OCPP device probes as
        ``reachable=False, error_reason='ocpp_central_system_unavailable'``.

        P15 — wrap the registered-chargers read so a misbehaving central
        system (raising or returning malformed payload) does not abort the
        connectivity check and the entire run.
        """
        if self._ocpp_central_system is None:
            return _outcome_unreachable(entry, "ocpp_central_system_unavailable")
        try:
            chargers = self._ocpp_central_system.list_registered_chargers()
            registered_ids = {charger.device_id for charger in chargers}
        except Exception as exc:  # noqa: BLE001 — defensive boundary at adapter edge
            logger.warning(
                "ocpp_list_registered_chargers_failed",
                component="installer_setup",
                device_id=entry.device_id,
                error=repr(exc),
            )
            return _outcome_unreachable(entry, "ocpp_list_registered_chargers_failed")
        if entry.device_id not in registered_ids:
            return _outcome_unreachable(entry, "ocpp_charger_not_connected")
        return _outcome_reachable(entry)


def _outcome_unreachable(entry: DeviceRegistryEntry, reason: str) -> ProbeOutcome:
    return ProbeOutcome(
        device_id=entry.device_id,
        role=entry.role,
        protocol=entry.protocol,
        reachable=False,
        timed_out=False,
        capability_status=None,
        write_capabilities=frozenset(),
        error_reason=reason,
        capability_profile=None,
    )


def _outcome_reachable(entry: DeviceRegistryEntry) -> ProbeOutcome:
    """Build a reachable outcome with the resolved capability profile.

    The capability profile is the SAME registry lookup PolicyGuard's P2 gate
    uses at dispatch time. Story 9.4's control-readiness check inspects the
    returned ``write_capabilities`` to determine whether the device can
    actually be commanded for its assigned role.
    """
    model = entry.model
    if model is None:
        # No model recorded — capability is "reduced" by definition (the same
        # rule discovery uses).
        profile: DeviceCapabilityProfile | None = None
        capability_status: CapabilityStatus = CapabilityStatus.reduced
        write_caps: frozenset[WriteCapability] = frozenset()
    else:
        profile = get_profile(
            device_id=entry.device_id, model=model, firmware_version=entry.firmware_version
        )
        capability_status = profile.capability_status
        write_caps = profile.write_capabilities
    return ProbeOutcome(
        device_id=entry.device_id,
        role=entry.role,
        protocol=entry.protocol,
        reachable=True,
        timed_out=False,
        capability_status=capability_status,
        write_capabilities=write_caps,
        error_reason=None,
        capability_profile=profile,
    )


__all__ = [
    "ProbeOutcome",
    "ProtocolAdapterFactory",
]
