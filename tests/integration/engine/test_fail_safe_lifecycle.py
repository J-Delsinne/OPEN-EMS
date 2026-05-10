"""Integration tests for fail-safe lifecycle (Story 8.4 AC12 #33-#34).

#33 — full fail-safe lifecycle: enter, stay, exit, with audit events appearing
once per transition and no command dispatch while in fail-safe.

#34 — control-loop crash: ``_on_control_loop_done`` callback marks
``LoopLiveness.crashed`` and emits a SYSTEM audit event.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from open_ems.core import (
    BatteryState,
    DeviceRole,
    GridMeterState,
    InverterState,
    StateStore,
)
from open_ems.core.commands import CommandResult, CommandStatus, DeviceCommand
from open_ems.core.devices import (
    DegradedDeviceState,
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)
from open_ems.engine import IntentExecutor, PolicyGuard, RetryPolicy
from open_ems.engine.control_loop import ControlLoop
from open_ems.services.audit_log import ObservabilityService
from open_ems.services.loop_liveness import LoopLiveness
from open_ems.settings import Settings
from open_ems.storage.repositories.event_log_repo import EventLogRepo
from open_ems.web.app import make_on_control_loop_done

_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


# ── Simulated adapters ────────────────────────────────────────────────────────


class _SimulatedInverter:
    def __init__(self) -> None:
        self.device_id = "inv-001"

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_state(self) -> InverterState | DegradedDeviceState:
        return InverterState(
            device_id=self.device_id,
            pv_power_kw=2.0,
            ac_power_kw=2.0,
            operating_mode="normal",
            read_at=datetime.now(UTC),
        )

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        return DeviceCapabilityProfile(
            device_id=self.device_id,
            model="SimInverter",
            read_capabilities=frozenset({ReadCapability.state, ReadCapability.power}),
            write_capabilities=frozenset(),
        )

    async def send_command(self, _cmd: DeviceCommand) -> CommandResult:
        raise AssertionError("Inverter has no write capability")


class _FailingThenHealthyGridMeter:
    """Returns DegradedDeviceState for the first ``failure_count`` polls, then healthy."""

    def __init__(self, *, failure_count: int) -> None:
        self.device_id = "grid-001"
        self._remaining_failures = failure_count

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_state(self) -> GridMeterState | DegradedDeviceState:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise TimeoutError("simulated grid-meter unreachable")
        return GridMeterState(
            device_id=self.device_id,
            grid_power_kw=1.5,
            energy_delivered_kwh=100.0,
            energy_returned_kwh=10.0,
            received_at=datetime.now(UTC),
        )

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        return DeviceCapabilityProfile(
            device_id=self.device_id,
            model="SimGridMeter",
            read_capabilities=frozenset({ReadCapability.power, ReadCapability.energy}),
            write_capabilities=frozenset(),
        )

    async def send_command(self, _cmd: DeviceCommand) -> CommandResult:
        raise AssertionError("Grid meter has no write capability")


class _FailingThenHealthyBattery:
    """Like the grid meter, but for the battery role."""

    def __init__(self, *, failure_count: int) -> None:
        self.device_id = "bat-001"
        self._remaining_failures = failure_count
        self.send_command_calls: list[DeviceCommand] = []

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_state(self) -> BatteryState | DegradedDeviceState:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise TimeoutError("simulated battery unreachable")
        return BatteryState(
            device_id=self.device_id,
            soc_percent=75.0,
            battery_power_kw=0.0,
            capacity_kwh=10.0,
            operating_mode="normal",
            read_at=datetime.now(UTC),
        )

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        return DeviceCapabilityProfile(
            device_id=self.device_id,
            model="SimBattery",
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
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        command_max_retries=0,
        command_retry_backoff_seconds=0.0,
        watchdog_missed_cycle_threshold=2,
        watchdog_cycle_deadline_seconds=60.0,
    )


def _audit_observability() -> tuple[ObservabilityService, MagicMock]:
    """Real ObservabilityService with EventLogRepo mocked AND audit() spied."""
    repo = MagicMock(spec=EventLogRepo)
    repo.append = AsyncMock(return_value=None)
    obs = ObservabilityService(repo=repo)
    # Replace audit with a spy that records calls but still runs the underlying behaviour
    original_audit = obs.audit
    audit_calls: list[dict[str, object]] = []

    async def spy_audit(**kwargs: object) -> None:
        audit_calls.append(dict(kwargs))
        await original_audit(**kwargs)  # type: ignore[arg-type]

    obs.audit = spy_audit  # type: ignore[method-assign]
    spy_mock = MagicMock()
    spy_mock.calls = audit_calls
    return obs, spy_mock


# ── Test #33: full fail-safe lifecycle ────────────────────────────────────────


async def test_fail_safe_full_lifecycle() -> None:
    """AC12 #33: enter on degraded → stay → exit when all entry-degraded recover."""
    inverter = _SimulatedInverter()
    grid = _FailingThenHealthyGridMeter(failure_count=2)  # cycles 1+2 degraded
    battery = _FailingThenHealthyBattery(failure_count=2)  # cycles 1+2 degraded

    state_store = StateStore(system_clock_status="valid")
    settings = _settings()
    obs, spy = _audit_observability()
    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)

    adapters = {
        DeviceRole.inverter: inverter,
        DeviceRole.grid_meter: grid,
        DeviceRole.battery: battery,
    }
    policy_guard = PolicyGuard(
        state_store=state_store,
        adapters=adapters,
        observability=obs,
        settings=settings,
    )
    retry_policy = RetryPolicy(
        policy_guard=policy_guard,
        observability=obs,
        settings=settings,
    )

    energy_repo = MagicMock()
    energy_repo.get_current_monthly_peak_kw = AsyncMock(return_value=0.0)
    energy_repo.write_peak_interval = AsyncMock(return_value=None)

    loop = ControlLoop(
        state_store=state_store,
        adapters=adapters,
        energy_repo=energy_repo,
        settings=settings,
        intent_executor=IntentExecutor(),
        retry_policy=retry_policy,
        loop_liveness=liveness,
        observability=obs,
    )

    # Cycle 1: grid_meter + battery degraded → engine recommends fail_safe → enter
    await loop._tick()  # noqa: SLF001
    assert liveness.in_fail_safe is True
    entry_summaries = [c["summary"] for c in spy.calls if c["event_type"] == "SYSTEM"]
    assert any("Fail-safe entered" in s for s in entry_summaries)  # type: ignore[arg-type]
    assert battery.send_command_calls == []  # no dispatch in fail-safe

    audits_after_cycle1 = len(spy.calls)

    # Cycle 2: still degraded → stay; no new SYSTEM audit
    await loop._tick()  # noqa: SLF001
    assert liveness.in_fail_safe is True
    assert len(spy.calls) == audits_after_cycle1
    assert battery.send_command_calls == []

    # Cycle 3: grid_meter + battery healthy → engine recommends normal → exit
    await loop._tick()  # noqa: SLF001
    assert liveness.in_fail_safe is False
    exit_summaries = [c["summary"] for c in spy.calls if c["event_type"] == "SYSTEM"]
    assert any("Fail-safe exited" in s for s in exit_summaries)  # type: ignore[arg-type]
    # Exactly one entry + one exit audit emitted across the lifecycle
    fs_audits = [
        c
        for c in spy.calls
        if c["event_type"] == "SYSTEM"
        and (
            "Fail-safe entered" in c["summary"]  # type: ignore[operator]
            or "Fail-safe exited" in c["summary"]  # type: ignore[operator]
        )
    ]
    assert len(fs_audits) == 2


