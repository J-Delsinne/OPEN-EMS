"""Unit tests for ``ProtocolAdapterFactory`` (Story 9.4 AC11 — P25).

Exercises the factory's production code path against stub DiscoveryService +
stub OCPPCentralSystem implementations. Asserts open → probe → close for each
protocol (modbus_tcp, dsmr_p1, ocpp_1_6) plus the error / unknown-protocol
branches the deployment validation connectivity check depends on.

Spec line 505: "exercise the factory's production code path against the
simulated adapter fixtures; assert open → probe → close cycle for each
protocol (modbus_tcp, ocpp_1_6, dsmr_p1)."
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from open_ems.adapters.discovery import DeviceProbeError
from open_ems.adapters.ocpp.central_system import OCPPRegisteredCharger
from open_ems.core.devices import CapabilityStatus, DeviceRole
from open_ems.services.protocol_adapter_factory import (
    ProtocolAdapterFactory,
    _parse_dsmr_address,
    _parse_host_port,
)
from open_ems.storage.repositories.device_repo import DeviceRegistryEntry

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubDiscoveryService:
    """Records each probe call + lets the test inject success / failure / hang."""

    def __init__(self) -> None:
        self.modbus_calls: list[dict[str, object]] = []
        self.dsmr_calls: list[dict[str, object]] = []
        self._modbus_error: BaseException | None = None
        self._dsmr_error: BaseException | None = None
        self._modbus_delay_s: float = 0.0
        self._dsmr_delay_s: float = 0.0

    def set_modbus_failure(self, exc: BaseException) -> None:
        self._modbus_error = exc

    def set_dsmr_failure(self, exc: BaseException) -> None:
        self._dsmr_error = exc

    def set_modbus_delay(self, seconds: float) -> None:
        self._modbus_delay_s = seconds

    def set_dsmr_delay(self, seconds: float) -> None:
        self._dsmr_delay_s = seconds

    async def probe_modbus_endpoint(
        self,
        *,
        device_id: str,
        host: str,
        port: int,
        model: str | None,
    ) -> None:
        self.modbus_calls.append(
            {"device_id": device_id, "host": host, "port": port, "model": model}
        )
        if self._modbus_delay_s > 0:
            await asyncio.sleep(self._modbus_delay_s)
        if self._modbus_error is not None:
            raise self._modbus_error

    async def probe_dsmr_endpoint(
        self,
        *,
        device_id: str,
        serial_port: str | None,
        tcp_host: str | None,
        tcp_port: int | None,
    ) -> None:
        self.dsmr_calls.append(
            {
                "device_id": device_id,
                "serial_port": serial_port,
                "tcp_host": tcp_host,
                "tcp_port": tcp_port,
            }
        )
        if self._dsmr_delay_s > 0:
            await asyncio.sleep(self._dsmr_delay_s)
        if self._dsmr_error is not None:
            raise self._dsmr_error


class _StubOCPPCentralSystem:
    def __init__(self, chargers: tuple[OCPPRegisteredCharger, ...] = ()) -> None:
        self._chargers = chargers
        self._raise: BaseException | None = None

    def set_raise(self, exc: BaseException) -> None:
        self._raise = exc

    def list_registered_chargers(self) -> tuple[OCPPRegisteredCharger, ...]:
        if self._raise is not None:
            raise self._raise
        return self._chargers


def _entry(
    *,
    device_id: str = "dev-1",
    protocol: str = "modbus_tcp",
    address: str = "192.168.1.10:502",
    model: str | None = "byd_hvs_v1",
    role: DeviceRole | None = DeviceRole.battery,
) -> DeviceRegistryEntry:
    return DeviceRegistryEntry(
        device_id=device_id,
        protocol=protocol,
        address=address,
        model=model,
        firmware_version=None,
        role=role,
        source="manual_entry",
        validated=True,
        last_capability_status="full",
        last_limitation_reason=None,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
        installer_acknowledged_unvalidated_at=None,
        role_assigned_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Modbus TCP — success / failure / timeout
# ---------------------------------------------------------------------------


async def test_modbus_probe_success_resolves_capability_profile() -> None:
    discovery = _StubDiscoveryService()
    factory = ProtocolAdapterFactory(discovery=discovery)  # type: ignore[arg-type]
    outcome = await factory.probe(
        _entry(device_id="batt-1", protocol="modbus_tcp", address="192.168.1.10:502"),
        timeout_s=1.0,
    )
    # Open → probe → close cycle: exactly one DiscoveryService call.
    assert len(discovery.modbus_calls) == 1
    assert discovery.modbus_calls[0]["host"] == "192.168.1.10"
    assert discovery.modbus_calls[0]["port"] == 502
    assert outcome.reachable is True
    assert outcome.timed_out is False
    assert outcome.protocol == "modbus_tcp"
    # Capability profile resolved from the registry-recorded model.
    assert outcome.capability_status is CapabilityStatus.full
    assert outcome.capability_profile is not None


async def test_modbus_probe_device_error_yields_unreachable() -> None:
    discovery = _StubDiscoveryService()
    discovery.set_modbus_failure(DeviceProbeError("connection refused"))
    factory = ProtocolAdapterFactory(discovery=discovery)  # type: ignore[arg-type]
    outcome = await factory.probe(
        _entry(protocol="modbus_tcp", address="10.0.0.1:502"),
        timeout_s=1.0,
    )
    assert outcome.reachable is False
    assert outcome.timed_out is False
    assert outcome.error_reason == "connection refused"


async def test_modbus_probe_timeout_yields_timed_out() -> None:
    discovery = _StubDiscoveryService()
    discovery.set_modbus_delay(2.0)
    factory = ProtocolAdapterFactory(discovery=discovery)  # type: ignore[arg-type]
    outcome = await factory.probe(
        _entry(protocol="modbus_tcp", address="10.0.0.1:502"),
        timeout_s=0.05,
    )
    assert outcome.reachable is False
    assert outcome.timed_out is True
    assert outcome.error_reason == "probe_timeout"


async def test_modbus_probe_rejects_unparsable_address() -> None:
    discovery = _StubDiscoveryService()
    factory = ProtocolAdapterFactory(discovery=discovery)  # type: ignore[arg-type]
    outcome = await factory.probe(
        _entry(protocol="modbus_tcp", address="not-a-valid-address"),
        timeout_s=1.0,
    )
    assert outcome.reachable is False
    assert outcome.timed_out is False
    assert outcome.error_reason == "modbus_address_unparsable"
    # Address rejected pre-probe → DiscoveryService never called.
    assert discovery.modbus_calls == []


# ---------------------------------------------------------------------------
# DSMR P1 — success / failure / timeout
# ---------------------------------------------------------------------------


async def test_dsmr_probe_success_serial_port() -> None:
    discovery = _StubDiscoveryService()
    factory = ProtocolAdapterFactory(discovery=discovery)  # type: ignore[arg-type]
    outcome = await factory.probe(
        _entry(
            device_id="meter-1",
            protocol="dsmr_p1",
            address="/dev/ttyUSB0",
            model="dsmr_p1",
            role=DeviceRole.grid_meter,
        ),
        timeout_s=1.0,
    )
    assert len(discovery.dsmr_calls) == 1
    assert discovery.dsmr_calls[0]["serial_port"] == "/dev/ttyUSB0"
    assert discovery.dsmr_calls[0]["tcp_host"] is None
    assert outcome.reachable is True
    assert outcome.protocol == "dsmr_p1"


async def test_dsmr_probe_success_tcp() -> None:
    discovery = _StubDiscoveryService()
    factory = ProtocolAdapterFactory(discovery=discovery)  # type: ignore[arg-type]
    outcome = await factory.probe(
        _entry(
            protocol="dsmr_p1",
            address="meter.local:2400",
            model="dsmr_p1",
            role=DeviceRole.grid_meter,
        ),
        timeout_s=1.0,
    )
    assert len(discovery.dsmr_calls) == 1
    assert discovery.dsmr_calls[0]["tcp_host"] == "meter.local"
    assert discovery.dsmr_calls[0]["tcp_port"] == 2400
    assert outcome.reachable is True


async def test_dsmr_probe_failure_yields_unreachable() -> None:
    discovery = _StubDiscoveryService()
    discovery.set_dsmr_failure(DeviceProbeError("no telegram"))
    factory = ProtocolAdapterFactory(discovery=discovery)  # type: ignore[arg-type]
    outcome = await factory.probe(
        _entry(
            protocol="dsmr_p1",
            address="/dev/ttyUSB0",
            model="dsmr_p1",
            role=DeviceRole.grid_meter,
        ),
        timeout_s=1.0,
    )
    assert outcome.reachable is False
    assert outcome.timed_out is False
    assert outcome.error_reason == "no telegram"


async def test_dsmr_probe_timeout_yields_timed_out() -> None:
    discovery = _StubDiscoveryService()
    discovery.set_dsmr_delay(2.0)
    factory = ProtocolAdapterFactory(discovery=discovery)  # type: ignore[arg-type]
    outcome = await factory.probe(
        _entry(
            protocol="dsmr_p1",
            address="/dev/ttyUSB0",
            model="dsmr_p1",
            role=DeviceRole.grid_meter,
        ),
        timeout_s=0.05,
    )
    assert outcome.reachable is False
    assert outcome.timed_out is True
    assert outcome.error_reason == "probe_timeout"


# ---------------------------------------------------------------------------
# OCPP 1.6 — central-system delegation, including P15 exception path
# ---------------------------------------------------------------------------


async def test_ocpp_probe_reachable_when_charger_registered() -> None:
    central = _StubOCPPCentralSystem(
        chargers=(
            OCPPRegisteredCharger(
                device_id="ev-1",
                charge_point_id="cp-1",
                address="/ocpp/cp-1",
                model="alfen_eve_v2",
            ),
        )
    )
    factory = ProtocolAdapterFactory(ocpp_central_system=central)  # type: ignore[arg-type]
    outcome = await factory.probe(
        _entry(
            device_id="ev-1",
            protocol="ocpp_1_6",
            address="/ocpp/cp-1",
            model="alfen_eve_v2",
            role=DeviceRole.ev_charger,
        ),
        timeout_s=1.0,
    )
    assert outcome.reachable is True
    assert outcome.protocol == "ocpp_1_6"


async def test_ocpp_probe_unreachable_when_charger_not_registered() -> None:
    central = _StubOCPPCentralSystem(chargers=())
    factory = ProtocolAdapterFactory(ocpp_central_system=central)  # type: ignore[arg-type]
    outcome = await factory.probe(
        _entry(
            device_id="ev-1",
            protocol="ocpp_1_6",
            address="/ocpp/cp-1",
            model="alfen_eve_v2",
            role=DeviceRole.ev_charger,
        ),
        timeout_s=1.0,
    )
    assert outcome.reachable is False
    assert outcome.error_reason == "ocpp_charger_not_connected"


async def test_ocpp_probe_unreachable_when_no_central_system() -> None:
    factory = ProtocolAdapterFactory(ocpp_central_system=None)
    outcome = await factory.probe(
        _entry(
            device_id="ev-1",
            protocol="ocpp_1_6",
            address="/ocpp/cp-1",
            model="alfen_eve_v2",
            role=DeviceRole.ev_charger,
        ),
        timeout_s=1.0,
    )
    assert outcome.reachable is False
    assert outcome.error_reason == "ocpp_central_system_unavailable"


async def test_ocpp_probe_central_system_raise_is_caught_per_p15() -> None:
    """P15 — list_registered_chargers() raising must NOT abort the run; the
    probe surfaces a typed unreachable outcome with the documented reason.
    """
    central = _StubOCPPCentralSystem()
    central.set_raise(RuntimeError("ocpp internal"))
    factory = ProtocolAdapterFactory(ocpp_central_system=central)  # type: ignore[arg-type]
    outcome = await factory.probe(
        _entry(
            device_id="ev-1",
            protocol="ocpp_1_6",
            address="/ocpp/cp-1",
            model="alfen_eve_v2",
            role=DeviceRole.ev_charger,
        ),
        timeout_s=1.0,
    )
    assert outcome.reachable is False
    assert outcome.error_reason == "ocpp_list_registered_chargers_failed"


# ---------------------------------------------------------------------------
# Unknown protocol + reduced-capability fallthrough
# ---------------------------------------------------------------------------


async def test_unknown_protocol_yields_typed_unreachable() -> None:
    """The Pydantic ``Protocol`` Literal rejects unknown protocols at the
    boundary, so this branch is only reachable via ``model_construct`` (which
    skips validation). Kept as a structural assertion that future protocol-
    enum additions can't silently bypass the factory's dispatch table.
    """
    # ``model_construct`` skips Pydantic validation so we can drive the
    # defensive fallthrough branch.
    entry = DeviceRegistryEntry.model_construct(
        device_id="dev-1",
        protocol="zigbee",  # type: ignore[arg-type]
        address="ignored",
        model=None,
        firmware_version=None,
        source="manual_entry",
        validated=True,
        last_capability_status=None,
        last_limitation_reason=None,
        first_seen_at=_NOW,
        last_seen_at=None,
        installer_acknowledged_unvalidated_at=None,
        role=None,
        role_assigned_at=None,
    )
    factory = ProtocolAdapterFactory()
    outcome = await factory.probe(entry, timeout_s=1.0)
    assert outcome.reachable is False
    assert outcome.timed_out is False
    assert outcome.error_reason == "unknown_protocol:zigbee"


async def test_reachable_with_no_model_resolves_reduced_capability() -> None:
    """``_outcome_reachable`` downgrades to ``CapabilityStatus.reduced`` when
    ``entry.model`` is None. The capability-gate contract treats a no-model
    device as REDUCED — the deployment validation capability_strategy check
    relies on this signal to surface WARN gaps.
    """
    discovery = _StubDiscoveryService()
    factory = ProtocolAdapterFactory(discovery=discovery)  # type: ignore[arg-type]
    outcome = await factory.probe(
        _entry(model=None),
        timeout_s=1.0,
    )
    assert outcome.reachable is True
    assert outcome.capability_status is CapabilityStatus.reduced
    assert outcome.write_capabilities == frozenset()
    assert outcome.capability_profile is None


# ---------------------------------------------------------------------------
# Address parsing — P14 invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "expected_host", "expected_port"),
    [
        ("192.168.1.10:502", "192.168.1.10", 502),
        ("[::1]:502", "::1", 502),
        ("[2001:db8::1]:1502", "2001:db8::1", 1502),
        ("meter.local:502", "meter.local", 502),
    ],
)
def test_parse_host_port_accepts_valid(
    address: str, expected_host: str, expected_port: int
) -> None:
    host, port = _parse_host_port(address)
    assert host == expected_host
    assert port == expected_port


@pytest.mark.parametrize(
    "address",
    [
        "",
        "no-port",
        "host:0",
        "host:65536",
        "host:-1",
        "host:not-a-number",
        "[::1]",
        "[::1]:",
        "[unterminated:502",
    ],
)
def test_parse_host_port_rejects_invalid(address: str) -> None:
    host, port = _parse_host_port(address)
    assert host is None
    assert port is None


def test_parse_dsmr_address_serial() -> None:
    serial, host, port = _parse_dsmr_address("/dev/ttyUSB0")
    assert serial == "/dev/ttyUSB0"
    assert host is None
    assert port is None


def test_parse_dsmr_address_tcp() -> None:
    serial, host, port = _parse_dsmr_address("meter.local:2400")
    assert serial is None
    assert host == "meter.local"
    assert port == 2400


@pytest.mark.parametrize(
    "address",
    ["", "garbage", "host:0", "host:99999"],
)
def test_parse_dsmr_address_rejects_invalid(address: str) -> None:
    serial, host, port = _parse_dsmr_address(address)
    assert serial is None
    assert host is None
    assert port is None
