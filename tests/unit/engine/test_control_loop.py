from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_ems.core import (
    DegradedDeviceState,
    DeviceRole,
    EnergyStrategy,
    GridMeterState,
    InverterState,
    StateStore,
    SystemOperatingMode,
)
from open_ems.core.commands import CommandResult, CommandStatus
from open_ems.core.state import ClockStatus
from open_ems.engine import (
    EvaluationResult,
    IntentExecutor,
    PartialIntervalTracker,
    RetryPolicy,
)
from open_ems.engine.control_loop import ControlLoop
from open_ems.engine.rules.battery_control import BatteryIntent, BatteryIntentAction
from open_ems.engine.rules.ev_scheduling import EVChargerIntent, EVChargerIntentAction
from open_ems.services.audit_log import ObservabilityService
from open_ems.services.loop_liveness import LoopLiveness
from open_ems.settings import Settings
from tests.fixtures.active_constraints import make_active_constraints_provider

_NOW = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _state_store(clock_status: ClockStatus = "valid") -> StateStore:
    return StateStore(system_clock_status=clock_status)


def _grid_meter_state(power_kw: float = 4.0, *, at: datetime = _NOW) -> GridMeterState:
    return GridMeterState(
        device_id="grid-001",
        grid_power_kw=power_kw,
        energy_delivered_kwh=100.0,
        energy_returned_kwh=10.0,
        received_at=at,
    )


def _inverter_state(*, at: datetime = _NOW) -> InverterState:
    return InverterState(
        device_id="inv-001",
        pv_power_kw=2.5,
        ac_power_kw=2.4,
        operating_mode="normal",
        read_at=at,
    )


def _evaluation_result(
    *, mode: SystemOperatingMode = SystemOperatingMode.normal
) -> EvaluationResult:
    intents = (
        BatteryIntent(
            action=BatteryIntentAction.hold,
            target_power_kw=None,
            reserve_floor_percent=20.0,
            reason_code="battery_unavailable",
            source_candidate=None,
        ),
        EVChargerIntent(
            action=EVChargerIntentAction.hold,
            target_charge_rate_kw=None,
            homeowner_override_active=False,
            reason_code="ev_charger_unavailable",
            source_candidate=None,
        ),
    )
    reasons = ("battery hold reason", "ev hold reason")
    return EvaluationResult(
        cycle_id=uuid.uuid4(),
        evaluated_at=_NOW,
        recommended_operating_mode=mode,
        intents=intents,
        decision_reasons=reasons,
        cycle_duration_ms=1,
    )


def _default_retry_policy_mock() -> MagicMock:
    rp = MagicMock(spec=RetryPolicy)
    rp.execute = AsyncMock(
        return_value=CommandResult(
            correlation_id=uuid.uuid4(),
            device_id="x",
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )
    )
    return rp


def _control_loop(
    *,
    adapters: dict[DeviceRole, Any] | None = None,
    energy_repo: AsyncMock | None = None,
    state_store: StateStore | None = None,
    intent_executor: IntentExecutor | None = None,
    retry_policy: RetryPolicy | MagicMock | None = None,
    loop_liveness: LoopLiveness | None = None,
    observability: ObservabilityService | MagicMock | None = None,
) -> ControlLoop:
    if energy_repo is None:
        energy_repo = AsyncMock()
        energy_repo.get_current_monthly_peak_kw = AsyncMock(return_value=0.0)
        energy_repo.write_peak_interval = AsyncMock()
    if loop_liveness is None:
        loop_liveness = LoopLiveness(
            missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0
        )
    if observability is None:
        observability = MagicMock(spec=ObservabilityService)
        observability.audit = AsyncMock(return_value=None)
    settings = _settings()
    return ControlLoop(
        state_store=state_store or _state_store(),
        adapters=adapters or {},
        energy_repo=energy_repo,
        settings=settings,
        intent_executor=intent_executor or IntentExecutor(),
        retry_policy=retry_policy or _default_retry_policy_mock(),
        loop_liveness=loop_liveness,
        observability=observability,
        active_constraints=make_active_constraints_provider(settings),
    )


def _set_tracker_to_boundary(loop: ControlLoop, interval_start: datetime) -> None:
    """Replace the loop's tracker with one starting at a known boundary."""
    loop._tracker = PartialIntervalTracker(interval_start)  # noqa: SLF001


