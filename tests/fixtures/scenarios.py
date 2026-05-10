"""Scenario fixture for end-to-end decision-engine simulation tests (Story 8.5 AC2, AC3).

This module is the *fixture layer*: it owns the ``SimulationScenario`` dataclass,
the four ``_Stub<Role>Adapter`` classes implementing the ``DeviceAdapter``
protocol, and four builder helpers that compose scenario state into the
production engine inputs (``StateStore``, adapters map, ``Settings``,
``EvaluationInput``). The actual end-to-end runner lives in
``tests/integration/engine/test_decision_engine_simulation.py``.

Design constraints (from Story 8.5 AC2 / Dev Notes):

* No database access. No real protocol libraries (``pymodbus``, ``ocpp``,
  ``dsmr_parser``). No ``EnergyRepo``. No ``PartialIntervalTracker``.
* ``command_max_retries=0`` so success/failure semantics aren't muddied by
  retry behaviour.
* ``PeakContext`` is constructed directly from scenario parameters — the
  partial-interval tracker's projection logic is tested independently.
* Capability profiles attached to ``BatteryControlContext`` /
  ``EVSchedulingContext`` MUST share the adapter's ``device_id`` (the engine's
  capability checks compare ``profile.device_id`` against the device state's
  ``device_id``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from open_ems.core import (
    BatteryState,
    DeviceAdapter,
    DeviceRole,
    EnergyStrategy,
    EVChargerState,
    GridMeterState,
    InverterState,
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
    DeviceState,
    ReadCapability,
    WriteCapability,
)
from open_ems.core.state import SystemSnapshot
from open_ems.engine.models import (
    BatteryControlContext,
    EvaluationInput,
    EVChargingWindow,
    EVSchedulingContext,
    PeakContext,
)
from open_ems.settings import Settings

_NOW: datetime = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)

_INVERTER_ID = "inv-001"
_BATTERY_ID = "bat-001"
_EV_ID = "ev-001"
_GRID_ID = "grid-001"


# ── SimulationScenario dataclass ──────────────────────────────────────────────


@dataclass(frozen=True)
class SimulationScenario:
    """Parameterisable scenario for end-to-end decision-engine simulation."""

    name: str
    grid_power_kw: float
    pv_power_kw: float
    inverter_ac_power_kw: float
    battery_soc_percent: float
    battery_capacity_kwh: float
    ev_session_active: bool
    ev_current_power_kw: float | None
    strategy: EnergyStrategy
    peak_limit_kw: float
    battery_reserve_floor_percent: float
    homeowner_override_active: bool = False
    ev_charging_window: EVChargingWindow | None = None
    target_ev_charge_rate_kw: float | None = None


# ── Capability profile helpers ────────────────────────────────────────────────


def _inverter_default_capability_profile(device_id: str) -> DeviceCapabilityProfile:
    return DeviceCapabilityProfile(
        device_id=device_id,
        model="StubInverter",
        read_capabilities=frozenset({ReadCapability.state, ReadCapability.power}),
        write_capabilities=frozenset(),
    )


def _battery_default_capability_profile(device_id: str) -> DeviceCapabilityProfile:
    return DeviceCapabilityProfile(
        device_id=device_id,
        model="StubBattery",
        read_capabilities=frozenset({ReadCapability.state, ReadCapability.soc}),
        write_capabilities=frozenset(
            {WriteCapability.set_charge_rate, WriteCapability.set_discharge_rate}
        ),
    )


def _ev_charger_default_capability_profile(device_id: str) -> DeviceCapabilityProfile:
    return DeviceCapabilityProfile(
        device_id=device_id,
        model="StubEVCharger",
        read_capabilities=frozenset({ReadCapability.state}),
        write_capabilities=frozenset({WriteCapability.set_ev_charge_current}),
    )


def _grid_meter_default_capability_profile(device_id: str) -> DeviceCapabilityProfile:
    return DeviceCapabilityProfile(
        device_id=device_id,
        model="StubGridMeter",
        read_capabilities=frozenset({ReadCapability.power, ReadCapability.energy}),
        write_capabilities=frozenset(),
    )


# ── Stub adapters (one per device role) ───────────────────────────────────────


class _StubInverterAdapter:
    """Stub inverter adapter — read-only by design; ``send_command`` raises."""

    def __init__(self, scenario: SimulationScenario, *, at: datetime) -> None:
        self.device_id = _INVERTER_ID
        self._scenario = scenario
        self._at = at
        self.send_command_calls: list[DeviceCommand] = []

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_state(self) -> DeviceState | DegradedDeviceState:
        return InverterState(
            device_id=self.device_id,
            pv_power_kw=self._scenario.pv_power_kw,
            ac_power_kw=self._scenario.inverter_ac_power_kw,
            operating_mode="normal",
            read_at=self._at,
        )

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        return _inverter_default_capability_profile(self.device_id)

    async def send_command(self, _cmd: DeviceCommand) -> CommandResult:
        raise AssertionError("inverter has no write capability")


class _StubBatteryAdapter:
    """Stub battery adapter — records every dispatched command and returns success."""

    def __init__(self, scenario: SimulationScenario, *, at: datetime) -> None:
        self.device_id = _BATTERY_ID
        self._scenario = scenario
        self._at = at
        # NB: instance-level list (not class-level) to avoid mutable-default trap.
        self.send_command_calls: list[DeviceCommand] = []

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_state(self) -> DeviceState | DegradedDeviceState:
        return BatteryState(
            device_id=self.device_id,
            soc_percent=self._scenario.battery_soc_percent,
            battery_power_kw=0.0,
            capacity_kwh=self._scenario.battery_capacity_kwh,
            operating_mode="normal",
            read_at=self._at,
        )

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        return _battery_default_capability_profile(self.device_id)

    async def send_command(self, cmd: DeviceCommand) -> CommandResult:
        assert cmd.device_id == self.device_id, (
            f"battery stub received command for wrong device_id: "
            f"cmd={cmd.device_id!r} adapter={self.device_id!r}"
        )
        self.send_command_calls.append(cmd)
        return CommandResult(
            correlation_id=cmd.correlation_id,
            device_id=self.device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )


class _StubEVChargerAdapter:
    """Stub EV charger adapter — records every dispatched command and returns success."""

    def __init__(self, scenario: SimulationScenario, *, at: datetime) -> None:
        self.device_id = _EV_ID
        self._scenario = scenario
        self._at = at
        self.send_command_calls: list[DeviceCommand] = []

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_state(self) -> DeviceState | DegradedDeviceState:
        active = self._scenario.ev_session_active
        return EVChargerState(
            device_id=self.device_id,
            status="charging" if active else "available",
            session_active=active,
            current_power_kw=self._scenario.ev_current_power_kw,
            power_source="meter_values" if active else None,
            power_measured_at=self._at if active else None,
            read_at=self._at,
        )

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        return _ev_charger_default_capability_profile(self.device_id)

    async def send_command(self, cmd: DeviceCommand) -> CommandResult:
        assert cmd.device_id == self.device_id, (
            f"ev_charger stub received command for wrong device_id: "
            f"cmd={cmd.device_id!r} adapter={self.device_id!r}"
        )
        self.send_command_calls.append(cmd)
        return CommandResult(
            correlation_id=cmd.correlation_id,
            device_id=self.device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )


class _StubGridMeterAdapter:
    """Stub grid meter adapter — read-only by design; ``send_command`` raises."""

    def __init__(self, scenario: SimulationScenario, *, at: datetime) -> None:
        self.device_id = _GRID_ID
        self._scenario = scenario
        self._at = at
        self.send_command_calls: list[DeviceCommand] = []

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_state(self) -> DeviceState | DegradedDeviceState:
        return GridMeterState(
            device_id=self.device_id,
            grid_power_kw=self._scenario.grid_power_kw,
            energy_delivered_kwh=0.0,
            energy_returned_kwh=0.0,
            received_at=self._at,
        )

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        return _grid_meter_default_capability_profile(self.device_id)

    async def send_command(self, _cmd: DeviceCommand) -> CommandResult:
        raise AssertionError("grid_meter has no write capability")


# ── 15-minute floor helper (mirrors partial_interval_tracker._floor_to_15_min) ─


def _floor_15min(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() != timedelta(0):
        raise ValueError("at must be a UTC datetime")
    return at.replace(minute=(at.minute // 15) * 15, second=0, microsecond=0)


# ── Builder helpers ───────────────────────────────────────────────────────────


async def build_scenario_state_store(
    scenario: SimulationScenario, *, at: datetime = _NOW
) -> StateStore:
    """Build a ``StateStore`` with the four scenario device states published.

    Returns the live store after a single ``publish()`` call so callers can
    snapshot it directly.
    """
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    inverter_state = InverterState(
        device_id=_INVERTER_ID,
        pv_power_kw=scenario.pv_power_kw,
        ac_power_kw=scenario.inverter_ac_power_kw,
        operating_mode="normal",
        read_at=at,
    )
    battery_state = BatteryState(
        device_id=_BATTERY_ID,
        soc_percent=scenario.battery_soc_percent,
        battery_power_kw=0.0,
        capacity_kwh=scenario.battery_capacity_kwh,
        operating_mode="normal",
        read_at=at,
    )
    ev_state = EVChargerState(
        device_id=_EV_ID,
        status="charging" if scenario.ev_session_active else "available",
        session_active=scenario.ev_session_active,
        current_power_kw=scenario.ev_current_power_kw,
        power_source="meter_values" if scenario.ev_session_active else None,
        power_measured_at=at if scenario.ev_session_active else None,
        read_at=at,
    )
    grid_state = GridMeterState(
        device_id=_GRID_ID,
        grid_power_kw=scenario.grid_power_kw,
        energy_delivered_kwh=0.0,
        energy_returned_kwh=0.0,
        received_at=at,
    )
    await store.publish(
        {
            DeviceRole.inverter: inverter_state,
            DeviceRole.battery: battery_state,
            DeviceRole.ev_charger: ev_state,
            DeviceRole.grid_meter: grid_state,
        },
        operating_mode=SystemOperatingMode.normal,
    )
    return store


def build_scenario_adapters(
    scenario: SimulationScenario, *, at: datetime = _NOW
) -> dict[DeviceRole, DeviceAdapter]:
    """Return a complete ``DeviceRole → DeviceAdapter`` mapping for the scenario."""
    return {
        DeviceRole.inverter: _StubInverterAdapter(scenario, at=at),
        DeviceRole.battery: _StubBatteryAdapter(scenario, at=at),
        DeviceRole.ev_charger: _StubEVChargerAdapter(scenario, at=at),
        DeviceRole.grid_meter: _StubGridMeterAdapter(scenario, at=at),
    }


def build_scenario_settings(scenario: SimulationScenario) -> Settings:
    """Return ``Settings`` with scenario peak/reserve overrides and retries disabled."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        peak_limit_kw=scenario.peak_limit_kw,
        battery_reserve_floor_percent=scenario.battery_reserve_floor_percent,
        command_max_retries=0,
        command_retry_backoff_seconds=0.0,
    )


