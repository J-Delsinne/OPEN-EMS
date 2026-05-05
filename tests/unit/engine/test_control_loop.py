from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from open_ems.core import (
    DegradedDeviceState,
    DeviceRole,
    GridMeterState,
    InverterState,
    StateStore,
    SystemOperatingMode,
)
from open_ems.core.state import ClockStatus
from open_ems.engine import (
    EvaluationResult,
    IntentExecutor,
    PartialIntervalTracker,
    PolicyGuard,
)
from open_ems.engine.control_loop import ControlLoop
from open_ems.engine.rules.battery_control import BatteryIntent, BatteryIntentAction
from open_ems.engine.rules.ev_scheduling import EVChargerIntent, EVChargerIntentAction
from open_ems.settings import Settings

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


def _control_loop(
    *,
    adapters: dict[DeviceRole, Any] | None = None,
    energy_repo: AsyncMock | None = None,
    state_store: StateStore | None = None,
    intent_executor: IntentExecutor | None = None,
    policy_guard: PolicyGuard | MagicMock | None = None,
) -> ControlLoop:
    if energy_repo is None:
        energy_repo = AsyncMock()
        energy_repo.get_current_monthly_peak_kw = AsyncMock(return_value=0.0)
        energy_repo.write_peak_interval = AsyncMock()
    return ControlLoop(
        state_store=state_store or _state_store(),
        adapters=adapters or {},
        energy_repo=energy_repo,
        settings=_settings(),
        intent_executor=intent_executor or IntentExecutor(),
        policy_guard=policy_guard or MagicMock(spec=PolicyGuard),
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


async def test_handle_evaluation_result_logs_warning_when_command_not_applied() -> None:
    """ControlLoop logs 'command_not_applied' when PolicyGuard returns applied=False (AC6, AC8)."""
    from open_ems.core.commands import (
        CommandOrigin,
        CommandResult,
        CommandStatus,
        SetBatteryChargeRateCommand,
    )

    not_applied = CommandResult(
        correlation_id=uuid.uuid4(),
        device_id="bat-001",
        status=CommandStatus.failed,
        applied=False,
        reason="device_not_ready",
    )
    cmd = SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=2.0,
    )
    mock_guard = AsyncMock(spec=PolicyGuard)
    mock_guard.authorize_and_dispatch = AsyncMock(return_value=not_applied)
    mock_executor = MagicMock(spec=IntentExecutor)
    mock_executor.translate = MagicMock(return_value=[cmd])

    loop = _control_loop(intent_executor=mock_executor, policy_guard=mock_guard)
    snapshot = loop._state_store.get_snapshot()  # noqa: SLF001

    with patch("open_ems.engine.control_loop.logger") as mock_logger:
        await loop._handle_evaluation_result(_evaluation_result(), snapshot)  # noqa: SLF001

    mock_logger.warning.assert_called_once()
    call = mock_logger.warning.call_args
    assert call.args[0] == "command_not_applied"
    assert call.kwargs["device_id"] == "bat-001"
    assert call.kwargs["status"] == CommandStatus.failed.value
    assert call.kwargs["reason"] == "device_not_ready"


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