async def test_poll_failure_converts_to_degraded_state() -> None:
    """Adapter raising during get_state() must produce DegradedDeviceState — never carry prior."""
    failing_adapter = MagicMock()
    failing_adapter.device_id = "grid-001"
    failing_adapter.get_state = AsyncMock(side_effect=TimeoutError("read timed out"))

    loop = _control_loop(adapters={DeviceRole.grid_meter: failing_adapter})

    result = await loop._poll_adapters()  # noqa: SLF001

    assert DeviceRole.grid_meter in result
    state = result[DeviceRole.grid_meter]
    assert isinstance(state, DegradedDeviceState)
    assert state.device_id == "grid-001"
    assert state.role is DeviceRole.grid_meter
    assert "read timed out" in state.reason


async def test_evaluation_input_reads_constraints_from_provider_not_settings() -> None:
    """AC10 #18: when the provider holds different values than ``Settings``,
    ``_build_evaluation_input`` must use the PROVIDER values."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from open_ems.core.constraints import ActiveConstraints
    from open_ems.services.active_constraints import ActiveConstraintsProvider
    from open_ems.storage.repositories.config_repo import ConfigRepo

    grid_adapter = MagicMock()
    grid_adapter.device_id = "grid-001"
    grid_adapter.get_state = AsyncMock(return_value=_grid_meter_state())
    inverter_adapter = MagicMock()
    inverter_adapter.device_id = "inv-001"
    inverter_adapter.get_state = AsyncMock(return_value=_inverter_state())

    settings = _settings()
    # Build a provider whose snapshot disagrees with Settings on both fields.
    provider = ActiveConstraintsProvider(repo=MagicMock(spec=ConfigRepo), settings=settings)
    provider._current = ActiveConstraints(  # noqa: SLF001
        peak_limit_kw=99.0,  # ≠ settings default
        battery_reserve_floor_percent=42.0,  # ≠ settings default
        config_version=7,
        activated_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
    )
    energy_repo = AsyncMock()
    energy_repo.get_current_monthly_peak_kw = AsyncMock(return_value=0.0)
    energy_repo.write_peak_interval = AsyncMock()
    loop = ControlLoop(
        state_store=_state_store(),
        adapters={
            DeviceRole.grid_meter: grid_adapter,
            DeviceRole.inverter: inverter_adapter,
        },
        energy_repo=energy_repo,
        settings=settings,
        intent_executor=IntentExecutor(),
        retry_policy=_default_retry_policy_mock(),
        loop_liveness=LoopLiveness(
            missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0
        ),
        observability=MagicMock(spec=ObservabilityService),
        active_constraints=provider,
    )
    interval_start = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    _set_tracker_to_boundary(loop, interval_start)
    now = datetime(2026, 5, 5, 12, 5, 0, tzinfo=UTC)

    device_states = await loop._poll_adapters()  # noqa: SLF001
    snapshot = await loop._state_store.publish(  # noqa: SLF001
        device_states, operating_mode=None
    )
    eval_input = loop._build_evaluation_input(snapshot, now)  # noqa: SLF001

    # PROVIDER values, not settings defaults.
    assert eval_input.peak_context.configured_peak_limit_kw == 99.0
    assert eval_input.battery_control.reserve_floor_percent == 42.0
    # And those are NOT the Settings defaults.
    assert eval_input.peak_context.configured_peak_limit_kw != settings.peak_limit_kw
    assert (
        eval_input.battery_control.reserve_floor_percent != settings.battery_reserve_floor_percent
    )


async def test_evaluation_input_constructed_from_fresh_snapshot() -> None:
    """EvaluationInput is built from the published snapshot's fields and current peak context."""
    grid_adapter = MagicMock()
    grid_adapter.device_id = "grid-001"
    grid_adapter.get_state = AsyncMock(return_value=_grid_meter_state(power_kw=3.5))
    inverter_adapter = MagicMock()
    inverter_adapter.device_id = "inv-001"
    inverter_adapter.get_state = AsyncMock(return_value=_inverter_state())

    loop = _control_loop(
        adapters={
            DeviceRole.grid_meter: grid_adapter,
            DeviceRole.inverter: inverter_adapter,
        }
    )
    interval_start = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    _set_tracker_to_boundary(loop, interval_start)
    now = datetime(2026, 5, 5, 12, 5, 0, tzinfo=UTC)

    device_states = await loop._poll_adapters()  # noqa: SLF001
    snapshot = await loop._state_store.publish(  # noqa: SLF001
        device_states, operating_mode=None
    )
    await loop._update_tracker(device_states, now)  # noqa: SLF001
    evaluation_input = loop._build_evaluation_input(snapshot, now)  # noqa: SLF001

    assert evaluation_input.grid_meter is snapshot.grid_meter
    assert evaluation_input.inverter is snapshot.inverter
    assert evaluation_input.peak_context.configured_peak_limit_kw == _settings().peak_limit_kw
    assert evaluation_input.peak_context.current_interval_start == interval_start
    assert (
        evaluation_input.battery_control.reserve_floor_percent
        == _settings().battery_reserve_floor_percent
    )
    assert evaluation_input.ev_scheduling.evaluated_at == now


