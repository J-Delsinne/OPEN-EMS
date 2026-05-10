# Story 9-0 Review Bundle

## Section A — Diff of modified files (git diff HEAD)

```diff
warning: in the working copy of '_bmad-output/planning-artifacts/architecture.md', LF will be replaced by CRLF the next time Git touches it
warning: in the working copy of 'src/open_ems/adapters/__init__.py', LF will be replaced by CRLF the next time Git touches it
warning: in the working copy of 'src/open_ems/adapters/dsmr/meter_adapter.py', LF will be replaced by CRLF the next time Git touches it
warning: in the working copy of 'src/open_ems/adapters/modbus/battery_adapter.py', LF will be replaced by CRLF the next time Git touches it
warning: in the working copy of 'src/open_ems/adapters/ocpp/charger_adapter.py', LF will be replaced by CRLF the next time Git touches it
warning: in the working copy of 'src/open_ems/core/commands.py', LF will be replaced by CRLF the next time Git touches it
diff --git a/_bmad-output/planning-artifacts/architecture.md b/_bmad-output/planning-artifacts/architecture.md
index 0615ea4..e6eb717 100644
--- a/_bmad-output/planning-artifacts/architecture.md
+++ b/_bmad-output/planning-artifacts/architecture.md
@@ -710,6 +710,8 @@ All `send_command` adapter implementations must return a `CommandResult` object
 
 The decision engine must not assume a command succeeded unless `CommandResult` confirms acceptance and either observed state confirms effect or the device protocol provides a reliable acknowledgment. Retries must be gated on `CommandResult.status` and must not retry if retrying would risk violating safety constraints.
 
+**Status as of Story 9.0 (2026-05-10):** All three controllable domain adapters (`BatteryAdapter`, `InverterAdapter`, `EVChargerAdapter`) honor the cross-adapter command contract — see `src/open_ems/adapters/__init__.py` module docstring for the single authoritative reference (status mapping table, correlation_id semantics, cancellation/timeout rules, AR16 enforcement). The contract is versioned via `COMMAND_CONTRACT_VERSION`; bump that constant whenever any clause changes. `GridMeterAdapter` is intentionally excluded — DSMR P1 is read-only (FR6b) and never grows a write path.
+
 ---
 
 ### Enforcement Summary
diff --git a/src/open_ems/adapters/__init__.py b/src/open_ems/adapters/__init__.py
index 4ba15c6..5f565b4 100644
--- a/src/open_ems/adapters/__init__.py
+++ b/src/open_ems/adapters/__init__.py
@@ -1,4 +1,110 @@
-"""Raw protocol adapter contracts for OPEN-EMS."""
+"""Raw protocol adapter contracts for OPEN-EMS.
+
+Cross-adapter command contract (v1.0)
+=====================================
+
+Single source of truth for ``DeviceAdapter.send_command(cmd) -> CommandResult``
+behavior across all controllable adapters (BatteryAdapter, InverterAdapter,
+EVChargerAdapter). Per-adapter docstrings reference this section by path; they
+do NOT restate clauses.
+
+GridMeterAdapter is intentionally excluded — DSMR P1 is read-only (FR6b);
+``send_command`` raises ``NotImplementedError`` with reason
+``"DSMR P1 is read-only"`` and never grows a write path.
+
+1. Authorization is upstream
+----------------------------
+PolicyGuard authorizes; the adapter executes. Adapters never re-check fail-safe
+mode, capability, or safety constraints — those have already passed.
+
+2. Command surface is exhaustive over the adapter's WriteCapability set
+-----------------------------------------------------------------------
+For every ``WriteCapability`` in the adapter's capability profile, exactly one
+``DeviceCommand`` subtype is dispatchable. For every ``DeviceCommand`` subtype
+NOT covered, ``send_command(cmd)`` raises
+``TypeError(f"{adapter_class} does not support {type(cmd).__name__}")``.
+This is intentionally NOT a ``CommandResult`` failure — it is a programming-
+error signal (PolicyGuard's capability gate should have caught it).
+
+3. Status mapping table
+-----------------------
+
+Source signal → (status, applied, reason form):
+
+- Protocol ``acked`` + affirmative response → (``success``, True, ``"ok"``)
+- Protocol ``acked`` + negative payload (OCPP) →
+  (``rejected``, False, ``f"<protocol>_rejected:<status>"``)
+- Protocol layer ``timeout`` →
+  (``timeout``, False, ``f"<protocol>_command_timeout"``)
+- Protocol layer ``error`` →
+  (``failed``, False, ``f"<protocol>_error:<error_code>"``)
+- Adapter-internal exception (bug) →
+  (``failed``, False, ``f"adapter_internal_error:<exc_type>"``)
+- correlation_id mismatch from protocol →
+  (``failed``, False, ``"correlation_id_mismatch"``)
+- Register-map encoding error (Modbus only) →
+  (``failed``, False, ``f"register_map_encoding_error:<msg>"``)
+- OCPP stop without active transaction →
+  (``failed``, False, ``"no_active_ocpp_transaction"``)
+
+``<protocol>`` ∈ {``modbus``, ``ocpp``}. Audit consumers can group reasons by
+protocol family without parsing free-form text.
+
+4. correlation_id round-trip is exact
+-------------------------------------
+``result.correlation_id == cmd.correlation_id`` (both ``uuid.UUID``). The
+adapter converts ``str(cmd.correlation_id)`` at the protocol-layer boundary
+and parses it back when ``ProtocolCommandResult`` returns. Mismatch (or
+unparseable) ⇒ ``CommandResult(status=failed, reason="correlation_id_mismatch")``
+plus a structured ``adapter_correlation_mismatch`` warning log.
+
+5. Cancellation propagates
+--------------------------
+``asyncio.CancelledError`` is NEVER caught by the adapter's ``send_command``.
+Cleanup happens via ``try/finally``, never ``except CancelledError``.
+``RetryPolicy._emit_cancellation_audit`` is the single audit emitter for
+cancelled commands. Adapters do NOT emit a parallel cancellation audit.
+
+6. Timeout boundary
+-------------------
+The adapter's protocol-layer timeout (``ModbusTcpAdapter.config.timeout_s``,
+``OCPPAdapterConfig.command_timeout_s``) is the primary boundary. PolicyGuard's
+outer ``asyncio.wait_for(..., timeout=COMMAND_DISPATCH_TIMEOUT_SECONDS)`` is
+defense in depth — under nominal operation, the protocol timeout always fires
+first. Adapters MUST return within ``protocol_timeout_s + epsilon`` under any
+non-cancellation condition.
+
+7. observed_state is None on success
+------------------------------------
+Adapters do NOT issue a post-write read to populate ``observed_state``. The
+control loop's polling cycle observes the post-write state on its next tick.
+An adapter MAY populate ``observed_state`` only if its protocol returns the
+post-write state in the same call (e.g. some Modbus "write+read" patterns);
+v1 leaves it None.
+
+8. AR16 enforcement
+-------------------
+All exceptions except ``asyncio.CancelledError`` (clause 5) and ``TypeError``
+(clause 2 — programming-error signal) are caught and wrapped as
+``CommandResult(status=failed, applied=False, reason="adapter_internal_error:<type>")``.
+The full exception is logged via structlog with ``exc_info=True`` BEFORE the
+``CommandResult`` is returned. PolicyGuard's outer ``except Exception`` becomes
+defense in depth that should never fire when adapters honor this clause.
+
+9. Idempotency is in the command, not the adapter
+-------------------------------------------------
+Adapters do NOT consult ``cmd.is_idempotent``. ``RetryPolicy`` reads it. The
+adapter's job is to faithfully execute one attempt.
+
+10. Audit emission is upstream
+------------------------------
+Adapters do NOT emit DECISION / DEVICE / CONSTRAINT audits. PolicyGuard emits
+CONSTRAINT on rejection; RetryPolicy emits DECISION on success and DEVICE on
+terminal failure / cancellation.
+
+The contract version is exposed as ``COMMAND_CONTRACT_VERSION`` for change
+tracking — bump it when any clause above changes.
+"""
 
 from open_ems.adapters.protocol import (
     ConnectionStatus,
@@ -14,7 +120,10 @@ from open_ems.adapters.protocol import (
     RawProtocolState,
 )
 
+COMMAND_CONTRACT_VERSION = "1.0"
+
 __all__ = [
+    "COMMAND_CONTRACT_VERSION",
     "ConnectionStatus",
     "ProtocolAdapter",
     "ProtocolCommandResult",
diff --git a/src/open_ems/adapters/capabilities/battery.py b/src/open_ems/adapters/capabilities/battery.py
index 9d9980a..56bef59 100644
--- a/src/open_ems/adapters/capabilities/battery.py
+++ b/src/open_ems/adapters/capabilities/battery.py
@@ -1,4 +1,12 @@
-"""Capability profiles for supported battery storage models."""
+"""Capability profiles for supported battery storage models.
+
+Story 9.0 decision (AC10): ``set_operating_mode`` is removed from BYD HVS/HVM
+profiles for v1. ``BatteryAdapter.send_command`` only implements rate setpoints
+(``set_charge_rate``, ``set_discharge_rate``); declaring an operating-mode
+capability without an implementation would violate the profile↔implementation
+lockstep invariant. If a future BYD model exposes a discrete operating-mode
+register write, add it back together with the corresponding command type.
+"""
 
 from __future__ import annotations
 
@@ -14,7 +22,6 @@ _WRITE_CAPS = frozenset(
     {
         WriteCapability.set_charge_rate,
         WriteCapability.set_discharge_rate,
-        WriteCapability.set_operating_mode,
     }
 )
 
diff --git a/src/open_ems/adapters/capabilities/inverter.py b/src/open_ems/adapters/capabilities/inverter.py
index 11251e6..1f6492f 100644
--- a/src/open_ems/adapters/capabilities/inverter.py
+++ b/src/open_ems/adapters/capabilities/inverter.py
@@ -1,4 +1,13 @@
-"""Capability profiles for supported PV inverter models."""
+"""Capability profiles for supported PV inverter models.
+
+Story 9.0 AC3 / AC10: v1 inverters are read-only. ``InverterAdapter.send_command``
+raises ``TypeError`` for any command type, and these profiles correspondingly
+declare an empty ``write_capabilities``. PolicyGuard's capability gate rejects
+every command type before it reaches the adapter; the adapter's TypeError is
+defense in depth. If a future inverter model exposes a Modbus write surface,
+add the corresponding ``WriteCapability`` member here together with the
+``InverterAdapter.send_command`` implementation in lockstep.
+"""
 
 from __future__ import annotations
 
@@ -10,7 +19,7 @@ from open_ems.core.devices import (
 )
 
 _FULL_CAPS = frozenset({ReadCapability.state, ReadCapability.power})
-_WRITE_CAPS = frozenset({WriteCapability.set_operating_mode})
+_WRITE_CAPS: frozenset[WriteCapability] = frozenset()
 
 INVERTER_PROFILES: dict[str, DeviceCapabilityProfile] = {
     "fronius_gen24_v1": DeviceCapabilityProfile(
diff --git a/src/open_ems/adapters/dsmr/meter_adapter.py b/src/open_ems/adapters/dsmr/meter_adapter.py
index b3c69ad..e0b98cf 100644
--- a/src/open_ems/adapters/dsmr/meter_adapter.py
+++ b/src/open_ems/adapters/dsmr/meter_adapter.py
@@ -95,8 +95,17 @@ class GridMeterAdapter:
         return profile
 
     async def send_command(self, cmd: DeviceCommand) -> CommandResult:
-        """DSMR P1 is a read-only meter — write capability is not supported."""
-        raise NotImplementedError("GridMeterAdapter does not support send_command (read-only)")
+        """DSMR P1 is a read-only meter — write capability is not supported.
+
+        Per AR16 and the cross-adapter contract in ``open_ems.adapters`` (clause 2),
+        this adapter is excluded from the controllable-adapter set; DSMR P1 will
+        never grow a write path. ``NotImplementedError`` (not ``TypeError``) is
+        the runtime signal that this adapter has no command surface at all,
+        distinct from a controllable adapter receiving an unsupported command type.
+        """
+        raise NotImplementedError(
+            "GridMeterAdapter does not support send_command (DSMR P1 is read-only)"
+        )
 
     def _to_degraded(self, reason: str, occurred_at: datetime) -> DegradedDeviceState:
         domain_reason = "reconnecting" if reason in _RECONNECTING_REASONS else reason
diff --git a/src/open_ems/adapters/modbus/battery_adapter.py b/src/open_ems/adapters/modbus/battery_adapter.py
index 27eebf7..b58a1ed 100644
--- a/src/open_ems/adapters/modbus/battery_adapter.py
+++ b/src/open_ems/adapters/modbus/battery_adapter.py
@@ -1,5 +1,12 @@
 """Domain-level battery adapter: translates RawModbusState → BatteryState.
 
+``send_command`` honors the cross-adapter command contract documented in
+``open_ems.adapters`` (the canonical reference). Per-clause behavior is enforced
+here without restating the contract; see that module's docstring for clauses
+1–10 (authorization, command surface, status mapping, correlation_id round-trip,
+cancellation, timeout boundary, observed_state, AR16 enforcement, idempotency,
+audit ownership).
+
 Battery power sign convention (system-wide, per Story 4.1 / devices.py):
     battery_power_kw > 0  →  charging (consuming power from PV or grid)
     battery_power_kw < 0  →  discharging (providing power to loads or grid)
@@ -7,6 +14,7 @@ Battery power sign convention (system-wide, per Story 4.1 / devices.py):
 
 from __future__ import annotations
 
+import uuid
 from datetime import UTC, datetime
 
 import structlog
@@ -21,8 +29,18 @@ from open_ems.adapters.modbus.register_maps import (
     MissingRegisterError,
 )
 from open_ems.adapters.modbus.tcp import ModbusTcpAdapter
-from open_ems.adapters.protocol import ProtocolDegradedState, RawModbusState
-from open_ems.core.commands import CommandResult, DeviceCommand
+from open_ems.adapters.protocol import (
+    ProtocolDegradedState,
+    RawModbusState,
+    RawProtocolCommand,
+)
+from open_ems.core.commands import (
+    CommandResult,
+    CommandStatus,
+    DeviceCommand,
+    SetBatteryChargeRateCommand,
+    SetBatteryDischargeRateCommand,
+)
 from open_ems.core.devices import (
     BatteryState,
     DegradedDeviceState,
@@ -40,6 +58,11 @@ _SUPPORTED_MODELS: dict[str, BatteryRegisterMap] = {
 # Protocol reasons that indicate a transient connection loss → mapped to "reconnecting"
 _RECONNECTING_REASONS: frozenset[str] = frozenset({"modbus_timeout", "modbus_error"})
 
+_COMMAND_NAME_BY_TYPE: dict[type, str] = {
+    SetBatteryChargeRateCommand: "set_battery_charge_rate",
+    SetBatteryDischargeRateCommand: "set_battery_discharge_rate",
+}
+
 
 class BatteryAdapter:
     """Domain-level battery adapter: translates RawModbusState → BatteryState.
@@ -110,8 +133,81 @@ class BatteryAdapter:
         return profile
 
     async def send_command(self, cmd: DeviceCommand) -> CommandResult:
-        """Modbus battery write path — implementation deferred (Epic 9)."""
-        raise NotImplementedError("BatteryAdapter.send_command is not yet implemented")
+        """Modbus battery write path.
+
+        Honors the cross-adapter command contract in ``open_ems.adapters``.
+        ``asyncio.CancelledError`` and ``TypeError`` propagate (clauses 5 and 2).
+        Every other exception is caught and wrapped (clause 8 / AR16).
+        """
+        if not isinstance(cmd, (SetBatteryChargeRateCommand, SetBatteryDischargeRateCommand)):
+            raise TypeError(f"BatteryAdapter does not support {type(cmd).__name__}")
+
+        try:
+            payload = self._register_map.command_payload(cmd)
+        except ValueError as exc:
+            return _failed(cmd, f"register_map_encoding_error:{exc}")
+        except Exception as exc:  # noqa: BLE001 — AR16: wrap unexpected exceptions
+            return self._wrap_internal_error(cmd, exc, where="register_map.command_payload")
+
+        try:
+            raw_command = RawProtocolCommand(
+                correlation_id=str(cmd.correlation_id),
+                device_id=self.device_id,
+                command_name=_COMMAND_NAME_BY_TYPE[type(cmd)],
+                payload=payload,
+            )
+        except Exception as exc:  # noqa: BLE001 — AR16
+            return self._wrap_internal_error(cmd, exc, where="raw_command_construction")
+
+        try:
+            protocol_result = await self._protocol_adapter.send_raw_command(raw_command)
+        except Exception as exc:  # noqa: BLE001 — AR16
+            # CancelledError is BaseException in 3.11+; not caught here, propagates.
+            return self._wrap_internal_error(cmd, exc, where="protocol_dispatch")
+
+        # correlation_id round-trip integrity (contract clause 4).
+        try:
+            returned_uuid = uuid.UUID(protocol_result.correlation_id)
+        except (ValueError, AttributeError, TypeError):
+            logger.warning(
+                "adapter_correlation_mismatch",
+                component="adapters",
+                device_id=self.device_id,
+                expected=str(cmd.correlation_id),
+                received=str(protocol_result.correlation_id),
+            )
+            return _failed(cmd, "correlation_id_mismatch")
+        if returned_uuid != cmd.correlation_id:
+            logger.warning(
+                "adapter_correlation_mismatch",
+                component="adapters",
+                device_id=self.device_id,
+                expected=str(cmd.correlation_id),
+                received=str(protocol_result.correlation_id),
+            )
+            return _failed(cmd, "correlation_id_mismatch")
+
+        return _map_protocol_result(cmd, protocol_result, protocol_prefix="modbus")
+
+    def _wrap_internal_error(
+        self, cmd: DeviceCommand, exc: BaseException, *, where: str
+    ) -> CommandResult:
+        logger.warning(
+            "adapter_internal_error",
+            component="adapters",
+            device_id=self.device_id,
+            command_type=type(cmd).__name__,
+            phase=where,
+            error=repr(exc),
+            exc_info=True,
+        )
+        return CommandResult(
+            correlation_id=cmd.correlation_id,
+            device_id=cmd.device_id,
+            status=CommandStatus.failed,
+            applied=False,
+            reason=f"adapter_internal_error:{type(exc).__name__}",
+        )
 
     def _to_degraded(self, reason: str, occurred_at: datetime) -> DegradedDeviceState:
         domain_reason = "reconnecting" if reason in _RECONNECTING_REASONS else reason
@@ -128,3 +224,52 @@ class BatteryAdapter:
             reason=domain_reason,
             occurred_at=occurred_at,
         )
+
+
+def _failed(cmd: DeviceCommand, reason: str) -> CommandResult:
+    return CommandResult(
+        correlation_id=cmd.correlation_id,
+        device_id=cmd.device_id,
+        status=CommandStatus.failed,
+        applied=False,
+        reason=reason,
+    )
+
+
+def _map_protocol_result(
+    cmd: DeviceCommand,
+    protocol_result: object,  # ProtocolCommandResult; typed as object to avoid circular import
+    *,
+    protocol_prefix: str,
+) -> CommandResult:
+    """Map a ``ProtocolCommandResult`` to a ``CommandResult`` per the contract clause 3."""
+    status = getattr(protocol_result, "protocol_status", None)
+    raw_response = getattr(protocol_result, "raw_response", None)
+
+    if status == "acked":
+        return CommandResult(
+            correlation_id=cmd.correlation_id,
+            device_id=cmd.device_id,
+            status=CommandStatus.success,
+            applied=True,
+            reason="ok",
+        )
+    if status == "timeout":
+        return CommandResult(
+            correlation_id=cmd.correlation_id,
+            device_id=cmd.device_id,
+            status=CommandStatus.timeout,
+            applied=False,
+            reason=f"{protocol_prefix}_command_timeout",
+        )
+    if status == "error":
+        error_code = "unknown"
+        if isinstance(raw_response, dict):
+            error_code = str(
+                raw_response.get("error") or raw_response.get("error_code") or "unknown"
+            )
+        return _failed(cmd, f"{protocol_prefix}_error:{error_code}")
+
+    # Defensive — protocol_status is a Literal in ProtocolCommandResult, so reaching
+    # here would mean a bug at the protocol layer.
+    return _failed(cmd, f"adapter_internal_error:unexpected_protocol_status:{status}")
diff --git a/src/open_ems/adapters/modbus/inverter_adapter.py b/src/open_ems/adapters/modbus/inverter_adapter.py
index 1123076..c3bc013 100644
--- a/src/open_ems/adapters/modbus/inverter_adapter.py
+++ b/src/open_ems/adapters/modbus/inverter_adapter.py
@@ -1,4 +1,14 @@
-"""Domain-level inverter adapter: translates RawModbusState → InverterState."""
+"""Domain-level inverter adapter: translates RawModbusState → InverterState.
+
+Story 9.0 AC3 / AC10: v1 inverters are read-only across all supported models
+(``fronius_gen24_v1``, ``huawei_sun2000_v3``, ``growatt_hybrid_v1``). The
+capability profiles in ``open_ems.adapters.capabilities.inverter`` declare an
+empty ``write_capabilities`` set, so PolicyGuard's capability gate rejects every
+command before it reaches ``send_command``. The ``TypeError`` raised here is
+the same defense-in-depth signal used by other controllable adapters when an
+unsupported command type slips through (cross-adapter contract clause 2;
+see ``open_ems.adapters`` module docstring).
+"""
 
 from __future__ import annotations
 
@@ -107,8 +117,15 @@ class InverterAdapter:
         return profile
 
     async def send_command(self, cmd: DeviceCommand) -> CommandResult:
-        """Modbus inverter write path — implementation deferred (Epic 9)."""
-        raise NotImplementedError("InverterAdapter.send_command is not yet implemented")
+        """v1 inverter has no write surface — every command type raises TypeError.
+
+        Per the cross-adapter command contract (clause 2), this is a programming-
+        error signal: PolicyGuard's capability gate should have rejected the
+        command before reaching the adapter. Returning ``TypeError`` (not a
+        ``CommandResult``) lets test suites surface a routing bug immediately
+        instead of silently mapping it to ``failed``.
+        """
+        raise TypeError(f"InverterAdapter does not support {type(cmd).__name__}")
 
     def _to_degraded(self, reason: str, occurred_at: datetime) -> DegradedDeviceState:
         domain_reason = "reconnecting" if reason in _RECONNECTING_REASONS else reason
diff --git a/src/open_ems/adapters/modbus/register_maps/__init__.py b/src/open_ems/adapters/modbus/register_maps/__init__.py
index d062ad2..e7a4a58 100644
--- a/src/open_ems/adapters/modbus/register_maps/__init__.py
+++ b/src/open_ems/adapters/modbus/register_maps/__init__.py
@@ -14,6 +14,7 @@ from open_ems.adapters.modbus.register_maps._base import (
     InvalidRegisterValueError,
     InverterRegisterMap,
     MissingRegisterError,
+    ModbusCommandPayload,
 )
 from open_ems.adapters.modbus.register_maps.byd_hvm_v1 import BydHvmV1
 from open_ems.adapters.modbus.register_maps.byd_hvs_v1 import BydHvsV1
@@ -31,4 +32,5 @@ __all__ = [
     "InvalidRegisterValueError",
     "InverterRegisterMap",
     "MissingRegisterError",
+    "ModbusCommandPayload",
 ]
diff --git a/src/open_ems/adapters/modbus/register_maps/_base.py b/src/open_ems/adapters/modbus/register_maps/_base.py
index 9a89d5c..c86fd28 100644
--- a/src/open_ems/adapters/modbus/register_maps/_base.py
+++ b/src/open_ems/adapters/modbus/register_maps/_base.py
@@ -7,9 +7,13 @@ MissingRegisterError and _require_reg.
 
 from __future__ import annotations
 
-from typing import Protocol
+from typing import Any, Protocol
 
 from open_ems.adapters.protocol import RawModbusState
+from open_ems.core.commands import (
+    SetBatteryChargeRateCommand,
+    SetBatteryDischargeRateCommand,
+)
 from open_ems.core.devices import BatteryState, InverterState
 
 
@@ -47,7 +51,29 @@ class InverterRegisterMap(Protocol):
     def map_state(self, raw: RawModbusState) -> InverterState: ...
 
 
+# A typed alias for the dict payload that BatteryAdapter forwards to
+# ``ModbusTcpAdapter.send_raw_command``. The shape matches ``_WriteRegisterPayload``
+# / ``_WriteRegistersPayload`` in ``adapters.modbus.tcp``; we keep the type as
+# ``dict[str, Any]`` here to avoid importing private TCP types.
+ModbusCommandPayload = dict[str, Any]
+
+
 class BatteryRegisterMap(Protocol):
     """Structural contract for all battery register map implementations."""
 
     def map_state(self, raw: RawModbusState) -> BatteryState: ...
+
+    def command_payload(
+        self,
+        cmd: SetBatteryChargeRateCommand | SetBatteryDischargeRateCommand,
+    ) -> ModbusCommandPayload:
+        """Encode a battery setpoint command into a Modbus write payload.
+
+        The returned dict is forwarded to ``ModbusTcpAdapter.send_raw_command``
+        as ``RawProtocolCommand.payload``. Implementations MUST raise
+        ``ValueError`` when ``cmd.rate_kw`` is outside the model's encodable
+        range; the calling adapter wraps that into
+        ``CommandResult(status=failed, reason=f"register_map_encoding_error:...")``
+        per the cross-adapter command contract.
+        """
+        ...
diff --git a/src/open_ems/adapters/modbus/register_maps/byd_hvm_v1.py b/src/open_ems/adapters/modbus/register_maps/byd_hvm_v1.py
index 736f01b..7db6a10 100644
--- a/src/open_ems/adapters/modbus/register_maps/byd_hvm_v1.py
+++ b/src/open_ems/adapters/modbus/register_maps/byd_hvm_v1.py
@@ -16,10 +16,15 @@ from __future__ import annotations
 
 from open_ems.adapters.modbus.register_maps._base import (
     InvalidRegisterValueError,
+    ModbusCommandPayload,
     _require_reg,
 )
 from open_ems.adapters.modbus.register_maps.utils import scale, uint16
 from open_ems.adapters.protocol import RawModbusState
+from open_ems.core.commands import (
+    SetBatteryChargeRateCommand,
+    SetBatteryDischargeRateCommand,
+)
 from open_ems.core.devices import BatteryState
 
 _REG_SOC = 200  # uint16, unit: 0.1 % (divide by 10 → %)  # PLACEHOLDER
@@ -28,6 +33,14 @@ _REG_POWER_W = 202  # uint16, unit: W (unsigned magnitude)  # PLACEHOLDER
 _REG_DIRECTION = 204  # uint16, 0 = charging, 1 = discharging  # PLACEHOLDER
 _REG_OPERATING_MODE = 205  # uint16 enum: 0=normal, 1=standby, 2=fault  # PLACEHOLDER
 
+# Setpoint write registers — placeholder addresses; verify against BYD docs at
+# installer commissioning. Encoded value is unsigned watts (uint16, 0..65 535).
+_REG_CHARGE_RATE_SETPOINT_W = 210  # PLACEHOLDER
+_REG_DISCHARGE_RATE_SETPOINT_W = 211  # PLACEHOLDER
+
+_MAX_SETPOINT_W = 65_535  # uint16 ceiling
+_MAX_SETPOINT_KW = _MAX_SETPOINT_W / 1000.0  # 65.535 kW
+
 _DIRECTION_CHARGE = 0
 _OPERATING_MODE_MAP: dict[int, str] = {
     0: "normal",
@@ -38,6 +51,19 @@ _OPERATING_MODE_MAP: dict[int, str] = {
 __all__ = ["BydHvmV1"]
 
 
+def _encode_setpoint_w(rate_kw: float, *, command_type: str) -> int:
+    """Encode rate_kw → unsigned-watts uint16 register value, raising on overflow."""
+    if rate_kw < 0.0:
+        # Pydantic Field(ge=0) on the command catches this earlier — defense in depth.
+        raise ValueError(f"{command_type} rate_kw must be >= 0 (got {rate_kw})")
+    watts = int(round(rate_kw * 1000))
+    if watts > _MAX_SETPOINT_W:
+        raise ValueError(
+            f"{command_type} rate_kw={rate_kw} exceeds register-map max ({_MAX_SETPOINT_KW} kW)"
+        )
+    return watts
+
+
 class BydHvmV1:
     """Register map for BYD Battery-Box Premium HVM (Modbus TCP, byd_hvm_v1)."""
 
@@ -64,3 +90,23 @@ class BydHvmV1:
             operating_mode=_OPERATING_MODE_MAP.get(mode_raw, "unknown"),
             read_at=raw.read_at,
         )
+
+    def command_payload(
+        self,
+        cmd: SetBatteryChargeRateCommand | SetBatteryDischargeRateCommand,
+    ) -> ModbusCommandPayload:
+        """Encode a battery setpoint into a Modbus ``write_register`` payload."""
+        if isinstance(cmd, SetBatteryChargeRateCommand):
+            address = _REG_CHARGE_RATE_SETPOINT_W
+            command_type = "set_battery_charge_rate"
+        elif isinstance(cmd, SetBatteryDischargeRateCommand):
+            address = _REG_DISCHARGE_RATE_SETPOINT_W
+            command_type = "set_battery_discharge_rate"
+        else:  # pragma: no cover — caller ensures command type
+            raise TypeError(f"BydHvmV1.command_payload does not support {type(cmd).__name__}")
+        value = _encode_setpoint_w(cmd.rate_kw, command_type=command_type)
+        return {
+            "operation": "write_register",
+            "address": address,
+            "value": value,
+        }
diff --git a/src/open_ems/adapters/modbus/register_maps/byd_hvs_v1.py b/src/open_ems/adapters/modbus/register_maps/byd_hvs_v1.py
index db69c93..b7be2f9 100644
--- a/src/open_ems/adapters/modbus/register_maps/byd_hvs_v1.py
+++ b/src/open_ems/adapters/modbus/register_maps/byd_hvs_v1.py
@@ -16,10 +16,15 @@ from __future__ import annotations
 
 from open_ems.adapters.modbus.register_maps._base import (
     InvalidRegisterValueError,
+    ModbusCommandPayload,
     _require_reg,
 )
 from open_ems.adapters.modbus.register_maps.utils import scale, uint16
 from open_ems.adapters.protocol import RawModbusState
+from open_ems.core.commands import (
+    SetBatteryChargeRateCommand,
+    SetBatteryDischargeRateCommand,
+)
 from open_ems.core.devices import BatteryState
 
 _REG_SOC = 100  # uint16, unit: 0.1 % (divide by 10 → %)  # PLACEHOLDER
@@ -28,6 +33,14 @@ _REG_POWER_W = 102  # uint16, unit: W (unsigned magnitude)  # PLACEHOLDER
 _REG_DIRECTION = 104  # uint16, 0 = charging, 1 = discharging  # PLACEHOLDER
 _REG_OPERATING_MODE = 105  # uint16 enum: 0=normal, 1=standby, 2=fault  # PLACEHOLDER
 
+# Setpoint write registers — placeholder addresses; verify against BYD docs at
+# installer commissioning. Encoded value is unsigned watts (uint16, 0..65 535).
+_REG_CHARGE_RATE_SETPOINT_W = 110  # PLACEHOLDER
+_REG_DISCHARGE_RATE_SETPOINT_W = 111  # PLACEHOLDER
+
+_MAX_SETPOINT_W = 65_535  # uint16 ceiling
+_MAX_SETPOINT_KW = _MAX_SETPOINT_W / 1000.0  # 65.535 kW
+
 _DIRECTION_CHARGE = 0
 _OPERATING_MODE_MAP: dict[int, str] = {
     0: "normal",
@@ -38,6 +51,19 @@ _OPERATING_MODE_MAP: dict[int, str] = {
 __all__ = ["BydHvsV1"]
 
 
+def _encode_setpoint_w(rate_kw: float, *, command_type: str) -> int:
+    """Encode rate_kw → unsigned-watts uint16 register value, raising on overflow."""
+    if rate_kw < 0.0:
+        # Pydantic Field(ge=0) on the command catches this earlier — defense in depth.
+        raise ValueError(f"{command_type} rate_kw must be >= 0 (got {rate_kw})")
+    watts = int(round(rate_kw * 1000))
+    if watts > _MAX_SETPOINT_W:
+        raise ValueError(
+            f"{command_type} rate_kw={rate_kw} exceeds register-map max ({_MAX_SETPOINT_KW} kW)"
+        )
+    return watts
+
+
 class BydHvsV1:
     """Register map for BYD Battery-Box Premium HVS (Modbus TCP, byd_hvs_v1)."""
 
@@ -64,3 +90,23 @@ class BydHvsV1:
             operating_mode=_OPERATING_MODE_MAP.get(mode_raw, "unknown"),
             read_at=raw.read_at,
         )
+
+    def command_payload(
+        self,
+        cmd: SetBatteryChargeRateCommand | SetBatteryDischargeRateCommand,
+    ) -> ModbusCommandPayload:
+        """Encode a battery setpoint into a Modbus ``write_register`` payload."""
+        if isinstance(cmd, SetBatteryChargeRateCommand):
+            address = _REG_CHARGE_RATE_SETPOINT_W
+            command_type = "set_battery_charge_rate"
+        elif isinstance(cmd, SetBatteryDischargeRateCommand):
+            address = _REG_DISCHARGE_RATE_SETPOINT_W
+            command_type = "set_battery_discharge_rate"
+        else:  # pragma: no cover — caller ensures command type
+            raise TypeError(f"BydHvsV1.command_payload does not support {type(cmd).__name__}")
+        value = _encode_setpoint_w(cmd.rate_kw, command_type=command_type)
+        return {
+            "operation": "write_register",
+            "address": address,
+            "value": value,
+        }
diff --git a/src/open_ems/adapters/ocpp/central_system.py b/src/open_ems/adapters/ocpp/central_system.py
index 199e3a8..d3b10f8 100644
--- a/src/open_ems/adapters/ocpp/central_system.py
+++ b/src/open_ems/adapters/ocpp/central_system.py
@@ -56,6 +56,14 @@ class _ChargerState:
         self.last_call_error: dict[str, Any] | None = None
         self.last_meter_values_at: datetime | None = None
         self.last_meter_values_power_kw: float | None = None
+        # Story 9.0: required by EVChargerAdapter to dispatch RemoteStopTransaction
+        # against the currently-active OCPP transaction. Reset on stop_transaction
+        # observation; intrinsically transient — not persisted across process restart.
+        self.last_known_transaction_id: int | None = None
+        # Monotonic counter feeding chargingProfileId in SetChargingProfile payloads.
+        # Stable per OCPP session (one charger connection); resets implicitly when
+        # _ChargerState is replaced on reconnect.
+        self.next_charging_profile_id: int = 1
 
 
 class _InternalChargePoint(_BaseChargePoint):  # type: ignore[misc]
@@ -111,6 +119,40 @@ class _InternalChargePoint(_BaseChargePoint):  # type: ignore[misc]
         }
         return call_result.StatusNotification()
 
+    @on(Action.start_transaction)  # type: ignore[untyped-decorator]
+    def on_start_transaction(
+        self,
+        connector_id: int,
+        id_tag: str,
+        meter_start: int,
+        timestamp: str,
+        **kwargs: Any,
+    ) -> Any:
+        # Issue a transaction id and remember it so EVChargerAdapter.send_command can
+        # construct a valid RemoteStopTransaction payload (Story 9.0 AC4). The id is
+        # monotonic per session; it does not need to survive a process restart because
+        # OCPP transaction ids are intrinsically scoped to an active charger session.
+        transaction_id = (self._state.last_known_transaction_id or 0) + 1
+        self._state.last_known_transaction_id = transaction_id
+        return call_result.StartTransaction(
+            transaction_id=transaction_id,
+            id_tag_info={"status": "Accepted"},
+        )
+
+    @on(Action.stop_transaction)  # type: ignore[untyped-decorator]
+    def on_stop_transaction(
+        self,
+        meter_stop: int,
+        timestamp: str,
+        transaction_id: int,
+        **kwargs: Any,
+    ) -> Any:
+        # Charger-initiated stop — clear the active transaction so the adapter
+        # rejects subsequent StopEVChargingCommand with no_active_ocpp_transaction.
+        if self._state.last_known_transaction_id == transaction_id:
+            self._state.last_known_transaction_id = None
+        return call_result.StopTransaction(id_tag_info={"status": "Accepted"})
+
     @on(Action.meter_values)  # type: ignore[untyped-decorator]
     def on_meter_values(
         self,
@@ -196,6 +238,28 @@ class OCPPChargerAdapter:
                 charge_point_id=self.config.charge_point_id,
             )
 
+    @property
+    def active_transaction_id(self) -> int | None:
+        """The most recent OCPP transaction id observed via StartTransaction.
+
+        Cleared on StopTransaction or reconnect. Read by EVChargerAdapter to
+        compose a RemoteStopTransaction payload (Story 9.0 AC4). Returns None if
+        no transaction is currently active.
+        """
+        return self._state.last_known_transaction_id
+
+    def reserve_charging_profile_id(self) -> int:
+        """Reserve the next session-scoped chargingProfileId for SetChargingProfile.
+
+        Monotonic per OCPP session; resets when the charger reconnects (a fresh
+        ``_ChargerState`` is created). Read by EVChargerAdapter to compose
+        SetChargingProfile payloads (Story 9.0 AC4 / Dev Notes § OCPP charging
+        profile composition).
+        """
+        profile_id = self._state.next_charging_profile_id
+        self._state.next_charging_profile_id = profile_id + 1
+        return profile_id
+
     async def get_raw_state(self) -> RawOCPPState | ProtocolDegradedState:
         """Return current raw OCPP state or a degraded state if disconnected or heartbeat stale."""
         state = self._state
diff --git a/src/open_ems/adapters/ocpp/charger_adapter.py b/src/open_ems/adapters/ocpp/charger_adapter.py
index b48c07d..2d5f9b5 100644
--- a/src/open_ems/adapters/ocpp/charger_adapter.py
+++ b/src/open_ems/adapters/ocpp/charger_adapter.py
@@ -1,16 +1,47 @@
-"""Domain-level EV charger adapter: translates RawOCPPState → EVChargerState."""
+"""Domain-level EV charger adapter: translates RawOCPPState → EVChargerState.
+
+``send_command`` honors the cross-adapter command contract documented in
+``open_ems.adapters`` (the canonical reference). OCPP-specific clauses on top
+of the cross-adapter contract:
+
+- ``SetEVChargingRateCommand`` → OCPP ``SetChargingProfile`` CALL with a single
+  ``TxProfile`` setting ``chargingRateUnit="W"`` and ``limit=int(rate_kw*1000)``.
+  Response ``status="Accepted"`` ⇒ ``CommandResult(success)``.
+  Response ``status="Rejected"`` ⇒ ``CommandResult(rejected, "ocpp_rejected:Rejected")``.
+- ``StopEVChargingCommand`` → OCPP ``RemoteStopTransaction`` with the active
+  transaction id observed from the charger's most recent ``StartTransaction``.
+  No active transaction ⇒ ``CommandResult(failed, "no_active_ocpp_transaction")``
+  (do NOT send a stop with a stale id — Story 8.0 retro hazard).
+
+OCPP CALL ``acked`` ≠ command applied; the response payload's ``status`` field
+is the authoritative signal. The protocol layer reports both alike as
+``protocol_status="acked"``; this adapter inspects the response payload to
+disambiguate (cross-adapter contract clause 3, OCPP-specific row).
+"""
 
 from __future__ import annotations
 
+import uuid
 from datetime import UTC, datetime
-from typing import Literal
+from typing import Any, Literal
 
 import structlog
 
 from open_ems.adapters.capabilities import get_profile
 from open_ems.adapters.ocpp.central_system import OCPPChargerAdapter
-from open_ems.adapters.protocol import ProtocolDegradedState, RawOCPPState
-from open_ems.core.commands import CommandResult, DeviceCommand
+from open_ems.adapters.protocol import (
+    ProtocolCommandResult,
+    ProtocolDegradedState,
+    RawOCPPState,
+    RawProtocolCommand,
+)
+from open_ems.core.commands import (
+    CommandResult,
+    CommandStatus,
+    DeviceCommand,
+    SetEVChargingRateCommand,
+    StopEVChargingCommand,
+)
 from open_ems.core.devices import (
     DegradedDeviceState,
     DeviceCapabilityProfile,
@@ -129,8 +160,166 @@ class EVChargerAdapter:
         return profile
 
     async def send_command(self, cmd: DeviceCommand) -> CommandResult:
-        """OCPP charger write path — implementation deferred (Epic 9)."""
-        raise NotImplementedError("EVChargerAdapter.send_command is not yet implemented")
+        """OCPP charger write path.
+
+        Honors the cross-adapter command contract in ``open_ems.adapters``.
+        ``asyncio.CancelledError`` and ``TypeError`` propagate (clauses 5 and 2).
+        Every other exception is caught and wrapped (clause 8 / AR16).
+        """
+        if isinstance(cmd, SetEVChargingRateCommand):
+            command_name = "SetChargingProfile"
+            try:
+                payload = self._compose_set_charging_profile_payload(cmd.rate_kw)
+            except Exception as exc:  # noqa: BLE001 — AR16
+                return self._wrap_internal_error(cmd, exc, where="ocpp_payload_composition")
+        elif isinstance(cmd, StopEVChargingCommand):
+            command_name = "RemoteStopTransaction"
+            transaction_id = self._protocol_adapter.active_transaction_id
+            if transaction_id is None:
+                return _failed(cmd, "no_active_ocpp_transaction")
+            payload = {"transaction_id": transaction_id}
+        else:
+            raise TypeError(f"EVChargerAdapter does not support {type(cmd).__name__}")
+
+        try:
+            raw_command = RawProtocolCommand(
+                correlation_id=str(cmd.correlation_id),
+                device_id=self.device_id,
+                command_name=command_name,
+                payload=payload,
+            )
+        except Exception as exc:  # noqa: BLE001 — AR16
+            return self._wrap_internal_error(cmd, exc, where="raw_command_construction")
+
+        try:
+            protocol_result = await self._protocol_adapter.send_raw_command(raw_command)
+        except Exception as exc:  # noqa: BLE001 — AR16. CancelledError is BaseException; not caught here.
+            return self._wrap_internal_error(cmd, exc, where="protocol_dispatch")
+
+        # correlation_id round-trip integrity (contract clause 4).
+        try:
+            returned_uuid = uuid.UUID(protocol_result.correlation_id)
+        except (ValueError, AttributeError, TypeError):
+            self._log_correlation_mismatch(cmd, protocol_result)
+            return _failed(cmd, "correlation_id_mismatch")
+        if returned_uuid != cmd.correlation_id:
+            self._log_correlation_mismatch(cmd, protocol_result)
+            return _failed(cmd, "correlation_id_mismatch")
+
+        return self._map_ocpp_result(cmd, protocol_result)
+
+    def _compose_set_charging_profile_payload(self, rate_kw: float) -> dict[str, Any]:
+        """Build an OCPP 1.6 SetChargingProfile payload for an EMS rate setpoint.
+
+        See Dev Notes § "OCPP charging profile composition" (Story 9.0).
+        """
+        if rate_kw < 0.0:
+            raise ValueError(f"rate_kw must be >= 0 (got {rate_kw})")
+        limit_w = int(round(rate_kw * 1000))
+        profile_id = self._protocol_adapter.reserve_charging_profile_id()
+        return {
+            "connector_id": 1,
+            "cs_charging_profiles": {
+                "charging_profile_id": profile_id,
+                "stack_level": 0,
+                "charging_profile_purpose": "TxProfile",
+                "charging_profile_kind": "Absolute",
+                "charging_schedule": {
+                    "charging_rate_unit": "W",
+                    "charging_schedule_period": [
+                        {"start_period": 0, "limit": limit_w},
+                    ],
+                },
+            },
+        }
+
+    def _map_ocpp_result(
+        self,
+        cmd: DeviceCommand,
+        protocol_result: ProtocolCommandResult,
+    ) -> CommandResult:
+        """Map ``ProtocolCommandResult`` → ``CommandResult`` per OCPP semantics.
+
+        OCPP ``acked`` does NOT mean "applied" — the response payload's ``status``
+        field is the authoritative signal. ``status="Accepted"`` ⇒ success;
+        ``status="Rejected"`` ⇒ rejected.
+        """
+        status = protocol_result.protocol_status
+        raw_response = protocol_result.raw_response
+
+        if status == "acked":
+            response_status = None
+            if isinstance(raw_response, dict):
+                response_status = raw_response.get("status")
+            if response_status == "Accepted":
+                return CommandResult(
+                    correlation_id=cmd.correlation_id,
+                    device_id=cmd.device_id,
+                    status=CommandStatus.success,
+                    applied=True,
+                    reason="ok",
+                )
+            # Anything other than Accepted is treated as a rejection. OCPP 1.6
+            # SetChargingProfile defines {Accepted, Rejected, NotSupported}; we
+            # report whatever the charger sent for downstream visibility.
+            reason_status = response_status if isinstance(response_status, str) else "Unknown"
+            return CommandResult(
+                correlation_id=cmd.correlation_id,
+                device_id=cmd.device_id,
+                status=CommandStatus.rejected,
+                applied=False,
+                reason=f"ocpp_rejected:{reason_status}",
+            )
+
+        if status == "timeout":
+            return CommandResult(
+                correlation_id=cmd.correlation_id,
+                device_id=cmd.device_id,
+                status=CommandStatus.timeout,
+                applied=False,
+                reason="ocpp_command_timeout",
+            )
+
+        if status == "error":
+            error_code = "unknown"
+            if isinstance(raw_response, dict):
+                error_code = str(
+                    raw_response.get("error") or raw_response.get("error_code") or "unknown"
+                )
+            return _failed(cmd, f"ocpp_error:{error_code}")
+
+        return _failed(cmd, f"adapter_internal_error:unexpected_protocol_status:{status}")
+
+    def _wrap_internal_error(
+        self, cmd: DeviceCommand, exc: BaseException, *, where: str
+    ) -> CommandResult:
+        logger.warning(
+            "adapter_internal_error",
+            component="adapters",
+            device_id=self.device_id,
+            command_type=type(cmd).__name__,
+            phase=where,
+            error=repr(exc),
+            exc_info=True,
+        )
+        return CommandResult(
+            correlation_id=cmd.correlation_id,
+            device_id=cmd.device_id,
+            status=CommandStatus.failed,
+            applied=False,
+            reason=f"adapter_internal_error:{type(exc).__name__}",
+        )
+
+    def _log_correlation_mismatch(
+        self, cmd: DeviceCommand, protocol_result: ProtocolCommandResult
+    ) -> None:
+        logger.warning(
+            "adapter_correlation_mismatch",
+            component="adapters",
+            device_id=self.device_id,
+            expected=str(cmd.correlation_id),
+            received=str(protocol_result.correlation_id),
+        )
 
     def _to_degraded(self, reason: str, occurred_at: datetime) -> DegradedDeviceState:
         domain_reason = "reconnecting" if reason in _RECONNECTING_REASONS else reason
@@ -147,3 +336,13 @@ class EVChargerAdapter:
             reason=domain_reason,
             occurred_at=occurred_at,
         )
+
+
+def _failed(cmd: DeviceCommand, reason: str) -> CommandResult:
+    return CommandResult(
+        correlation_id=cmd.correlation_id,
+        device_id=cmd.device_id,
+        status=CommandStatus.failed,
+        applied=False,
+        reason=reason,
+    )
diff --git a/src/open_ems/core/commands.py b/src/open_ems/core/commands.py
index 9f2df7c..f826065 100644
--- a/src/open_ems/core/commands.py
+++ b/src/open_ems/core/commands.py
@@ -13,6 +13,13 @@ Three-stage pipeline (Story 8.3 adds RetryPolicy on top of Story 8.2):
 PolicyGuard is the single mandatory dispatch path (AR15). Adapters never receive
 ``send_command()`` calls from any other component.
 
+As of Story 9.0, every controllable adapter (Battery, Inverter, EV Charger)
+honors the cross-adapter command contract documented in
+``open_ems.adapters`` (single source of truth) — closing the AR16 gap that
+existed during Epic 8 (where ``send_command`` was a stub raising
+``NotImplementedError``). DSMR P1 remains read-only (FR6b) and is excluded
+from the controllable-adapter set.
+
 Idempotency classification (``is_idempotent: ClassVar[bool]``) is intrinsic to
 the command type and lives at the class level — NOT as a Pydantic field — so it
 cannot be overridden per instance and so ``ConfigDict(extra="forbid")`` does not
diff --git a/tests/unit/adapters/test_adapter_capabilities.py b/tests/unit/adapters/test_adapter_capabilities.py
index 9477c66..9761ae3 100644
--- a/tests/unit/adapters/test_adapter_capabilities.py
+++ b/tests/unit/adapters/test_adapter_capabilities.py
@@ -63,9 +63,11 @@ async def test_inverter_adapter_capabilities_fronius() -> None:
     assert profile.capability_status == CapabilityStatus.full
     assert profile.device_id == "inv-001"
     assert profile.model == "fronius_gen24_v1"
-    assert WriteCapability.set_operating_mode in profile.write_capabilities
+    # Story 9.0 AC3: inverter is read-only in v1 — write_capabilities is empty.
+    assert profile.write_capabilities == frozenset()
     assert WriteCapability.set_charge_rate not in profile.write_capabilities
     assert WriteCapability.set_discharge_rate not in profile.write_capabilities
+    assert WriteCapability.set_operating_mode not in profile.write_capabilities
 
 
 @pytest.mark.asyncio
diff --git a/tests/unit/adapters/test_capability_registry.py b/tests/unit/adapters/test_capability_registry.py
index a14316c..561f505 100644
--- a/tests/unit/adapters/test_capability_registry.py
+++ b/tests/unit/adapters/test_capability_registry.py
@@ -22,7 +22,8 @@ def test_get_profile_fronius_returns_full_profile() -> None:
     assert profile.capability_status == CapabilityStatus.full
     assert ReadCapability.state in profile.read_capabilities
     assert ReadCapability.power in profile.read_capabilities
-    assert WriteCapability.set_operating_mode in profile.write_capabilities
+    # Story 9.0 AC3: v1 inverter is read-only.
+    assert profile.write_capabilities == frozenset()
 
 
 def test_get_profile_inverter_does_not_have_charge_or_discharge() -> None:
@@ -34,13 +35,14 @@ def test_get_profile_inverter_does_not_have_charge_or_discharge() -> None:
 def test_get_profile_huawei_returns_full_profile() -> None:
     profile = get_profile("dev-1", "huawei_sun2000_v3")
     assert profile.capability_status == CapabilityStatus.full
-    assert WriteCapability.set_operating_mode in profile.write_capabilities
+    assert profile.write_capabilities == frozenset()
 
 
 def test_get_profile_growatt_returns_full_profile() -> None:
     profile = get_profile("dev-1", "growatt_hybrid_v1")
     assert profile.capability_status == CapabilityStatus.full
     assert ReadCapability.power in profile.read_capabilities
+    assert profile.write_capabilities == frozenset()
 
 
 # ---------------------------------------------------------------------------
@@ -54,7 +56,8 @@ def test_get_profile_byd_hvs_returns_full_profile() -> None:
     assert ReadCapability.soc in profile.read_capabilities
     assert WriteCapability.set_charge_rate in profile.write_capabilities
     assert WriteCapability.set_discharge_rate in profile.write_capabilities
-    assert WriteCapability.set_operating_mode in profile.write_capabilities
+    # Story 9.0 AC10: set_operating_mode removed from BYD profiles for v1.
+    assert WriteCapability.set_operating_mode not in profile.write_capabilities
 
 
 def test_get_profile_byd_hvm_returns_full_profile() -> None:
```

