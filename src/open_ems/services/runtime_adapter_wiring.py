"""Runtime adapter wiring — builds PolicyGuard / ControlLoop adapter maps from device_registry.

Story 9.X — the canonical resolution of Story 8-2's deferred finding
``[app.py:223-236]`` ("``adapters={}`` in production silently rejects all
commands"). Before this story, ``PolicyGuard`` and ``ControlLoop`` were
constructed with empty adapter mappings; every command rejected at P1
(``adapter_not_registered``). The runtime adapter map shipped by this story
is the materialization of the installer-wizard-acknowledged ``device_registry``
into live adapter instances.

The single public function is ``build_runtime_adapter_map(...)``. The returned
``RuntimeAdapterMap`` is a frozen triple:

* ``policy_guard_adapters`` — passed to ``PolicyGuard(adapters=...)``.
  Excludes ``DeviceRole.grid_meter`` (DSMR P1 is read-only) and
  ``DeviceRole.inverter`` (v1 inverters have empty ``write_capabilities``).
* ``control_loop_adapters`` — passed to ``ControlLoop(adapters=...)``.
  Includes every constructed adapter (including grid_meter for telemetry).
* ``protocols_by_role`` — the originating ``device_registry.protocol`` value
  for each role in ``control_loop_adapters``. Carried for AC8 structured log
  emission at lifespan step 4h.

For roles present in both maps, the adapter instance is ``is``-identical
(AC11 of Story 9.X).

OCPP design (AC4 spec-mandated proxy)
=====================================

OCPP chargers initiate the WebSocket connection (per ``OCPPCentralSystem``
docstring). At lifespan startup, a charger may not yet have sent
``BootNotification``. AC4 mandates a ``_DeferredOCPPAdapter`` proxy that:

* exposes the AC4-mandated reason strings on the three contract surfaces
  pre-boot (``ocpp_charger_not_connected`` for ``get_state``; raises
  ``RuntimeError("ocpp_charger_not_connected")`` from ``get_capabilities``;
  returns a failed ``CommandResult`` with reason ``ocpp_charger_not_connected``
  from ``send_command``);
* delegates verbatim to the wrapped ``EVChargerAdapter`` once the charger
  is connected.

Eager ``OCPPCentralSystem.register(config)`` runs at wiring time (no other
code path calls ``register``), establishing the raw ``OCPPChargerAdapter``
identity that survives WebSocket reconnects. The proxy wraps the
``EVChargerAdapter`` (domain) around that raw adapter and re-checks
connection state on every call. The Story 8-2 ``Mapping`` immutability
invariant is preserved — the proxy resolves the connection state freshly
on each call without mutating PolicyGuard's adapter map.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from open_ems.adapters.address_parsing import parse_dsmr_address, parse_host_port
from open_ems.adapters.dsmr.meter_adapter import GridMeterAdapter
from open_ems.adapters.dsmr.p1 import DSMRAdapter, DSMRAdapterConfig
from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.modbus.inverter_adapter import InverterAdapter
from open_ems.adapters.modbus.tcp import (
    ModbusRegisterRange,
    ModbusTcpAdapter,
    ModbusTcpAdapterConfig,
)
from open_ems.adapters.ocpp.central_system import (
    OCPPAdapterConfig,
    OCPPCentralSystem,
    OCPPChargerAdapter,
)
from open_ems.adapters.ocpp.charger_adapter import EVChargerAdapter
from open_ems.core import DeviceAdapter, DeviceRole
from open_ems.core.commands import CommandResult, CommandStatus, DeviceCommand
from open_ems.core.devices import (
    DegradedDeviceState,
    DeviceCapabilityProfile,
    DeviceState,
)
from open_ems.settings import Settings
from open_ems.storage.repositories.device_repo import DeviceRegistryEntry, DeviceRepo

logger = structlog.get_logger(__name__)

# OCPP 1.6 chargePointId allowed character set. Matches the python-ocpp library's
# permissive surface while rejecting whitespace, slashes, and other characters
# that would break WebSocket URL routing.
_OCPP_CHARGE_POINT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class RuntimeAdapterWiringError(RuntimeError):
    """Structural failure during ``build_runtime_adapter_map``.

    Distinct exception type so the lifespan handler (Story 9.X AC9) can
    pattern-match and emit a structured ``startup_failed`` log with
    ``reason="runtime_adapter_wiring_failed"`` before ``SystemExit(1)``.
    """


@dataclass(frozen=True)
class RuntimeAdapterMap:
    """Frozen triple of adapter mappings + originating protocols.

    ``policy_guard_adapters`` is a strict subset of ``control_loop_adapters``:
    grid_meter and inverter slots appear only in the control-loop map.
    ``protocols_by_role`` carries the originating ``device_registry.protocol``
    string for every role in ``control_loop_adapters`` so AC8's structured
    log emission can include the ``(role, protocol, device_id)`` triple
    without re-querying the device repo.
    """

    policy_guard_adapters: Mapping[DeviceRole, DeviceAdapter]
    control_loop_adapters: Mapping[DeviceRole, DeviceAdapter]
    protocols_by_role: Mapping[DeviceRole, str]


# Modbus register ranges per known model. The capability registry alignment
# check at lifespan step 4a fails the process on unknown models, so any
# (validated, role-assigned) modbus entry reaching this module is guaranteed
# to have a key here. Values are PLACEHOLDER per the register_maps modules'
# own PLACEHOLDER markers; verify against actual device documentation when
# real hardware is commissioned.
_MODBUS_REGISTER_RANGES_BY_MODEL: dict[str, tuple[ModbusRegisterRange, ...]] = {
    # byd_hvs_v1: registers 100-111
    "byd_hvs_v1": (ModbusRegisterRange(table="holding", start_address=100, count=20),),
    # byd_hvm_v1: registers 200-211
    "byd_hvm_v1": (ModbusRegisterRange(table="holding", start_address=200, count=20),),
    # fronius_gen24_v1: registers 40083-40110
    "fronius_gen24_v1": (ModbusRegisterRange(table="holding", start_address=40083, count=28),),
    # huawei_sun2000_v3: registers 32064-32090
    "huawei_sun2000_v3": (ModbusRegisterRange(table="holding", start_address=32064, count=27),),
    # growatt_hybrid_v1: registers 3001-3004
    "growatt_hybrid_v1": (ModbusRegisterRange(table="holding", start_address=3001, count=10),),
}


def _is_writable_role(role: DeviceRole) -> bool:
    """Whether a role belongs in ``policy_guard_adapters``.

    Excluded: ``grid_meter`` (DSMR P1 read-only — cross-adapter contract
    clause 1) and ``inverter`` (v1 inverters have empty
    ``write_capabilities``; including them is dead weight that would
    surface as P4 ``capability_missing`` if a future command type were
    introduced without a registry update — fail-loud absence is better).
    """
    return role not in (DeviceRole.grid_meter, DeviceRole.inverter)


class _DeferredOCPPAdapter:
    """AC4 spec-mandated proxy: lazy resolution of OCPP connection state.

    Wraps an ``EVChargerAdapter`` so that the pre-boot window (and any
    transient disconnect) surfaces via the AC4-mandated reason strings
    rather than the underlying adapter's ``reconnecting`` mapping.

    Eager ``OCPPCentralSystem.register()`` at wiring time establishes the
    raw ``OCPPChargerAdapter`` identity that survives WebSocket reconnects;
    this proxy then re-checks the connection state on every call and either
    short-circuits with the AC4 contract surface or delegates verbatim to
    the wrapped ``EVChargerAdapter``.

    Story 8-2's ``Mapping`` immutability invariant is preserved: the proxy
    object is captured by reference into the adapter map at construction
    time and is never replaced; only its internal resolution behavior
    changes as the underlying OCPP connection comes and goes.
    """

    def __init__(
        self,
        device_id: str,
        ocpp_charger: OCPPChargerAdapter,
        ev_charger: EVChargerAdapter,
    ) -> None:
        self.device_id = device_id
        self._ocpp_charger = ocpp_charger
        self._ev_charger = ev_charger

    async def connect(self) -> None:
        await self._ev_charger.connect()

    async def disconnect(self) -> None:
        await self._ev_charger.disconnect()

    def _is_connected(self) -> bool:
        # Peek the raw adapter's connection state. The alternative —
        # awaiting get_raw_state() — issues a second protocol call per
        # invocation and complicates correlation; the connected flag is the
        # source of truth that get_raw_state itself reads.
        return self._ocpp_charger._state.connected  # noqa: SLF001

    async def get_state(self) -> DeviceState | DegradedDeviceState:
        if not self._is_connected():
            return DegradedDeviceState(
                device_id=self.device_id,
                role=DeviceRole.ev_charger,
                reason="ocpp_charger_not_connected",
                occurred_at=datetime.now(UTC),
            )
        return await self._ev_charger.get_state()

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        if not self._is_connected():
            raise RuntimeError("ocpp_charger_not_connected")
        return await self._ev_charger.get_capabilities()

    async def send_command(self, cmd: DeviceCommand) -> CommandResult:
        if not self._is_connected():
            return CommandResult(
                correlation_id=cmd.correlation_id,
                device_id=cmd.device_id,
                status=CommandStatus.failed,
                applied=False,
                reason="ocpp_charger_not_connected",
            )
        return await self._ev_charger.send_command(cmd)


async def build_runtime_adapter_map(
    *,
    device_repo: DeviceRepo,
    ocpp_central_system: OCPPCentralSystem,
    settings: Settings,
) -> RuntimeAdapterMap:
    """Read device_registry, construct adapters, return the runtime map.

    Empty registry (no validated + role-assigned rows) returns
    ``RuntimeAdapterMap(policy_guard_adapters={}, control_loop_adapters={},
    protocols_by_role={})`` — the pre-installer-wizard-complete cold-start
    state.

    Raises ``RuntimeAdapterWiringError`` on:
    * multi-device-per-role (AC12 — defense in depth on top of Story 9-2's
      role-assignment invariant);
    * unsupported ``(protocol, role)`` combination;
    * malformed address;
    * missing model for modbus_tcp + battery/inverter (capability-registry
      drift would have failed startup earlier; this is a final guard);
    * any other constructor failure (pydantic ``ValidationError`` etc.) —
      wrapped so AC9's fail-loud handler matches.

    On partial-construct failure, every adapter built so far is
    ``disconnect()``-ed (best-effort) before the exception propagates so no
    socket / serial / WebSocket handle leaks.

    The ``settings`` parameter is currently unused but threaded through for
    forward compatibility (timeouts, reconnect delays).
    """
    del settings  # reserved for future per-adapter tuning

    entries = await device_repo.list_all()
    eligible: list[DeviceRegistryEntry] = [
        entry for entry in entries if entry.validated and entry.role is not None
    ]

    if not eligible:
        return RuntimeAdapterMap(
            policy_guard_adapters={},
            control_loop_adapters={},
            protocols_by_role={},
        )

    # AC12 — multi-device-per-role defense in depth.
    by_role: dict[DeviceRole, DeviceRegistryEntry] = {}
    for entry in eligible:
        assert entry.role is not None  # narrowed by filter
        if entry.role in by_role:
            raise RuntimeAdapterWiringError(
                f"Multiple devices assigned to role {entry.role.value!r}: "
                f"{by_role[entry.role].device_id!r} and {entry.device_id!r}"
            )
        by_role[entry.role] = entry

    policy_guard_adapters: dict[DeviceRole, DeviceAdapter] = {}
    control_loop_adapters: dict[DeviceRole, DeviceAdapter] = {}
    protocols_by_role: dict[DeviceRole, str] = {}

    constructed: list[DeviceAdapter] = []
    try:
        for role, entry in by_role.items():
            try:
                adapter = _construct_adapter(entry, ocpp_central_system)
            except RuntimeAdapterWiringError:
                raise
            except Exception as exc:  # noqa: BLE001 — wrap any constructor failure so AC9 fail-loud handler matches
                raise RuntimeAdapterWiringError(
                    f"Adapter construction failed for device {entry.device_id!r} "
                    f"({entry.protocol!r}, {role.value!r}): {type(exc).__name__}: {exc}"
                ) from exc
            constructed.append(adapter)
            control_loop_adapters[role] = adapter
            protocols_by_role[role] = entry.protocol
            if _is_writable_role(role):
                # AC11 — same instance reference, not a copy.
                policy_guard_adapters[role] = adapter
    except BaseException:
        # Best-effort teardown of partial construction so sockets / serial /
        # WebSocket handles do not leak before the lifespan fail-loud handler
        # runs SystemExit(1).
        for adapter in constructed:
            try:
                await adapter.disconnect()
            except Exception:  # noqa: BLE001 — best-effort
                logger.warning(
                    "adapter_partial_construct_cleanup_failed",
                    device_id=getattr(adapter, "device_id", "<unknown>"),
                    exc_info=True,
                )
        raise

    return RuntimeAdapterMap(
        policy_guard_adapters=policy_guard_adapters,
        control_loop_adapters=control_loop_adapters,
        protocols_by_role=protocols_by_role,
    )


def _construct_adapter(
    entry: DeviceRegistryEntry,
    ocpp_central_system: OCPPCentralSystem,
) -> DeviceAdapter:
    """Construct a single domain adapter from one device_registry row.

    Raises ``RuntimeAdapterWiringError`` on unsupported ``(protocol, role)``
    combinations or malformed addresses. Other constructor failures
    (pydantic ``ValidationError`` etc.) are wrapped by the caller in
    ``build_runtime_adapter_map``.
    """
    role = entry.role
    assert role is not None

    if entry.protocol == "modbus_tcp":
        return _construct_modbus_adapter(entry)
    if entry.protocol == "dsmr_p1":
        return _construct_dsmr_adapter(entry)
    if entry.protocol == "ocpp_1_6":
        return _construct_ocpp_adapter(entry, ocpp_central_system)

    raise RuntimeAdapterWiringError(
        f"Unsupported (protocol, role) for device {entry.device_id!r}: "
        f"({entry.protocol!r}, {role.value!r})"
    )


def _construct_modbus_adapter(entry: DeviceRegistryEntry) -> DeviceAdapter:
    role = entry.role
    assert role is not None

    if role not in (DeviceRole.battery, DeviceRole.inverter):
        raise RuntimeAdapterWiringError(
            f"Unsupported (protocol, role) for device {entry.device_id!r}: "
            f"('modbus_tcp', {role.value!r})"
        )

    host, port = parse_host_port(entry.address)
    if host is None or port is None:
        raise RuntimeAdapterWiringError(
            f"Invalid modbus_tcp address for device {entry.device_id!r}: {entry.address!r}"
        )

    if entry.model is None:
        raise RuntimeAdapterWiringError(
            f"modbus_tcp + {role.value} requires a model for device {entry.device_id!r}"
        )

    register_ranges = _MODBUS_REGISTER_RANGES_BY_MODEL.get(entry.model)
    if register_ranges is None:
        raise RuntimeAdapterWiringError(
            f"No runtime register-range mapping for model {entry.model!r} "
            f"(device {entry.device_id!r}). Add an entry to "
            f"_MODBUS_REGISTER_RANGES_BY_MODEL."
        )

    config = ModbusTcpAdapterConfig(
        device_id=entry.device_id,
        host=host,
        port=port,
        registers=register_ranges,
    )
    modbus = ModbusTcpAdapter(config)

    if role == DeviceRole.battery:
        return BatteryAdapter(entry.device_id, modbus, entry.model)
    return InverterAdapter(entry.device_id, modbus, entry.model)


def _construct_dsmr_adapter(entry: DeviceRegistryEntry) -> DeviceAdapter:
    role = entry.role
    assert role is not None

    if role != DeviceRole.grid_meter:
        raise RuntimeAdapterWiringError(
            f"Unsupported (protocol, role) for device {entry.device_id!r}: "
            f"('dsmr_p1', {role.value!r})"
        )

    serial_port, tcp_host, tcp_port = parse_dsmr_address(entry.address)
    if serial_port is None and tcp_host is None:
        raise RuntimeAdapterWiringError(
            f"Invalid dsmr_p1 address for device {entry.device_id!r}: {entry.address!r}"
        )

    config = DSMRAdapterConfig(
        device_id=entry.device_id,
        serial_port=serial_port,
        tcp_host=tcp_host,
        tcp_port=tcp_port,
    )
    return GridMeterAdapter(entry.device_id, DSMRAdapter(config))


def _construct_ocpp_adapter(
    entry: DeviceRegistryEntry,
    ocpp_central_system: OCPPCentralSystem,
) -> DeviceAdapter:
    role = entry.role
    assert role is not None

    if role != DeviceRole.ev_charger:
        raise RuntimeAdapterWiringError(
            f"Unsupported (protocol, role) for device {entry.device_id!r}: "
            f"('ocpp_1_6', {role.value!r})"
        )

    # The OCPP address recorded by Story 9.1 discovery is ``/{charge_point_id}``;
    # strip the leading slash if present. The charge_point_id is the OCPP-
    # protocol identifier (used in WebSocket URL); device_id is the system-
    # wide identifier (used in CommandResult / audit / device_registry).
    charge_point_id = entry.address.lstrip("/") or entry.device_id
    if not _OCPP_CHARGE_POINT_ID_RE.fullmatch(charge_point_id):
        raise RuntimeAdapterWiringError(
            f"Invalid OCPP charge_point_id derived for device {entry.device_id!r} "
            f"from address {entry.address!r}: {charge_point_id!r} "
            f"(must match [A-Za-z0-9_.-]+)"
        )

    config = OCPPAdapterConfig(
        device_id=entry.device_id,
        charge_point_id=charge_point_id,
    )

    # ``OCPPCentralSystem.register`` is a destructive overwrite: each call
    # replaces any existing entry under the same charge_point_id. That is
    # safe here because (1) build_runtime_adapter_map runs at most once per
    # lifespan, BEFORE any WebSocket can connect (the lifespan has not yet
    # yielded), and (2) no other production call site invokes register().
    # The get_adapter() guard below is a defensive idempotency fence for
    # test fixtures that may pre-populate the central system.
    raw_adapter = ocpp_central_system.get_adapter(charge_point_id)
    if raw_adapter is None:
        raw_adapter = ocpp_central_system.register(config)

    ev_charger = EVChargerAdapter(entry.device_id, raw_adapter)
    return _DeferredOCPPAdapter(entry.device_id, raw_adapter, ev_charger)


__all__ = [
    "RuntimeAdapterMap",
    "RuntimeAdapterWiringError",
    "build_runtime_adapter_map",
]