async def test_finalize_before_evaluate_ordering() -> None:
    """write_peak_interval MUST be called before evaluate_cycle when a tick crosses a boundary."""
    grid_adapter = MagicMock()
    grid_adapter.device_id = "grid-001"
    grid_adapter.get_state = AsyncMock(return_value=_grid_meter_state(power_kw=2.0))
    inverter_adapter = MagicMock()
    inverter_adapter.device_id = "inv-001"
    inverter_adapter.get_state = AsyncMock(return_value=_inverter_state())

    loop = _control_loop(
        adapters={
            DeviceRole.grid_meter: grid_adapter,
            DeviceRole.inverter: inverter_adapter,
        }
    )
    interval_start = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    _set_tracker_to_boundary(loop, interval_start)

    call_order: list[str] = []
    loop._energy_repo.write_peak_interval.side_effect = (  # noqa: SLF001
        lambda *args, **kwargs: call_order.append("write_peak_interval")
    )

    def _spy_evaluate_cycle(_input: object) -> EvaluationResult:
        call_order.append("evaluate_cycle")
        return _evaluation_result()

    post_boundary = datetime(2026, 5, 5, 12, 15, 1, tzinfo=UTC)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return post_boundary

    with (
        patch("open_ems.engine.control_loop.evaluate_cycle", _spy_evaluate_cycle),
        patch("open_ems.engine.control_loop.datetime", _FrozenDatetime),
    ):
        await loop._tick()  # noqa: SLF001

    assert "write_peak_interval" in call_order
    assert "evaluate_cycle" in call_order
    assert call_order.index("write_peak_interval") < call_order.index("evaluate_cycle")


async def test_peak_intervals_data_quality_complete_when_samples_present() -> None:
    """Healthy meter + sample_count > 0 at boundary → write_peak_interval called with complete."""
    grid_adapter = MagicMock()
    grid_adapter.device_id = "grid-001"

    loop = _control_loop(adapters={DeviceRole.grid_meter: grid_adapter})
    interval_start = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    _set_tracker_to_boundary(loop, interval_start)

    # Accumulate a sample within the interval
    await loop._update_tracker(  # noqa: SLF001
        {DeviceRole.grid_meter: _grid_meter_state(power_kw=4.0)},
        datetime(2026, 5, 5, 12, 5, 0, tzinfo=UTC),
    )
    # Cross the boundary while meter is still healthy
    await loop._update_tracker(  # noqa: SLF001
        {DeviceRole.grid_meter: _grid_meter_state(power_kw=4.0)},
        datetime(2026, 5, 5, 12, 15, 1, tzinfo=UTC),
    )

    loop._energy_repo.write_peak_interval.assert_called_once()  # noqa: SLF001
    call_kwargs = loop._energy_repo.write_peak_interval.call_args.kwargs  # noqa: SLF001
    assert call_kwargs["data_quality"] == "complete"