def build_scenario_evaluation_input(
    scenario: SimulationScenario,
    snapshot: SystemSnapshot,
    *,
    at: datetime = _NOW,
) -> EvaluationInput:
    """Build a complete ``EvaluationInput`` from scenario parameters and a snapshot.

    ``PeakContext`` is constructed directly from scenario parameters (the
    partial-interval tracker is intentionally bypassed — see Story 8.5 dev notes).
    """
    ev_load_kw = scenario.ev_current_power_kw if scenario.ev_current_power_kw is not None else 0.0
    projection_kw = scenario.grid_power_kw + ev_load_kw
    peak_context = PeakContext(
        current_partial_window_projection_kw=projection_kw,
        configured_peak_limit_kw=scenario.peak_limit_kw,
        current_monthly_recorded_peak_kw=0.0,
        current_interval_start=_floor_15min(at),
        current_interval_elapsed_seconds=0,
    )
    battery_control = BatteryControlContext(
        reserve_floor_percent=scenario.battery_reserve_floor_percent,
        capability_profile=_battery_default_capability_profile(_BATTERY_ID),
    )
    ev_scheduling = EVSchedulingContext(
        capability_profile=_ev_charger_default_capability_profile(_EV_ID),
        charging_window=scenario.ev_charging_window,
        homeowner_override_active=scenario.homeowner_override_active,
        evaluated_at=at,
        target_charge_rate_kw=scenario.target_ev_charge_rate_kw,
    )
    return EvaluationInput.from_snapshot(
        snapshot,
        peak_context=peak_context,
        strategy=scenario.strategy,
        battery_control=battery_control,
        ev_scheduling=ev_scheduling,
    )