# ── Test #34: control loop crash ──────────────────────────────────────────────


async def test_control_loop_crash_emits_audit_and_marks_loop_liveness() -> None:
    """AC12 #34: _on_control_loop_done fires on crash → mark_crashed + SYSTEM audit.

    Wires the REAL ``make_on_control_loop_done`` factory from ``app.py`` so a
    drift in the production callback surfaces here rather than silently passing
    a copy-pasted reimplementation.
    """
    obs, spy = _audit_observability()
    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)
    on_control_loop_done = make_on_control_loop_done(liveness, obs)

    async def crashing_loop() -> None:
        raise RuntimeError("simulated tick failure")

    task = asyncio.create_task(crashing_loop(), name="control_loop")
    task.add_done_callback(on_control_loop_done)

    # Wait for the task to complete (raise) AND for the audit-emit task scheduled
    # in the done callback to run.
    try:
        await task
    except RuntimeError:
        pass
    # Yield to let the create_task(_emit_crash_audit()) run
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert liveness.crashed is True
    assert liveness.in_fail_safe is True
    assert liveness.crash_reason is not None
    assert "RuntimeError" in liveness.crash_reason

    crash_audits = [
        c
        for c in spy.calls
        if c["event_type"] == "SYSTEM" and "Control loop crashed" in c["summary"]  # type: ignore[operator]
    ]
    assert len(crash_audits) == 1
    assert "RuntimeError" in str(crash_audits[0]["summary"])