async def test_peak_intervals_data_quality_incomplete_when_meter_degraded() -> None:
    """Degraded grid meter at the boundary → write_peak_interval called with incomplete."""
    loop = _control_loop()
    interval_start = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    _set_tracker_to_boundary(loop, interval_start)

    # Accumulate a healthy sample first so sample_count > 0 at boundary
    await loop._update_tracker(  # noqa: SLF001
        {DeviceRole.grid_meter: _grid_meter_state(power_kw=4.0)},
        datetime(2026, 5, 5, 12, 5, 0, tzinfo=UTC),
    )

    degraded = DegradedDeviceState(
        device_id="grid-001",
        role=DeviceRole.grid_meter,
        reason="adapter timeout",
        occurred_at=_NOW,
    )
    await loop._update_tracker(  # noqa: SLF001
        {DeviceRole.grid_meter: degraded},
        datetime(2026, 5, 5, 12, 15, 1, tzinfo=UTC),
    )

    loop._energy_repo.write_peak_interval.assert_called_once()  # noqa: SLF001
    call_kwargs = loop._energy_repo.write_peak_interval.call_args.kwargs  # noqa: SLF001
    assert call_kwargs["data_quality"] == "incomplete"


async def test_peak_intervals_not_skipped_in_degraded_mode() -> None:
    """operating_mode=degraded must not suppress peak_intervals persistence."""
    loop = _control_loop()
    loop._last_mode = SystemOperatingMode.degraded  # noqa: SLF001
    interval_start = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    _set_tracker_to_boundary(loop, interval_start)

    await loop._update_tracker(  # noqa: SLF001
        {DeviceRole.grid_meter: _grid_meter_state(power_kw=4.0)},
        datetime(2026, 5, 5, 12, 5, 0, tzinfo=UTC),
    )
    await loop._update_tracker(  # noqa: SLF001
        {DeviceRole.grid_meter: _grid_meter_state(power_kw=4.0)},
        datetime(2026, 5, 5, 12, 15, 1, tzinfo=UTC),
    )

    loop._energy_repo.write_peak_interval.assert_called_once()  # noqa: SLF001


async def test_control_loop_dispatches_via_retry_policy_not_policy_guard() -> None:
    """AC7 + AC10 #15: loop awaits RetryPolicy.execute per command; never calls PolicyGuard."""
    from open_ems.core.commands import CommandOrigin, SetBatteryChargeRateCommand
    from open_ems.engine import PolicyGuard

    cmd = SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=2.0,
    )
    success = CommandResult(
        correlation_id=cmd.correlation_id,
        device_id="bat-001",
        status=CommandStatus.success,
        applied=True,
        reason="ok",
    )
    mock_retry = MagicMock(spec=RetryPolicy)
    mock_retry.execute = AsyncMock(return_value=success)
    mock_guard = MagicMock(spec=PolicyGuard)
    mock_guard.authorize_and_dispatch = AsyncMock()
    mock_executor = MagicMock(spec=IntentExecutor)
    mock_executor.translate = MagicMock(return_value=[cmd])

    loop = _control_loop(intent_executor=mock_executor, retry_policy=mock_retry)
    snapshot = loop._state_store.get_snapshot()  # noqa: SLF001

    await loop._handle_evaluation_result(_evaluation_result(), snapshot)  # noqa: SLF001

    mock_retry.execute.assert_awaited_once_with(cmd)
    mock_guard.authorize_and_dispatch.assert_not_called()


async def test_peak_intervals_not_skipped_in_fail_safe_mode() -> None:
    """operating_mode=fail_safe must not suppress peak_intervals persistence."""
    loop = _control_loop()
    loop._last_mode = SystemOperatingMode.fail_safe  # noqa: SLF001
    interval_start = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    _set_tracker_to_boundary(loop, interval_start)

    await loop._update_tracker(  # noqa: SLF001
        {DeviceRole.grid_meter: _grid_meter_state(power_kw=4.0)},
        datetime(2026, 5, 5, 12, 5, 0, tzinfo=UTC),
    )
    await loop._update_tracker(  # noqa: SLF001
        {DeviceRole.grid_meter: _grid_meter_state(power_kw=4.0)},
        datetime(2026, 5, 5, 12, 15, 1, tzinfo=UTC),
    )

    loop._energy_repo.write_peak_interval.assert_called_once()  # noqa: SLF001


# ── Story 8.4: fail-safe lifecycle tests (AC4, AC5, AC10; AC12 #20-#28) ─────


def _battery_state(*, at: datetime = _NOW) -> object:
    from open_ems.core import BatteryState

    return BatteryState(
        device_id="bat-001",
        soc_percent=50.0,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="hold",
        read_at=at,
    )