## Section B — New test files (untracked)

### tests/unit/adapters/modbus/register_maps/test_byd_command_payload.py

```python
"""Unit tests for BYD HVS / HVM ``command_payload`` encoding (Story 9.0 AC2)."""

from __future__ import annotations

import pytest

from open_ems.adapters.modbus.register_maps import BydHvmV1, BydHvsV1
from open_ems.core.commands import (
    CommandOrigin,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
)
from open_ems.core.devices import DeviceRole

# Setpoint write registers — must match the placeholder addresses in the maps.
HVS_REG_CHARGE = 110
HVS_REG_DISCHARGE = 111
HVM_REG_CHARGE = 210
HVM_REG_DISCHARGE = 211

REGISTER_MAPS_AND_REGS = [
    pytest.param(BydHvsV1(), HVS_REG_CHARGE, HVS_REG_DISCHARGE, id="byd_hvs_v1"),
    pytest.param(BydHvmV1(), HVM_REG_CHARGE, HVM_REG_DISCHARGE, id="byd_hvm_v1"),
]


def _make_charge_cmd(rate_kw: float) -> SetBatteryChargeRateCommand:
    return SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


def _make_discharge_cmd(rate_kw: float) -> SetBatteryDischargeRateCommand:
    return SetBatteryDischargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_charge_nominal(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    cmd = _make_charge_cmd(rate_kw=3.5)
    payload = register_map.command_payload(cmd)  # type: ignore[attr-defined]
    assert payload == {
        "operation": "write_register",
        "address": charge_reg,
        "value": 3500,  # 3.5 kW → 3500 W
    }


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_discharge_nominal(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    cmd = _make_discharge_cmd(rate_kw=2.0)
    payload = register_map.command_payload(cmd)  # type: ignore[attr-defined]
    assert payload == {
        "operation": "write_register",
        "address": discharge_reg,
        "value": 2000,
    }


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_zero_rate_is_valid(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    """0.0 kW is a valid setpoint (effectively idle/stop)."""
    cmd = _make_charge_cmd(rate_kw=0.0)
    payload = register_map.command_payload(cmd)  # type: ignore[attr-defined]
    assert payload["value"] == 0


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_max_boundary(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    """65.535 kW is exactly the uint16 ceiling."""
    cmd = _make_charge_cmd(rate_kw=65.535)
    payload = register_map.command_payload(cmd)  # type: ignore[attr-defined]
    assert payload["value"] == 65_535


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_above_max_raises_value_error(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    """Above-max rate raises ValueError so the adapter wraps it as register_map_encoding_error."""
    cmd = _make_charge_cmd(rate_kw=100.0)  # 100 000 W exceeds 65 535
    with pytest.raises(ValueError, match="exceeds register-map max"):
        register_map.command_payload(cmd)  # type: ignore[attr-defined]


@pytest.mark.parametrize("register_map, charge_reg, discharge_reg", REGISTER_MAPS_AND_REGS)
def test_command_payload_rounding(
    register_map: object, charge_reg: int, discharge_reg: int
) -> None:
    """Sub-watt precision rounds to nearest integer watt."""
    cmd = _make_charge_cmd(rate_kw=3.4567)
    payload = register_map.command_payload(cmd)  # type: ignore[attr-defined]
    # 3.4567 * 1000 = 3456.7 → rounds to 3457
    assert payload["value"] == 3457
```

