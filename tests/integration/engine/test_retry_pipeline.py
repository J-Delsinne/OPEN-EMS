"""Integration tests for the full retry pipeline (Story 8.3 AC10 #16, #17).

Exercises Intent → IntentExecutor → RetryPolicy → PolicyGuard → adapter with
real implementations of every layer except the EventLogRepo underneath
ObservabilityService.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from open_ems.core import (
    BatteryState,
    DeviceRole,
    EVChargerState,
    StateStore,
    SystemOperatingMode,
)
from open_ems.core.commands import (
    CommandResult,
    CommandStatus,
    DeviceCommand,
)
from open_ems.core.devices import (
    DegradedDeviceState,
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)
from open_ems.engine import EvaluationResult, IntentExecutor, PolicyGuard, RetryPolicy
from open_ems.engine.rules.battery_control import BatteryIntent, BatteryIntentAction
from open_ems.engine.rules.ev_scheduling import EVChargerIntent, EVChargerIntentAction
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings
from open_ems.storage.repositories.event_log_repo import EventLogRepo

_NOW = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


class SimulatedBatteryAdapter:
    """Battery adapter that fails the first ``failure_count`` send_command calls, then succeeds."""

    def __init__(self, *, device_id: str = "bat-001", failure_count: int = 0) -> None:
        self.device_id = device_id
        self._remaining_failures = failure_count
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
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            return CommandResult(
                correlation_id=cmd.correlation_id,
                device_id=self.device_id,
                status=CommandStatus.failed,
                applied=False,
                reason="communication_error",
            )
        return CommandResult(
            correlation_id=cmd.correlation_id,
            device_id=self.device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )


class SimulatedEVChargerAdapter:
    """EV charger adapter that fails N send_command calls, then succeeds (Story 8.3 AC10 #17)."""

    def __init__(self, *, device_id: str = "ev-001", failure_count: int = 0) -> None:
        self.device_id = device_id
        self._remaining_failures = failure_count
        self.send_command_calls: list[DeviceCommand] = []

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_state(self) -> EVChargerState | DegradedDeviceState:
        return EVChargerState(
            device_id=self.device_id,
            status="charging",
            session_active=True,
            current_power_kw=3.0,
            power_source="meter_values",
            power_measured_at=_NOW,
            read_at=_NOW,
        )

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        return DeviceCapabilityProfile(
            device_id=self.device_id,
            model="SimulatedEVCharger",
            read_capabilities=frozenset({ReadCapability.state}),
            write_capabilities=frozenset({WriteCapability.set_ev_charge_current}),
        )

    async def send_command(self, cmd: DeviceCommand) -> CommandResult:
        self.send_command_calls.append(cmd)
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            return CommandResult(
                correlation_id=cmd.correlation_id,
                device_id=self.device_id,
                status=CommandStatus.failed,
                applied=False,
                reason="ocpp_call_failed",
            )
        return CommandResult(
            correlation_id=cmd.correlation_id,
            device_id=self.device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        command_max_retries=2,
        command_retry_backoff_seconds=0.0,
    )


async def _state_store_with_battery() -> StateStore:
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    battery = BatteryState(
        device_id="bat-001",
        soc_percent=80.0,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW,
    )
    await store.publish({DeviceRole.battery: battery}, operating_mode=SystemOperatingMode.normal)
    return store


async def _state_store_with_ev_charger() -> StateStore:
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    ev = EVChargerState(
        device_id="ev-001",
        status="charging",
        session_active=True,
        current_power_kw=3.0,
        power_source="meter_values",
        power_measured_at=_NOW,
        read_at=_NOW,
    )
    await store.publish({DeviceRole.ev_charger: ev}, operating_mode=SystemOperatingMode.normal)
    return store


def _result_with(intent: BatteryIntent | EVChargerIntent) -> EvaluationResult:
    return EvaluationResult(
        cycle_id=uuid.uuid4(),
        evaluated_at=_NOW,
        recommended_operating_mode=SystemOperatingMode.normal,
        intents=(intent,),
        decision_reasons=("test",),
        cycle_duration_ms=1,
    )


def _battery_charge_intent() -> BatteryIntent:
    return BatteryIntent(
        action=BatteryIntentAction.charge,
        target_power_kw=2.0,
        reserve_floor_percent=20.0,
        reason_code="test_charge",
        source_candidate=None,
    )


def _ev_stop_intent() -> EVChargerIntent:
    return EVChargerIntent(
        action=EVChargerIntentAction.stop,
        target_charge_rate_kw=None,
        homeowner_override_active=False,
        reason_code="test_stop",
        source_candidate=None,
    )


def _audit_spy_observability() -> tuple[ObservabilityService, AsyncMock]:
    """Real ObservabilityService instance with the underlying repo mocked AND audit() spied."""
    repo = AsyncMock(spec=EventLogRepo)
    obs = ObservabilityService(repo=repo)
    spy = AsyncMock()
    obs.audit = spy  # type: ignore[method-assign]
    return obs, spy


async def test_full_pipeline_with_idempotent_retry_recovery() -> None:
    """AC10 #16: BatteryIntent(charge) recovers after 2 transient failures; ZERO audit events."""
    store = await _state_store_with_battery()
    adapter = SimulatedBatteryAdapter(failure_count=2)
    obs, audit_spy = _audit_spy_observability()
    settings = _settings()

    executor = IntentExecutor()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=settings,
    )
    retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

    snapshot = store.get_snapshot()
    commands = executor.translate(_result_with(_battery_charge_intent()), snapshot)
    assert len(commands) == 1

    cmd_result = await retry.execute(commands[0])

    assert cmd_result.status is CommandStatus.success
    assert cmd_result.applied is True
    assert len(adapter.send_command_calls) == 3  # 2 failures + 1 success
    audit_spy.assert_not_awaited()  # ZERO DEVICE events; ZERO CONSTRAINT events


async def test_full_pipeline_with_non_idempotent_fail_closed() -> None:
    """AC10 #17: EVChargerIntent(stop) fails once → no retry → exactly ONE DEVICE audit event."""
    store = await _state_store_with_ev_charger()
    adapter = SimulatedEVChargerAdapter(failure_count=1)
    obs, audit_spy = _audit_spy_observability()
    settings = _settings()

    executor = IntentExecutor()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.ev_charger: adapter},
        observability=obs,
        settings=settings,
    )
    retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

    snapshot = store.get_snapshot()
    commands = executor.translate(_result_with(_ev_stop_intent()), snapshot)
    assert len(commands) == 1

    cmd_result = await retry.execute(commands[0])

    assert cmd_result.status is CommandStatus.failed
    assert cmd_result.applied is False
    assert len(adapter.send_command_calls) == 1  # NO retries on a non-idempotent command
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["event_type"] == "DEVICE"
    assert kwargs["actor"] == "system"
    assert kwargs["device_id"] == "ev-001"
    assert "after 1 attempt(s)" in kwargs["summary"]
    assert "ocpp_call_failed" in kwargs["summary"]