async def _publish_snapshot_with_degraded(
    loop: ControlLoop,
    *,
    grid_degraded: bool,
    battery_degraded: bool = False,
) -> Any:
    """Build and publish a snapshot through the loop's StateStore."""
    states: dict[DeviceRole, Any] = {
        DeviceRole.inverter: _inverter_state(),
    }
    if grid_degraded:
        states[DeviceRole.grid_meter] = DegradedDeviceState(
            device_id="grid-001",
            role=DeviceRole.grid_meter,
            reason="adapter timeout",
            occurred_at=_NOW,
        )
    else:
        states[DeviceRole.grid_meter] = _grid_meter_state()
    if battery_degraded:
        states[DeviceRole.battery] = DegradedDeviceState(
            device_id="bat-001",
            role=DeviceRole.battery,
            reason="adapter timeout",
            occurred_at=_NOW,
        )
    return await loop._state_store.publish(states, operating_mode=None)  # noqa: SLF001


async def test_fail_safe_entry_emits_audit_and_skips_dispatch() -> None:
    """AC12 #20: fail_safe mode + non-empty intents → no dispatch, audit emitted."""
    from open_ems.core.commands import CommandOrigin, SetBatteryChargeRateCommand

    cmd = SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=2.0,
    )
    mock_executor = MagicMock(spec=IntentExecutor)
    mock_executor.translate = MagicMock(return_value=[cmd])
    mock_retry = MagicMock(spec=RetryPolicy)
    mock_retry.execute = AsyncMock()
    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)
    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)

    loop = _control_loop(
        intent_executor=mock_executor,
        retry_policy=mock_retry,
        observability=observability,
        loop_liveness=liveness,
    )
    snapshot = await _publish_snapshot_with_degraded(loop, grid_degraded=True)

    await loop._handle_evaluation_result(  # noqa: SLF001
        _evaluation_result(mode=SystemOperatingMode.fail_safe), snapshot
    )

    mock_executor.translate.assert_not_called()
    mock_retry.execute.assert_not_called()
    assert observability.audit.await_count == 1
    call = observability.audit.await_args
    assert call.kwargs["event_type"] == "SYSTEM"
    assert "Fail-safe entered" in call.kwargs["summary"]
    assert liveness.in_fail_safe is True


async def test_fail_safe_consecutive_cycles_no_repeat_audit() -> None:
    """AC12 #21: two consecutive fail_safe cycles → audit emitted exactly once (entry only)."""
    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)
    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)

    loop = _control_loop(observability=observability, loop_liveness=liveness)
    snapshot = await _publish_snapshot_with_degraded(loop, grid_degraded=True)

    await loop._handle_evaluation_result(  # noqa: SLF001
        _evaluation_result(mode=SystemOperatingMode.fail_safe), snapshot
    )
    await loop._handle_evaluation_result(  # noqa: SLF001
        _evaluation_result(mode=SystemOperatingMode.fail_safe), snapshot
    )

    assert observability.audit.await_count == 1


async def test_fail_safe_summary_lists_degraded_roles() -> None:
    """AC12 #22: degraded grid_meter and battery → audit summary contains both role names."""
    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)

    loop = _control_loop(observability=observability)
    snapshot = await _publish_snapshot_with_degraded(
        loop, grid_degraded=True, battery_degraded=True
    )

    await loop._handle_evaluation_result(  # noqa: SLF001
        _evaluation_result(mode=SystemOperatingMode.fail_safe), snapshot
    )

    summary = observability.audit.await_args.kwargs["summary"]
    assert "grid_meter" in summary
    assert "battery" in summary