### tests/unit/adapters/modbus/test_battery_adapter_send_command.py

```python
"""Unit tests for ``BatteryAdapter.send_command`` (Story 9.0).

Covers AC2, AC5, AC6, AC7 (timeout reason form), AC8, AC9, AC11.1–AC11.9 for the
battery scope. Uses a fake ``ModbusTcpAdapter``-shaped protocol adapter to drive
``ProtocolCommandResult`` outcomes deterministically.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import structlog.testing

from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.protocol import (
    ProtocolCommandResult,
    RawProtocolCommand,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandStatus,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.core.devices import DeviceRole

# ---------------------------------------------------------------------------
# Fake protocol adapter — programmable per test
# ---------------------------------------------------------------------------


class FakeModbusProtocolAdapter:
    """Captures send_raw_command calls and returns programmed results."""

    def __init__(self) -> None:
        self._next_result: ProtocolCommandResult | None = None
        self._next_exc: BaseException | None = None
        self._cancel: bool = False
        self.last_command: RawProtocolCommand | None = None
        self.send_calls: int = 0

    def program_result(self, result: ProtocolCommandResult) -> None:
        self._next_result = result
        self._next_exc = None

    def program_exception(self, exc: BaseException) -> None:
        self._next_exc = exc
        self._next_result = None

    def program_cancellation(self) -> None:
        self._cancel = True

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        self.send_calls += 1
        self.last_command = command
        if self._cancel:
            raise asyncio.CancelledError()
        if self._next_exc is not None:
            raise self._next_exc
        assert self._next_result is not None, "must program a result first"
        return self._next_result

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(model: str = "byd_hvs_v1") -> tuple[BatteryAdapter, FakeModbusProtocolAdapter]:
    fake = FakeModbusProtocolAdapter()
    adapter = BatteryAdapter(
        device_id="bat-001",
        protocol_adapter=fake,  # type: ignore[arg-type]
        model=model,
    )
    return adapter, fake


def _charge_cmd(rate_kw: float = 3.0) -> SetBatteryChargeRateCommand:
    return SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


def _discharge_cmd(rate_kw: float = 2.0) -> SetBatteryDischargeRateCommand:
    return SetBatteryDischargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


def _make_protocol_result(
    cmd_correlation_id: uuid.UUID,
    *,
    status: str = "acked",
    raw_response: dict[str, Any] | None = None,
    correlation_id_override: str | None = None,
) -> ProtocolCommandResult:
    if raw_response is None:
        raw_response = {"operation": "write_register", "address": 110, "value": 3000}
    return ProtocolCommandResult(
        correlation_id=correlation_id_override or str(cmd_correlation_id),
        device_id="bat-001",
        protocol_status=status,  # type: ignore[arg-type]
        raw_response=raw_response,
    )


# ---------------------------------------------------------------------------
# AC11.1 — Success path (charge, discharge, both BYD models)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["byd_hvs_v1", "byd_hvm_v1"])
async def test_send_command_charge_success(model: str) -> None:
    adapter, fake = _make_adapter(model=model)
    cmd = _charge_cmd(rate_kw=3.0)
    fake.program_result(_make_protocol_result(cmd.correlation_id, status="acked"))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.success
    assert result.applied is True
    assert result.reason == "ok"
    assert result.correlation_id == cmd.correlation_id
    assert result.observed_state is None  # contract clause 7
    assert fake.send_calls == 1
    sent = fake.last_command
    assert sent is not None
    assert sent.command_name == "set_battery_charge_rate"
    assert sent.correlation_id == str(cmd.correlation_id)
    # payload encodes 3 kW = 3000 W
    assert sent.payload["value"] == 3000  # type: ignore[index]


async def test_send_command_discharge_success() -> None:
    adapter, fake = _make_adapter()
    cmd = _discharge_cmd(rate_kw=2.5)
    fake.program_result(_make_protocol_result(cmd.correlation_id, status="acked"))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.success
    assert result.applied is True
    assert fake.last_command is not None
    assert fake.last_command.command_name == "set_battery_discharge_rate"
    assert fake.last_command.payload["value"] == 2500  # type: ignore[index]


# ---------------------------------------------------------------------------
# AC11.2 — Timeout
# ---------------------------------------------------------------------------


async def test_send_command_timeout_returns_modbus_command_timeout() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_result(
        _make_protocol_result(
            cmd.correlation_id,
            status="timeout",
            raw_response={"error": "modbus_timeout"},
        )
    )

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.timeout
    assert result.applied is False
    assert result.reason == "modbus_command_timeout"
    assert result.correlation_id == cmd.correlation_id


# ---------------------------------------------------------------------------
# AC11.3 — Protocol error
# ---------------------------------------------------------------------------


async def test_send_command_protocol_error_returns_modbus_error_with_code() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_result(
        _make_protocol_result(
            cmd.correlation_id,
            status="error",
            raw_response={"error": "modbus_exception"},
        )
    )

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "modbus_error:modbus_exception"


# ---------------------------------------------------------------------------
# AC11.5 — Unsupported command type → TypeError
# ---------------------------------------------------------------------------


async def test_send_command_with_ev_command_raises_typeerror() -> None:
    adapter, _ = _make_adapter()
    cmd = SetEVChargingRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=3.0,
    )
    with pytest.raises(TypeError, match="BatteryAdapter does not support"):
        await adapter.send_command(cmd)


async def test_send_command_with_stop_command_raises_typeerror() -> None:
    adapter, _ = _make_adapter()
    cmd = StopEVChargingCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
    )
    with pytest.raises(TypeError, match="BatteryAdapter does not support"):
        await adapter.send_command(cmd)


# ---------------------------------------------------------------------------
# AC11.6 — Adapter internal error (RuntimeError mid-encoding) wrapped per AR16
# ---------------------------------------------------------------------------


async def test_send_command_protocol_layer_unexpected_exception_wrapped() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_exception(RuntimeError("library version mismatch"))

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "adapter_internal_error:RuntimeError"
    assert any(e["event"] == "adapter_internal_error" for e in logs)


# ---------------------------------------------------------------------------
# AC11.7 — CancelledError propagates without wrapping
# ---------------------------------------------------------------------------


async def test_send_command_cancellation_propagates_without_wrapping() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_cancellation()

    with pytest.raises(asyncio.CancelledError):
        await adapter.send_command(cmd)


# ---------------------------------------------------------------------------
# AC11.8 — correlation_id mismatch → failed/correlation_id_mismatch
# ---------------------------------------------------------------------------


async def test_send_command_correlation_id_mismatch_returns_failed() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    other = uuid.uuid4()
    fake.program_result(
        _make_protocol_result(
            cmd.correlation_id,
            status="acked",
            correlation_id_override=str(other),
        )
    )

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "correlation_id_mismatch"
    assert result.correlation_id == cmd.correlation_id  # NOT the mismatched one
    assert any(e["event"] == "adapter_correlation_mismatch" for e in logs)


async def test_send_command_unparseable_correlation_id_returns_failed() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_result(
        _make_protocol_result(
            cmd.correlation_id,
            status="acked",
            correlation_id_override="not-a-valid-uuid",
        )
    )

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.reason == "correlation_id_mismatch"


# ---------------------------------------------------------------------------
# AC11.9 — Register-map encoding error wrapped as register_map_encoding_error
# ---------------------------------------------------------------------------


async def test_send_command_register_map_encoding_error_wrapped() -> None:
    adapter, fake = _make_adapter()
    # 100 kW exceeds register-map's uint16 ceiling (65.535 kW)
    cmd = _charge_cmd(rate_kw=100.0)

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason.startswith("register_map_encoding_error:")
    # Protocol layer must NOT have been called
    assert fake.send_calls == 0


# ---------------------------------------------------------------------------
# AC8 — correlation_id round-trip is exact on success
# ---------------------------------------------------------------------------


async def test_send_command_correlation_id_round_trip_exact_uuid() -> None:
    adapter, fake = _make_adapter()
    cmd = _charge_cmd()
    fake.program_result(_make_protocol_result(cmd.correlation_id, status="acked"))

    result = await adapter.send_command(cmd)

    assert result.correlation_id == cmd.correlation_id
    assert isinstance(result.correlation_id, uuid.UUID)
```

