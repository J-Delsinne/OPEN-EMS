"""Unit tests for ``build_runtime_adapter_map`` (Story 9.X).

Cover AC13's ten scenarios: empty registry, single-device per protocol,
multi-device-per-role defense, unsupported (protocol, role), malformed
address, and OCPP pre-/post-boot behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.modbus.inverter_adapter import InverterAdapter
from open_ems.adapters.ocpp.central_system import OCPPCentralSystem
from open_ems.core.commands import CommandOrigin, CommandResult, CommandStatus
from open_ems.core.devices import DegradedDeviceState, DeviceRole
from open_ems.services.runtime_adapter_wiring import (
    RuntimeAdapterMap,
    RuntimeAdapterWiringError,
    _DeferredOCPPAdapter,
    build_runtime_adapter_map,
)
from open_ems.settings import Settings
from open_ems.storage.repositories.device_repo import DeviceRegistryEntry

_NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)


class _FakeDeviceRepo:
    """In-memory ``DeviceRepo`` stand-in returning a fixed list of entries.

    Satisfies the structural surface ``build_runtime_adapter_map`` calls:
    only ``list_all()`` is exercised.
    """

    def __init__(self, entries: list[DeviceRegistryEntry]) -> None:
        self._entries = entries

    async def list_all(self) -> list[DeviceRegistryEntry]:
        return list(self._entries)


def _entry(
    *,
    device_id: str = "dev-1",
    protocol: str = "modbus_tcp",
    address: str = "192.168.1.10:502",
    model: str | None = "byd_hvs_v1",
    role: DeviceRole | None = DeviceRole.battery,
    validated: bool = True,
) -> DeviceRegistryEntry:
    return DeviceRegistryEntry(
        device_id=device_id,
        protocol=protocol,  # type: ignore[arg-type]
        address=address,
        model=model,
        firmware_version=None,
        role=role,
        source="manual_entry",
        validated=validated,
        last_capability_status="full" if validated else None,
        last_limitation_reason=None,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
        installer_acknowledged_unvalidated_at=None,
        role_assigned_at=_NOW if role is not None else None,
    )


def _settings() -> Settings:
    # Settings() requires no required-field overrides for the wiring tests; the
    # builder threads ``settings`` through but does not currently consult it.
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# AC7 — empty registry cold-start
# ---------------------------------------------------------------------------


async def test_empty_registry_returns_empty_runtime_map() -> None:
    repo = _FakeDeviceRepo(entries=[])
    ocpp = OCPPCentralSystem()
    result = await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=ocpp,
        settings=_settings(),
    )
    assert isinstance(result, RuntimeAdapterMap)
    assert result.policy_guard_adapters == {}
    assert result.control_loop_adapters == {}


async def test_unvalidated_or_unrolled_devices_excluded() -> None:
    repo = _FakeDeviceRepo(
        entries=[
            # validated=False — excluded
            _entry(
                device_id="dev-a",
                protocol="modbus_tcp",
                address="10.0.0.1:502",
                model="byd_hvs_v1",
                role=DeviceRole.battery,
                validated=False,
            ),
            # role is None — excluded
            _entry(
                device_id="dev-b",
                protocol="modbus_tcp",
                address="10.0.0.2:502",
                model="byd_hvs_v1",
                role=None,
            ),
        ]
    )
    result = await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=OCPPCentralSystem(),
        settings=_settings(),
    )
    assert result.policy_guard_adapters == {}
    assert result.control_loop_adapters == {}


# ---------------------------------------------------------------------------
# AC3 / AC11 — Modbus battery wiring + instance-identity
# ---------------------------------------------------------------------------


async def test_battery_modbus_wired_into_both_maps_same_instance() -> None:
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="batt-1",
                protocol="modbus_tcp",
                address="10.0.0.10:502",
                model="byd_hvs_v1",
                role=DeviceRole.battery,
            )
        ]
    )
    result = await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=OCPPCentralSystem(),
        settings=_settings(),
    )
    assert set(result.policy_guard_adapters.keys()) == {DeviceRole.battery}
    assert set(result.control_loop_adapters.keys()) == {DeviceRole.battery}
    pg = result.policy_guard_adapters[DeviceRole.battery]
    cl = result.control_loop_adapters[DeviceRole.battery]
    # AC11 — exact instance identity, not a copy
    assert pg is cl
    assert isinstance(pg, BatteryAdapter)
    assert pg.device_id == "batt-1"


async def test_inverter_modbus_excluded_from_policy_guard_map() -> None:
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="inv-1",
                protocol="modbus_tcp",
                address="10.0.0.20:502",
                model="fronius_gen24_v1",
                role=DeviceRole.inverter,
            )
        ]
    )
    result = await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=OCPPCentralSystem(),
        settings=_settings(),
    )
    # Inverter has empty write_capabilities in v1 — exclude from PolicyGuard.
    assert DeviceRole.inverter not in result.policy_guard_adapters
    assert DeviceRole.inverter in result.control_loop_adapters
    assert isinstance(result.control_loop_adapters[DeviceRole.inverter], InverterAdapter)


# ---------------------------------------------------------------------------
# AC3 — DSMR grid meter
# ---------------------------------------------------------------------------


async def test_dsmr_grid_meter_only_in_control_loop_map() -> None:
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="meter-1",
                protocol="dsmr_p1",
                address="/dev/ttyUSB0",
                model="dsmr_p1",
                role=DeviceRole.grid_meter,
            )
        ]
    )
    result = await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=OCPPCentralSystem(),
        settings=_settings(),
    )
    # DSMR is read-only — cross-adapter contract clause 1 — never in PolicyGuard.
    assert DeviceRole.grid_meter not in result.policy_guard_adapters
    assert DeviceRole.grid_meter in result.control_loop_adapters
    assert result.control_loop_adapters[DeviceRole.grid_meter].device_id == "meter-1"


async def test_dsmr_tcp_address_form_accepted() -> None:
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="meter-tcp",
                protocol="dsmr_p1",
                address="meter.local:2400",
                model="dsmr_p1",
                role=DeviceRole.grid_meter,
            )
        ]
    )
    result = await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=OCPPCentralSystem(),
        settings=_settings(),
    )
    assert DeviceRole.grid_meter in result.control_loop_adapters


# ---------------------------------------------------------------------------
# AC3 / AC4 — OCPP wiring (eager register + EVChargerAdapter wrap)
# ---------------------------------------------------------------------------


async def test_ocpp_ev_charger_eagerly_registered_in_central_system() -> None:
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="cp-001",
                protocol="ocpp_1_6",
                address="/cp-001",
                model="ocpp_1_6",
                role=DeviceRole.ev_charger,
            )
        ]
    )
    ocpp = OCPPCentralSystem()
    assert ocpp.get_adapter("cp-001") is None  # baseline
    result = await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=ocpp,
        settings=_settings(),
    )
    # Eager registration: the raw OCPPChargerAdapter now exists in the central
    # system, ready for a future WebSocket endpoint to call handle_charger.
    assert ocpp.get_adapter("cp-001") is not None
    # AC4 — the slot in both maps is the spec-mandated _DeferredOCPPAdapter
    # proxy. Same instance across the two maps (AC11).
    pg = result.policy_guard_adapters[DeviceRole.ev_charger]
    cl = result.control_loop_adapters[DeviceRole.ev_charger]
    assert pg is cl
    assert isinstance(pg, _DeferredOCPPAdapter)
    assert pg.device_id == "cp-001"


async def test_ocpp_register_idempotent_when_central_system_pre_populated() -> None:
    """Defensive idempotency fence: a test fixture that pre-populates the
    central system before wiring runs MUST NOT cause the wiring to create a
    second raw adapter that orphans the first.
    """
    from open_ems.adapters.ocpp.central_system import OCPPAdapterConfig

    ocpp = OCPPCentralSystem()
    pre_existing = ocpp.register(OCPPAdapterConfig(device_id="cp-pre", charge_point_id="cp-pre"))
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="cp-pre",
                protocol="ocpp_1_6",
                address="/cp-pre",
                model="ocpp_1_6",
                role=DeviceRole.ev_charger,
            )
        ]
    )
    await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=ocpp,
        settings=_settings(),
    )
    # Same raw adapter instance — wiring did not overwrite it.
    assert ocpp.get_adapter("cp-pre") is pre_existing


async def test_ocpp_pre_boot_get_state_returns_ac4_reason() -> None:
    """AC4 — pre-boot ``get_state()`` returns the spec-mandated reason.

    Before the proxy, the eager-register impl surfaced
    ``reason="reconnecting"`` (collapsing pre-boot and transient-flap into
    one observable). The _DeferredOCPPAdapter proxy restores the spec
    distinction: pre-boot is ``ocpp_charger_not_connected``; a transient
    flap (raw adapter went connected then disconnected) keeps the
    underlying EVChargerAdapter mapping (``reconnecting``).
    """
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="cp-002",
                protocol="ocpp_1_6",
                address="/cp-002",
                model="ocpp_1_6",
                role=DeviceRole.ev_charger,
            )
        ]
    )
    ocpp = OCPPCentralSystem()
    result = await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=ocpp,
        settings=_settings(),
    )
    adapter = result.control_loop_adapters[DeviceRole.ev_charger]
    state = await adapter.get_state()
    assert isinstance(state, DegradedDeviceState)
    assert state.reason == "ocpp_charger_not_connected"
    assert state.role == DeviceRole.ev_charger
    assert state.device_id == "cp-002"


async def test_ocpp_pre_boot_get_capabilities_raises_ac4_runtime_error() -> None:
    """AC4 — pre-boot ``get_capabilities()`` raises
    ``RuntimeError('ocpp_charger_not_connected')``. PolicyGuard P2 catches
    this and emits ``capability_check_failed`` audit pre-boot.
    """
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="cp-004",
                protocol="ocpp_1_6",
                address="/cp-004",
                model="ocpp_1_6",
                role=DeviceRole.ev_charger,
            )
        ]
    )
    ocpp = OCPPCentralSystem()
    result = await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=ocpp,
        settings=_settings(),
    )
    adapter = result.control_loop_adapters[DeviceRole.ev_charger]
    with pytest.raises(RuntimeError, match="ocpp_charger_not_connected"):
        await adapter.get_capabilities()


async def test_ocpp_pre_boot_send_command_returns_failed_with_ac4_reason() -> None:
    """AC4 — pre-boot ``send_command()`` returns a failed CommandResult
    with reason ``ocpp_charger_not_connected``, preserving correlation_id
    and device_id from the command.
    """
    from open_ems.core.commands import SetEVChargingRateCommand

    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="cp-003",
                protocol="ocpp_1_6",
                address="/cp-003",
                model="ocpp_1_6",
                role=DeviceRole.ev_charger,
            )
        ]
    )
    ocpp = OCPPCentralSystem()
    result = await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=ocpp,
        settings=_settings(),
    )
    adapter = result.control_loop_adapters[DeviceRole.ev_charger]
    cmd = SetEVChargingRateCommand(
        device_id="cp-003",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )
    result_cmd: CommandResult = await adapter.send_command(cmd)
    assert result_cmd.applied is False
    assert result_cmd.status == CommandStatus.failed
    assert result_cmd.reason == "ocpp_charger_not_connected"
    assert result_cmd.correlation_id == cmd.correlation_id
    assert result_cmd.device_id == cmd.device_id


async def test_ocpp_post_boot_proxy_delegates_get_capabilities() -> None:
    """AC4 — once the underlying raw adapter is ``connected=True``, the
    proxy delegates verbatim to ``EVChargerAdapter.get_capabilities()``
    (no longer raises). The post-boot delegation path is exercised here
    via a direct flip of the raw adapter's connected flag, simulating
    ``OCPPChargerAdapter.handle_connection`` having run.
    """
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="cp-005",
                protocol="ocpp_1_6",
                address="/cp-005",
                model="ocpp_1_6",
                role=DeviceRole.ev_charger,
            )
        ]
    )
    ocpp = OCPPCentralSystem()
    result = await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=ocpp,
        settings=_settings(),
    )
    adapter = result.control_loop_adapters[DeviceRole.ev_charger]
    raw = ocpp.get_adapter("cp-005")
    assert raw is not None
    raw._state.connected = True  # noqa: SLF001 — simulate handle_connection having run
    # Post-boot: get_capabilities now delegates and returns the OCPP profile.
    profile = await adapter.get_capabilities()
    assert profile is not None
    assert profile.device_id == "cp-005"


@pytest.mark.parametrize(
    "address",
    ["/", "/cp/with-slash", "/cp with space"],
)
async def test_ocpp_invalid_charge_point_id_raises(address: str) -> None:
    """AC4 / charge_point_id validation — addresses that yield an invalid
    OCPP identifier (empty after lstrip, embedded slash, whitespace) raise
    ``RuntimeAdapterWiringError`` rather than silently producing a routing
    mismatch at WebSocket boot time.
    """
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="cp valid id",  # also invalid as fallback
                protocol="ocpp_1_6",
                address=address,
                model="ocpp_1_6",
                role=DeviceRole.ev_charger,
            )
        ]
    )
    with pytest.raises(RuntimeAdapterWiringError, match="Invalid OCPP charge_point_id"):
        await build_runtime_adapter_map(
            device_repo=repo,  # type: ignore[arg-type]
            ocpp_central_system=OCPPCentralSystem(),
            settings=_settings(),
        )


# ---------------------------------------------------------------------------
# AC12 — multi-device-per-role defense in depth
# ---------------------------------------------------------------------------


async def test_multi_device_per_role_raises() -> None:
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="batt-a",
                protocol="modbus_tcp",
                address="10.0.0.1:502",
                model="byd_hvs_v1",
                role=DeviceRole.battery,
            ),
            _entry(
                device_id="batt-b",
                protocol="modbus_tcp",
                address="10.0.0.2:502",
                model="byd_hvs_v1",
                role=DeviceRole.battery,
            ),
        ]
    )
    with pytest.raises(RuntimeAdapterWiringError, match="Multiple devices assigned"):
        await build_runtime_adapter_map(
            device_repo=repo,  # type: ignore[arg-type]
            ocpp_central_system=OCPPCentralSystem(),
            settings=_settings(),
        )


# ---------------------------------------------------------------------------
# AC3 — unsupported (protocol, role) combinations
# ---------------------------------------------------------------------------


async def test_unsupported_protocol_role_combination_raises() -> None:
    # dsmr_p1 + battery is structurally impossible (DSMR is grid_meter-only)
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="weird-1",
                protocol="dsmr_p1",
                address="/dev/ttyUSB0",
                model="dsmr_p1",
                role=DeviceRole.battery,
            )
        ]
    )
    with pytest.raises(RuntimeAdapterWiringError, match="Unsupported"):
        await build_runtime_adapter_map(
            device_repo=repo,  # type: ignore[arg-type]
            ocpp_central_system=OCPPCentralSystem(),
            settings=_settings(),
        )


async def test_modbus_tcp_grid_meter_combination_raises() -> None:
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="weird-2",
                protocol="modbus_tcp",
                address="10.0.0.1:502",
                model="byd_hvs_v1",
                role=DeviceRole.grid_meter,
            )
        ]
    )
    with pytest.raises(RuntimeAdapterWiringError, match="Unsupported"):
        await build_runtime_adapter_map(
            device_repo=repo,  # type: ignore[arg-type]
            ocpp_central_system=OCPPCentralSystem(),
            settings=_settings(),
        )


# ---------------------------------------------------------------------------
# AC3 — malformed address handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["no-colon-address", "host:not-a-port", "host:0", "host:99999"],
)
async def test_modbus_malformed_address_raises(address: str) -> None:
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="batt-x",
                protocol="modbus_tcp",
                address=address,
                model="byd_hvs_v1",
                role=DeviceRole.battery,
            )
        ]
    )
    with pytest.raises(RuntimeAdapterWiringError, match="Invalid modbus_tcp"):
        await build_runtime_adapter_map(
            device_repo=repo,  # type: ignore[arg-type]
            ocpp_central_system=OCPPCentralSystem(),
            settings=_settings(),
        )


async def test_modbus_empty_address_rejected_at_entry_construction() -> None:
    """The empty-string address is rejected by Pydantic at
    ``DeviceRegistryEntry`` construction (NonEmptyStr), NOT by the wiring.
    This test documents the upstream defense; the wiring's own
    ``Invalid modbus_tcp address`` path is exercised by the parametrize
    above for non-empty malformed inputs.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _entry(
            device_id="batt-x",
            protocol="modbus_tcp",
            address="",
            model="byd_hvs_v1",
            role=DeviceRole.battery,
        )