async def test_fail_safe_exit_requires_recommendation_and_recovery() -> None:
    """AC12 #23: stay while degraded; exit + dispatch only when both engine recommends normal AND device recovered."""  # noqa: E501
    from open_ems.core.commands import CommandOrigin, SetBatteryChargeRateCommand

    cmd = SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=2.0,
    )
    mock_executor = MagicMock(spec=IntentExecutor)
    mock_executor.translate = MagicMock(return_value=[cmd])
    mock_retry = MagicMock(spec=RetryPolicy)
    mock_retry.execute = AsyncMock(
        return_value=CommandResult(
            correlation_id=cmd.correlation_id,
            device_id="bat-001",
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )
    )
    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)
    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)

    loop = _control_loop(
        intent_executor=mock_executor,
        retry_policy=mock_retry,
        observability=observability,
        loop_liveness=liveness,
    )

    # Cycle 1: degraded grid_meter, mode=fail_safe → enter fail-safe
    snap1 = await _publish_snapshot_with_degraded(loop, grid_degraded=True)
    await loop._handle_evaluation_result(  # noqa: SLF001
        _evaluation_result(mode=SystemOperatingMode.fail_safe), snap1
    )
    assert liveness.in_fail_safe is True
    assert observability.audit.await_count == 1  # entry audit

    # Cycle 2: still degraded but engine now says mode=normal → STAY in fail-safe; no dispatch
    snap2 = await _publish_snapshot_with_degraded(loop, grid_degraded=True)
    await loop._handle_evaluation_result(  # noqa: SLF001
        _evaluation_result(mode=SystemOperatingMode.normal), snap2
    )
    assert liveness.in_fail_safe is True
    assert observability.audit.await_count == 1  # no exit audit
    mock_retry.execute.assert_not_called()

    # Cycle 3: grid_meter recovered + mode=normal → exit fail-safe, dispatch on this cycle
    snap3 = await _publish_snapshot_with_degraded(loop, grid_degraded=False)
    await loop._handle_evaluation_result(  # noqa: SLF001
        _evaluation_result(mode=SystemOperatingMode.normal), snap3
    )
    assert liveness.in_fail_safe is False
    assert observability.audit.await_count == 2  # entry + exit
    exit_summary = observability.audit.await_args.kwargs["summary"]
    assert "Fail-safe exited" in exit_summary
    mock_retry.execute.assert_awaited_once_with(cmd)


async def test_fail_safe_exit_audit_lists_recovered_devices() -> None:
    """AC12 #24: exit summary contains the originally-degraded role names."""
    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)
    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)

    loop = _control_loop(observability=observability, loop_liveness=liveness)

    # Enter fail-safe with grid_meter and battery degraded
    snap_in = await _publish_snapshot_with_degraded(loop, grid_degraded=True, battery_degraded=True)
    await loop._handle_evaluation_result(  # noqa: SLF001
        _evaluation_result(mode=SystemOperatingMode.fail_safe), snap_in
    )

    # Both recovered + engine recommends normal → exit
    snap_out = await _publish_snapshot_with_degraded(loop, grid_degraded=False)
    # battery slot still None on this snapshot; the entry-degraded battery role is not yet healthy.
    # Adjust: include healthy battery via an explicit publish.
    healthy_states: dict[DeviceRole, Any] = {
        DeviceRole.inverter: _inverter_state(),
        DeviceRole.grid_meter: _grid_meter_state(),
        DeviceRole.battery: _battery_state(),
    }
    snap_out = await loop._state_store.publish(healthy_states, operating_mode=None)  # noqa: SLF001

    exit_result = _evaluation_result(mode=SystemOperatingMode.normal)
    await loop._handle_evaluation_result(exit_result, snap_out)  # noqa: SLF001

    # Last audit call is the exit
    exit_call = observability.audit.await_args
    summary = exit_call.kwargs["summary"]
    assert "Fail-safe exited" in summary
    assert "grid_meter" in summary
    assert "battery" in summary
    assert str(exit_result.cycle_id) in summary


async def test_fail_safe_does_not_exit_while_engine_still_recommends_fail_safe() -> None:
    """AC12 #25: entry-degraded recovered, but engine still says fail_safe → STAY."""
    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)
    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)

    loop = _control_loop(observability=observability, loop_liveness=liveness)

    # Enter fail-safe
    snap_in = await _publish_snapshot_with_degraded(loop, grid_degraded=True)
    await loop._handle_evaluation_result(  # noqa: SLF001
        _evaluation_result(mode=SystemOperatingMode.fail_safe), snap_in
    )
    assert liveness.in_fail_safe is True

    # grid_meter is healthy, but engine still says fail_safe
    snap_healthy = await _publish_snapshot_with_degraded(loop, grid_degraded=False)
    await loop._handle_evaluation_result(  # noqa: SLF001
        _evaluation_result(mode=SystemOperatingMode.fail_safe), snap_healthy
    )

    assert liveness.in_fail_safe is True
    # Only entry audit so far (the second fail_safe cycle is a no-op for audits)
    assert observability.audit.await_count == 1