### tests/unit/adapters/modbus/test_inverter_adapter_send_command.py

```python
"""Unit tests for ``InverterAdapter.send_command`` (Story 9.0 AC3, AC10, AC11).

v1 inverters are read-only — every command type raises ``TypeError`` directly.
PolicyGuard's capability gate is the primary rejection point; the adapter's
TypeError is defense in depth.
"""

from __future__ import annotations

import pytest

from open_ems.adapters.modbus.inverter_adapter import InverterAdapter
from open_ems.core.commands import (
    CommandOrigin,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.core.devices import DeviceRole


class FakeModbusProtocolAdapter:
    """Minimal fake — InverterAdapter.send_command never reaches the protocol layer."""

    async def get_raw_state(self) -> None:  # pragma: no cover
        raise AssertionError("send_command must not call protocol layer")

    async def send_raw_command(self, _: object) -> None:  # pragma: no cover
        raise AssertionError("InverterAdapter.send_command must not call send_raw_command")

    async def close(self) -> None:
        pass


def _make_adapter(model: str = "fronius_gen24_v1") -> InverterAdapter:
    return InverterAdapter(
        device_id="inv-001",
        protocol_adapter=FakeModbusProtocolAdapter(),  # type: ignore[arg-type]
        model=model,
    )


def _battery_charge_cmd() -> SetBatteryChargeRateCommand:
    return SetBatteryChargeRateCommand(
        device_id="inv-001",
        device_role=DeviceRole.inverter,
        origin=CommandOrigin.decision_engine,
        rate_kw=2.0,
    )


def _battery_discharge_cmd() -> SetBatteryDischargeRateCommand:
    return SetBatteryDischargeRateCommand(
        device_id="inv-001",
        device_role=DeviceRole.inverter,
        origin=CommandOrigin.decision_engine,
        rate_kw=2.0,
    )


def _ev_rate_cmd() -> SetEVChargingRateCommand:
    return SetEVChargingRateCommand(
        device_id="inv-001",
        device_role=DeviceRole.inverter,
        origin=CommandOrigin.decision_engine,
        rate_kw=2.0,
    )


def _stop_cmd() -> StopEVChargingCommand:
    return StopEVChargingCommand(
        device_id="inv-001",
        device_role=DeviceRole.inverter,
        origin=CommandOrigin.decision_engine,
    )


@pytest.mark.parametrize(
    "command_factory",
    [_battery_charge_cmd, _battery_discharge_cmd, _ev_rate_cmd, _stop_cmd],
)
@pytest.mark.parametrize(
    "model",
    ["fronius_gen24_v1", "huawei_sun2000_v3", "growatt_hybrid_v1"],
)
async def test_inverter_send_command_raises_typeerror_for_every_command_type(
    command_factory: object, model: str
) -> None:
    adapter = _make_adapter(model=model)
    cmd = command_factory()  # type: ignore[operator]
    with pytest.raises(TypeError, match="InverterAdapter does not support"):
        await adapter.send_command(cmd)
```

