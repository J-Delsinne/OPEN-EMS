"""PolicyGuard: mandatory safety gate for every device command (AR15, FR14).

Stage 2 of the two-stage execution pipeline (Story 8.2). Every command produced
by ``IntentExecutor`` (or any other future origin) must be authorized here
before any adapter ``send_command()`` is invoked.

Story 9.0c: the capability + dispatch surface is locked by a documented contract
with six structurally-distinct rejection paths (P0–P5). The order of evaluation
is load-bearing — short-circuiting earlier paths preserves the audit semantics
and prevents unnecessary I/O.

Capability-failure contract (P0–P4)
====================================

PolicyGuard evaluates the following paths IN ORDER. Each produces a
``CONSTRAINT`` audit event, leaves ``applied=False``, and returns a
``CommandResult`` with the exact ``reason`` string listed below
(``status=CommandStatus.rejected`` for all of P0–P4):

* **P0** — ``snapshot.operating_mode is SystemOperatingMode.fail_safe`` →
  ``reason="fail_safe_mode_active"``.
* **P1** — ``command.device_role not in self._adapters`` →
  ``reason="adapter_not_registered"``.
* **P2** — ``adapter.get_capabilities()`` raises / times out / returns a
  non-profile / device_id mismatch → ``reason="capability_check_failed"``.
* **P3** — ``_required_write_capability(command)`` raises ``TypeError`` →
  ``reason="unknown_command_type"``.
* **P4** — ``required_capability not in profile.write_capabilities`` →
  ``reason=f"capability_missing: {required_capability.value}"`` (the suffix
  is the exact ``WriteCapability`` enum value, e.g.
  ``"capability_missing: set_discharge_rate"``).

**Evaluation order is load-bearing.** A fail-safe-mode rejection MUST NOT
incur a capability lookup; an unregistered-adapter rejection MUST NOT incur
a capability lookup; an unknown-command-type rejection MUST NOT incur a
capability-presence check.

Adapter-side contract for ``get_capabilities()`` (referenced by P2)
====================================================================

PolicyGuard relies on the following invariants from every ``DeviceAdapter``:

1. **No protocol I/O.** ``get_capabilities()`` MUST be synchronous-fast — it
   reads ``self._model`` and calls ``get_profile(...)``. Any blocking I/O is
   a contract violation and will trigger P2 via the
   ``CAPABILITY_CHECK_TIMEOUT_SECONDS`` timeout.
2. **Always returns a profile.** Unknown-firmware / unknown-model devices
   MUST still return a ``DeviceCapabilityProfile`` (REDUCED, no write
   capabilities) — ``get_capabilities()`` MUST NEVER raise for that reason.
   The capability registry's ``get_profile()`` enforces this; adapters are
   expected to delegate without re-raising.
3. **device_id consistency.** The returned profile's ``device_id`` MUST equal
   the adapter's configured ``device_id``. A mismatch is treated as P2
   (defense in depth at the PolicyGuard layer).
4. **model consistency.** The returned profile's ``model`` MUST equal the
   adapter's configured ``_model`` (or, for fixed-model adapters like OCPP /
   DSMR, the value of ``_FIXED_MODEL``). The capability registry's
   ``get_profile()`` preserves this via ``model_copy(update={...})``, which
   does NOT mutate the ``model`` key — so a well-behaved adapter that calls
   ``get_profile(self._device_id, self._model)`` is structurally safe. This
   invariant is documented but NOT additionally checked at the PolicyGuard
   layer: adding a runtime check would require exposing ``adapter._model`` as
   public API, and the registry's ``model_copy`` is the structural guarantee.
   A buggy adapter that calls ``get_profile(self._device_id, "wrong_model")``
   would return a profile with the wrong write_capabilities; P4 would then
   evaluate against the wrong set — this is an adapter-side correctness
   concern and is out of scope for the PolicyGuard contract.

Any post-await failure inside the ``try:`` block (raised exception, non-profile
return, or device_id mismatch) is classified as P2 ``capability_check_failed``
for consistent operator-facing vocabulary.

Safety-constraint rejection paths (evaluated AFTER P0–P4)
==========================================================

After the capability gate passes, ``_check_safety_constraints`` evaluates four
domain-level guards IN THE FOLLOWING ORDER (load-bearing). Each emits a
``CONSTRAINT`` audit and returns ``CommandStatus.rejected``:

1. ``"battery_state_unavailable_for_safety_check"`` — Story 9.0c (AC4):
   a discharge command whose ``snapshot.battery`` is not a ``BatteryState``
   instance (``None`` or ``DegradedDeviceState``) is rejected before the
   SoC-floor check. Fires ONLY for ``SetBatteryDischargeRateCommand``; charge
   commands have no SoC-floor precondition and pass through. This check is
   evaluated FIRST because the absence of battery state makes the downstream
   SoC-floor and conservative-mode branches unreachable for discharge — and
   the AC4 reason is the more specific signal (state-unavailable vs
   mode-throttle), so it dominates when both would apply.
2. ``"battery_soc_at_or_below_reserve_floor"`` — discharge command where
   ``snapshot.battery.soc_percent <= constraints.battery_reserve_floor_percent``.
3. ``"commanded_rate_exceeds_peak_limit"`` — charge or EV command whose
   ``rate_kw > constraints.peak_limit_kw``.
4. ``"conservative_mode_blocks_load_increase"`` — discharge or EV command while
   ``snapshot.operating_mode is SystemOperatingMode.conservative``. Evaluated
   LAST so the more specific battery-state and rate-limit reasons (1–3) win
   when both would apply.

Ordering invariant: a discharge command with ``operating_mode=conservative`` AND
``snapshot.battery is None`` is rejected with reason (1)
``battery_state_unavailable_for_safety_check`` — NOT (4)
``conservative_mode_blocks_load_increase``. Cold-start vocabulary (D2 from the
9.0c review) depends on this ordering: the cold-start window (``StateStore``
default ``operating_mode=degraded``, ``snapshot.battery=None``) presents (1)
as the deterministic discharge-rejection reason.

Post-dispatch enforcement (P5) (Story 9.0c, AC6)
=================================================

After ``adapter.send_command(...)`` returns, PolicyGuard asserts
``result.correlation_id == command.correlation_id``. On mismatch:

* **P5** — ``result.correlation_id != command.correlation_id`` post-dispatch
  → ``reason="adapter_correlation_id_mismatch"``,
  ``status=CommandStatus.correlation_broken``. NOT ``rejected`` (the command
  was dispatched, not blocked) and NOT ``failed`` (the outcome is
  indeterminate — the adapter may have applied the command). The dedicated
  ``correlation_broken`` status lets ``RetryPolicy`` refuse to retry: a retry
  of a non-idempotent command after a partial-dispatch could double-execute.

The synthetic ``CommandResult`` carries the ORIGINAL command's
``correlation_id`` (not the adapter's bogus one) to preserve audit correlation.
A ``CONSTRAINT`` audit row is emitted via the standard rejection path — the
audit channel is shared by pre-dispatch rejections (P0–P4) and post-dispatch
integrity failures (P5); the ``status`` and ``rejection_reason`` fields are
the distinguishing markers for downstream consumers.

Cross-references
================

* ``open_ems.adapters.capabilities.__init__`` — capability registry +
  ``validate_capability_registry_alignment()`` (Story 9.0c, AC5).
* ``open_ems.core.devices.WriteCapability`` — the canonical write-capability
  enum referenced by P4.
* ``open_ems.adapters`` module docstring — cross-adapter command contract
  (correlation_id round-trip is clause 4; AC6 enforces that clause at the
  PolicyGuard layer).
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
from open_ems.core.devices import DeviceCapabilityProfile, WriteCapability
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

        # P0: fail-safe mode short-circuits the entire flow (no capability lookup).
        if snapshot.operating_mode is SystemOperatingMode.fail_safe:
            return await self._reject(command, "fail_safe_mode_active")

        # P1: unregistered adapter short-circuits capability lookup.
        adapter = self._adapters.get(command.device_role)
        if adapter is None:
            return await self._reject(command, "adapter_not_registered")

        # P2: capability lookup. Any exception, non-profile return, or device_id
        # mismatch is classified as ``capability_check_failed`` for consistency.
        # ``except Exception`` excludes ``BaseException`` — so ``CancelledError``
        # propagates and a control-loop shutdown is never silently swallowed.
        try:
            profile = await asyncio.wait_for(
                adapter.get_capabilities(), timeout=CAPABILITY_CHECK_TIMEOUT_SECONDS
            )
            if not isinstance(profile, DeviceCapabilityProfile):
                raise TypeError(
                    f"get_capabilities() returned {type(profile).__name__}, "
                    "expected DeviceCapabilityProfile"
                )
            if profile.device_id != command.device_id:
                raise ValueError(
                    f"profile.device_id={profile.device_id!r} does not match "
                    f"command.device_id={command.device_id!r}"
                )
        except Exception as exc:  # noqa: BLE001 — capability lookup must not propagate; CancelledError (BaseException) escapes by design
            # P3 (9.0c review): include the exception class + message in the log
            # so operators can distinguish timeout / connection-error / shape
            # violation from a generic "capability_check_failed". The rejection
            # ``reason`` field stays the contract-locked string.
            logger.warning(
                "capability_check_failed",
                device_id=command.device_id,
                device_role=command.device_role.value,
                error=repr(exc),
                component="engine",
            )
            return await self._reject(command, "capability_check_failed")

        # P3: unknown command type short-circuits the capability-presence check.
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

        # P4: required capability missing from the profile.
        if required_capability not in profile.write_capabilities:
            return await self._reject(command, f"capability_missing: {required_capability.value}")

        # Safety constraints (evaluated AFTER P0–P4).
        constraint_violation = self._check_safety_constraints(command, snapshot)
        if constraint_violation is not None:
            return await self._reject(command, constraint_violation)

        # Dispatch.
        try:
            result = await asyncio.wait_for(
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
        except Exception as exc:  # noqa: BLE001 — AR16 mandates no exception propagation; CancelledError escapes by design
            logger.warning(
                "command_dispatch_failed",
                device_id=command.device_id,
                device_role=command.device_role.value,
                error=repr(exc),
                component="engine",
            )
            # Stable reason key (not ``repr(exc)``) so the UX failure-reason
            # map (state_serialization._OVERRIDE_FAILURE_REASONS) can resolve
            # this branch deterministically. The Python-level diagnostic
            # detail is preserved in the structlog event above.
            return CommandResult(
                correlation_id=command.correlation_id,
                device_id=command.device_id,
                status=CommandStatus.failed,
                applied=False,
                reason="command_dispatch_failed",
            )

        # P5 (post-dispatch): correlation_id round-trip enforcement. A buggy
        # adapter that returns a different UUID would silently break audit
        # correlation. Status is ``correlation_broken`` (not ``failed``) so
        # RetryPolicy can refuse to retry — the dispatch outcome is
        # indeterminate (the adapter may have applied the command), and a retry
        # of a non-idempotent command would double-execute. The synthetic
        # result preserves the command's ORIGINAL correlation_id for audit.
        if result.correlation_id != command.correlation_id:
            logger.warning(
                "policy_guard_correlation_mismatch",
                device_id=command.device_id,
                expected_correlation_id=str(command.correlation_id),
                received_correlation_id=str(result.correlation_id),
                component="engine",
            )
            return await self._reject(
                command,
                "adapter_correlation_id_mismatch",
                status=CommandStatus.correlation_broken,
            )

        return result

    def _check_safety_constraints(
        self,
        command: DeviceCommand,
        snapshot: SystemSnapshot,
    ) -> str | None:
        constraints = self._active_constraints.get()

        # AC4 (Story 9.0c): a discharge command without a published BatteryState
        # MUST NOT bypass the SoC-floor check by silent skip. Evaluated BEFORE
        # the SoC-floor branch so the explicit reason fires.
        if isinstance(command, SetBatteryDischargeRateCommand) and not isinstance(
            snapshot.battery, BatteryState
        ):
            return "battery_state_unavailable_for_safety_check"

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

    async def _reject(
        self,
        command: DeviceCommand,
        reason: str,
        *,
        status: CommandStatus = CommandStatus.rejected,
    ) -> CommandResult:
        """Emit a CONSTRAINT audit and return a non-success ``CommandResult``.

        Used by two kinds of refusal:

        * **Pre-dispatch rejections (P0–P4 + safety constraints)** —
          ``status=CommandStatus.rejected`` (default). The command never
          reached the adapter; ``applied=False`` is authoritative.
        * **Post-dispatch integrity failure (P5)** —
          ``status=CommandStatus.correlation_broken``. The adapter MAY have
          applied the command at the device, but returned a bogus
          correlation_id; outcome is indeterminate. Reuses this helper so the
          single CONSTRAINT audit code path covers both refusal classes;
          downstream consumers distinguish via ``status`` and
          ``rejection_reason``. (D3 from the 9.0c review.)

        The audit ``event_type`` is always ``"CONSTRAINT"``. Audit-write
        failures are caught and logged but do not block the result return.
        """
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
            status=status,
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