async def test_cycle_completion_marked_after_successful_tick() -> None:
    """AC12 #26: successful _tick → mark_cycle_complete called once."""
    grid_adapter = MagicMock()
    grid_adapter.device_id = "grid-001"
    grid_adapter.get_state = AsyncMock(return_value=_grid_meter_state())
    inverter_adapter = MagicMock()
    inverter_adapter.device_id = "inv-001"
    inverter_adapter.get_state = AsyncMock(return_value=_inverter_state())

    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)
    spy_mark = MagicMock(side_effect=liveness.mark_cycle_complete)
    liveness.mark_cycle_complete = spy_mark  # type: ignore[method-assign]

    loop = _control_loop(
        adapters={
            DeviceRole.grid_meter: grid_adapter,
            DeviceRole.inverter: inverter_adapter,
        },
        loop_liveness=liveness,
    )

    await loop._tick()  # noqa: SLF001

    spy_mark.assert_called_once()


async def test_cycle_completion_marked_for_fail_safe_cycle() -> None:
    """AC12 #27: fail-safe cycle still marks completion."""
    grid_adapter = MagicMock()
    grid_adapter.device_id = "grid-001"
    grid_adapter.get_state = AsyncMock(return_value=_grid_meter_state())
    inverter_adapter = MagicMock()
    inverter_adapter.device_id = "inv-001"
    inverter_adapter.get_state = AsyncMock(return_value=_inverter_state())

    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)
    spy_mark = MagicMock(side_effect=liveness.mark_cycle_complete)
    liveness.mark_cycle_complete = spy_mark  # type: ignore[method-assign]

    observability = MagicMock(spec=ObservabilityService)
    observability.audit = AsyncMock(return_value=None)

    def _fail_safe_evaluate(_input: object) -> EvaluationResult:
        return _evaluation_result(mode=SystemOperatingMode.fail_safe)

    loop = _control_loop(
        adapters={
            DeviceRole.grid_meter: grid_adapter,
            DeviceRole.inverter: inverter_adapter,
        },
        loop_liveness=liveness,
        observability=observability,
    )

    with patch("open_ems.engine.control_loop.evaluate_cycle", _fail_safe_evaluate):
        await loop._tick()  # noqa: SLF001

    spy_mark.assert_called_once()
    assert liveness.in_fail_safe is True


async def test_cycle_completion_NOT_marked_when_slots_missing() -> None:
    """AC12 #28: missing-slots early return → mark_cycle_complete NOT called."""
    # No adapters → snapshot has no inverter and no grid_meter → ValidationError → early return
    liveness = LoopLiveness(missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0)
    spy_mark = MagicMock()
    liveness.mark_cycle_complete = spy_mark  # type: ignore[method-assign]

    loop = _control_loop(loop_liveness=liveness)

    await loop._tick()  # noqa: SLF001

    spy_mark.assert_not_called()


# ── Story 9.0d: initial_monthly_peak_kw seed (AC2, AC7) ────────────────────


async def test_initial_monthly_peak_kw_seeds_evaluation_input() -> None:
    """AC7 #1: seed value reaches PeakContext on the very first cycle, before any rollover."""
    grid_adapter = MagicMock()
    grid_adapter.device_id = "grid-001"
    grid_adapter.get_state = AsyncMock(return_value=_grid_meter_state())
    inverter_adapter = MagicMock()
    inverter_adapter.device_id = "inv-001"
    inverter_adapter.get_state = AsyncMock(return_value=_inverter_state())

    energy_repo = AsyncMock()
    energy_repo.get_current_monthly_peak_kw = AsyncMock(return_value=0.0)
    energy_repo.write_peak_interval = AsyncMock()
    settings = _settings()
    loop = ControlLoop(
        state_store=_state_store(),
        adapters={
            DeviceRole.grid_meter: grid_adapter,
            DeviceRole.inverter: inverter_adapter,
        },
        energy_repo=energy_repo,
        settings=settings,
        intent_executor=IntentExecutor(),
        retry_policy=_default_retry_policy_mock(),
        loop_liveness=LoopLiveness(
            missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0
        ),
        observability=MagicMock(spec=ObservabilityService),
        active_constraints=make_active_constraints_provider(settings),
        initial_monthly_peak_kw=14.7,
    )
    interval_start = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    _set_tracker_to_boundary(loop, interval_start)
    now = datetime(2026, 5, 5, 12, 5, 0, tzinfo=UTC)

    device_states = await loop._poll_adapters()  # noqa: SLF001
    snapshot = await loop._state_store.publish(  # noqa: SLF001
        device_states, operating_mode=None
    )
    # Build evaluation input WITHOUT calling _update_tracker first — proves the
    # seed value drives the very first cycle, before any rollover refresh.
    evaluation_input = loop._build_evaluation_input(snapshot, now)  # noqa: SLF001

    assert evaluation_input.peak_context.current_monthly_recorded_peak_kw == 14.7
    # And the rollover-refresh path was NOT exercised on this read.
    energy_repo.get_current_monthly_peak_kw.assert_not_called()