### tests/unit/adapters/ocpp/test_charger_adapter_send_command.py

```python
"""Unit tests for ``EVChargerAdapter.send_command`` (Story 9.0).

Covers AC4, AC5, AC6, AC8, AC9, AC11.1–AC11.10 (EV charger scope) including the
unique OCPP-only cases: rejected (response.status="Rejected"), no-active-OCPP-
transaction stop, and SetChargingProfile payload composition.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import structlog.testing

from open_ems.adapters.ocpp.charger_adapter import EVChargerAdapter
from open_ems.adapters.protocol import (
    ProtocolCommandResult,
    RawProtocolCommand,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandStatus,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.core.devices import DeviceRole

# ---------------------------------------------------------------------------
# Fake OCPPChargerAdapter — programmable per test
# ---------------------------------------------------------------------------


class FakeOCPPProtocolAdapter:
    """Captures send_raw_command calls and exposes adapter-shaped accessors."""

    def __init__(self) -> None:
        self.active_transaction_id: int | None = 42
        self._next_profile_id: int = 7
        self._next_result: ProtocolCommandResult | None = None
        self._next_exc: BaseException | None = None
        self._cancel: bool = False
        self.last_command: RawProtocolCommand | None = None
        self.send_calls: int = 0
        self.profile_reservations: int = 0

    def reserve_charging_profile_id(self) -> int:
        self.profile_reservations += 1
        pid = self._next_profile_id
        self._next_profile_id += 1
        return pid

    def program_result(self, result: ProtocolCommandResult) -> None:
        self._next_result = result
        self._next_exc = None

    def program_exception(self, exc: BaseException) -> None:
        self._next_exc = exc
        self._next_result = None

    def program_cancellation(self) -> None:
        self._cancel = True

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        self.send_calls += 1
        self.last_command = command
        if self._cancel:
            raise asyncio.CancelledError()
        if self._next_exc is not None:
            raise self._next_exc
        assert self._next_result is not None, "must program a result first"
        return self._next_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter() -> tuple[EVChargerAdapter, FakeOCPPProtocolAdapter]:
    fake = FakeOCPPProtocolAdapter()
    adapter = EVChargerAdapter(
        device_id="ev-001",
        protocol_adapter=fake,  # type: ignore[arg-type]
    )
    return adapter, fake


def _rate_cmd(rate_kw: float = 7.0) -> SetEVChargingRateCommand:
    return SetEVChargingRateCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


def _stop_cmd() -> StopEVChargingCommand:
    return StopEVChargingCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
    )


def _accepted_result(
    correlation_id: uuid.UUID, *, correlation_id_override: str | None = None
) -> ProtocolCommandResult:
    return ProtocolCommandResult(
        correlation_id=correlation_id_override or str(correlation_id),
        device_id="ev-001",
        protocol_status="acked",
        raw_response={"status": "Accepted"},
    )


def _rejected_result(correlation_id: uuid.UUID) -> ProtocolCommandResult:
    return ProtocolCommandResult(
        correlation_id=str(correlation_id),
        device_id="ev-001",
        protocol_status="acked",
        raw_response={"status": "Rejected"},
    )


def _timeout_result(correlation_id: uuid.UUID) -> ProtocolCommandResult:
    return ProtocolCommandResult(
        correlation_id=str(correlation_id),
        device_id="ev-001",
        protocol_status="timeout",
        raw_response={"error": "ocpp_timeout"},
    )


def _error_result(
    correlation_id: uuid.UUID, error_code: str = "NotImplemented"
) -> ProtocolCommandResult:
    return ProtocolCommandResult(
        correlation_id=str(correlation_id),
        device_id="ev-001",
        protocol_status="error",
        raw_response={"error_code": error_code, "error_description": "no"},
    )


# ---------------------------------------------------------------------------
# AC11.1 — Success path: SetChargingProfile + Accepted
# ---------------------------------------------------------------------------


async def test_set_rate_success_returns_success_with_ok_reason() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd(rate_kw=7.0)
    fake.program_result(_accepted_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.success
    assert result.applied is True
    assert result.reason == "ok"
    assert result.correlation_id == cmd.correlation_id
    assert result.observed_state is None

    sent = fake.last_command
    assert sent is not None
    assert sent.command_name == "SetChargingProfile"
    assert isinstance(sent.payload, dict)
    payload: dict[str, Any] = sent.payload  # type: ignore[assignment]
    assert payload["connector_id"] == 1
    profile = payload["cs_charging_profiles"]
    assert profile["charging_profile_purpose"] == "TxProfile"
    assert profile["charging_profile_kind"] == "Absolute"
    assert profile["stack_level"] == 0
    assert profile["charging_profile_id"] == 7  # first reservation
    schedule = profile["charging_schedule"]
    assert schedule["charging_rate_unit"] == "W"
    assert schedule["charging_schedule_period"] == [{"start_period": 0, "limit": 7000}]
    assert fake.profile_reservations == 1


async def test_set_rate_zero_kw_emits_zero_limit() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd(rate_kw=0.0)
    fake.program_result(_accepted_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.success
    profile = fake.last_command.payload["cs_charging_profiles"]  # type: ignore[index]
    assert profile["charging_schedule"]["charging_schedule_period"][0]["limit"] == 0


# ---------------------------------------------------------------------------
# AC11.4 — OCPP-only: response status="Rejected" → CommandResult(rejected)
# ---------------------------------------------------------------------------


async def test_set_rate_rejected_response_returns_rejected() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_result(_rejected_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.rejected
    assert result.applied is False
    assert result.reason == "ocpp_rejected:Rejected"


async def test_set_rate_acked_with_unknown_status_treated_as_rejected() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_result(
        ProtocolCommandResult(
            correlation_id=str(cmd.correlation_id),
            device_id="ev-001",
            protocol_status="acked",
            raw_response={"status": "NotSupported"},
        )
    )

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.rejected
    assert result.reason == "ocpp_rejected:NotSupported"


# ---------------------------------------------------------------------------
# AC11.2 — Timeout
# ---------------------------------------------------------------------------


async def test_set_rate_timeout_returns_ocpp_command_timeout() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_result(_timeout_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.timeout
    assert result.applied is False
    assert result.reason == "ocpp_command_timeout"


# ---------------------------------------------------------------------------
# AC11.3 — Protocol error
# ---------------------------------------------------------------------------


async def test_set_rate_protocol_error_returns_ocpp_error_with_code() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_result(_error_result(cmd.correlation_id, error_code="GenericError"))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "ocpp_error:GenericError"


# ---------------------------------------------------------------------------
# AC11.5 — Unsupported command type → TypeError
# ---------------------------------------------------------------------------


async def test_send_command_with_battery_charge_command_raises_typeerror() -> None:
    adapter, _ = _make_adapter()
    cmd = SetBatteryChargeRateCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=3.0,
    )
    with pytest.raises(TypeError, match="EVChargerAdapter does not support"):
        await adapter.send_command(cmd)


async def test_send_command_with_battery_discharge_command_raises_typeerror() -> None:
    adapter, _ = _make_adapter()
    cmd = SetBatteryDischargeRateCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=3.0,
    )
    with pytest.raises(TypeError, match="EVChargerAdapter does not support"):
        await adapter.send_command(cmd)


# ---------------------------------------------------------------------------
# AC11.6 — Adapter internal error wrapped per AR16
# ---------------------------------------------------------------------------


async def test_send_command_protocol_layer_unexpected_exception_wrapped() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_exception(RuntimeError("library version mismatch"))

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.reason == "adapter_internal_error:RuntimeError"
    assert any(e["event"] == "adapter_internal_error" for e in logs)


# ---------------------------------------------------------------------------
# AC11.7 — CancelledError propagates
# ---------------------------------------------------------------------------


async def test_send_command_cancellation_propagates_without_wrapping() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_cancellation()

    with pytest.raises(asyncio.CancelledError):
        await adapter.send_command(cmd)


# ---------------------------------------------------------------------------
# AC11.8 — correlation_id mismatch
# ---------------------------------------------------------------------------


async def test_send_command_correlation_id_mismatch_returns_failed() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    other = uuid.uuid4()
    fake.program_result(_accepted_result(cmd.correlation_id, correlation_id_override=str(other)))

    with structlog.testing.capture_logs() as logs:
        result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.reason == "correlation_id_mismatch"
    assert result.correlation_id == cmd.correlation_id
    assert any(e["event"] == "adapter_correlation_mismatch" for e in logs)


# ---------------------------------------------------------------------------
# AC11.10 — StopEVChargingCommand without active transaction
# ---------------------------------------------------------------------------


async def test_stop_command_without_active_transaction_returns_failed() -> None:
    adapter, fake = _make_adapter()
    fake.active_transaction_id = None
    cmd = _stop_cmd()

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "no_active_ocpp_transaction"
    # Protocol layer must NOT have been called — avoids the stale-id stop hazard
    assert fake.send_calls == 0


async def test_stop_command_with_active_transaction_dispatches_remote_stop() -> None:
    adapter, fake = _make_adapter()
    fake.active_transaction_id = 42
    cmd = _stop_cmd()
    fake.program_result(_accepted_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.success
    sent = fake.last_command
    assert sent is not None
    assert sent.command_name == "RemoteStopTransaction"
    assert sent.payload == {"transaction_id": 42}


async def test_stop_command_acked_rejected_returns_rejected() -> None:
    adapter, fake = _make_adapter()
    fake.active_transaction_id = 42
    cmd = _stop_cmd()
    fake.program_result(_rejected_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.rejected
    assert result.reason == "ocpp_rejected:Rejected"


# ---------------------------------------------------------------------------
# correlation_id round-trip — exact UUID equality on success
# ---------------------------------------------------------------------------


async def test_send_command_correlation_id_round_trip_exact_uuid() -> None:
    adapter, fake = _make_adapter()
    cmd = _rate_cmd()
    fake.program_result(_accepted_result(cmd.correlation_id))

    result = await adapter.send_command(cmd)

    assert result.correlation_id == cmd.correlation_id
    assert isinstance(result.correlation_id, uuid.UUID)
```

