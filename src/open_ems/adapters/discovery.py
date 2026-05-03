"""Device discovery service: probe endpoints and register self-announcing devices.

``DeviceProbeError`` — raised when a probe attempt fails (device unreachable or
not responding with expected data within the timeout).

``DiscoveryService`` — coordinates endpoint probing across protocols:

    - ``probe_modbus_endpoint()``      : Modbus TCP — opens a single-register read
    - ``probe_dsmr_endpoint()``        : DSMR P1   — starts the adapter and waits
                                         for the first telegram
    - ``register_ocpp_discovery()``    : OCPP       — synchronous; used when a
                                         charger self-registers via BootNotification

All successful probes emit ``event="device_discovered"`` via structlog.
All failed probes emit ``event="device_not_found"`` via structlog.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from open_ems.adapters.capabilities import get_profile
from open_ems.adapters.dsmr.p1 import DSMRAdapter, DSMRAdapterConfig
from open_ems.adapters.modbus.tcp import (
    ModbusRegisterRange,
    ModbusTcpAdapter,
    ModbusTcpAdapterConfig,
)
from open_ems.adapters.protocol import ProtocolDegradedState, RawDSMRState
from open_ems.core.devices import CapabilityStatus, DeviceDiscoveryResult

logger = structlog.get_logger(__name__)

_PROBE_TIMEOUT_S: float = 10.0
_DSMR_PROBE_TIMEOUT_S: float = 30.0

# Minimal probe register — holding register 0, count 1
_PROBE_REGISTER = ModbusRegisterRange(table="holding", start_address=0, count=1)


class DeviceProbeError(Exception):
    """Raised when a discovery probe fails to reach or identify the device."""


class DiscoveryService:
    """Coordinates device discovery probes across Modbus TCP, DSMR P1, and OCPP.

    ``modbus_client_factory`` and ``dsmr_transport_factory`` are injected for
    testability; pass ``None`` (the default) to use the real transport.
    """

    def __init__(
        self,
        modbus_client_factory: Any | None = None,
        dsmr_transport_factory: Any | None = None,
    ) -> None:
        self._modbus_client_factory = modbus_client_factory
        self._dsmr_transport_factory = dsmr_transport_factory

    async def probe_modbus_endpoint(
        self,
        *,
        device_id: str,
        host: str,
        port: int = 502,
        model: str | None = None,
    ) -> DeviceDiscoveryResult:
        """Probe a Modbus TCP endpoint by issuing a single holding-register read.

        Raises ``DeviceProbeError`` if the device is unreachable or returns a
        degraded protocol state.
        """
        config = ModbusTcpAdapterConfig(
            device_id=device_id,
            host=host,
            port=port,
            registers=((_PROBE_REGISTER),),
        )
        kwargs: dict[str, Any] = {}
        if self._modbus_client_factory is not None:
            kwargs["client_factory"] = self._modbus_client_factory

        adapter = ModbusTcpAdapter(config, **kwargs)
        try:
            raw = await asyncio.wait_for(adapter.get_raw_state(), timeout=_PROBE_TIMEOUT_S)
        except TimeoutError as exc:
            logger.warning(
                "device_not_found",
                component="discovery",
                device_id=device_id,
                protocol="modbus_tcp",
                address=f"{host}:{port}",
            )
            raise DeviceProbeError(
                f"Modbus probe timeout for {device_id} at {host}:{port}"
            ) from exc
        finally:
            await adapter.close()

        if isinstance(raw, ProtocolDegradedState):
            logger.warning(
                "device_not_found",
                component="discovery",
                device_id=device_id,
                protocol="modbus_tcp",
                address=f"{host}:{port}",
                reason=raw.reason,
            )
            raise DeviceProbeError(
                f"Modbus probe degraded for {device_id} at {host}:{port}: {raw.reason}"
            )

        capability_status = _resolve_capability_status(model)
        result = DeviceDiscoveryResult(
            device_id=device_id,
            protocol="modbus_tcp",
            address=f"{host}:{port}",
            model=model,
            capability_status=capability_status,
        )
        logger.info(
            "device_discovered",
            component="discovery",
            device_id=device_id,
            protocol="modbus_tcp",
            address=f"{host}:{port}",
            model=model,
        )
        return result

    async def probe_dsmr_endpoint(
        self,
        *,
        device_id: str,
        serial_port: str | None = None,
        tcp_host: str | None = None,
        tcp_port: int | None = None,
        dsmr_version: str = "5",
    ) -> DeviceDiscoveryResult:
        """Probe a DSMR P1 endpoint by waiting for at least one telegram.

        Raises ``DeviceProbeError`` if no telegram arrives within the timeout.
        """
        if serial_port is None and tcp_host is None:
            raise DeviceProbeError(
                f"DSMR probe for {device_id} requires serial_port or tcp_host/tcp_port"
            )
        address = serial_port or f"{tcp_host}:{tcp_port}"
        config = DSMRAdapterConfig(
            device_id=device_id,
            serial_port=serial_port,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            dsmr_version=dsmr_version,  # type: ignore[arg-type]
        )
        kwargs: dict[str, Any] = {}
        if self._dsmr_transport_factory is not None:
            kwargs["transport_factory"] = self._dsmr_transport_factory

        adapter = DSMRAdapter(config, **kwargs)
        try:
            await adapter.start()
            raw = await self._wait_for_dsmr_telegram(adapter)
        except DeviceProbeError:
            logger.warning(
                "device_not_found",
                component="discovery",
                device_id=device_id,
                protocol="dsmr_p1",
                address=address,
            )
            raise
        except Exception as exc:  # noqa: BLE001 — all transport failures become DeviceProbeError
            logger.warning(
                "device_not_found",
                component="discovery",
                device_id=device_id,
                protocol="dsmr_p1",
                address=address,
            )
            raise DeviceProbeError(
                f"DSMR probe failed for {device_id} at {address}: {exc}"
            ) from exc
        finally:
            await adapter.stop()

        _ = raw  # telegram received — presence alone confirms the device
        capability_status = _resolve_capability_status("dsmr_p1")
        result = DeviceDiscoveryResult(
            device_id=device_id,
            protocol="dsmr_p1",
            address=address,
            model="dsmr_p1",
            capability_status=capability_status,
        )
        logger.info(
            "device_discovered",
            component="discovery",
            device_id=device_id,
            protocol="dsmr_p1",
            address=address,
        )
        return result

    async def _wait_for_dsmr_telegram(self, adapter: DSMRAdapter) -> RawDSMRState:
        """Poll get_raw_state() until a RawDSMRState arrives or timeout expires."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _DSMR_PROBE_TIMEOUT_S
        while True:
            try:
                raw = await adapter.get_raw_state()
            except Exception as exc:  # noqa: BLE001 — transport errors become DeviceProbeError
                raise DeviceProbeError(f"DSMR probe: error reading state: {exc}") from exc
            if isinstance(raw, RawDSMRState):
                return raw
            if loop.time() >= deadline:
                raise DeviceProbeError("DSMR probe: no telegram received within timeout")
            await asyncio.sleep(0.1)

    def register_ocpp_discovery(
        self,
        *,
        device_id: str,
        address: str,
        model: str | None = None,
    ) -> DeviceDiscoveryResult:
        """Register an OCPP charger that self-announced via BootNotification.

        This is a synchronous call — the charger has already connected, so no
        network probe is needed.
        """
        capability_status = _resolve_capability_status(model)
        result = DeviceDiscoveryResult(
            device_id=device_id,
            protocol="ocpp_1_6",
            address=address,
            model=model,
            capability_status=capability_status,
        )
        logger.info(
            "device_discovered",
            component="discovery",
            device_id=device_id,
            protocol="ocpp_1_6",
            address=address,
            model=model,
        )
        return result


def _resolve_capability_status(model: str | None) -> CapabilityStatus:
    """Return capability status from registry if model is known, else reduced."""
    if model is None:
        return CapabilityStatus.reduced
    profile = get_profile("_discovery_probe", model)
    return profile.capability_status