async def test_modbus_battery_without_model_raises() -> None:
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="batt-no-model",
                protocol="modbus_tcp",
                address="10.0.0.1:502",
                model=None,
                role=DeviceRole.battery,
            )
        ]
    )
    with pytest.raises(RuntimeAdapterWiringError, match="requires a model"):
        await build_runtime_adapter_map(
            device_repo=repo,  # type: ignore[arg-type]
            ocpp_central_system=OCPPCentralSystem(),
            settings=_settings(),
        )


async def test_modbus_battery_with_unknown_model_raises() -> None:
    # NOTE: capability registry alignment would normally fail startup before
    # this code path runs; the guard here is defense in depth.
    # But DeviceRegistryEntry doesn't validate the model string against
    # the registry, so we can construct one.
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="batt-unknown",
                protocol="modbus_tcp",
                address="10.0.0.1:502",
                model="totally-fake-model-v9",
                role=DeviceRole.battery,
            )
        ]
    )
    with pytest.raises(RuntimeAdapterWiringError, match="No runtime register-range mapping"):
        await build_runtime_adapter_map(
            device_repo=repo,  # type: ignore[arg-type]
            ocpp_central_system=OCPPCentralSystem(),
            settings=_settings(),
        )


# ---------------------------------------------------------------------------
# Multi-role wiring — three adapters, one map
# ---------------------------------------------------------------------------


