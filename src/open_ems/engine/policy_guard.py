"""PolicyGuard: mandatory safety gate for every device command (AR15, FR14).

Stage 2 of the two-stage execution pipeline (Story 8.2). Every command produced
by ``IntentExecutor`` (or any other future origin) must be authorized here
before any adapter ``send_command()`` is invoked. PolicyGuard:

- rejects all commands when ``SystemOperatingMode.fail_safe`` is active,
- enforces the declared device capability profile (no write without capability),
- enforces safety constraints (battery reserve floor, peak limit bounds,
  conservative-mode load suppression) — Story 9.0b: the two safety values
  are read via ``ActiveConstraintsProvider``, not ``Settings``,
- wraps adapter exceptions and timeouts as typed ``CommandResult`` values so
  raw exceptions never propagate to the control loop (AR16),
- emits a ``CONSTRAINT`` audit event for every rejection (mandatory).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import structlog

from open_ems.core import (
    BatteryState,
    DeviceAdapter,
    DeviceRole,
    StateStore,
    SystemOperatingMode,
)
from open_ems.core.commands import (
    CommandResult,
    CommandStatus,
    DeviceCommand,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.core.devices import WriteCapability
from open_ems.core.state import SystemSnapshot
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings

COMMAND_DISPATCH_TIMEOUT_SECONDS: float = 10.0
CAPABILITY_CHECK_TIMEOUT_SECONDS: float = 5.0

logger = structlog.get_logger(__name__)


class PolicyGuard:
    """Authorize and dispatch device commands; the only legal call site for ``send_command()``."""

    def __init__(
        self,
        *,
        state_store: StateStore,
        adapters: Mapping[DeviceRole, DeviceAdapter],
        observability: ObservabilityService,
        settings: Settings,
        active_constraints: ActiveConstraintsProvider,
    ) -> None:
        self._state_store = state_store
        self._adapters = adapters
        self._observability = observability
        self._settings = settings
        self._active_constraints = active_constraints

    async def authorize_and_dispatch(self, command: DeviceCommand) -> CommandResult:
        snapshot = self._state_store.get_snapshot()

        if snapshot.operating_mode is SystemOperatingMode.fail_safe:
            return await self._reject(command, "fail_safe_mode_active")

        adapter = self._adapters.get(command.device_role)
        if adapter is None:
            return await self._reject(command, "adapter_not_registered")

        try:
            profile = await asyncio.wait_for(
                adapter.get_capabilities(), timeout=CAPABILITY_CHECK_TIMEOUT_SECONDS
            )
        except Exception:  # noqa: BLE001 — capability lookup must not propagate
            logger.warning(
                "capability_check_failed",
                device_id=command.device_id,
                device_role=command.device_role.value,
                component="engine",
            )
            return await self._reject(command, "capability_check_failed")

        try:
            required_capability = _required_write_capability(command)
        except TypeError:
            logger.warning(
                "unknown_command_type",
                device_id=command.device_id,
                command_type=type(command).__name__,
                component="engine",
            )
            return await self._reject(command, "unknown_command_type")
        if required_capability not in profile.write_capabilities:
            return await self._reject(command, f"capability_missing: {required_capability.value}")

        constraint_violation = self._check_safety_constraints(command, snapshot)
        if constraint_violation is not None:
            return await self._reject(command, constraint_violation)

        try:
            return await asyncio.wait_for(
                adapter.send_command(command),
                timeout=COMMAND_DISPATCH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "command_dispatch_timeout",
                device_id=command.device_id,
                device_role=command.device_role.value,
                component="engine",
            )
            return CommandResult(
                correlation_id=command.correlation_id,
                device_id=command.device_id,
                status=CommandStatus.timeout,
                applied=False,
                reason="command_timeout",
            )
        except Exception as exc:  # noqa: BLE001 — AR16 mandates no exception propagation
            logger.warning(
                "command_dispatch_failed",
                device_id=command.device_id,
                device_role=command.device_role.value,
                error=repr(exc),
                component="engine",
            )
            return CommandResult(
                correlation_id=command.correlation_id,
                device_id=command.device_id,
                status=CommandStatus.failed,
                applied=False,
                reason=repr(exc),
            )

    def _check_safety_constraints(
        self,
        command: DeviceCommand,
        snapshot: SystemSnapshot,
    ) -> str | None:
        constraints = self._active_constraints.get()

        if isinstance(command, SetBatteryDischargeRateCommand) and isinstance(
            snapshot.battery, BatteryState
        ):
            if snapshot.battery.soc_percent <= constraints.battery_reserve_floor_percent:
                return "battery_soc_at_or_below_reserve_floor"

        if isinstance(command, (SetBatteryChargeRateCommand, SetEVChargingRateCommand)):
            if command.rate_kw > constraints.peak_limit_kw:
                return "commanded_rate_exceeds_peak_limit"

        if snapshot.operating_mode is SystemOperatingMode.conservative and isinstance(
            command, (SetBatteryDischargeRateCommand, SetEVChargingRateCommand)
        ):
            return "conservative_mode_blocks_load_increase"

        return None

    async def _reject(self, command: DeviceCommand, reason: str) -> CommandResult:
        try:
            await self._observability.audit(
                actor="system",
                event_type="CONSTRAINT",
                summary=f"Command rejected: {reason}",
                device_id=command.device_id,
                detail={
                    "correlation_id": str(command.correlation_id),
                    "command_type": type(command).__name__,
                    "rejection_reason": reason,
                },
            )
        except Exception:  # noqa: BLE001 — audit failure must not prevent rejection return
            logger.error(
                "constraint_audit_write_failed",
                device_id=command.device_id,
                reason=reason,
                component="engine",
            )
        return CommandResult(
            correlation_id=command.correlation_id,
            device_id=command.device_id,
            status=CommandStatus.rejected,
            applied=False,
            reason=reason,
        )


def _required_write_capability(command: DeviceCommand) -> WriteCapability:
    if isinstance(command, SetBatteryChargeRateCommand):
        return WriteCapability.set_charge_rate
    if isinstance(command, SetBatteryDischargeRateCommand):
        return WriteCapability.set_discharge_rate
    if isinstance(command, (SetEVChargingRateCommand, StopEVChargingCommand)):
        return WriteCapability.set_ev_charge_current
    raise TypeError(f"Unhandled command type: {type(command).__name__}")
