"""Raw protocol adapter contracts for OPEN-EMS.

Cross-adapter command contract (v1.1)
=====================================

Single source of truth for ``DeviceAdapter.send_command(cmd) -> CommandResult``
behavior across all controllable adapters (BatteryAdapter, InverterAdapter,
EVChargerAdapter). Per-adapter docstrings reference this section by path; they
do NOT restate clauses.

GridMeterAdapter is intentionally excluded — DSMR P1 is read-only (FR6b);
``send_command`` raises ``NotImplementedError`` with reason
``"DSMR P1 is read-only"`` and never grows a write path.

1. Authorization is upstream
----------------------------
PolicyGuard authorizes; the adapter executes. Adapters never re-check fail-safe
mode, capability, or safety constraints — those have already passed.

2. Command surface is exhaustive over the adapter's WriteCapability set
-----------------------------------------------------------------------
For every ``WriteCapability`` in the adapter's capability profile, exactly one
``DeviceCommand`` subtype is dispatchable. For every ``DeviceCommand`` subtype
NOT covered, ``send_command(cmd)`` raises
``TypeError(f"{adapter_class} does not support {type(cmd).__name__}")``.
This is intentionally NOT a ``CommandResult`` failure — it is a programming-
error signal (PolicyGuard's capability gate should have caught it).

3. Status mapping table
-----------------------

Source signal → (status, applied, reason form):

- Protocol ``acked`` + affirmative response → (``success``, True, ``"ok"``)
- Protocol ``acked`` + OCPP ``status="Rejected"`` →
  (``rejected``, False, ``"ocpp_rejected:Rejected"``)
- Protocol ``acked`` + OCPP ``status="NotSupported"`` or any other non-Accepted
  non-Rejected value →
  (``failed``, False, ``f"ocpp_unsupported:<status>"``) — permanent capability
  error; RetryPolicy must not retry.
- Protocol layer ``timeout`` →
  (``timeout``, False, ``f"<protocol>_command_timeout"``)
- Protocol layer ``error`` →
  (``failed``, False, ``f"<protocol>_error:<error_code>"``)
- Adapter-internal exception (bug) →
  (``failed``, False, ``f"adapter_internal_error:<exc_type>"``)
- correlation_id mismatch from protocol →
  (``failed``, False, ``"correlation_id_mismatch"``)
- Register-map encoding error (Modbus only) →
  (``failed``, False, ``f"register_map_encoding_error:<msg>"``)
- OCPP stop without active transaction →
  (``failed``, False, ``"no_active_ocpp_transaction"``)

``<protocol>`` ∈ {``modbus``, ``ocpp``}. Audit consumers can group reasons by
protocol family without parsing free-form text. ``ocpp_rejected:*`` is a soft
refusal the charger could grant later; ``ocpp_unsupported:*`` is a permanent
inability to honor the CALL.

4. correlation_id round-trip is exact
-------------------------------------
``result.correlation_id == cmd.correlation_id`` (both ``uuid.UUID``). The
adapter converts ``str(cmd.correlation_id)`` at the protocol-layer boundary
and parses it back when ``ProtocolCommandResult`` returns. Mismatch (or
unparseable) ⇒ ``CommandResult(status=failed, reason="correlation_id_mismatch")``
plus a structured ``adapter_correlation_mismatch`` warning log.

5. Cancellation propagates
--------------------------
``asyncio.CancelledError`` is NEVER caught by the adapter's ``send_command``.
Cleanup happens via ``try/finally``, never ``except CancelledError``.
``RetryPolicy._emit_cancellation_audit`` is the single audit emitter for
cancelled commands. Adapters do NOT emit a parallel cancellation audit.

6. Timeout boundary
-------------------
The adapter's protocol-layer timeout (``ModbusTcpAdapter.config.timeout_s``,
``OCPPAdapterConfig.command_timeout_s``) is the primary boundary. PolicyGuard's
outer ``asyncio.wait_for(..., timeout=COMMAND_DISPATCH_TIMEOUT_SECONDS)`` is
defense in depth — under nominal operation, the protocol timeout always fires
first. Adapters MUST return within ``protocol_timeout_s + epsilon`` under any
non-cancellation condition.

7. observed_state is None on success
------------------------------------
Adapters do NOT issue a post-write read to populate ``observed_state``. The
control loop's polling cycle observes the post-write state on its next tick.
An adapter MAY populate ``observed_state`` only if its protocol returns the
post-write state in the same call (e.g. some Modbus "write+read" patterns);
v1 leaves it None.

8. AR16 enforcement
-------------------
All exceptions except ``asyncio.CancelledError`` (clause 5) and ``TypeError``
(clause 2 — programming-error signal) are caught and wrapped as
``CommandResult(status=failed, applied=False, reason="adapter_internal_error:<type>")``.
The full exception is logged via structlog with ``exc_info=True`` BEFORE the
``CommandResult`` is returned. PolicyGuard's outer ``except Exception`` becomes
defense in depth that should never fire when adapters honor this clause.

9. Idempotency is in the command, not the adapter
-------------------------------------------------
Adapters do NOT consult ``cmd.is_idempotent``. ``RetryPolicy`` reads it. The
adapter's job is to faithfully execute one attempt.

10. Audit emission is upstream
------------------------------
Adapters do NOT emit DECISION / DEVICE / CONSTRAINT audits. PolicyGuard emits
CONSTRAINT on rejection; RetryPolicy emits DECISION on success and DEVICE on
terminal failure / cancellation.

The contract version is exposed as ``COMMAND_CONTRACT_VERSION`` for change
tracking — bump it when any clause above changes.
"""

from open_ems.adapters.protocol import (
    ConnectionStatus,
    ProtocolAdapter,
    ProtocolCommandResult,
    ProtocolDegradedState,
    ProtocolStatus,
    RawDSMRState,
    RawModbusState,
    RawOCPPState,
    RawPayload,
    RawProtocolCommand,
    RawProtocolState,
)

COMMAND_CONTRACT_VERSION = "1.1"

__all__ = [
    "COMMAND_CONTRACT_VERSION",
    "ConnectionStatus",
    "ProtocolAdapter",
    "ProtocolCommandResult",
    "ProtocolDegradedState",
    "ProtocolStatus",
    "RawDSMRState",
    "RawModbusState",
    "RawOCPPState",
    "RawPayload",
    "RawProtocolCommand",
    "RawProtocolState",
]