async def test_multi_role_wiring_battery_inverter_grid_ev() -> None:
    repo = _FakeDeviceRepo(
        entries=[
            _entry(
                device_id="batt-1",
                protocol="modbus_tcp",
                address="10.0.0.10:502",
                model="byd_hvs_v1",
                role=DeviceRole.battery,
            ),
            _entry(
                device_id="inv-1",
                protocol="modbus_tcp",
                address="10.0.0.20:502",
                model="fronius_gen24_v1",
                role=DeviceRole.inverter,
            ),
            _entry(
                device_id="meter-1",
                protocol="dsmr_p1",
                address="/dev/ttyUSB0",
                model="dsmr_p1",
                role=DeviceRole.grid_meter,
            ),
            _entry(
                device_id="cp-001",
                protocol="ocpp_1_6",
                address="/cp-001",
                model="ocpp_1_6",
                role=DeviceRole.ev_charger,
            ),
        ]
    )
    result = await build_runtime_adapter_map(
        device_repo=repo,  # type: ignore[arg-type]
        ocpp_central_system=OCPPCentralSystem(),
        settings=_settings(),
    )
    # ControlLoop sees all four; PolicyGuard sees battery + ev_charger only.
    assert set(result.control_loop_adapters.keys()) == {
        DeviceRole.battery,
        DeviceRole.inverter,
        DeviceRole.grid_meter,
        DeviceRole.ev_charger,
    }
    assert set(result.policy_guard_adapters.keys()) == {
        DeviceRole.battery,
        DeviceRole.ev_charger,
    }
    # Instance-identity holds for overlapping roles
    assert (
        result.policy_guard_adapters[DeviceRole.battery]
        is result.control_loop_adapters[DeviceRole.battery]
    )
    assert (
        result.policy_guard_adapters[DeviceRole.ev_charger]
        is result.control_loop_adapters[DeviceRole.ev_charger]
    )