### tests/unit/adapters/test_cross_adapter_consistency.py

```python
"""Cross-adapter consistency tests (Story 9.0 AC11).

Asserts that every controllable adapter (Battery, EV Charger) honors the same
shape of CommandResult for each protocol outcome. Inverter is read-only and is
exercised separately for its TypeError-only stance (AC3).

Reason-prefix invariant (AC5):
- Success: ``reason == "ok"`` exactly
- Timeout: ``reason == f"{protocol}_command_timeout"``
- Error:   ``reason`` starts with ``f"{protocol}_"`` (concrete code suffix)
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.ocpp.charger_adapter import EVChargerAdapter
from open_ems.adapters.protocol import (
    ProtocolCommandResult,
    RawProtocolCommand,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandStatus,
    DeviceCommand,
    SetBatteryChargeRateCommand,
    SetEVChargingRateCommand,
)
from open_ems.core.devices import DeviceRole

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeModbus:
    def __init__(self) -> None:
        self.next_result: ProtocolCommandResult | None = None

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        assert self.next_result is not None
        return self.next_result

    async def close(self) -> None:
        pass


class _FakeOCPP:
    def __init__(self) -> None:
        self.next_result: ProtocolCommandResult | None = None
        self.active_transaction_id: int | None = 99
        self._next_profile_id: int = 1

    def reserve_charging_profile_id(self) -> int:
        pid = self._next_profile_id
        self._next_profile_id += 1
        return pid

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        assert self.next_result is not None
        return self.next_result


# ---------------------------------------------------------------------------
# Adapter setup
# ---------------------------------------------------------------------------


def _battery_setup() -> tuple[BatteryAdapter, _FakeModbus, DeviceCommand, str]:
    fake = _FakeModbus()
    adapter = BatteryAdapter("bat-1", fake, "byd_hvs_v1")  # type: ignore[arg-type]
    cmd = SetBatteryChargeRateCommand(
        device_id="bat-1",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )
    return adapter, fake, cmd, "modbus"


def _ev_setup() -> tuple[EVChargerAdapter, _FakeOCPP, DeviceCommand, str]:
    fake = _FakeOCPP()
    adapter = EVChargerAdapter("ev-1", fake)  # type: ignore[arg-type]
    cmd = SetEVChargingRateCommand(
        device_id="ev-1",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=3.0,
    )
    return adapter, fake, cmd, "ocpp"


ADAPTER_SETUPS: list[tuple[str, Callable[[], tuple[Any, Any, DeviceCommand, str]]]] = [
    ("battery", _battery_setup),
    ("ev_charger", _ev_setup),
]


def _success_payload(protocol: str) -> dict[str, Any]:
    if protocol == "modbus":
        return {"operation": "write_register", "address": 110, "value": 1000}
    return {"status": "Accepted"}


# ---------------------------------------------------------------------------
# Cross-adapter parametrized tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name, setup", ADAPTER_SETUPS)
async def test_success_path_returns_success_applied_true_reason_ok(
    name: str, setup: Callable[[], tuple[Any, Any, DeviceCommand, str]]
) -> None:
    adapter, fake, cmd, protocol = setup()
    fake.next_result = ProtocolCommandResult(
        correlation_id=str(cmd.correlation_id),
        device_id=cmd.device_id,
        protocol_status="acked",
        raw_response=_success_payload(protocol),
    )

    result = await adapter.send_command(cmd)

    # AC5: applied=True ⇔ status=success (Pydantic invariant)
    assert result.status is CommandStatus.success
    assert result.applied is True
    assert result.reason == "ok"
    assert result.observed_state is None  # contract clause 7
    assert result.correlation_id == cmd.correlation_id


@pytest.mark.parametrize("name, setup", ADAPTER_SETUPS)
async def test_timeout_reason_has_protocol_command_timeout_form(
    name: str, setup: Callable[[], tuple[Any, Any, DeviceCommand, str]]
) -> None:
    adapter, fake, cmd, protocol = setup()
    fake.next_result = ProtocolCommandResult(
        correlation_id=str(cmd.correlation_id),
        device_id=cmd.device_id,
        protocol_status="timeout",
        raw_response={"error": f"{protocol}_timeout"},
    )

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.timeout
    assert result.applied is False
    assert result.reason == f"{protocol}_command_timeout"


@pytest.mark.parametrize("name, setup", ADAPTER_SETUPS)
async def test_error_reason_starts_with_protocol_prefix(
    name: str, setup: Callable[[], tuple[Any, Any, DeviceCommand, str]]
) -> None:
    adapter, fake, cmd, protocol = setup()
    fake.next_result = ProtocolCommandResult(
        correlation_id=str(cmd.correlation_id),
        device_id=cmd.device_id,
        protocol_status="error",
        raw_response={"error": "some_failure", "error_code": "some_failure"},
    )

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason.startswith(f"{protocol}_")


@pytest.mark.parametrize("name, setup", ADAPTER_SETUPS)
async def test_correlation_id_mismatch_uses_canonical_reason(
    name: str, setup: Callable[[], tuple[Any, Any, DeviceCommand, str]]
) -> None:
    adapter, fake, cmd, protocol = setup()
    other = uuid.uuid4()
    fake.next_result = ProtocolCommandResult(
        correlation_id=str(other),
        device_id=cmd.device_id,
        protocol_status="acked",
        raw_response=_success_payload(protocol),
    )

    result = await adapter.send_command(cmd)

    assert result.status is CommandStatus.failed
    assert result.applied is False
    assert result.reason == "correlation_id_mismatch"
    assert result.correlation_id == cmd.correlation_id


@pytest.mark.parametrize("name, setup", ADAPTER_SETUPS)
async def test_pydantic_invariant_applied_iff_success_holds(
    name: str, setup: Callable[[], tuple[Any, Any, DeviceCommand, str]]
) -> None:
    """Each non-success path must report applied=False; success must report applied=True."""
    adapter, fake, cmd, protocol = setup()
    for protocol_status, raw_response in [
        ("acked", _success_payload(protocol)),
        ("timeout", {"error": "x"}),
        ("error", {"error": "x", "error_code": "x"}),
    ]:
        fake.next_result = ProtocolCommandResult(
            correlation_id=str(cmd.correlation_id),
            device_id=cmd.device_id,
            protocol_status=protocol_status,  # type: ignore[arg-type]
            raw_response=raw_response,
        )
        result = await adapter.send_command(cmd)
        if result.status is CommandStatus.success:
            assert result.applied is True
        else:
            assert result.applied is False


# ---------------------------------------------------------------------------
# Inverter — TypeError-only stance is its own consistency requirement
# ---------------------------------------------------------------------------


async def test_inverter_send_command_consistently_typeerrors_for_all_command_types() -> None:
    from open_ems.adapters.modbus.inverter_adapter import InverterAdapter
    from open_ems.core.commands import (
        SetBatteryChargeRateCommand as _Charge,
    )
    from open_ems.core.commands import (
        SetBatteryDischargeRateCommand as _Discharge,
    )
    from open_ems.core.commands import (
        SetEVChargingRateCommand as _EV,
    )
    from open_ems.core.commands import (
        StopEVChargingCommand as _Stop,
    )

    class _NoOpModbus:
        async def get_raw_state(self) -> Any:  # pragma: no cover
            raise AssertionError

        async def send_raw_command(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

        async def close(self) -> None:
            pass

    adapter = InverterAdapter("inv-1", _NoOpModbus(), "fronius_gen24_v1")  # type: ignore[arg-type]
    common_kwargs: dict[str, Any] = {
        "device_id": "inv-1",
        "device_role": DeviceRole.inverter,
        "origin": CommandOrigin.decision_engine,
    }
    for cmd in (
        _Charge(rate_kw=1.0, **common_kwargs),
        _Discharge(rate_kw=1.0, **common_kwargs),
        _EV(rate_kw=1.0, **common_kwargs),
        _Stop(**common_kwargs),
    ):
        with pytest.raises(TypeError):
            await adapter.send_command(cmd)


# Suppress unused-import lint when uuid is referenced only via fakes
_ = (asyncio,)
```