def test_initial_monthly_peak_kw_default_is_zero() -> None:
    """AC7 #2: omitting the argument keeps the legacy default (0.0); fixtures stay diff-free."""
    loop = _control_loop()
    assert loop._monthly_peak_kw == 0.0  # noqa: SLF001


@pytest.mark.parametrize(
    "bogus_value",
    [
        pytest.param(-0.001, id="slightly_negative"),
        pytest.param(-1.0, id="negative_one"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive_infinity"),
        pytest.param(float("-inf"), id="negative_infinity"),
    ],
)
def test_initial_monthly_peak_kw_rejects_negative_and_nan(bogus_value: float) -> None:
    """AC2 + AC7 #3: constructor rejects non-finite or negative seed with ValueError."""
    # Sanity: each bogus value really is bogus by the predicate the validator uses.
    assert bogus_value < 0.0 or not math.isfinite(bogus_value)
    with pytest.raises(ValueError, match="initial_monthly_peak_kw"):
        _control_loop_with_seed(bogus_value)


@pytest.mark.parametrize(
    "strategy",
    [
        EnergyStrategy.minimize_cost,
        EnergyStrategy.maximize_self_consumption,
        EnergyStrategy.prioritize_ev,
    ],
)
async def test_evaluation_input_strategy_reads_from_snapshot_active_strategy(
    strategy: EnergyStrategy,
) -> None:
    """Story 10.1 AC3 / AC17: ``_build_evaluation_input`` MUST source ``strategy``
    from ``snapshot.active_strategy`` rather than a hardcoded enum value. Each
    EnergyStrategy variant is exercised end-to-end through the StateStore →
    ControlLoop boundary."""
    grid_adapter = MagicMock()
    grid_adapter.device_id = "grid-001"
    grid_adapter.get_state = AsyncMock(return_value=_grid_meter_state())
    inverter_adapter = MagicMock()
    inverter_adapter.device_id = "inv-001"
    inverter_adapter.get_state = AsyncMock(return_value=_inverter_state())
    store = StateStore(system_clock_status="valid", active_strategy=strategy)
    loop = _control_loop(
        state_store=store,
        adapters={
            DeviceRole.grid_meter: grid_adapter,
            DeviceRole.inverter: inverter_adapter,
        },
    )
    interval_start = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    _set_tracker_to_boundary(loop, interval_start)

    device_states = await loop._poll_adapters()  # noqa: SLF001
    snapshot = await store.publish(device_states, operating_mode=None)
    evaluation_input = loop._build_evaluation_input(  # noqa: SLF001
        snapshot, datetime(2026, 5, 5, 12, 5, 0, tzinfo=UTC)
    )

    assert evaluation_input.strategy is strategy


def _control_loop_with_seed(seed: float) -> ControlLoop:
    """Construct a ControlLoop with a specified initial_monthly_peak_kw seed.

    Mirrors ``_control_loop()`` defaults; only the seed differs. Used by the
    AC7 #3 parametrized validator test so each bogus value triggers the
    constructor validator rather than a fixture-construction error.
    """
    energy_repo = AsyncMock()
    energy_repo.get_current_monthly_peak_kw = AsyncMock(return_value=0.0)
    energy_repo.write_peak_interval = AsyncMock()
    settings = _settings()
    return ControlLoop(
        state_store=_state_store(),
        adapters={},
        energy_repo=energy_repo,
        settings=settings,
        intent_executor=IntentExecutor(),
        retry_policy=_default_retry_policy_mock(),
        loop_liveness=LoopLiveness(
            missed_cycle_threshold_seconds=20.0, cycle_deadline_seconds=60.0
        ),
        observability=MagicMock(spec=ObservabilityService),
        active_constraints=make_active_constraints_provider(settings),
        initial_monthly_peak_kw=seed,
    )
