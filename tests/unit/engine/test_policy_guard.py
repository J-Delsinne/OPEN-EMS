"""Unit tests for PolicyGuard — safety enforcement and adapter dispatch."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from open_ems.core import (
    BatteryState,
    DeviceAdapter,
    DeviceRole,
    StateStore,
    SystemOperatingMode,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandResult,
    CommandStatus,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.core.constraints import ActiveConstraints
from open_ems.core.devices import (
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings
from open_ems.storage.repositories.config_repo import ConfigRepo

_NOW = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _active_constraints_provider(
    settings: Settings | None = None,
) -> ActiveConstraintsProvider:
    """Build a pre-hydrated provider seeded from ``Settings`` for sync test fixtures."""
    s = settings if settings is not None else _settings()
    provider = ActiveConstraintsProvider(repo=MagicMock(spec=ConfigRepo), settings=s)
    # Bypass async hydrate() in sync test setup by setting the cached snapshot directly.
    provider._current = ActiveConstraints(  # noqa: SLF001
        peak_limit_kw=s.peak_limit_kw,
        battery_reserve_floor_percent=s.battery_reserve_floor_percent,
        config_version=0,
        activated_at=_NOW,
    )
    return provider


def _state_store(operating_mode: SystemOperatingMode = SystemOperatingMode.normal) -> StateStore:
    return StateStore(system_clock_status="valid", operating_mode=operating_mode)


async def _publish_battery(store: StateStore, *, soc_percent: float = 50.0) -> None:
    battery = BatteryState(
        device_id="bat-001",
        soc_percent=soc_percent,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW,
    )
    await store.publish({DeviceRole.battery: battery})


def _capability_profile(
    *,
    device_id: str = "bat-001",
    write_caps: frozenset[WriteCapability] = frozenset({WriteCapability.set_discharge_rate}),
    read_caps: frozenset[ReadCapability] = frozenset({ReadCapability.state}),
) -> DeviceCapabilityProfile:
    return DeviceCapabilityProfile(
        device_id=device_id,
        model="TestBattery",
        read_capabilities=read_caps,
        write_capabilities=write_caps,
    )


def _adapter(
    *,
    device_id: str = "bat-001",
    write_caps: frozenset[WriteCapability] = frozenset({WriteCapability.set_discharge_rate}),
    send_command_result: CommandResult | None = None,
) -> MagicMock:
    adapter = MagicMock(spec=DeviceAdapter)
    adapter.device_id = device_id
    adapter.get_capabilities = AsyncMock(
        return_value=_capability_profile(device_id=device_id, write_caps=write_caps)
    )
    if send_command_result is None:
        send_command_result = CommandResult(
            correlation_id=uuid.uuid4(),
            device_id=device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )
    adapter.send_command = AsyncMock(return_value=send_command_result)
    return adapter


def _observability_with_audit_spy() -> tuple[ObservabilityService, AsyncMock]:
    spy = AsyncMock()
    obs = ObservabilityService(repo=MagicMock())
    obs.audit = spy  # type: ignore[method-assign]
    return obs, spy


def _discharge_command(
    *, device_id: str = "bat-001", rate_kw: float = 1.0
) -> SetBatteryDischargeRateCommand:
    return SetBatteryDischargeRateCommand(
        device_id=device_id,
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


def _charge_command(*, rate_kw: float = 1.0) -> SetBatteryChargeRateCommand:
    return SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


def _ev_charge_command(*, rate_kw: float = 5.0) -> SetEVChargingRateCommand:
    return SetEVChargingRateCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


def _stop_ev_command() -> StopEVChargingCommand:
    return StopEVChargingCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
    )


def _adapters_map(
    adapters: Mapping[DeviceRole, DeviceAdapter],
) -> dict[DeviceRole, DeviceAdapter]:
    return dict(adapters)


# ---------------------------------------------------------------------------
# AC8 unit tests
# ---------------------------------------------------------------------------


async def test_constraint_violation_rejected_with_audit_event() -> None:
    """SoC at reserve floor → discharge rejected AND audit CONSTRAINT event emitted."""
    store = _state_store()
    await _publish_battery(store, soc_percent=20.0)  # at reserve floor
    adapter = _adapter()
    obs, audit_spy = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    cmd = _discharge_command()
    result = await guard.authorize_and_dispatch(cmd)

    assert result.status is CommandStatus.rejected
    assert result.applied is False
    assert result.reason == "battery_soc_at_or_below_reserve_floor"
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "CONSTRAINT"
    assert kwargs["actor"] == "system"
    # Story 8.5 AC9: rejection audit detail carries correlation_id
    detail = kwargs["detail"]
    assert detail["correlation_id"] == str(cmd.correlation_id)
    assert detail["command_type"] == "SetBatteryDischargeRateCommand"
    assert detail["rejection_reason"] == "battery_soc_at_or_below_reserve_floor"
    adapter.send_command.assert_not_awaited()


async def test_capability_missing_blocks_send_command() -> None:
    """Adapter capability profile lacking the required write cap blocks send_command()."""
    store = _state_store()
    await _publish_battery(store, soc_percent=80.0)
    adapter = _adapter(write_caps=frozenset())  # no write capabilities at all
    obs, audit_spy = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    result = await guard.authorize_and_dispatch(_discharge_command())

    assert result.status is CommandStatus.rejected
    assert result.applied is False
    assert "capability_missing" in result.reason
    adapter.send_command.assert_not_awaited()
    audit_spy.assert_awaited_once()


async def test_command_result_applied_false_is_not_treated_as_success() -> None:
    """Adapter returning CommandResult(applied=False) is propagated verbatim — not coerced."""
    store = _state_store()
    await _publish_battery(store, soc_percent=80.0)
    not_applied = CommandResult(
        correlation_id=uuid.uuid4(),
        device_id="bat-001",
        status=CommandStatus.failed,
        applied=False,
        reason="device_not_ready",
    )
    adapter = _adapter(send_command_result=not_applied)
    obs, _ = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    result = await guard.authorize_and_dispatch(_discharge_command())

    assert result.applied is False
    assert result.status is CommandStatus.failed
    assert result.reason == "device_not_ready"


def test_command_result_status_uses_enum_values() -> None:
    """CommandStatus enum has exactly: success, failed, timeout, rejected."""
    assert {member.value for member in CommandStatus} == {
        "success",
        "failed",
        "timeout",
        "rejected",
    }


async def test_fail_safe_mode_rejects_all_commands() -> None:
    """SystemOperatingMode.fail_safe → ALL commands rejected; no send_command(), audit fires."""
    store = _state_store(operating_mode=SystemOperatingMode.fail_safe)
    adapter = _adapter()
    obs, audit_spy = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    result = await guard.authorize_and_dispatch(_charge_command())

    assert result.status is CommandStatus.rejected
    assert result.applied is False
    assert result.reason == "fail_safe_mode_active"
    adapter.send_command.assert_not_awaited()
    adapter.get_capabilities.assert_not_awaited()
    audit_spy.assert_awaited_once()


# ---------------------------------------------------------------------------
# Additional coverage: peak limit, conservative mode, adapter exception, timeout
# ---------------------------------------------------------------------------


async def test_peak_limit_bounds_check_rejects_excessive_rate() -> None:
    store = _state_store()
    await _publish_battery(store, soc_percent=80.0)
    adapter = _adapter(write_caps=frozenset({WriteCapability.set_charge_rate}))
    obs, audit_spy = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    result = await guard.authorize_and_dispatch(_charge_command(rate_kw=999.0))

    assert result.status is CommandStatus.rejected
    assert result.reason == "commanded_rate_exceeds_peak_limit"
    adapter.send_command.assert_not_awaited()
    audit_spy.assert_awaited_once()


async def test_adapter_exception_is_wrapped_as_failed_command_result() -> None:
    """Raw adapter exception MUST NOT propagate — wrapped as CommandResult(failed)."""
    store = _state_store()
    await _publish_battery(store, soc_percent=80.0)
    adapter = _adapter()
    adapter.send_command = AsyncMock(side_effect=RuntimeError("modbus_explode"))
    obs, _ = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    result = await guard.authorize_and_dispatch(_discharge_command())

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert "modbus_explode" in result.reason


async def test_adapter_timeout_is_wrapped_as_timeout_command_result() -> None:
    store = _state_store()
    await _publish_battery(store, soc_percent=80.0)
    adapter = _adapter()
    adapter.send_command = AsyncMock(side_effect=TimeoutError())
    obs, _ = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    result = await guard.authorize_and_dispatch(_discharge_command())

    assert result.status is CommandStatus.timeout
    assert result.applied is False


async def test_adapter_not_registered_is_rejected() -> None:
    store = _state_store()
    await _publish_battery(store, soc_percent=80.0)
    obs, audit_spy = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={},  # no adapter for battery role
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    result = await guard.authorize_and_dispatch(_discharge_command())

    assert result.status is CommandStatus.rejected
    assert result.reason == "adapter_not_registered"
    audit_spy.assert_awaited_once()


async def test_capability_check_failure_is_rejected() -> None:
    """If get_capabilities raises, the command is rejected with capability_check_failed."""
    store = _state_store()
    await _publish_battery(store, soc_percent=80.0)
    adapter = MagicMock(spec=DeviceAdapter)
    adapter.device_id = "bat-001"
    adapter.get_capabilities = AsyncMock(side_effect=RuntimeError("boom"))
    adapter.send_command = AsyncMock()
    obs, audit_spy = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    result = await guard.authorize_and_dispatch(_discharge_command())

    assert result.status is CommandStatus.rejected
    assert result.reason == "capability_check_failed"
    adapter.send_command.assert_not_awaited()
    audit_spy.assert_awaited_once()


async def test_conservative_mode_blocks_discharge() -> None:
    store = _state_store(operating_mode=SystemOperatingMode.conservative)
    await _publish_battery(store, soc_percent=80.0)
    # We need to also keep operating_mode through the publish — pass explicitly:
    battery = BatteryState(
        device_id="bat-001",
        soc_percent=80.0,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW,
    )
    await store.publish(
        {DeviceRole.battery: battery}, operating_mode=SystemOperatingMode.conservative
    )
    adapter = _adapter()
    obs, audit_spy = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    result = await guard.authorize_and_dispatch(_discharge_command())

    assert result.status is CommandStatus.rejected
    assert result.reason == "conservative_mode_blocks_load_increase"
    adapter.send_command.assert_not_awaited()
    audit_spy.assert_awaited_once()


async def test_successful_dispatch_returns_adapter_result_no_audit() -> None:
    """Happy path: adapter returns success → no CONSTRAINT audit event emitted."""
    store = _state_store()
    await _publish_battery(store, soc_percent=80.0)
    adapter = _adapter()
    obs, audit_spy = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    result = await guard.authorize_and_dispatch(_discharge_command())

    assert result.status is CommandStatus.success
    assert result.applied is True
    audit_spy.assert_not_awaited()
    adapter.send_command.assert_awaited_once()


async def test_stop_ev_command_requires_set_ev_charge_current_capability() -> None:
    store = _state_store()
    adapter = MagicMock(spec=DeviceAdapter)
    adapter.device_id = "ev-001"
    adapter.get_capabilities = AsyncMock(
        return_value=_capability_profile(
            device_id="ev-001", write_caps=frozenset({WriteCapability.set_ev_charge_current})
        )
    )
    adapter.send_command = AsyncMock(
        return_value=CommandResult(
            correlation_id=uuid.uuid4(),
            device_id="ev-001",
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )
    )
    obs, _ = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.ev_charger: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    result = await guard.authorize_and_dispatch(_stop_ev_command())

    assert result.status is CommandStatus.success
    adapter.send_command.assert_awaited_once()


async def test_command_dispatch_timeout_is_caught_via_wait_for() -> None:
    """When adapter.send_command stalls past the dispatch timeout → CommandStatus.timeout."""
    store = _state_store()
    await _publish_battery(store, soc_percent=80.0)
    adapter = _adapter()

    async def _hang(_cmd: object) -> CommandResult:
        await asyncio.sleep(60.0)
        raise AssertionError("should not return")

    adapter.send_command = AsyncMock(side_effect=_hang)
    obs, _ = _observability_with_audit_spy()

    with patch("open_ems.engine.policy_guard.COMMAND_DISPATCH_TIMEOUT_SECONDS", 0.05):
        guard = PolicyGuard(
            state_store=store,
            adapters={DeviceRole.battery: adapter},
            observability=obs,
            settings=_settings(),
            active_constraints=_active_constraints_provider(),
        )
        result = await guard.authorize_and_dispatch(_discharge_command())

    assert result.status is CommandStatus.timeout
    assert result.applied is False


# ─── Story 8.5 AC12 #7 — _reject audit detail.correlation_id ───────────────────


async def test_reject_audit_includes_correlation_id_in_detail() -> None:
    """AC12 #7: any PolicyGuard rejection branch carries correlation_id in audit detail.

    Drives the capability-missing branch (cheapest to set up) — the detail-field
    contract is identical across all rejection branches because it's centralised
    in ``_reject``.
    """
    store = _state_store()
    await _publish_battery(store, soc_percent=80.0)
    adapter = _adapter(write_caps=frozenset())  # no write capabilities
    obs, audit_spy = _observability_with_audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_active_constraints_provider(),
    )

    cmd = _discharge_command()
    result = await guard.authorize_and_dispatch(cmd)

    assert result.status is CommandStatus.rejected
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "CONSTRAINT"
    detail = kwargs["detail"]
    assert detail["correlation_id"] == str(cmd.correlation_id)
    assert detail["command_type"] == "SetBatteryDischargeRateCommand"
    assert "capability_missing" in detail["rejection_reason"]