### tests/unit/adapters/test_cancellation_propagation.py

```python
"""Cross-adapter cancellation propagation tests (Story 9.0 AC6, Task 8).

Asserts:
1. CancelledError raised mid-``send_command`` propagates without being wrapped
   into a CommandResult (cross-adapter contract clause 5).
2. The adapter does NOT emit a parallel cancellation audit / structured log —
   ``RetryPolicy._emit_cancellation_audit`` is the single emitter.
3. Modbus ``_lock`` is released on cancellation (no resource leak).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import structlog.testing

from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.modbus.tcp import (
    ModbusRegisterRange,
    ModbusTcpAdapter,
    ModbusTcpAdapterConfig,
)
from open_ems.adapters.ocpp.charger_adapter import EVChargerAdapter
from open_ems.adapters.protocol import (
    ProtocolCommandResult,
    RawProtocolCommand,
)
from open_ems.core.commands import (
    CommandOrigin,
    SetBatteryChargeRateCommand,
    SetEVChargingRateCommand,
)
from open_ems.core.devices import DeviceRole

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _BlockingModbus:
    """Suspends ``send_raw_command`` until cancelled — Modbus-shaped."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cleanup_observed = False

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        self.entered.set()
        try:
            await asyncio.sleep(60.0)
            raise AssertionError("not reached")
        finally:
            self.cleanup_observed = True

    async def close(self) -> None:
        pass


class _BlockingOCPP:
    """Suspends ``send_raw_command`` until cancelled — OCPP-shaped."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cleanup_observed = False
        self.active_transaction_id: int | None = 1
        self._next_profile_id: int = 1

    def reserve_charging_profile_id(self) -> int:
        pid = self._next_profile_id
        self._next_profile_id += 1
        return pid

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        self.entered.set()
        try:
            await asyncio.sleep(60.0)
            raise AssertionError("not reached")
        finally:
            self.cleanup_observed = True


# ---------------------------------------------------------------------------
# Battery cancellation propagation
# ---------------------------------------------------------------------------


async def test_battery_cancellation_propagates_without_wrapping_or_logging() -> None:
    fake = _BlockingModbus()
    adapter = BatteryAdapter("bat-1", fake, "byd_hvs_v1")  # type: ignore[arg-type]
    cmd = SetBatteryChargeRateCommand(
        device_id="bat-1",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )

    with structlog.testing.capture_logs() as logs:
        send_task: asyncio.Task[Any] = asyncio.create_task(adapter.send_command(cmd))
        await fake.entered.wait()
        send_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await send_task

    # No CommandResult is returned on cancellation — contract clause 5.
    assert send_task.cancelled() or send_task.done()
    # Adapter must not log a cancellation event of its own.
    forbidden_events = {
        "device_command_cancelled",
        "retry_cancelled",
        "command_cancelled",
        "adapter_cancelled",
    }
    assert not any(e.get("event") in forbidden_events for e in logs), (
        f"adapter emitted forbidden cancellation log: {logs}"
    )
    # Resource cleanup observed via the protocol layer's try/finally.
    assert fake.cleanup_observed is True


# ---------------------------------------------------------------------------
# EV charger cancellation propagation
# ---------------------------------------------------------------------------


async def test_ev_charger_cancellation_propagates_without_wrapping_or_logging() -> None:
    fake = _BlockingOCPP()
    adapter = EVChargerAdapter("ev-1", fake)  # type: ignore[arg-type]
    cmd = SetEVChargingRateCommand(
        device_id="ev-1",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=3.0,
    )

    with structlog.testing.capture_logs() as logs:
        send_task: asyncio.Task[Any] = asyncio.create_task(adapter.send_command(cmd))
        await fake.entered.wait()
        send_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await send_task

    assert send_task.cancelled() or send_task.done()
    forbidden_events = {
        "device_command_cancelled",
        "retry_cancelled",
        "command_cancelled",
        "adapter_cancelled",
    }
    assert not any(e.get("event") in forbidden_events for e in logs)
    assert fake.cleanup_observed is True


# ---------------------------------------------------------------------------
# Modbus protocol-layer lock is released on cancellation
# ---------------------------------------------------------------------------


class _CancellableClient:
    """Minimal async modbus client that suspends in write_register until cancelled."""

    def __init__(self) -> None:
        self.connected: bool = True
        self.entered = asyncio.Event()

    async def connect(self) -> bool:  # pragma: no cover — already connected
        return True

    def close(self) -> None:
        self.connected = False

    def read_holding_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> Any:  # pragma: no cover
        raise AssertionError("not used in cancellation test")

    def read_input_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> Any:  # pragma: no cover
        raise AssertionError("not used in cancellation test")

    async def write_register(self, address: int, value: int, *, device_id: int = 1) -> Any:
        self.entered.set()
        await asyncio.sleep(60.0)
        raise AssertionError("not reached")

    async def write_registers(
        self, address: int, values: list[int], *, device_id: int = 1
    ) -> Any:  # pragma: no cover
        raise AssertionError("not used in this test")


async def test_modbus_lock_released_on_cancellation() -> None:
    """Mid-write cancellation must release the adapter's _lock so the next call proceeds."""
    client = _CancellableClient()
    config = ModbusTcpAdapterConfig(
        device_id="bat-1",
        host="127.0.0.1",
        port=5020,
        registers=(ModbusRegisterRange(table="holding", start_address=0, count=1),),
        timeout_s=1.0,
    )
    adapter = ModbusTcpAdapter(config, client_factory=lambda _: client)  # type: ignore[arg-type, return-value]

    raw_command = RawProtocolCommand(
        correlation_id=str(uuid.uuid4()),
        device_id="bat-1",
        command_name="set_battery_charge_rate",
        payload={"operation": "write_register", "address": 110, "value": 1000},
    )

    send_task: asyncio.Task[Any] = asyncio.create_task(adapter.send_raw_command(raw_command))
    await client.entered.wait()
    send_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await send_task

    # The lock must have been released — a second send_raw_command should be able
    # to acquire it without blocking. We verify by checking the underlying lock.
    assert not adapter._lock.locked(), "Modbus _lock was not released after cancellation"
```

### tests/integration/adapters/test_battery_send_command.py

```python
"""Integration tests for the real BatteryAdapter end-to-end (Story 9.0 AC11).

Path: control loop trigger → RetryPolicy → PolicyGuard → BatteryAdapter →
ModbusTcpAdapter → simulated pymodbus client → register write → CommandResult.

Uses an in-process fake pymodbus client (no real network) to drive each
ProtocolStatus outcome and verify that the contract holds across the whole
pipeline.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.modbus.tcp import (
    ModbusRegisterRange,
    ModbusTcpAdapter,
    ModbusTcpAdapterConfig,
)
from open_ems.core import (
    BatteryState,
    DeviceRole,
    StateStore,
    SystemOperatingMode,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandStatus,
    SetBatteryChargeRateCommand,
)
from open_ems.engine import PolicyGuard, RetryPolicy
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings
from open_ems.storage.repositories.event_log_repo import EventLogRepo

_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake pymodbus client
# ---------------------------------------------------------------------------


class _FakeModbusResponse:
    def __init__(self, *, registers: list[int] | None = None, error: bool = False) -> None:
        self._registers = registers or []
        self._error = error

    @property
    def registers(self) -> list[int]:
        return self._registers

    def isError(self) -> bool:  # noqa: N802 — pymodbus naming
        return self._error


class _FakeModbusClient:
    def __init__(
        self,
        *,
        write_should_error: bool = False,
        write_should_timeout: bool = False,
    ) -> None:
        self.connected: bool = True
        self.write_calls: list[tuple[int, int]] = []
        self._write_should_error = write_should_error
        self._write_should_timeout = write_should_timeout

    async def connect(self) -> bool:
        return True

    def close(self) -> None:
        self.connected = False

    def read_holding_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> Any:  # pragma: no cover — read path not exercised here
        async def _coro() -> _FakeModbusResponse:
            return _FakeModbusResponse(registers=[0] * count)

        return _coro()

    def read_input_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> Any:  # pragma: no cover
        async def _coro() -> _FakeModbusResponse:
            return _FakeModbusResponse(registers=[0] * count)

        return _coro()

    def write_register(self, address: int, value: int, *, device_id: int = 1) -> Any:
        async def _coro() -> _FakeModbusResponse:
            self.write_calls.append((address, value))
            if self._write_should_timeout:
                await asyncio.sleep(60.0)
            if self._write_should_error:
                return _FakeModbusResponse(error=True)
            return _FakeModbusResponse()

        return _coro()

    def write_registers(
        self, address: int, values: list[int], *, device_id: int = 1
    ) -> Any:  # pragma: no cover
        async def _coro() -> _FakeModbusResponse:
            return _FakeModbusResponse()

        return _coro()


# ---------------------------------------------------------------------------
# Pipeline fixture builder
# ---------------------------------------------------------------------------


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


def _settings(timeout_s: float = 5.0) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        command_max_retries=2,
        command_retry_backoff_seconds=0.0,
    )


def _modbus_adapter(client: _FakeModbusClient, *, timeout_s: float = 1.0) -> ModbusTcpAdapter:
    config = ModbusTcpAdapterConfig(
        device_id="bat-001",
        host="127.0.0.1",
        port=5020,
        registers=(ModbusRegisterRange(table="holding", start_address=0, count=1),),
        timeout_s=timeout_s,
    )
    return ModbusTcpAdapter(config, client_factory=lambda _: client)  # type: ignore[arg-type, return-value]


def _audit_spy_observability() -> tuple[ObservabilityService, AsyncMock]:
    repo = AsyncMock(spec=EventLogRepo)
    obs = ObservabilityService(repo=repo)
    spy = AsyncMock()
    obs.audit = spy  # type: ignore[method-assign]
    return obs, spy


def _charge_command() -> SetBatteryChargeRateCommand:
    return SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=2.5,
        correlation_id=uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# AC11 integration #1 — end-to-end battery charge command, success path
# ---------------------------------------------------------------------------


async def test_end_to_end_battery_charge_success_writes_register() -> None:
    store = await _state_store_with_battery()
    client = _FakeModbusClient()
    modbus = _modbus_adapter(client)
    battery = BatteryAdapter("bat-001", modbus, "byd_hvs_v1")

    obs, audit_spy = _audit_spy_observability()
    settings = _settings()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: battery},
        observability=obs,
        settings=settings,
    )
    retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

    cmd = _charge_command()
    result = await retry.execute(cmd)

    assert result.status is CommandStatus.success
    assert result.applied is True
    assert result.reason == "ok"
    assert result.correlation_id == cmd.correlation_id
    # Register write actually reached the simulated Modbus client.
    assert client.write_calls == [(110, 2500)]  # 2.5 kW → 2500 W at HVS charge register
    # DECISION audit emitted exactly once on success.
    assert audit_spy.await_count == 1
    decision = audit_spy.await_args_list[0]
    assert decision.kwargs["event_type"] == "DECISION"


# ---------------------------------------------------------------------------
# AC11 integration #3 — end-to-end timeout reaches PolicyGuard as protocol timeout
# ---------------------------------------------------------------------------


async def test_end_to_end_battery_timeout_reaches_policy_guard_with_modbus_reason() -> None:
    """AC7: protocol-layer timeout fires first; outer PolicyGuard timeout is defense in depth.

    Setting ``timeout_s=0.05`` on the Modbus adapter ensures the protocol-layer
    ``asyncio.wait_for`` fires well before PolicyGuard's 10s outer timeout.
    """
    store = await _state_store_with_battery()
    client = _FakeModbusClient(write_should_timeout=True)
    modbus = _modbus_adapter(client, timeout_s=0.05)
    battery = BatteryAdapter("bat-001", modbus, "byd_hvs_v1")

    obs, _ = _audit_spy_observability()
    settings = _settings()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: battery},
        observability=obs,
        settings=settings,
    )

    cmd = _charge_command()
    result = await guard.authorize_and_dispatch(cmd)

    assert result.status is CommandStatus.timeout
    assert result.applied is False
    assert result.reason == "modbus_command_timeout"
    # PolicyGuard's outer timeout reason ("command_timeout") MUST NOT be the
    # reason — the protocol timeout fired first.
    assert result.reason != "command_timeout"


# ---------------------------------------------------------------------------
# AC11 integration #4 — RetryPolicy retries against a flaky Modbus server
# ---------------------------------------------------------------------------


class _FlakeyModbusClient(_FakeModbusClient):
    """Fails the first ``failure_count`` writes (with isError), then succeeds."""

    def __init__(self, *, failure_count: int) -> None:
        super().__init__()
        self._remaining_failures = failure_count

    def write_register(self, address: int, value: int, *, device_id: int = 1) -> Any:
        async def _coro() -> _FakeModbusResponse:
            self.write_calls.append((address, value))
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
                return _FakeModbusResponse(error=True)
            return _FakeModbusResponse()

        return _coro()


async def test_end_to_end_battery_retry_recovers_after_transient_modbus_error() -> None:
    """AC11 integration #4: flaky Modbus server; second attempt succeeds via RetryPolicy."""
    store = await _state_store_with_battery()
    client = _FlakeyModbusClient(failure_count=1)
    modbus = _modbus_adapter(client)
    battery = BatteryAdapter("bat-001", modbus, "byd_hvs_v1")

    obs, audit_spy = _audit_spy_observability()
    settings = _settings()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: battery},
        observability=obs,
        settings=settings,
    )
    retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

    cmd = _charge_command()
    result = await retry.execute(cmd)

    assert result.status is CommandStatus.success
    assert result.applied is True
    # Two writes: first failed, second succeeded.
    assert len(client.write_calls) == 2
    # Single DECISION audit on the recovered success.
    assert audit_spy.await_count == 1
    assert audit_spy.await_args_list[0].kwargs["event_type"] == "DECISION"


# ---------------------------------------------------------------------------
# Negative space: PolicyGuard's outer except is not reached on adapter internal error
# ---------------------------------------------------------------------------


class _ExplodingModbusClient(_FakeModbusClient):
    """Raises a non-Modbus exception on write to verify AR16 wrapping."""

    def write_register(self, address: int, value: int, *, device_id: int = 1) -> Any:
        async def _coro() -> _FakeModbusResponse:
            raise RuntimeError("library version mismatch")

        return _coro()


async def test_end_to_end_battery_adapter_internal_error_wrapped_not_propagated() -> None:
    """AC9: adapter wraps unexpected exceptions; PolicyGuard's outer except is unreachable here.

    The Modbus protocol layer translates known exception families into
    ``ProtocolCommandResult(error)``; ``RuntimeError`` is unknown and propagates
    out of ``send_raw_command``. The domain adapter's ``except Exception``
    wraps it as ``adapter_internal_error:RuntimeError``. PolicyGuard's outer
    ``except Exception`` therefore never fires.
    """
    store = await _state_store_with_battery()
    client = _ExplodingModbusClient()
    modbus = _modbus_adapter(client)
    battery = BatteryAdapter("bat-001", modbus, "byd_hvs_v1")

    obs, _ = _audit_spy_observability()
    settings = _settings()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: battery},
        observability=obs,
        settings=settings,
    )

    cmd = _charge_command()
    result = await guard.authorize_and_dispatch(cmd)

    # Adapter wrapped the exception — reason has the canonical prefix.
    assert result.status is CommandStatus.failed
    assert result.reason == "adapter_internal_error:RuntimeError"
    # PolicyGuard's outer wrap would have used ``repr(exc)`` as reason; we
    # verify the adapter reached the result first.
    assert "RuntimeError(" not in result.reason
```

