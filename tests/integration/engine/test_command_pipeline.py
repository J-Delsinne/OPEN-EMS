"""Integration tests for the full command pipeline: Intent → Executor → PolicyGuard → Adapter."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from open_ems.core import (
    BatteryState,
    DeviceRole,
    StateStore,
    SystemOperatingMode,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandResult,
    CommandStatus,
    DeviceCommand,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
)
from open_ems.core.devices import (
    DegradedDeviceState,
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)
from open_ems.engine import EvaluationResult, IntentExecutor, PolicyGuard, RetryPolicy
from open_ems.engine.rules.battery_control import BatteryIntent, BatteryIntentAction
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings
from tests.fixtures.active_constraints import make_active_constraints_provider

_NOW = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


class SimulatedBatteryAdapter:
    """In-memory DeviceAdapter implementing the full structural protocol for integration tests."""

    def __init__(self, *, device_id: str = "bat-001") -> None:
        self.device_id = device_id
        self.send_command_calls: list[DeviceCommand] = []

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_state(self) -> BatteryState | DegradedDeviceState:
        return BatteryState(
            device_id=self.device_id,
            soc_percent=80.0,
            battery_power_kw=0.0,
            capacity_kwh=10.0,
            operating_mode="normal",
            read_at=_NOW,
        )

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        return DeviceCapabilityProfile(
            device_id=self.device_id,
            model="SimulatedBattery",
            read_capabilities=frozenset({ReadCapability.state, ReadCapability.soc}),
            write_capabilities=frozenset(
                {WriteCapability.set_charge_rate, WriteCapability.set_discharge_rate}
            ),
        )

    async def send_command(self, cmd: DeviceCommand) -> CommandResult:
        self.send_command_calls.append(cmd)
        return CommandResult(
            correlation_id=cmd.correlation_id,
            device_id=self.device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


async def _state_store_with_battery(*, soc_percent: float) -> StateStore:
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    battery = BatteryState(
        device_id="bat-001",
        soc_percent=soc_percent,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW,
    )
    await store.publish({DeviceRole.battery: battery}, operating_mode=SystemOperatingMode.normal)
    return store


def _battery_charge_intent() -> BatteryIntent:
    return BatteryIntent(
        action=BatteryIntentAction.charge,
        target_power_kw=2.0,
        reserve_floor_percent=20.0,
        reason_code="test_charge",
        source_candidate=None,
    )


def _result_with(intent: BatteryIntent) -> EvaluationResult:
    return EvaluationResult(
        cycle_id=uuid.uuid4(),
        evaluated_at=_NOW,
        recommended_operating_mode=SystemOperatingMode.normal,
        intents=(intent,),
        decision_reasons=("test",),
        cycle_duration_ms=1,
    )


def _observability_with_audit_spy() -> tuple[ObservabilityService, AsyncMock]:
    spy = AsyncMock()
    obs = ObservabilityService(repo=MagicMock())
    obs.audit = spy  # type: ignore[method-assign]
    return obs, spy


async def test_full_pipeline_allowed_path() -> None:
    """BatteryIntent(charge) → IntentExecutor → RetryPolicy → PolicyGuard → adapter (success).

    Story 8.5 AC11 / AC1: the success path now emits exactly ONE DECISION audit
    via RetryPolicy. The full pipeline is therefore wired through RetryPolicy so
    the audit contract is exercised end-to-end.
    """
    store = await _state_store_with_battery(soc_percent=80.0)
    adapter = SimulatedBatteryAdapter()
    obs, audit_spy = _observability_with_audit_spy()
    executor = IntentExecutor()
    settings = _settings()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=settings,
        active_constraints=make_active_constraints_provider(settings),
    )
    retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

    snapshot = store.get_snapshot()
    commands = executor.translate(_result_with(_battery_charge_intent()), snapshot)
    assert len(commands) == 1
    assert isinstance(commands[0], SetBatteryChargeRateCommand)
    assert commands[0].origin is CommandOrigin.decision_engine

    cmd_result = await retry.execute(commands[0])

    assert cmd_result.status is CommandStatus.success
    assert cmd_result.applied is True
    assert len(adapter.send_command_calls) == 1
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DECISION"
    assert kwargs["device_id"] == "bat-001"
    assert kwargs["detail"]["correlation_id"] == str(commands[0].correlation_id)


async def test_full_pipeline_rejected_path() -> None:
    """Discharge command at reserve floor → PolicyGuard rejects, audit fires, adapter NOT called."""
    store = await _state_store_with_battery(soc_percent=20.0)  # at reserve floor
    adapter = SimulatedBatteryAdapter()
    obs, audit_spy = _observability_with_audit_spy()
    settings = _settings()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=settings,
        active_constraints=make_active_constraints_provider(settings),
    )

    discharge = SetBatteryDischargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )

    cmd_result = await guard.authorize_and_dispatch(discharge)

    assert cmd_result.status is CommandStatus.rejected
    assert cmd_result.reason == "battery_soc_at_or_below_reserve_floor"
    assert adapter.send_command_calls == []  # adapter never invoked
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "CONSTRAINT"
    assert kwargs["actor"] == "system"
    # Story 8.5 AC9: rejection audit detail carries correlation_id
    assert kwargs["detail"]["correlation_id"] == str(discharge.correlation_id)
    assert kwargs["detail"]["command_type"] == "SetBatteryDischargeRateCommand"
    assert kwargs["detail"]["rejection_reason"] == "battery_soc_at_or_below_reserve_floor"