### tests/integration/adapters/test_ev_charger_send_command.py

```python
"""Integration tests for the real EVChargerAdapter end-to-end (Story 9.0 AC11).

Path: control loop trigger → RetryPolicy → PolicyGuard → EVChargerAdapter →
OCPPChargerAdapter → simulated charger over a fake WebSocket → SetChargingProfile
CALL → CommandResult.

Reuses the dual-queue fake WebSocket pattern from the OCPP unit tests.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from open_ems.adapters.ocpp import OCPPAdapterConfig, OCPPChargerAdapter
from open_ems.adapters.ocpp.charger_adapter import EVChargerAdapter
from open_ems.core import (
    DeviceRole,
    EVChargerState,
    StateStore,
    SystemOperatingMode,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandStatus,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.engine import PolicyGuard, RetryPolicy
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings
from open_ems.storage.repositories.event_log_repo import EventLogRepo

_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake WebSocket — mirrors the unit test pattern
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    def __init__(self) -> None:
        self._from_client: asyncio.Queue[str | None] = asyncio.Queue()
        self._to_client: asyncio.Queue[str] = asyncio.Queue()

    async def recv(self) -> str:
        msg = await self._from_client.get()
        if msg is None:
            raise ConnectionError("charger_disconnected")
        return msg

    async def send(self, message: str) -> None:
        await self._to_client.put(message)

    async def client_send(self, message: str) -> None:
        await self._from_client.put(message)

    async def client_recv(self) -> str:
        return await self._to_client.get()

    def close(self) -> None:
        self._from_client.put_nowait(None)


async def _connect_charger(ws: _FakeWebSocket, adapter: OCPPChargerAdapter) -> asyncio.Task[None]:
    loop_task: asyncio.Task[None] = asyncio.create_task(adapter.handle_connection(ws))
    boot_payload = {"chargePointVendor": "Wallbox", "chargePointModel": "Pulsar"}
    await ws.client_send(json.dumps([2, "boot-1", "BootNotification", boot_payload]))
    response = json.loads(await asyncio.wait_for(ws.client_recv(), timeout=2.0))
    assert response[0] == 3
    assert response[2]["status"] == "Accepted"
    return loop_task


# ---------------------------------------------------------------------------
# Pipeline fixture builders
# ---------------------------------------------------------------------------


async def _state_store_with_ev() -> StateStore:
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


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        command_max_retries=2,
        command_retry_backoff_seconds=0.0,
    )


def _audit_spy_observability() -> tuple[ObservabilityService, AsyncMock]:
    repo = AsyncMock(spec=EventLogRepo)
    obs = ObservabilityService(repo=repo)
    spy = AsyncMock()
    obs.audit = spy  # type: ignore[method-assign]
    return obs, spy


def _ocpp_adapter() -> OCPPChargerAdapter:
    return OCPPChargerAdapter(
        OCPPAdapterConfig(
            device_id="ev-001",
            charge_point_id="cp-001",
            command_timeout_s=2.0,
        )
    )


def _ev_rate_command(rate_kw: float = 5.0) -> SetEVChargingRateCommand:
    return SetEVChargingRateCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
        correlation_id=uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# AC11 integration #2 — end-to-end EV charge rate command, success path
# ---------------------------------------------------------------------------


async def test_end_to_end_ev_charge_rate_success_via_set_charging_profile() -> None:
    ws = _FakeWebSocket()
    ocpp = _ocpp_adapter()
    loop_task = await _connect_charger(ws, ocpp)
    try:
        ev_adapter = EVChargerAdapter("ev-001", ocpp)
        store = await _state_store_with_ev()
        obs, audit_spy = _audit_spy_observability()
        settings = _settings()
        guard = PolicyGuard(
            state_store=store,
            adapters={DeviceRole.ev_charger: ev_adapter},
            observability=obs,
            settings=settings,
        )
        retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

        cmd = _ev_rate_command(rate_kw=5.0)

        async def _charger_side() -> dict[str, Any]:
            msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
            call_msg = json.loads(msg_str)
            assert call_msg[2] == "SetChargingProfile"
            payload: dict[str, Any] = call_msg[3]
            await ws.client_send(json.dumps([3, call_msg[1], {"status": "Accepted"}]))
            return payload

        charger_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(_charger_side())

        result = await retry.execute(cmd)
        observed_payload = await asyncio.wait_for(charger_task, timeout=2.0)

        assert result.status is CommandStatus.success
        assert result.applied is True
        assert result.reason == "ok"
        assert result.correlation_id == cmd.correlation_id

        # Verify the SetChargingProfile payload composed by the adapter.
        # OCPP library lowercases / camelcases field names depending on direction;
        # python-ocpp sends camelCase on the wire.
        profile = observed_payload["csChargingProfiles"]
        assert profile["chargingProfilePurpose"] == "TxProfile"
        assert profile["chargingProfileKind"] == "Absolute"
        schedule = profile["chargingSchedule"]
        assert schedule["chargingRateUnit"] == "W"
        assert schedule["chargingSchedulePeriod"] == [{"startPeriod": 0, "limit": 5000}]

        # DECISION audit emitted exactly once.
        assert audit_spy.await_count == 1
        assert audit_spy.await_args_list[0].kwargs["event_type"] == "DECISION"
    finally:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=2.0)


async def test_end_to_end_ev_rejected_response_returns_rejected_no_retry() -> None:
    """Charger returns status=Rejected: result.status=rejected, RetryPolicy stops retrying."""
    ws = _FakeWebSocket()
    ocpp = _ocpp_adapter()
    loop_task = await _connect_charger(ws, ocpp)
    try:
        ev_adapter = EVChargerAdapter("ev-001", ocpp)
        store = await _state_store_with_ev()
        obs, audit_spy = _audit_spy_observability()
        settings = _settings()
        guard = PolicyGuard(
            state_store=store,
            adapters={DeviceRole.ev_charger: ev_adapter},
            observability=obs,
            settings=settings,
        )
        retry = RetryPolicy(policy_guard=guard, observability=obs, settings=settings)

        cmd = _ev_rate_command(rate_kw=5.0)

        async def _charger_side() -> None:
            msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
            call_msg = json.loads(msg_str)
            await ws.client_send(json.dumps([3, call_msg[1], {"status": "Rejected"}]))

        charger_task: asyncio.Task[None] = asyncio.create_task(_charger_side())

        result = await retry.execute(cmd)
        await asyncio.wait_for(charger_task, timeout=2.0)

        assert result.status is CommandStatus.rejected
        assert result.applied is False
        assert result.reason == "ocpp_rejected:Rejected"
        # DEVICE audit on terminal failure (rejected).
        assert audit_spy.await_count == 1
        assert audit_spy.await_args_list[0].kwargs["event_type"] == "DEVICE"
    finally:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=2.0)


async def test_end_to_end_ev_stop_without_active_transaction_returns_failed() -> None:
    """No StartTransaction observed: StopEVChargingCommand → no_active_ocpp_transaction."""
    ws = _FakeWebSocket()
    ocpp = _ocpp_adapter()
    loop_task = await _connect_charger(ws, ocpp)
    try:
        ev_adapter = EVChargerAdapter("ev-001", ocpp)
        store = await _state_store_with_ev()
        obs, _ = _audit_spy_observability()
        settings = _settings()
        guard = PolicyGuard(
            state_store=store,
            adapters={DeviceRole.ev_charger: ev_adapter},
            observability=obs,
            settings=settings,
        )

        stop_cmd = StopEVChargingCommand(
            device_id="ev-001",
            device_role=DeviceRole.ev_charger,
            origin=CommandOrigin.decision_engine,
        )
        result = await guard.authorize_and_dispatch(stop_cmd)

        assert result.status is CommandStatus.failed
        assert result.reason == "no_active_ocpp_transaction"
    finally:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=2.0)


async def test_end_to_end_ev_stop_after_start_transaction_dispatches_remote_stop() -> None:
    """Charger StartTransaction → adapter remembers id → Stop dispatches RemoteStopTransaction."""
    ws = _FakeWebSocket()
    ocpp = _ocpp_adapter()
    loop_task = await _connect_charger(ws, ocpp)
    try:
        # Charger sends StartTransaction; central system replies with a transaction id.
        start_payload = {
            "connectorId": 1,
            "idTag": "user-1",
            "meterStart": 0,
            "timestamp": "2026-05-10T12:00:00Z",
        }
        await ws.client_send(json.dumps([2, "st-1", "StartTransaction", start_payload]))
        start_response = json.loads(await asyncio.wait_for(ws.client_recv(), timeout=2.0))
        assert start_response[0] == 3
        transaction_id = start_response[2]["transactionId"]
        assert isinstance(transaction_id, int) and transaction_id > 0

        ev_adapter = EVChargerAdapter("ev-001", ocpp)
        store = await _state_store_with_ev()
        obs, _ = _audit_spy_observability()
        settings = _settings()
        guard = PolicyGuard(
            state_store=store,
            adapters={DeviceRole.ev_charger: ev_adapter},
            observability=obs,
            settings=settings,
        )

        stop_cmd = StopEVChargingCommand(
            device_id="ev-001",
            device_role=DeviceRole.ev_charger,
            origin=CommandOrigin.decision_engine,
        )

        async def _charger_side() -> dict[str, Any]:
            msg_str = await asyncio.wait_for(ws.client_recv(), timeout=2.0)
            call_msg = json.loads(msg_str)
            assert call_msg[2] == "RemoteStopTransaction"
            payload: dict[str, Any] = call_msg[3]
            await ws.client_send(json.dumps([3, call_msg[1], {"status": "Accepted"}]))
            return payload

        charger_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(_charger_side())

        result = await guard.authorize_and_dispatch(stop_cmd)
        observed_payload = await asyncio.wait_for(charger_task, timeout=2.0)

        assert result.status is CommandStatus.success
        assert observed_payload == {"transactionId": transaction_id}
    finally:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=2.0)


async def test_end_to_end_ev_charge_rate_timeout_returns_ocpp_command_timeout() -> None:
    """AC7: protocol-layer OCPP timeout fires before PolicyGuard's outer 10s timeout."""
    ws = _FakeWebSocket()
    ocpp = OCPPChargerAdapter(
        OCPPAdapterConfig(
            device_id="ev-001",
            charge_point_id="cp-001",
            command_timeout_s=0.1,  # very short to force protocol timeout first
        )
    )
    loop_task = await _connect_charger(ws, ocpp)
    try:
        ev_adapter = EVChargerAdapter("ev-001", ocpp)
        store = await _state_store_with_ev()
        obs, _ = _audit_spy_observability()
        settings = _settings()
        guard = PolicyGuard(
            state_store=store,
            adapters={DeviceRole.ev_charger: ev_adapter},
            observability=obs,
            settings=settings,
        )

        cmd = _ev_rate_command()

        # Charger never responds — protocol timeout fires.
        result = await guard.authorize_and_dispatch(cmd)

        assert result.status is CommandStatus.timeout
        assert result.reason == "ocpp_command_timeout"
        # Drain the leftover CALL so loop_task can exit cleanly.
        try:
            await asyncio.wait_for(ws.client_recv(), timeout=0.5)
        except TimeoutError:
            pass
    finally:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=2.0)
```

