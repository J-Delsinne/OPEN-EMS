# Story 9.0: Real Adapter Command Execution Contracts and Implementations

Status: done

> **Origin:** Epic 8 retrospective (2026-05-10) — Story 9.0 is a hard-gate prep story. Until 9.0 is `Status: done` with all reviews resolved, Story 9.1's ACs cannot be authored. Story 9.1's dev notes will cite the contracts produced here verbatim.
>
> **A2-triggered:** True. Matched triggers: **T2** (retries/cancellation across adapters), **T5** (multi-adapter coordination — Battery + Inverter + EV Charger). Partial: T1 (lifecycle), T4 (timing). All R1–R7 orchestration artifacts are mandatory and present below.

## Story

As a developer composing the installer workflow on top of the runtime control execution layer,
I want every controllable adapter (Battery, Inverter, EV Charger) to implement `send_command()` with consistent failure semantics, identical retry/cancellation behavior, lossless `correlation_id` round-trip, and a documented contract that PolicyGuard / RetryPolicy can rely on without per-adapter special cases,
so that Story 9.1 onwards can build the installer site setup wizard on a single, uniform command-execution contract — without rediscovering integration semantics inside each consumer story.

## Acceptance Criteria

### AC1 — Cross-adapter common contract (the canonical reference)

**GIVEN** any of the three controllable domain adapters (`BatteryAdapter`, `InverterAdapter`, `EVChargerAdapter`)
**WHEN** `send_command(cmd)` is called by `PolicyGuard.authorize_and_dispatch()`
**THEN** the implementation MUST honor every clause of the cross-adapter common contract documented in Dev Notes § "Cross-adapter command contract" — verbatim, no per-adapter divergence.
**AND** the contract is referenced as the single source of truth by `BatteryAdapter`, `InverterAdapter`, `EVChargerAdapter` module docstrings.
**AND** `GridMeterAdapter.send_command()` continues to raise `NotImplementedError` with reason "DSMR P1 is read-only" — DSMR is never expected to grow a write path; this is documented in the adapter's docstring referencing AR16 + the read-only nature of the meter capability profile.

---

### AC2 — `BatteryAdapter.send_command()` Modbus write path

**GIVEN** a `BatteryAdapter` configured with a supported model (`byd_hvs_v1` or `byd_hvm_v1`) and wired to a live `ModbusTcpAdapter`
**WHEN** `send_command(cmd)` is called with `SetBatteryChargeRateCommand` or `SetBatteryDischargeRateCommand`
**THEN** the adapter:
1. Calls `BatteryRegisterMap.command_payload(cmd)` (new method on register-map interface) to obtain a `RawProtocolCommand` payload (Modbus `write_register` or `write_registers` operation)
2. Constructs a `RawProtocolCommand` carrying `correlation_id=str(cmd.correlation_id)`, `device_id=self.device_id`, `command_name="set_battery_charge_rate"` or `"set_battery_discharge_rate"`, `payload=<register-map-derived>`
3. Awaits `self._protocol_adapter.send_raw_command(raw_cmd)`
4. Maps the returned `ProtocolCommandResult` to `CommandResult` per the cross-adapter contract status table (AC1)
5. On success path (`protocol_status="acked"`), returns `CommandResult(status=success, applied=True, reason="ok", observed_state=None)`. The next control-loop polling cycle observes the post-write state — observed_state is intentionally NOT populated by an extra read here (avoids round-trip cost; control loop already polls).

**AND** unsupported command types (any `DeviceCommand` subtype other than the two listed) raise `TypeError(f"BatteryAdapter does not support {type(cmd).__name__}")`. This is a programming error path — PolicyGuard's capability gate should prevent it from reaching the adapter, but the assertion is structural defense in depth.

**AND** if `command_payload(cmd)` raises `ValueError` (e.g. rate exceeds register-map's encodable range), the adapter wraps it as `CommandResult(status=failed, applied=False, reason=f"register_map_encoding_error:{exc}")` — never propagates the exception (AR16).

**AND** `correlation_id` round-trip is exact: `result.correlation_id == cmd.correlation_id` (UUID, not the string form sent over the wire). The adapter is responsible for parsing the string returned by `ProtocolCommandResult.correlation_id` back to UUID.

---

### AC3 — `InverterAdapter.send_command()` behavior (currently no command surface)

**GIVEN** the three supported inverter models (`fronius_gen24_v1`, `huawei_sun2000_v3`, `growatt_hybrid_v1`) have no commanded-write capabilities exposed in v1 (capability profiles list `read_capabilities` only, not `write_capabilities`)
**WHEN** `InverterAdapter.send_command(cmd)` is called
**THEN** the adapter raises `TypeError(f"InverterAdapter does not support {type(cmd).__name__}")` for any command type — the inverter is read-only in v1.

**AND** this is structural: PolicyGuard's capability gate (`capability_missing` rejection) will prevent any command from reaching `InverterAdapter.send_command()` in normal operation. The `TypeError` is the same defense-in-depth pattern as AC2.

**AND** the inverter capability profiles in `src/open_ems/adapters/capabilities/inverter.py` are verified to NOT contain any `WriteCapability` member. If any future inverter model adds write support, this AC is revisited; v1 is read-only across all three current models.

**AND** `InverterAdapter.send_command()` no longer raises `NotImplementedError` (that's a runtime-deferred-implementation signal, not a contract signal). It raises `TypeError` (programming-error signal — call site is wrong).

---

### AC4 — `EVChargerAdapter.send_command()` OCPP write path

**GIVEN** an `EVChargerAdapter` wired to a connected `OCPPChargerAdapter`
**WHEN** `send_command(cmd)` is called with `SetEVChargingRateCommand` or `StopEVChargingCommand`
**THEN** the adapter:
1. Constructs a `RawProtocolCommand` whose `command_name` and `payload` map to the appropriate OCPP 1.6 CALL:
   - `SetEVChargingRateCommand(rate_kw=R)` → `command_name="SetChargingProfile"`, `payload={...}` per OCPP 1.6 §5.16 with a `TxProfile` setting `chargingRateUnit="W"`, `limit=int(R*1000)`. Profile shape MUST set `connectorId=1`, `chargingProfileId` and `stackLevel` derived per Dev Notes § "OCPP charging profile composition" (single active TxProfile per session).
   - `StopEVChargingCommand` → `command_name="RemoteStopTransaction"`, `payload={"transactionId": <last_active_transaction_id>}`. The transaction id is read from `_ChargerState.last_call_result` (StartTransaction response observed via OCPP) or `last_status_notification` if available; if no active transaction is known, return `CommandResult(status=failed, applied=False, reason="no_active_ocpp_transaction")` — do NOT issue a stop with a stale id (that's the Story 8.0 retro's "non-idempotent stop" hazard).
2. Awaits `self._protocol_adapter.send_raw_command(raw_cmd)` (the OCPP central system handler, with `command_timeout_s` bounded internally)
3. Maps `ProtocolCommandResult` → `CommandResult` per AC1's status table

**AND** on `protocol_status="acked"` from OCPP: status maps to `success` ONLY if the OCPP response carries an explicit acceptance signal (`status="Accepted"` for SetChargingProfile, `status="Accepted"` for RemoteStopTransaction). If the OCPP response is ACK at the protocol layer but `status="Rejected"`, `CommandResult(status=rejected, applied=False, reason=f"ocpp_rejected:{response_status}")` is returned. This is critical: OCPP `acked` ≠ command applied; the response payload's `status` field is the authoritative signal.

**AND** on `protocol_status="timeout"` → `CommandResult(status=timeout, applied=False, reason="ocpp_command_timeout")`. Timeout outcome is INDETERMINATE — the OCPP charger may have applied the profile despite the response timing out. RetryPolicy's existing timeout-vs-failure distinction (visible in `retry_policy.py:_emit_failure_audit`) handles this correctly for idempotent commands. For `StopEVChargingCommand` (non-idempotent) RetryPolicy will not retry, which is correct.

**AND** on `protocol_status="error"` → `CommandResult(status=failed, applied=False, reason=f"ocpp_error:{raw_response.error_code}")` where `error_code` is extracted from `ProtocolCommandResult.raw_response`.

**AND** unsupported command types raise `TypeError` per AC2's pattern.

---

### AC5 — Cross-adapter status mapping consistency

**GIVEN** the per-adapter mappings in AC2 / AC3 / AC4
**WHEN** `ProtocolCommandResult.protocol_status` values are mapped to `CommandStatus`
**THEN** the mapping is identical across adapters:

| Protocol status | CommandStatus | applied | reason form |
|---|---|---|---|
| `acked` (with affirmative response) | `success` | `True` | `"ok"` |
| `acked` (with negative response, OCPP only) | `rejected` | `False` | `f"ocpp_rejected:{status}"` |
| `timeout` | `timeout` | `False` | `f"{protocol}_command_timeout"` |
| `error` | `failed` | `False` | `f"{protocol}_error:..."` |

**AND** the `applied=True` ⇔ `status=success` Pydantic invariant on `CommandResult` is honored — the test fixture should assert this for every adapter's success path.

**AND** `reason` strings follow the prefix pattern `<protocol>_<error-class>` so audit-log consumers can group by transport family without parsing free-form text. `<protocol>` ∈ {`modbus`, `ocpp`}.

---

### AC6 — Cancellation propagation through the adapter layer

**GIVEN** `RetryPolicy.execute(command)` is awaiting `PolicyGuard.authorize_and_dispatch(command)` which is awaiting `adapter.send_command(command)` which is awaiting `protocol_adapter.send_raw_command(raw_cmd)`
**WHEN** the outer task receives `asyncio.CancelledError` (e.g. control-loop shutdown, Story 8.4 fail-safe transition)
**THEN** the cancellation propagates cooperatively through every layer:
- `protocol_adapter.send_raw_command()` is the cancellation point (it owns `asyncio.wait_for` on the underlying protocol library)
- The domain adapter's `send_command()` does NOT swallow `CancelledError` — it propagates immediately
- The `_lock` (Modbus) / connection state (OCPP) cleanup happens via `try/finally` patterns, NOT by catching `CancelledError`
- `RetryPolicy._emit_cancellation_audit` (existing, Story 8.4) is the single audit emitter for cancelled commands; adapters never emit a cancellation audit themselves

**AND** unit tests verify that injecting `asyncio.CancelledError` mid-`send_command()` propagates without a partial CommandResult being returned — the exception MUST propagate.

**AND** observability: a `device_command_cancelled` structured log line is NOT emitted by the adapter layer (avoiding double-logging — `RetryPolicy.retry_cancelled` already covers it). Any adapter-internal cleanup is logged at DEBUG level only.

---

### AC7 — Timeout boundary compliance

**GIVEN** `PolicyGuard.authorize_and_dispatch()` wraps `adapter.send_command()` in `asyncio.wait_for(..., timeout=COMMAND_DISPATCH_TIMEOUT_SECONDS)` (currently 10.0s)
**WHEN** an adapter's underlying protocol call takes longer than the protocol's own timeout (`ModbusTcpAdapter.config.timeout_s` ≤ 10.0s; `OCPPAdapterConfig.command_timeout_s` ≤ 10.0s)
**THEN** the adapter's protocol-layer timeout fires first and returns `ProtocolCommandResult(protocol_status="timeout")` — the adapter maps this to `CommandResult(status=timeout, applied=False)` and returns
**AND** PolicyGuard's outer `asyncio.wait_for` is therefore never the timeout boundary in nominal operation; it acts as a defense-in-depth bound against a buggy adapter that fails to honor its own timeout

**AND** unit tests verify: with adapter timeout < PolicyGuard timeout, the adapter's `CommandResult.status=timeout` is returned (not PolicyGuard's `command_timeout` reason). Tests use injected sleep delays to verify ordering.

**AND** the adapter contract documents (in module docstrings) that `send_command()` MUST return within `protocol_timeout_s + epsilon` under all non-cancellation conditions.

---

### AC8 — `correlation_id` round-trip integrity

**GIVEN** a `DeviceCommand` carries `correlation_id: uuid.UUID`
**WHEN** the adapter constructs a `RawProtocolCommand` (where `correlation_id: NonEmptyStr`)
**THEN** the adapter passes `str(cmd.correlation_id)` to `RawProtocolCommand.correlation_id`
**AND** when the `ProtocolCommandResult` returns, the adapter parses `result.correlation_id` back to UUID and uses it in the returned `CommandResult.correlation_id`
**AND** if the adapter detects `result.correlation_id != str(cmd.correlation_id)` (buggy protocol layer), it returns `CommandResult(status=failed, applied=False, reason="correlation_id_mismatch")` and emits a structured `adapter_correlation_mismatch` warning log. This closes the deferred finding from Story 8.2 review.

**AND** if `result.correlation_id` cannot be parsed as a UUID (malformed), same handling: `failed`/`correlation_id_mismatch`. The protocol-layer adapter is the source-of-truth for the wire format; the domain adapter is the source-of-truth for the typed UUID.

---

### AC9 — Adapter exception → `CommandResult` wrapping (AR16 enforcement)

**GIVEN** an unexpected exception arises inside any adapter's `send_command()` (e.g. an `AttributeError` from a register map bug; a `ValueError` from an OCPP payload encoding bug; a `RuntimeError` from a library version mismatch)
**WHEN** the exception type is NOT one of: `asyncio.CancelledError` (propagates per AC6), `TypeError` (programming-error path per AC2/AC3/AC4 — propagates so test suites surface the bug)
**THEN** the adapter catches it via `except Exception` and returns `CommandResult(status=failed, applied=False, reason=f"adapter_internal_error:{type(exc).__name__}")` — never propagates to the control loop. The full exception is logged via structlog with `exc_info=True`.

**AND** PolicyGuard's existing outer `except Exception` (lines 125-139 of `policy_guard.py`) becomes a redundant but safe second layer — it should NEVER fire in nominal operation if adapters honor this AC. A test verifies this: instrument PolicyGuard's catch path to assert it isn't reached for any of the failure scenarios in the adapter test matrix.

---

### AC10 — Capability profile alignment

**GIVEN** the cross-adapter contract requires that `send_command()` only be reachable for command types the adapter genuinely supports
**WHEN** `BatteryAdapter`, `InverterAdapter`, `EVChargerAdapter` are loaded
**THEN** their respective capability profiles in `src/open_ems/adapters/capabilities/` are verified to declare exactly the `WriteCapability` members the adapter implements:
- `BatteryAdapter` (BYD HVS/HVM): `set_charge_rate`, `set_discharge_rate` (existing) — `set_operating_mode` is currently in profile but unsupported by `send_command`; v1 decision: **REMOVE `set_operating_mode` from BYD profiles** (or implement it). Default action: remove. Document the decision in the profile file.
- `InverterAdapter` (all 3 models): empty `write_capabilities` — no change (already empty)
- `EVChargerAdapter` (`ocpp_1_6`): `set_ev_charge_current` (existing). `stop_ev_charging` is implicitly covered — review if `WriteCapability` enum needs a `stop_ev_charging` member; v1 decision documented in Dev Notes.

**AND** unit tests for each adapter assert: for every `WriteCapability` in the adapter's profile, the corresponding command type is dispatchable via `send_command()`. For every `WriteCapability` NOT in the profile, the corresponding command type raises `TypeError`. The two assertions together ensure profile↔implementation are in lockstep.

**AND** P4 carve-out (Story 9-0c — "Capability-Profile Failure Semantics") is informed by this AC's findings; any additional capability-profile gaps surfaced here are flagged in 9-0c's dev notes triage.

---

### AC11 — Test matrix per adapter

For each of `BatteryAdapter`, `InverterAdapter` (read-only TypeError-only path), `EVChargerAdapter`:

**Unit tests** (using protocol-layer mocks):
1. ✅ Success: command accepted, `CommandResult(status=success, applied=True, reason="ok")` returned, correlation_id round-trip exact.
2. ✅ Timeout: protocol returns `protocol_status=timeout`, `CommandResult(status=timeout, applied=False, reason=<protocol>_command_timeout)` returned.
3. ✅ Error: protocol returns `protocol_status=error`, `CommandResult(status=failed, applied=False, reason="<protocol>_error:...")` returned.
4. ✅ Rejected (OCPP only): protocol acks but response.status="Rejected"; `CommandResult(status=rejected, applied=False, reason="ocpp_rejected:Rejected")`.
5. ✅ Unsupported command type: `TypeError` raised.
6. ✅ Adapter internal error: injected `RuntimeError` mid-encoding; wrapped as `CommandResult(status=failed, applied=False, reason="adapter_internal_error:RuntimeError")`. PolicyGuard's outer except not reached.
7. ✅ Cancellation: `asyncio.CancelledError` injected mid-await; propagates without wrapping.
8. ✅ correlation_id mismatch: protocol returns mismatched id; `CommandResult(status=failed, applied=False, reason="correlation_id_mismatch")`.
9. ✅ Battery only: register-map encoding error (`ValueError`); wrapped as `register_map_encoding_error:...`.
10. ✅ EV Charger only: stop without active transaction → `no_active_ocpp_transaction`.

**Integration tests** (using real `ModbusTcpAdapter` against a simulated Modbus server / `OCPPChargerAdapter` against a simulated charger):
1. End-to-end battery charge command via PolicyGuard → BatteryAdapter → ModbusTcpAdapter → simulated server → register written → CommandResult(success, applied=True).
2. End-to-end EV charge rate command via PolicyGuard → EVChargerAdapter → OCPPChargerAdapter → simulated charger → SetChargingProfile sent → CommandResult(success, applied=True).
3. End-to-end timeout: simulated server delays beyond `timeout_s`; CommandResult(timeout) reaches PolicyGuard.
4. End-to-end retry: SimulatedBatteryAdapter (existing fixture in `test_retry_pipeline.py`) replaced with real BatteryAdapter + flaky simulated Modbus server; RetryPolicy retries; second attempt succeeds.

**Cross-adapter consistency tests**:
1. Parametrized over (adapter, success-mock) tuples: assert `CommandResult.status==success ∧ applied==True ∧ reason=="ok"` exactly.
2. Parametrized over (adapter, timeout-mock) tuples: assert reason has prefix `f"{protocol}_command_timeout"`.
3. Parametrized over (adapter, error-mock) tuples: assert reason has prefix `f"{protocol}_"`.

---

### AC12 — Existing test suite passes; type checks clean

- Regression: `uv run python -m pytest tests/ --no-cov -q` — all existing 871 tests pass + the new tests from AC11.
- `uv run python -m mypy src/` — no new type errors.
- `uv run python -m ruff check .` — clean.
- `uv run python -m ruff format --check .` — clean.

**AND** the existing `SimulatedBatteryAdapter` / `SimulatedEVChargerAdapter` fixtures in `tests/integration/engine/test_retry_pipeline.py` and `test_command_pipeline.py` are NOT removed — they continue to serve as control-loop integration test fixtures. Real-adapter integration tests live alongside them under `tests/integration/adapters/`.

---

### AC13 — Adversarial 3-layer review with severity tagging (process AC)

**GIVEN** Story 9.0 is the first cross-adapter behavioral coupling story (A6 trigger from Epic 8 retro fired)
**AND** A2 is True (per evaluation in dev notes)
**WHEN** the story reaches `Status: review`
**THEN** the tiered review model from Epic 8 retro A3 applies:
- **Layer 1 (Blind Hunter):** unsafe implementation patterns — exception swallowing, missing await timeouts, broken correlation_id paths, contract drift between adapters
- **Layer 2 (Edge Case Hunter):** lifecycle/runtime failures — concurrent dispatch + lock interaction, OCPP reconnect mid-command, Modbus client wedge, cancellation timing windows
- **Layer 3 (Acceptance Auditor):** AC drift — every AC1–AC12 clause traced to a test or a design decision

**AND** every finding carries a severity tag (HIGH / MEDIUM / LOW). `Status: review → done` is BLOCKED on any unresolved HIGH.

---

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: AC12)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — record exact passing count as baseline (expected: 871 from Epic 8 close). **Result: 871 passed (matches expected baseline).**
  - [x] Run `uv run python -m ruff check .` — **All checks passed.**
  - [x] Run `uv run python -m ruff format --check .` — **172 files already formatted.**
  - [x] Run `uv run python -m mypy src/` — **Success: no issues found in 79 source files.**
  - [x] Note any pre-existing failures; do not hide them by touching unrelated code. **No pre-existing failures.**

- [x] Task 1: Author the canonical cross-adapter command contract (AC1)
  - [x] Cross-adapter command contract authored in `src/open_ems/adapters/__init__.py` module docstring with `COMMAND_CONTRACT_VERSION = "1.0"`.
  - [x] BatteryAdapter / InverterAdapter / EVChargerAdapter / GridMeterAdapter module docstrings reference the contract.
  - [x] GridMeterAdapter.send_command continues to raise NotImplementedError; docstring updated to cite contract clause 2 + DSMR P1 read-only stance.

- [x] Task 2: Extend register maps with command_payload(cmd) (AC2)
  - [x] Added `command_payload` to `BatteryRegisterMap` Protocol in `register_maps/_base.py`; introduced `ModbusCommandPayload` type alias.
  - [x] Implemented `command_payload` for `BydHvsV1` (registers 110 charge / 111 discharge) and `BydHvmV1` (registers 210 / 211). Placeholder addresses; encoding is uint16 W (rate_kw × 1000 rounded).
  - [x] Out-of-range rate_kw (>65.535 kW) raises ValueError; the adapter wraps it as `register_map_encoding_error:...` per AC2.
  - [x] `set_operating_mode` is out of scope for v1 — see AC10. Removed from BYD profiles.
  - [x] Unit tests in `tests/unit/adapters/modbus/register_maps/test_byd_command_payload.py`: nominal, boundary (0.0 and 65.535 kW), above-max ValueError, sub-watt rounding (12 cases across both maps).

- [x] Task 3: Implement `BatteryAdapter.send_command` (AC2, AC5, AC6, AC7, AC8, AC9, AC10, AC11)
  - [x] Replaced `raise NotImplementedError` with full Modbus write path.
  - [x] Constructs RawProtocolCommand via `_register_map.command_payload(cmd)`.
  - [x] Maps ProtocolCommandResult → CommandResult per AC5 status table (`modbus_command_timeout`, `modbus_error:<code>`).
  - [x] Wraps unexpected exceptions per AC9 as `adapter_internal_error:<exc_type>` with structlog `exc_info=True`.
  - [x] correlation_id round-trip per AC8: UUID → str on send, parses returned string back to UUID; mismatch returns `correlation_id_mismatch` with structured `adapter_correlation_mismatch` log.
  - [x] Unit tests in `tests/unit/adapters/modbus/test_battery_adapter_send_command.py` covering AC11.1–AC11.9 (success, timeout, error, TypeError, internal-error wrapping, cancellation, correlation mismatch, unparseable correlation, register-map encoding error). 13 tests.
  - [x] Updated `capabilities/battery.py`: `set_operating_mode` removed from BYD HVS/HVM profiles per AC10. Existing test assertions updated.

- [x] Task 4: Affirm `InverterAdapter.send_command` is TypeError-only (AC3, AC10, AC11)
  - [x] Replaced `raise NotImplementedError` with `raise TypeError(...)`. Module docstring documents v1 read-only stance and references the cross-adapter contract.
  - [x] `capabilities/inverter.py` profiles updated: `write_capabilities=frozenset()` for all 3 models. `set_operating_mode` removed (it had no implementation).
  - [x] Unit tests in `tests/unit/adapters/modbus/test_inverter_adapter_send_command.py`: parametrized over (4 command types × 3 inverter models) = 12 cases, all asserting `TypeError("InverterAdapter does not support …")`.
  - [x] PolicyGuard capability-gate behavior confirmed via existing `test_policy_guard.py` (no new test needed — empty `write_capabilities` ⇒ every command type already triggers `capability_missing` rejection in unchanged PolicyGuard logic).

- [x] Task 5: Implement `EVChargerAdapter.send_command` (AC4, AC5, AC6, AC7, AC8, AC9, AC10, AC11)
  - [x] Replaced `raise NotImplementedError` with full OCPP write path per AC4.
  - [x] Added `last_known_transaction_id` and `next_charging_profile_id` to `_ChargerState`; added `start_transaction` and `stop_transaction` handlers in `_InternalChargePoint` to maintain those fields. Added `OCPPChargerAdapter.active_transaction_id` (read accessor) and `reserve_charging_profile_id()` (monotonic counter). StopEVChargingCommand without active transaction returns `failed/no_active_ocpp_transaction` (no stale-id stop dispatched).
  - [x] SetChargingProfile payload composed per Dev Notes (TxProfile, Absolute, single schedule period, charging_rate_unit="W").
  - [x] OCPP-specific mapping: `acked + status="Accepted"` ⇒ success; `acked + status="Rejected"` (or any other status) ⇒ rejected with `ocpp_rejected:<status>`.
  - [x] Wraps exceptions per AC9 as `adapter_internal_error:<exc_type>`.
  - [x] correlation_id round-trip per AC8.
  - [x] Unit tests in `tests/unit/adapters/ocpp/test_charger_adapter_send_command.py`: 15 cases covering AC11.1–AC11.10 (success, rejected, NotSupported→rejected, timeout, error, TypeError × 2 battery commands, internal error wrapping, cancellation, correlation mismatch, stop-without-transaction, stop-with-transaction, stop-rejected, correlation round-trip).
  - [x] `capabilities/ev_charger.py` profile already aligned: `set_ev_charge_current` covers both SetEVChargingRateCommand and StopEVChargingCommand (the latter mapped via PolicyGuard's `_required_write_capability`). No profile change needed; AC10 lockstep verified.

- [x] Task 6: Cross-adapter consistency tests (AC11 cross-adapter section)
  - [x] `tests/unit/adapters/test_cross_adapter_consistency.py` — 11 cases.
  - [x] Parametrized over (Battery, EV Charger): success → `reason=="ok"`; timeout → `reason==f"{protocol}_command_timeout"`; error → `reason.startswith(f"{protocol}_")`; correlation mismatch → `reason=="correlation_id_mismatch"`.
  - [x] Pydantic invariant `applied=True ⇔ status=success` verified across success / timeout / error paths.
  - [x] Inverter TypeError-only stance verified across all 4 command types.

- [x] Task 7: Integration tests (AC11 integration section)
  - [x] `tests/integration/adapters/test_battery_send_command.py` — real BatteryAdapter against fake pymodbus client (4 tests: success, timeout, retry-recovery via RetryPolicy, AR16 wrap of unexpected RuntimeError).
  - [x] `tests/integration/adapters/test_ev_charger_send_command.py` — real EVChargerAdapter against simulated OCPP charger via fake WebSocket (5 tests: success via SetChargingProfile, rejected response, stop-without-transaction, stop-with-transaction via real StartTransaction handler, OCPP timeout).
  - [x] End-to-end through PolicyGuard / RetryPolicy verified for both Battery (DECISION audit on success, retry recovers) and EV Charger (DECISION on success, DEVICE on rejected).
  - [x] AC7 timeout ordering verified: adapter timeout (`modbus_command_timeout` / `ocpp_command_timeout`) wins, NOT PolicyGuard's outer `command_timeout`.

- [x] Task 8: Cancellation propagation tests (AC6)
  - [x] `tests/unit/adapters/test_cancellation_propagation.py` — 3 tests. Battery and EV Charger: CancelledError raised mid-`send_command` propagates; no CommandResult returned; protocol-layer try/finally cleanup observed.
  - [x] Adapter layer asserted to NOT emit `device_command_cancelled`/`retry_cancelled`/`adapter_cancelled` events — RetryPolicy remains the single emitter.
  - [x] Modbus `_lock` released on cancellation: `test_modbus_lock_released_on_cancellation` injects mid-write CancelledError on real `ModbusTcpAdapter` and asserts `_lock.locked() is False` after.

- [x] Task 9: Documentation pass
  - [x] Each adapter module docstring (Battery, Inverter, EV Charger, Grid Meter) cites the cross-adapter contract in `open_ems.adapters` and documents its specific command surface.
  - [x] `core/commands.py` module docstring updated: notes that as of Story 9.0, every controllable adapter honors the contract; AR16 gap from Epic 8 is closed.
  - [x] `architecture.md` § CommandResult Pattern updated: cites `COMMAND_CONTRACT_VERSION` and notes all 3 controllable adapters satisfy the contract; DSMR P1 read-only stance documented.

- [x] Task 10: Final quality gate (AC12)
  - [x] `uv run python -m pytest tests/ --no-cov -q` — **946 passed** (baseline 871 + 75 new tests).
  - [x] `uv run python -m mypy src/` — **Success: no issues found in 79 source files**.
  - [x] `uv run python -m ruff check .` — **All checks passed**.
  - [x] `uv run python -m ruff format --check .` — **181 files already formatted**.
  - [x] Zero new findings in any linter category.

- [x] Task 11: Submit for adversarial 3-layer review (AC13)
  - [x] Status: review (this commit).
  - [x] Layer 1 (Blind Hunter): unsafe patterns sweep — completed 2026-05-10.
  - [x] Layer 2 (Edge Case Hunter): lifecycle/runtime sweep — completed 2026-05-10.
  - [x] Layer 3 (Acceptance Auditor): AC1–AC12 → test traceability matrix — completed 2026-05-10.
  - [x] Severity tags HIGH/MEDIUM/LOW on every finding. No HIGH unresolved at `Status: review → done`.

### Review Findings (2026-05-10)

**Summary:** 4 HIGH, 13 MEDIUM, 5 LOW patch findings; 3 decision-needed; 10 deferred; 4 dismissed as noise.
**Acceptance Auditor verdict:** AC1–AC11 PASS; AC12 NOT-VERIFIED (auditor cannot run gates). No AC violation HIGH/MEDIUM.
**Source of HIGHs:** All four HIGH findings are clustered in the OCPP layer (transaction lifecycle, concurrency, cancellation cleanup).

#### Decision-needed — RESOLVED (2026-05-10)

- [x] [Review][Decision→Patch] **OCPP `NotSupported` mapping** — Resolved: split into `failed/ocpp_unsupported:<status>`. Added to patch list.
- [x] [Review][Decision→Patch] **OCPP cancellation cleanup at python-ocpp library boundary** — Resolved: add real-library cancellation integration test. Added to patch list.
- [x] [Review][Decision→Patch] **Sub-watt setpoints round to 0 W silently** — Resolved: encoder raises `ValueError` for `0 < rate_kw < 0.0005`; adapter wraps as `register_map_encoding_error:sub_watt_precision`. Added to patch list.

#### Patch findings from resolved decisions

- [x] [Review][Patch][MED] **Map OCPP `NotSupported` to `failed/ocpp_unsupported:<status>`** [`src/open_ems/adapters/ocpp/charger_adapter.py:_map_ocpp_result`, contract docstring in `adapters/__init__.py`] — Treat OCPP response payload `status="NotSupported"` (or any future non-{Accepted,Rejected} value that signals permanent inability) as `CommandStatus.failed` with reason `ocpp_unsupported:<status>`. Keep `Rejected` → `rejected/ocpp_rejected:Rejected`. Update cross-adapter status table in contract docstring (`adapters/__init__.py`). Update test `test_set_rate_acked_with_unknown_status_treated_as_rejected` to assert `failed/ocpp_unsupported:NotSupported`. Verify RetryPolicy does NOT retry `failed/ocpp_unsupported:*` (idempotent commands may currently retry on `failed`).
- [x] [Review][Patch][MED] **Add real-library OCPP cancellation integration test** [`tests/integration/adapters/test_ev_charger_send_command.py` (extend)] — exercise `OCPPChargerAdapter.send_raw_command` through the real `python-ocpp` library `handler.call` under outer-task cancellation (real WebSocket fixture). Assert: (a) `CancelledError` propagates without `CommandResult`, (b) the library's pending-response state is cleaned up (test by issuing a new command after cancellation and verifying no cross-talk between message ids). If the test surfaces a real leak, file a follow-up; do not block this review on the fix.
- [x] [Review][Patch][MED] **Reject sub-watt non-zero rate as encoding error** [`src/open_ems/adapters/modbus/register_maps/byd_hvs_v1.py:_encode_setpoint_w`, `byd_hvm_v1.py:_encode_setpoint_w`] — raise `ValueError("sub_watt_precision: rate_kw={rate_kw} below 0.0005 kW minimum")` when `0 < rate_kw < 0.0005`. Adapter wraps as `register_map_encoding_error:sub_watt_precision`. Add tests covering: exact 0.0 (accepted, writes 0), 0.0001 (rejected), 0.0005 (accepted, writes 1 W). Update `_encode_setpoint_w` docstring to document the boundary.

#### Patch findings — HIGH (blocking `review → done`)

- [x] [Review][Patch][HIGH] **OCPP reconnect resets `last_known_transaction_id` → stop commands fail spuriously** [`src/open_ems/adapters/ocpp/central_system.py:208-218`, `src/open_ems/adapters/ocpp/charger_adapter.py:175-180`] — `handle_connection` creates a fresh `_ChargerState()` on every reconnect, wiping `last_known_transaction_id` even if the charger's underlying transaction is still active (OCPP 1.6 transactions survive WebSocket reconnects). `StopEVChargingCommand.is_idempotent = False` so RetryPolicy will not retry. Inverse of the Story 8.0 retro stale-id hazard — EMS now refuses to stop a still-active transaction whenever the WebSocket flaps.
- [x] [Review][Patch][HIGH] **OCPP `send_raw_command` lacks concurrent-call serialization** [`src/open_ems/adapters/ocpp/central_system.py:284-307`] — No `asyncio.Lock` on the OCPP path; Modbus has `async with self._lock`. Two concurrent `EVChargerAdapter.send_command` invocations (retry overlap + control-loop dispatch) call `handler.call` over the same WebSocket with no serialization. Zero test coverage for concurrent dispatch on the OCPP adapter. python-ocpp library has known concurrent-call bugs.
- [x] [Review][Patch][HIGH] **OCPP `on_start_transaction` overwrites in-flight transaction id without invariant check** [`src/open_ems/adapters/ocpp/central_system.py:122-140`] — `transaction_id = (self._state.last_known_transaction_id or 0) + 1` then assigned unconditionally. Multi-connector charger or buggy/malicious second `StartTransaction` mid-session silently overwrites the active id; subsequent `RemoteStopTransaction` targets the wrong transaction. `connector_id` param is accepted but never used.
- [x] [Review][Patch][HIGH] **OCPP `on_stop_transaction` silently swallows mismatched `transaction_id`** [`src/open_ems/adapters/ocpp/central_system.py:142-154`] — On `last_known_transaction_id != transaction_id` (charger reports a different id), the handler accepts the StopTransaction (`Accepted`) and leaves the local id stale. No warning log. Subsequent stop commands will dispatch against the stale id (with the H1 race making it worse on reconnect).

#### Patch findings — MEDIUM

- [x] [Review][Patch][MED] **TOCTOU between `active_transaction_id` read and dispatch in stop command** [`src/open_ems/adapters/ocpp/charger_adapter.py:175-209`] — `transaction_id` captured at start of stop branch; multiple `await`s occur before `send_raw_command`. Charger's own StopTransaction during this window clears `last_known_transaction_id`; adapter dispatches against a stale id. Related to HIGH findings; address holistically.
- [x] [Review][Patch][MED] **`next_charging_profile_id` reset on reconnect → collides with charger-persisted profiles** [`src/open_ems/adapters/ocpp/central_system.py:64-66, 251-261`] — Counter resets to 1 on reconnect; docstring claim "monotonic per OCPP session" is misleading. Real chargers do not garbage-collect TxProfile entries until a new session, so reusing profile_id=1 can overwrite an existing profile.
- [x] [Review][Patch][MED] **`last_known_transaction_id` clear semantics inconsistent** [`src/open_ems/adapters/ocpp/central_system.py:230-239`] — Cleared on reconnect (state replacement) and on `on_stop_transaction` (match only), NOT on disconnect-without-reconnect. Intent is unclear; document or unify.
- [x] [Review][Patch][MED] **`_encode_setpoint_w(inf)` raises `OverflowError` → wrapped as `adapter_internal_error` instead of `register_map_encoding_error`** [`src/open_ems/adapters/modbus/register_maps/byd_hvs_v1.py:55-65`, `byd_hvm_v1.py:55-65`, `battery_adapter.py:145-150`] — Battery adapter catches `ValueError` to map to `register_map_encoding_error`, but `int(round(inf * 1000))` raises `OverflowError`, which falls to the generic `Exception` handler. Mis-categorized reason for audit consumers.
- [x] [Review][Patch][MED] **`correlation_id` mismatch log emits raw received string (log-injection surface)** [`src/open_ems/adapters/modbus/battery_adapter.py:330-346`] — `received=str(protocol_result.correlation_id)` logs untrusted input verbatim. structlog escapes JSON but downstream text-stream consumers may not. Sanitize or hash.
- [x] [Review][Patch][MED] **`_map_protocol_result` typed as `object` — type-system escape hatch** [`src/open_ems/adapters/modbus/battery_adapter.py:388-424`] — `getattr(protocol_result, "protocol_status", None)` with `object` typing returns misleading `adapter_internal_error:unexpected_protocol_status:None` for shape mismatches that should fail static checks.
- [x] [Review][Patch][MED] **Battery test fakes don't verify HVM register address** [`tests/unit/adapters/modbus/test_battery_adapter_send_command.py:1351-1357`] — `_make_protocol_result` pre-fills `address=110` (HVS charge) for both HVS and HVM parametrized cases; HVM should write to 210. Test doesn't assert `sent.payload["address"]`. Register-map drift would not be caught.
- [x] [Review][Patch][MED] **Pydantic `applied_iff_success` invariant test only proves consistency, not biconditional** [`tests/unit/adapters/test_cross_adapter_consistency.py:2268-2289`] — Test asserts the existing outputs are consistent. Never attempts to CONSTRUCT a `CommandResult(success, applied=False)` to prove the Pydantic validator rejects it.
- [x] [Review][Patch][MED] **Integration timeout test may hang in cleanup** [`tests/integration/adapters/test_battery_send_command.py:2784-2812`] — `_FakeModbusClient` uses `await asyncio.sleep(60.0)` with no `try/finally`. If `wait_for` cancellation fails to interrupt, test hangs until pytest timeout.
- [x] [Review][Patch][MED] **Audit count test asserts only spy invocation, not retry semantics** [`tests/integration/adapters/test_battery_send_command.py:2855-2864`] — Only checks `audit_spy.await_count == 1`. RetryPolicy changes that suppress retry-attempt audits would not be caught.
- [x] [Review][Patch][MED] **`obs.audit = spy` method-assignment bypasses real audit semantics** [`tests/integration/adapters/test_battery_send_command.py:2725-2730`] — Replaces bound method with `AsyncMock`. Bypasses enrichment/redaction in the real audit path. Use real `ObservabilityService` and inspect persisted rows instead.
- [x] [Review][Patch][MED] **Tests don't assert `observed_state is None` on non-success paths** [`tests/unit/adapters/...`] — Contract clause 7 says `observed_state=None` on success; non-success paths are silent. A future "helpful" addition populating `observed_state` on timeout/error would not be caught.
- [x] [Review][Patch][MED] **`RawProtocolCommand` Pydantic validation path uncovered** [`src/open_ems/adapters/modbus/battery_adapter.py:152-160`] — A register-map subclass returning `None` or wrong shape would be caught by AR16 wrap but no unit test exercises this.

#### Patch findings — LOW

- [x] [Review][Patch][LOW] **Duplicate `_failed` helper across battery and EV charger adapters** [`src/open_ems/adapters/modbus/battery_adapter.py:378-385`, `src/open_ems/adapters/ocpp/charger_adapter.py:1008-1015`] — Two byte-for-byte copies. Move to `src/open_ems/adapters/__init__.py` or a shared `_helpers.py`.
- [x] [Review][Patch][LOW] **`_ = (asyncio,)` lint-silencer hack** [`tests/unit/adapters/test_cross_adapter_consistency.py:2339`] — Either remove the import or restructure so the lint rule is satisfied without the dummy assignment.
- [x] [Review][Patch][LOW] **Unparseable correlation test doesn't assert log emission** [`tests/unit/adapters/modbus/test_battery_adapter_send_command.py:1509-1523`] — `test_send_command_correlation_id_mismatch_returns_failed` asserts the structured log; the unparseable variant does not. Inconsistent rigor.
- [x] [Review][Patch][LOW] **GridMeter `NotImplementedError` test not added by 9.0** [`tests/unit/adapters/dsmr/` (missing)] — AC1 requires GridMeter `send_command` raises `NotImplementedError` with "DSMR P1 is read-only" reason. Code is correct but no Story 9.0-added test exercises it directly.
- [x] [Review][Patch][LOW] **AC12 quality gates not independently re-run by review** — Dev claims 946 passing, mypy/ruff/format clean. Auditor cannot verify in review mode; recommend running once before close.

#### Deferred findings

- [x] [Review][Defer] **Cancellation between successful protocol write and CommandResult construction leaves audit-log gap** [`battery_adapter.py:162-190`] — deferred, architectural concern beyond this story (RetryPolicy audit semantics)
- [x] [Review][Defer] **Cold-start StopEVChargingCommand for pre-EMS transactions** [`charger_adapter.py:175-180`] — deferred, by-design per `_ChargerState` docstring (transaction id is intrinsically transient)
- [x] [Review][Defer] **`_compose_set_charging_profile_payload` reserves profile_id before connectivity check** [`charger_adapter.py:211-234`] — deferred, cosmetic counter creep
- [x] [Review][Defer] **OCPP `not_connected` mapped to `ocpp_error:not_connected` (non-canonical)** [`charger_adapter.py:283-289`] — deferred, audit-fidelity gap; address with broader OCPP error-prefix scheme later
- [x] [Review][Defer] **Dead `connector_id` parameter in `on_start_transaction`** [`central_system.py:714`] — deferred, multi-connector support is a future story
- [x] [Review][Defer] **`# type: ignore` proliferation in tests** — deferred, broader typing improvement; suggests `RawProtocolCommand.payload` is too loosely typed
- [x] [Review][Defer] **`# noqa: BLE001` blocks lack per-site programming-error test** — deferred, AR16-wrap coverage adequate at integration level
- [x] [Review][Defer] **`_BlockingModbus` cancellation test does not exercise real Modbus `_lock`** — deferred, covered separately by `test_modbus_lock_released_on_cancellation`
- [x] [Review][Defer] **PolicyGuard catch path not directly instrumented (AC9)** — deferred, reason-string inference is correctness-equivalent
- [x] [Review][Defer] **No single programmatic lockstep iterator test (AC10)** — deferred, functional coverage of both halves exists

#### Dismissed (noise / handled elsewhere)

- Cancellation `except Exception` Python version dependency comment — Python 3.11+ target; `CancelledError` is `BaseException`, correctly not caught
- `on_start_transaction`/`on_stop_transaction` not async — style, not bug; consistent with neighbors
- `InverterAdapter.send_command(None)` produces "NoneType" in TypeError message — defense-in-depth works; cosmetic
- `correlation_id` empty-string branch unreachable due to `NonEmptyStr` field — defensive coverage adequate

#### Review resolution summary (2026-05-10)

**Status: all decision-needed resolved; all 25 patches applied; quality gates green.**

- **Decisions resolved as patches:**
  - OCPP `NotSupported` → split into `failed/ocpp_unsupported:<status>` (contract v1.1).
  - OCPP cancellation cleanup → real-library integration test added; recovery verified.
  - Sub-watt setpoints → encoder raises `ValueError`; adapter wraps as `register_map_encoding_error:sub_watt_precision`.
- **HIGH OCPP findings closed:**
  - H1 reconnect-resets-transaction-id → `handle_connection` preserves `last_known_transaction_id` and `next_charging_profile_id`.
  - H2 concurrent dispatch → `OCPPChargerAdapter._send_lock` serializes `send_raw_command`.
  - H3 overlapping `StartTransaction` → warning log before overwrite; `active_transaction_connector_id` tracked.
  - H4 mismatched `StopTransaction` → warning log; local id preserved on mismatch.
- **Contract changes:** `COMMAND_CONTRACT_VERSION` bumped 1.0 → 1.1 (status mapping table now distinguishes OCPP `Rejected` vs `NotSupported`).
- **Quality gates (final):**
  - `uv run python -m pytest tests/ --no-cov -q` — **972 passed** (946 baseline + 26 new tests).
  - `uv run python -m mypy src/` — **Success: no issues found in 80 source files**.
  - `uv run python -m ruff check .` — **All checks passed**.
  - `uv run python -m ruff format --check .` — **183 files already formatted**.
- **New test files / additions:**
  - `tests/unit/adapters/ocpp/test_ocpp_lifecycle.py` — 7 tests (transaction lifecycle, concurrency lock, profile id preservation).
  - `tests/integration/adapters/test_ev_charger_send_command.py::test_real_python_ocpp_library_cancellation_propagates_and_recovers` — real-library cancellation cleanup verification.
  - `tests/unit/adapters/dsmr/test_meter_adapter.py::test_send_command_raises_not_implemented_error_with_read_only_reason` — closes AC1 GridMeter gap.
  - `tests/unit/adapters/test_cross_adapter_consistency.py::test_pydantic_invariant_rejects_success_with_applied_false` — direct Pydantic invariant test (true biconditional).
  - Plus updates to `test_byd_command_payload.py` (sub-watt, inf, boundary), `test_battery_adapter_send_command.py` (HVM register address, sanitized log, observed_state, validation wrapping), `test_charger_adapter_send_command.py` (NotSupported→ocpp_unsupported).
- **New source files:** `src/open_ems/adapters/_helpers.py` — shared `failed_result(cmd, reason)` builder.

---

## Dev Notes

### Cross-adapter command contract (the canonical reference)

All adapters implementing `DeviceAdapter.send_command(cmd: DeviceCommand) -> CommandResult` MUST honor the following clauses. This is the single source of truth — per-adapter docstrings link here, never restate.

**1. Authorization is upstream.** PolicyGuard authorizes; the adapter executes. The adapter never re-checks fail-safe mode, capability, or safety constraints. Those have already passed.

**2. Command surface is exhaustive over the adapter's WriteCapability set.** For every WriteCapability in the adapter's capability profile, exactly one DeviceCommand subtype is dispatchable. For every DeviceCommand subtype NOT covered by the adapter's WriteCapability, `send_command(cmd)` raises `TypeError(f"{adapter_class} does not support {type(cmd).__name__}")`. This is intentionally NOT a `CommandResult` failure — it is a programming-error signal (capability gate should have caught it).

**3. Status mapping table.**

| Source signal | CommandResult.status | applied | reason form |
|---|---|---|---|
| Protocol acked + affirmative payload | `success` | `True` | `"ok"` |
| Protocol acked + negative payload (OCPP "Rejected") | `rejected` | `False` | `f"<protocol>_rejected:<status>"` |
| Protocol layer timeout | `timeout` | `False` | `f"<protocol>_command_timeout"` |
| Protocol layer error | `failed` | `False` | `f"<protocol>_error:<error_code>"` |
| Adapter-internal exception (programming bug) | `failed` | `False` | `f"adapter_internal_error:<exc_type>"` |
| correlation_id mismatch from protocol | `failed` | `False` | `"correlation_id_mismatch"` |
| Register-map encoding error (Modbus only) | `failed` | `False` | `f"register_map_encoding_error:<msg>"` |
| OCPP stop without active transaction | `failed` | `False` | `"no_active_ocpp_transaction"` |

**4. correlation_id round-trip is exact.** `result.correlation_id == cmd.correlation_id` (both UUID). Adapter is responsible for str ↔ UUID conversion across the protocol-layer boundary. Mismatch is `failed` per the table above and is reported as a structured warning log (`adapter_correlation_mismatch`).

**5. Cancellation propagates.** `asyncio.CancelledError` is NEVER caught by the adapter's `send_command`. Cleanup happens via `try/finally`, never `except CancelledError`. RetryPolicy emits the cancellation audit; adapters do not.

**6. Timeout boundary.** Adapter's protocol-layer timeout is the primary boundary. PolicyGuard's outer `asyncio.wait_for` is defense in depth. Under nominal operation, the protocol timeout always fires first.

**7. observed_state is None on success.** Adapters do NOT issue a post-write read to populate `observed_state`. The control loop's polling cycle observes the post-write state on its next tick. Optional: an adapter MAY populate `observed_state` if its protocol returns the post-write state in the same call (e.g. some Modbus "write+read" patterns). For v1, leave None.

**8. AR16 enforcement.** All exceptions except `asyncio.CancelledError` and `TypeError` are wrapped as `CommandResult(status=failed, applied=False, reason="adapter_internal_error:<type>")`. The full exception is logged via structlog with `exc_info=True` BEFORE the CommandResult is returned.

**9. Idempotency is in the command, not the adapter.** Adapters do not consult `cmd.is_idempotent`. RetryPolicy reads it. The adapter's job is to faithfully execute one attempt.

**10. Audit emission is upstream.** Adapters do NOT emit DECISION / DEVICE / CONSTRAINT audits. PolicyGuard emits CONSTRAINT on rejection; RetryPolicy emits DECISION on success and DEVICE on terminal failure / cancellation.

### OCPP charging profile composition

For `SetEVChargingRateCommand(rate_kw=R)`:

```python
payload = {
    "connector_id": 1,
    "cs_charging_profiles": {
        "charging_profile_id": <stable_per_session_id>,  # e.g. 1, established at session start
        "stack_level": 0,
        "charging_profile_purpose": "TxProfile",  # active during current transaction
        "charging_profile_kind": "Absolute",
        "charging_schedule": {
            "charging_rate_unit": "W",
            "charging_schedule_period": [
                {"start_period": 0, "limit": int(R * 1000)},
            ],
        },
    },
}
```

Notes:
- `chargingProfilePurpose=TxProfile` overrides any TxDefaultProfile during the current transaction. This is the intended scope for runtime EMS rate control.
- `chargingRateUnit=W` is broadly supported; `A` (amps) requires phase-aware computation we don't have in v1.
- `stackLevel=0` and a single absolute schedule period is the simplest stable rate command. Multi-period schedules are out of scope for v1 — Epic 13 may revisit for tariff-aware scheduling.
- `chargingProfileId` should be stable per OCPP session and increment on session change. The adapter tracks it in `_ChargerState` (new field) or derives it from a counter tied to BootNotification.

For `StopEVChargingCommand`:

```python
payload = {"transaction_id": <int from last StartTransaction response>}
```

The `transaction_id` is observed from the OCPP CALL response when the charger sends `StartTransaction`. The current `_ChargerState.last_call_result` already captures CALL responses; Story 9.0 needs to ensure this field carries the StartTransaction response specifically (or a dedicated `last_known_transaction_id` field is added to `_ChargerState`). Recommended: add `last_known_transaction_id: int | None` to `_ChargerState` and update on `StartTransaction.conf` observation.

### Architecture references

- **AR15** (`PolicyGuard.authorize_and_dispatch()` is the single required path) — `_bmad-output/planning-artifacts/epics.md:152`. The adapter contract reinforces this: adapters never expose a public path callable from outside the engine.
- **AR16** (typed `CommandResult` from every `send_command()`) — `_bmad-output/planning-artifacts/epics.md:153`. This story is where AR16 finally holds for all 3 controllable adapters.
- **AR17** (dual logging sinks) — `_bmad-output/planning-artifacts/epics.md:154`. Adapter exception wrapping (AC9) emits structlog + sets up RetryPolicy's audit emission.
- **CommandResult Pattern** — `architecture.md:700-714`. Story 9.0's status mapping (AC5) is the concrete realization of this pattern across all transports.
- **Pattern: Degraded state is returned, not raised** — `architecture.md:414-435`. Same shape applies to `send_command`: failures are returned as typed CommandResult, never raised.

### File structure references

| Path | Action |
|---|---|
| `src/open_ems/adapters/__init__.py` or new `src/open_ems/adapters/_command_contract.py` | NEW — author the cross-adapter contract docstring |
| `src/open_ems/adapters/modbus/battery_adapter.py` | UPDATE — implement send_command per AC2 |
| `src/open_ems/adapters/modbus/inverter_adapter.py` | UPDATE — TypeError-only per AC3 |
| `src/open_ems/adapters/modbus/register_maps/__init__.py` | UPDATE — extend BatteryRegisterMap interface with command_payload |
| `src/open_ems/adapters/modbus/register_maps/byd_hvs_v1.py` | UPDATE — implement command_payload |
| `src/open_ems/adapters/modbus/register_maps/byd_hvm_v1.py` | UPDATE — implement command_payload |
| `src/open_ems/adapters/ocpp/charger_adapter.py` | UPDATE — implement send_command per AC4 |
| `src/open_ems/adapters/ocpp/central_system.py` | UPDATE — `_ChargerState` may grow `last_known_transaction_id` |
| `src/open_ems/adapters/dsmr/meter_adapter.py` | UPDATE — docstring only, behavior unchanged |
| `src/open_ems/adapters/capabilities/battery.py` | UPDATE — remove `set_operating_mode` from profiles (AC10) |
| `src/open_ems/adapters/capabilities/inverter.py` | UPDATE — verify empty write_capabilities, document v1 stance |
| `src/open_ems/adapters/capabilities/ev_charger.py` | UPDATE — verify alignment with EVChargerAdapter |
| `tests/unit/adapters/modbus/test_battery_adapter_send_command.py` | NEW |
| `tests/unit/adapters/modbus/test_inverter_adapter_send_command.py` | NEW |
| `tests/unit/adapters/ocpp/test_charger_adapter_send_command.py` | NEW |
| `tests/unit/adapters/test_cross_adapter_consistency.py` | NEW — parametrized cross-adapter tests |
| `tests/unit/adapters/modbus/register_maps/test_byd_*_command_payload.py` | NEW |
| `tests/integration/adapters/test_battery_send_command.py` | NEW |
| `tests/integration/adapters/test_ev_charger_send_command.py` | NEW |

### Testing standards summary

- pytest + pytest-asyncio for all new tests (per architecture.md §Testing).
- Simulated protocol layer via mocks for unit tests; existing `test_command_pipeline.py` / `test_retry_pipeline.py` SimulatedAdapter classes remain as control-loop fixtures.
- New integration tests use the existing simulated Modbus server / simulated OCPP charger harnesses (already in place from Epics 3 and 8).
- `tests/fixtures/scenarios.py` (new in 8.5) is appropriate to extend with command-execution scenarios if cross-test reuse warrants.

### Previous story intelligence — what we carry from Epic 8

From the Epic 8 retro and the deferred-work log:

- **Story 8-2 deferred:** "`adapters={}` in production silently rejects all commands" — Epic 9 (this story) is the explicit fix. After 9.0 ships, the empty-adapter-map state is no longer a normal operating mode.
- **Story 8-2 deferred:** "`correlation_id` mismatch from adapter `CommandResult` passed through unchecked" — closed by AC8 of this story.
- **Story 8-3 deferred:** "`asyncio.sleep` between attempts not shielded against cancellation" — RetryPolicy concern, NOT this story's scope; flagged in deferred-findings triage R7 below.
- **Story 8-4 deferred:** "`observability.audit` slow DB write blocks watchdog heartbeat" — out of this story's scope; R7 triage classifies as acceptable-post.
- **Style precedent:** Story 8-0 / 8-0b set the pattern for prep stories (focused scope, comprehensive AC matrix, strict pre/post quality gates). Apply that pattern here.

### Library/framework notes

- `pymodbus` (existing): `write_register` and `write_registers` paths in `ModbusTcpAdapter._write_payload` (lines 308-330) are already implemented. Story 9.0 just composes the payload via the register map.
- `ocpp` Python library (existing): `ocpp.v16.call.SetChargingProfile`, `ocpp.v16.call.RemoteStopTransaction` are the relevant CALL classes. `OCPPChargerAdapter.send_raw_command` (lines 220-300) handles the dispatch already.
- No new dependencies. uv.lock unchanged.

### Project Structure Notes

- Alignment: `src/open_ems/adapters/` already contains the canonical structure. New files fit naturally.
- Detected variance: `_command_contract.py` is a new pattern — doc-only modules. Acceptable; precedent exists for `architecture.md` style "decision documents" living alongside code as docstrings. Alternative: place the contract in `src/open_ems/adapters/__init__.py` docstring. **Decision:** put it in `src/open_ems/adapters/__init__.py` docstring + a top-level constant `COMMAND_CONTRACT_VERSION = "1.0"` for change tracking.

### Orchestration Risk Analysis (A2-triggered — REQUIRED)

> **A2 triggers matched:** T2 (retries/cancellation across all controllable adapters), T5 (multi-adapter coordination — Battery + Inverter + EV Charger contracts unified). Partial: T1 (lifecycle — connect/disconnect cycle interactions with send), T4 (timing — protocol_timeout_s ↔ PolicyGuard timeout ↔ control_loop_interval_seconds).

#### R1 — Composition-risk analysis

This story converges four operational domains in a single shipping unit:

1. **Multi-adapter coordination (T5).** Three adapter implementations (Battery, Inverter, EV Charger) ship together with cross-adapter consistency requirements. Risk: cross-adapter drift — e.g. Battery returns reasons in `f"modbus_<error>"` form while EV Charger returns reasons in `f"<error>_ocpp"` form. Mitigation: AC5 explicitly tabulates the canonical mapping and AC11's cross-adapter consistency tests assert identical reason prefix structure. Precedent: Epic 4 retro flagged "adapter scaling" as a coupling concern; A6 from Epic 8 retro fired explicitly for this story.

2. **Retry/cancellation semantics (T2).** Adapters must propagate `CancelledError` cooperatively without emitting parallel cancellation audits. Risk: an adapter catches `CancelledError` to "clean up" and either (a) suppresses the propagation, leaving RetryPolicy hanging, or (b) emits a duplicate audit event, polluting the DEVICE audit stream. Precedent: Story 8-4 review caught `_emit_cancellation_audit` semantic issues — this story is one layer below and must not reintroduce them. Mitigation: AC6 explicitly forbids `except CancelledError` in adapters; cancellation tests verify propagation without partial CommandResult.

3. **Timeout boundary stacking (T4).** Three timeouts compose: protocol-layer (`timeout_s` ≤ 10s), PolicyGuard outer (`COMMAND_DISPATCH_TIMEOUT_SECONDS=10s`), and indirectly the control-loop tick budget. Risk: under high adapter latency, the protocol-layer timeout doesn't fire fast enough and PolicyGuard's outer wait_for fires instead, producing a CommandResult with reason `"command_timeout"` (PolicyGuard's wording) instead of `"<protocol>_command_timeout"` (adapter's wording). Audit consumers expecting the protocol-prefixed form will misclassify. Mitigation: AC7 explicitly states the protocol timeout MUST be the boundary; tests verify ordering by injecting sleep delays. Belt-and-suspenders: PolicyGuard's outer timeout is documented as defense-in-depth.

4. **Capability profile ↔ implementation drift (partial T1).** A new write capability declared in a profile but not implemented in the adapter (or vice versa) silently produces either `capability_missing` rejections (false negative) or `TypeError` from the adapter (programming error reaching production). Mitigation: AC10's lockstep test asserts profile↔implementation alignment for every write capability. P4 (Story 9-0c) addresses the broader failure-mode contract.

5. **Observed_state ambiguity (partial T1).** AC1's clause 7 says `observed_state=None` on success; the next polling cycle observes post-write state. Risk: a test or downstream consumer expects observed_state to be populated. Mitigation: contract is explicit; observed_state population stays optional and is documented as a v1 trade-off (avoiding the round-trip cost of a post-write read).

#### R2 — State-transition table

Story 9.0 modifies state machines in two places: `BatteryAdapter` / `InverterAdapter` / `EVChargerAdapter` (which currently have no per-command state), and `_ChargerState` (which gains `last_known_transaction_id`).

**Per-command state machine inside `send_command()`:**

| State | Allowed transitions | Trigger | Side-effects |
|---|---|---|---|
| `idle` (no in-flight cmd) | → `encoding` | `send_command(cmd)` invoked | None |
| `encoding` | → `protocol_dispatching`, → `failed_encoding` | register_map / payload composition | None on success; on encoding error: structlog warning, return `CommandResult(failed)` |
| `protocol_dispatching` | → `awaiting_protocol_result` | `await protocol_adapter.send_raw_command(raw_cmd)` enters | None |
| `awaiting_protocol_result` | → `mapping_result`, → `cancelled` (CancelledError), → `internal_error` (other Exception) | protocol return / cancel / unexpected exc | None on nominal; cancellation: NO audit (RetryPolicy owns); unexpected exc: structlog warning + `CommandResult(failed)` |
| `mapping_result` | → `idle` (CommandResult returned) | ProtocolCommandResult inspected | None — pure mapping |
| `failed_encoding` / `internal_error` | → `idle` | CommandResult(failed) returned | structlog warning, NO audit |
| `cancelled` | → propagates to caller | `raise` (no return) | NO audit, NO CommandResult |

**`_ChargerState.last_known_transaction_id` (OCPP):**

| State | Transitions | Trigger | Side-effects |
|---|---|---|---|
| `None` (no transaction observed) | → `<int>` | `StartTransaction.conf` from charger captured | None |
| `<int>` (active or stale) | → `None` | `StopTransaction.conf` from charger captured OR session disconnect | None |
| `<int>` | → unchanged | StatusNotification, MeterValues, etc. | None |

`StopEVChargingCommand` reads this field; `None` → `CommandResult(failed, "no_active_ocpp_transaction")`.

#### R3 — Impossible-state analysis (with cold-start coverage)

**Forbidden state combinations and their structural invariants:**

1. **`CommandResult(applied=True, status=failed/timeout/rejected)`** — invariant enforced by Pydantic `_applied_iff_success` validator on `CommandResult` (commands.py:130-140). Constructor would raise `ValidationError`. No code path can produce this.

2. **Adapter returns `CommandResult.correlation_id` ≠ `cmd.correlation_id`** without flagging it — invariant: AC8 mandates the mismatch path returns `failed/correlation_id_mismatch`. Test asserts no other code path can pass through a mismatched correlation_id silently.

3. **`asyncio.CancelledError` propagates from adapter AND a cancellation audit is emitted by the adapter** — invariant: AC6 forbids `except CancelledError` in adapter code. RetryPolicy is the single audit emitter for cancellation. Test verifies no adapter-layer audit emit on cancellation.

4. **`StopEVChargingCommand` dispatched to a charger with no observed transaction id** — invariant: AC4 requires the explicit `no_active_ocpp_transaction` failure path BEFORE any OCPP RemoteStopTransaction is constructed. A stale transaction id is not used.

5. **`InverterAdapter.send_command()` reaches a non-TypeError code path with a real DeviceCommand** — invariant: AC3's TypeError-only stance + AC10's lockstep capability check. PolicyGuard's capability gate rejects every command before it reaches send_command. Defense in depth: send_command itself raises TypeError unconditionally.

**Cold-start / startup-grace coverage (mandatory):**

Story 9.0's adapters become live during the architecture's startup sequence Step 6 ("Initialize protocol adapters (connect to devices)"). Cold-start sequence specific to send_command:

1. **t=0 (process start):** `app.py` lifespan begins; adapters not yet constructed.
2. **t=migrations done:** PolicyGuard initialized with `adapters={}` map (this is the deferred 8-2 finding's window; v1 production fills the map when device discovery runs in Story 9.1).
3. **Pre-9.1:** Adapter map remains empty. Any `send_command` reaching PolicyGuard is rejected with `adapter_not_registered` — this is the safe state. The adapter's `send_command` is unreachable from PolicyGuard.
4. **At first registered adapter (post-9.1):** PolicyGuard's `_adapters[role]` resolves to a real adapter. From this moment `send_command` is reachable.
5. **Pre-first-OCPP-BootNotification:** EV charger's `_ChargerState.connected=False`. `EVChargerAdapter.send_command` should NOT be reached because:
   - The adapter's capability profile is consulted by PolicyGuard FIRST
   - `get_capabilities()` returns the v1 profile regardless of connection state
   - But `send_command` execution will hit `protocol_adapter._handler is None or not self._state.connected` → `ProtocolCommandResult(error, "not_connected")` → mapped to `CommandResult(failed, "ocpp_error:not_connected")`. This is correct.
6. **Pre-first-Modbus-connect:** `ModbusTcpAdapter._client is None`. First send_command triggers `_ensure_connected()`; on connection failure → `_ModbusCommunicationError` → `ProtocolCommandResult(error, "modbus_error")` → `CommandResult(failed, "modbus_error:...")`. Correct.

**Normal-operation marker:** Once each registered adapter has completed at least one successful `get_state()` cycle (publishing a non-Degraded state to StateStore), the system is in normal operation for that adapter. Cold-start window for `send_command` is the same window — no separate readiness signal needed because PolicyGuard is the gate.

**Temporarily-relaxed invariants during cold-start:**
- Adapter map may be empty (8-2 deferred). Behavior is well-defined: every command rejected with `adapter_not_registered`. No invariant relaxation; just an empty configuration.
- `_ChargerState.last_known_transaction_id=None` until first StartTransaction observed. `StopEVChargingCommand` correctly rejects. No invariant relaxation.

#### R4 — Cancellation ownership map

Every async operation introduced or modified by Story 9.0:

| Async operation | CancelledError owner | Cancellation audit owner | Cleanup owner | Reporting field semantics under cancel |
|---|---|---|---|---|
| `BatteryAdapter.send_command(cmd)` | RetryPolicy.execute (existing) | RetryPolicy.`_emit_cancellation_audit` (existing) | `try/finally` on `ModbusTcpAdapter._lock` (already in place) | N/A — adapter never reports attempts |
| `InverterAdapter.send_command(cmd)` | N/A (raises TypeError synchronously before any await) | N/A | N/A | N/A |
| `EVChargerAdapter.send_command(cmd)` | RetryPolicy.execute (existing) | RetryPolicy.`_emit_cancellation_audit` (existing) | `try/finally` on `OCPPChargerAdapter.handler.call` boundary (existing) | N/A |
| `ModbusTcpAdapter.send_raw_command(raw_cmd)` (existing path; reused by 9.0) | Outer task (RetryPolicy) | RetryPolicy | `_lock` released in `finally` (existing in `async with self._lock`) | N/A |
| `OCPPChargerAdapter.send_raw_command(raw_cmd)` (existing path; reused by 9.0) | Outer task (RetryPolicy) | RetryPolicy | `asyncio.wait_for` cancels the inner `handler.call`; OCPP library handles connection cleanup on its own | N/A |
| `BatteryRegisterMap.command_payload(cmd)` (new, sync method) | N/A (sync) | N/A | N/A | N/A |

**Reporting field semantics during cancellation:**
- `attempts=N` (in RetryPolicy's audit) refers to the attempt-being-made when cancellation arrived. Pre-existing semantics ambiguity (Story 8-5 deferred); NOT introduced by this story.
- Adapter does NOT populate any cancellation-specific field. The `CancelledError` propagates without ever returning a `CommandResult`.

**No-cancellation paths:**
- `InverterAdapter.send_command` is fully synchronous (raises TypeError). No cancellation point.
- Register map encoding (`command_payload`) is fully synchronous. No cancellation point.
- correlation_id parsing is synchronous. No cancellation point.

**Resource cleanup on cancellation:**
- Modbus: `async with self._lock` releases the lock. The `_client` is preserved (next call reuses it) — this is correct because cancellation is not a connection failure.
- OCPP: `asyncio.wait_for` cancels the `handler.call` coroutine; the library handles its own state. The adapter does NOT set `_state.last_call_error` on cancellation (cancellation is not an error from the OCPP charger's perspective — it's our task being cancelled).

#### R5 — Before-first-successful-cycle lifecycle review

Trace from process start to first successful `send_command` outcome:

1. **t=0:** Process starts. Pydantic-settings loaded.
2. **t≈100ms:** Alembic migrations run.
3. **t≈300ms:** Time sync check. StateStore initialized (empty snapshot). PolicyGuard initialized with `adapters={}` map.
4. **t≈400ms:** Control loop task started. Watchdog task started. **At this point, every send_command attempt rejects with `adapter_not_registered`. RetryPolicy's first attempt returns immediately; non-idempotent commands are not retried; idempotent commands are retried 2x more then DEVICE-audited.**
5. **t≈500ms+:** `/health/ready` returns 200. Web routes accept traffic.
6. **t = (Story 9.1 site setup activated):** Installer wizard probes adapters; for each successful probe, `app.py` (or a setup service) registers the adapter into PolicyGuard's `_adapters` map. **From this moment, send_command is reachable for that role.**
7. **First successful send_command:** Decision engine's IntentExecutor produces a command → RetryPolicy → PolicyGuard (capability check passes, safety check passes) → adapter.send_command → protocol layer → ack → CommandResult(success). RetryPolicy emits DECISION audit. Control loop publishes new state on next poll cycle.

**What the system publishes/logs/audits in this window:**
- StateStore snapshots: published every control-loop tick, even with empty adapter map (degraded states for unregistered roles).
- structlog: `control_loop_started`, `watchdog_started`, `health_ready` events.
- audit_log: NOTHING related to commands until first command is dispatched. No premature DEVICE/DECISION/CONSTRAINT events.

**Temporarily-relaxed invariants:**
- "Every command pass through PolicyGuard" — STILL HOLDS. The only path to send_command is through PolicyGuard. The empty-map state means every command is rejected at the PolicyGuard level (correctly), not bypassed.
- "Capability profile is authoritative" — STILL HOLDS. With no adapter, there's no profile to check; capability_missing path is never reached for unregistered roles.

**Normal operation marker:** First `CommandResult(status=success, applied=True)` returned from any controllable adapter. Operationally: first DECISION audit row in `event_log`. Test fixture: integration test verifies the full path from cold start through adapter registration through first successful command.

#### R6 — Source-of-truth ownership per datum

| Datum | Single owner | Read path | Drift risk |
|---|---|---|---|
| Active write capabilities for a device model | `src/open_ems/adapters/capabilities/<role>.py` | PolicyGuard reads via `adapter.get_capabilities()` | **Drift risk vs. adapter implementation** — addressed by AC10's lockstep test |
| `correlation_id` (UUID) | `DeviceCommand.correlation_id` (Pydantic-validated UUID) | Adapter converts to str at protocol boundary, parses back from response | None — round-trip integrity asserted in AC8 tests |
| `correlation_id` (string form on the wire) | Protocol-layer `RawProtocolCommand.correlation_id` (NonEmptyStr) | Set by adapter; echoed back by `ProtocolCommandResult` | None — protocol-layer round-trip is the protocol library's responsibility (pymodbus / ocpp) |
| Active OCPP transaction id for a charger | `_ChargerState.last_known_transaction_id` (NEW field) | Updated on `StartTransaction`/`StopTransaction.conf`; read by `EVChargerAdapter.send_command` for stop commands | **Single owner — the per-charger _ChargerState instance.** No DB persistence; on process restart, transaction id is unknown until next OCPP message. Documented in dev notes as v1 trade-off; Story 9-0d (P2 hydration) does NOT cover this — OCPP transaction ids are intrinsically transient. |
| Modbus client instance | `ModbusTcpAdapter._client` (existing) | Lazy-initialized on first call | None — single owner |
| `CommandResult` (post-dispatch) | RetryPolicy returns it to control loop | Audit emitter sees it once; control loop sees it once; nobody else | None — frozen Pydantic model |
| Adapter timeout configuration | `ModbusTcpAdapterConfig.timeout_s` / `OCPPAdapterConfig.command_timeout_s` (existing) | Read at adapter init; not reloaded | Mild — Settings doesn't propagate per-adapter timeouts. Acceptable for v1; flagged as deferred-finding-eligible if installer needs per-device tuning later. |
| `WriteCapability` enum members | `src/open_ems/core/devices.py` | Imported by capabilities/* and policy_guard | None — single source |

**No drift-risk fields introduced by Story 9.0** beyond the already-documented capability-profile-vs-implementation lockstep (mitigated by AC10).

#### R7 — Deferred-findings triage

Scanned `_bmad-output/implementation-artifacts/deferred-work.md` for findings whose component or invariant overlaps Story 9.0:

| Finding (bracket ref) | Classification | Rationale |
|---|---|---|
| `[control_loop.py, app.py:_on_control_loop_done]` (8-2) — "Control loop crash not restarted after unhandled exception" | acceptable-post | Story 8.4 already addressed via watchdog. 9.0 does not change crash behavior. |
| `[app.py:223-236]` (8-2) — "`adapters={}` in production silently rejects all commands" | **partially-resolved-by-this-story** | Story 9.0 makes adapter implementations real but does NOT register them. Story 9.1 does the registration. Update the deferred-work entry post-9.1; for now, keep tracking. |
| `[policy_guard.py:44-45]` (8-2) — "Timeout constants not exposed via Settings" | safe-during | Story 9.0 honors the existing constants; no change needed. Promote to Settings if installer-tuning surfaces in 9.1+. |
| `[policy_guard.py:67]` (8-2) — "PolicyGuard re-fetches snapshot independently (stale-read window)" | safe-during | Adapter implementations don't worsen this. Belongs to PolicyGuard refactor scope. |
| `[policy_guard.py:137-141]` (8-2) — "Discharge command bypasses SoC floor check when battery state unavailable" | safe-during | Capability gate + discharge-floor check ordering is unchanged. Real adapter behavior makes the check more relevant but doesn't introduce new hazard. |
| `[policy_guard.py:97-100]` (8-2) — "`correlation_id` mismatch from adapter `CommandResult` passed through unchecked" | **must-resolve-in-this-story** | **Closed by AC8.** Adapter detects mismatch and returns `failed/correlation_id_mismatch`. Tests verify. |
| `[intent_executor.py:66]` (8-2) — "Zero-rate commands (`rate_kw=0.0`) produced and dispatched" | safe-during | Adapters handle 0.0 by passing through to register-map / OCPP encoding. Battery: 0.0 charge/discharge is valid (no-op). EV: 0.0 charging rate via SetChargingProfile is "essentially stop". Behavior is well-defined; flag in dev notes. |
| `[control_loop.py:54]` (8-1) — "`_monthly_peak_kw` not hydrated from DB" | acceptable-post | Story 9-0d (P2) handles this. NOT 9.0's scope. |
| `[control_loop.py:84-98]` (8-1) — "Sequential adapter polling blocks when one is slow" | safe-during | Adapter implementation realism makes this more visible but Story 9.0 doesn't change polling. May surface during 9.4 deployment validation; epic 9 retro will revisit. |
| `[control_loop.py:33, settings.py]` (8-1) — "Adapter timeout (10s) not bounded by `control_loop_interval_seconds`" | safe-during | Same — measurement during 9.4 may force action. Not 9.0's scope. |
| `[retry_policy.py:execute]` (8-3, 8-4, 8-5) — Multiple cancellation/attempts semantics findings | safe-during | RetryPolicy is upstream of adapters; 9.0 honors RetryPolicy as-is. Cancellation audit asymmetry (8-5 deferred) and `attempts=N` overstate (8-5 deferred) belong to RetryPolicy refactor scope. |
| `[adapters/ocpp/central_system.py:on_meter_values]` (4-3) — `reversed()` ordering assumption | safe-during | Reads are unaffected; commands don't depend on this. |
| `[adapters/ocpp/central_system.py:274-278]` (3-3) — "`OCPPCentralSystem.register()` silently overwrites existing adapter" | safe-during | 9.0 does not call register() in new ways. 9.1 wizard does — flag for 9.1 dev notes. |
| `[adapters/ocpp/charger_adapter.py:get_state]` (4-3) — "Negative power guard fires on every poll once triggered" | safe-during | Read-path concern; commands unaffected. |
| `[capabilities]` (4-4) — "Template profiles with `device_id="__placeholder__"` in public `__all__`" | safe-during | Profile-loading concern; AC10 verifies the runtime read path uses `get_profile()` which substitutes the real device_id. Direct dict imports remain a hazard for future stories. |
| `[capabilities]` (4-4) — "`_SUPPORTED_MODELS` and capability registry have no cross-validation" | **partially-touched-by-this-story** | AC10's lockstep check is the targeted fix for write_capabilities. The broader registry-vs-supported-models check remains for Story 9-0c (P4 carve-out). |
| `[adapters/discovery.py]` (4-5) — Discovery probe edge cases | safe-during | Discovery is read-only; 9.0 implements writes. Out of scope. |

**Empty-overlap categories:** event_log, web/auth, web/csrf, sessions, time_sync — none touched by 9.0.

### References

- [Source: _bmad-output/implementation-artifacts/epic-8-retro-2026-05-10.md#Story 9.0 — Real Adapter Command Execution Contracts and Implementations] — primary spec
- [Source: _bmad-output/planning-artifacts/architecture.md#CommandResult Pattern] (lines 700-714)
- [Source: _bmad-output/planning-artifacts/architecture.md#Adapter Interface Pattern] (lines 683-697)
- [Source: _bmad-output/planning-artifacts/architecture.md#Pattern: Degraded state is returned, not raised] (lines 414-435)
- [Source: _bmad-output/planning-artifacts/architecture.md#Enforcement Summary] (lines 715-748)
- [Source: _bmad-output/planning-artifacts/epics.md:152-154] — AR15, AR16, AR17 definitions
- [Source: src/open_ems/core/commands.py] — DeviceCommand subtypes, CommandResult, applied⇔success invariant
- [Source: src/open_ems/engine/policy_guard.py] — outer dispatch with COMMAND_DISPATCH_TIMEOUT_SECONDS
- [Source: src/open_ems/engine/retry_policy.py] — retry semantics, audit emission, cancellation handling
- [Source: src/open_ems/adapters/protocol.py] — RawProtocolCommand, ProtocolCommandResult, ProtocolStatus
- [Source: src/open_ems/adapters/modbus/tcp.py:219-249] — ModbusTcpAdapter.send_raw_command (already implemented)
- [Source: src/open_ems/adapters/ocpp/central_system.py:220-300] — OCPPChargerAdapter.send_raw_command (already implemented)
- [Source: tests/integration/engine/test_retry_pipeline.py:42-142] — SimulatedBatteryAdapter / SimulatedEVChargerAdapter precedent
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — full deferred-work register (R7 triage source)

## Dev Agent Record

### Agent Model Used

Claude Opus 4.7 (`claude-opus-4-7`) via `bmad-dev-story` workflow on 2026-05-10.

### Debug Log References

- Pre-story baseline: 871 tests pass, mypy/ruff/format clean.
- Post-implementation: 946 tests pass (+75 new), mypy/ruff/format clean.
- One mypy finding during dev (`Missing type arguments for generic type "frozenset"` on `_WRITE_CAPS` in `inverter.py`) was fixed by parameterising as `frozenset[WriteCapability]`.
- One ruff format / line-length cluster was resolved by reflowing the contract docstring's status-mapping table into a bullet form (no semantic change).

### Completion Notes List

- Story scaffolded by `bmad-create-story` with structural A1/A2 enforcement (Epic 8 retro action items A1+A2). A2-triggered: True (T2, T5 strong; T1, T4 partial). All seven orchestration artifacts R1–R7 authored substantively before `Status: ready-for-dev` was set.
- **Cross-adapter command contract (v1.0)** authored in `src/open_ems/adapters/__init__.py` with `COMMAND_CONTRACT_VERSION` constant. All four adapter modules (Battery, Inverter, EV Charger, Grid Meter) reference it.
- **BatteryAdapter** (`SetBatteryChargeRateCommand`, `SetBatteryDischargeRateCommand`): full Modbus write path via new `BatteryRegisterMap.command_payload(cmd)`. Both BYD HVS and HVM register maps implement encoding to uint16 watts (placeholder write registers 110/111 and 210/211; verify at installer commissioning). Out-of-range rate_kw raises `ValueError` and is wrapped as `register_map_encoding_error:...`.
- **InverterAdapter**: TypeError-only stance — v1 inverters declared read-only across all 3 supported models. Capability profiles updated to `write_capabilities=frozenset()`; `set_operating_mode` removed (it had no implementation behind it).
- **EVChargerAdapter**: full OCPP write path. Added `_ChargerState.last_known_transaction_id` and `next_charging_profile_id` fields; new `start_transaction` / `stop_transaction` handlers in `_InternalChargePoint` maintain them. Public accessors `OCPPChargerAdapter.active_transaction_id` (read) and `reserve_charging_profile_id()` (monotonic per-session counter) expose just enough state for the domain adapter without breaching encapsulation. SetChargingProfile composed per Dev Notes (TxProfile, Absolute, charging_rate_unit="W"); RemoteStopTransaction uses the active transaction id, returning `failed/no_active_ocpp_transaction` if none observed (closes Story 8.0 retro hazard).
- **OCPP `acked` ≠ command applied**: the adapter inspects the response payload's `status` field. `Accepted` ⇒ success; anything else (Rejected, NotSupported, etc.) ⇒ rejected with `ocpp_rejected:<status>`. RetryPolicy correctly does NOT retry rejected commands (Story 8.3 semantics).
- **AC8 — correlation_id mismatch contract**: closed the deferred Story 8.2 finding (`[policy_guard.py:97-100]`). Both adapters parse `result.correlation_id` back to UUID; mismatch (or unparseable) ⇒ `failed/correlation_id_mismatch` with structured `adapter_correlation_mismatch` warning log. The returned `CommandResult.correlation_id` is always the original `cmd.correlation_id`, never the mismatched value.
- **AC9 — AR16 wrapping**: every adapter wraps unexpected `Exception` (not `BaseException`/`CancelledError`/`TypeError`) as `adapter_internal_error:<exc_type>` with `exc_info=True` structlog log. PolicyGuard's outer `except Exception` becomes redundant defense-in-depth.
- **AC10 — capability profile alignment**: BYD profiles now contain only `set_charge_rate` and `set_discharge_rate` (lockstep with adapter implementation). Inverter profiles contain no write capabilities. EV charger profile contains `set_ev_charge_current` (covers both rate and stop commands per PolicyGuard's `_required_write_capability` mapping — verified consistent).
- **Test count**: 75 new tests across 7 new test files. 12 register-map tests (`test_byd_command_payload.py`), 13 battery send_command tests, 12 inverter TypeError tests, 15 EV charger send_command tests, 11 cross-adapter consistency tests, 3 cancellation propagation tests, 4 battery integration tests, 5 EV charger integration tests. **946 total tests pass**.
- **R7 deferred-findings closures**:
  - `[policy_guard.py:97-100]` (Story 8.2) — correlation_id mismatch passed through unchecked: **CLOSED** by AC8.
  - `[app.py:223-236]` (Story 8.2) — `adapters={}` silently rejects: **partially resolved** — adapter implementations exist; registration is Story 9.1's responsibility.
- **Test fixture preservation**: `SimulatedBatteryAdapter` / `SimulatedEVChargerAdapter` in `tests/integration/engine/test_retry_pipeline.py` and `test_command_pipeline.py` retained unchanged — they remain as control-loop integration fixtures. New real-adapter integration tests live alongside them under `tests/integration/adapters/`.
- **Zero new dependencies**: `uv.lock` unchanged. All work uses existing `pymodbus` and `ocpp` libraries.

### File List

**New files (source):**
- `src/open_ems/adapters/__init__.py` — overhauled to host the canonical cross-adapter command contract docstring + `COMMAND_CONTRACT_VERSION` constant.

**Modified files (source):**
- `src/open_ems/adapters/modbus/register_maps/_base.py` — added `ModbusCommandPayload` alias and `command_payload` method to `BatteryRegisterMap` Protocol.
- `src/open_ems/adapters/modbus/register_maps/__init__.py` — export `ModbusCommandPayload`.
- `src/open_ems/adapters/modbus/register_maps/byd_hvs_v1.py` — `command_payload` implementation; placeholder write registers; `_encode_setpoint_w` helper.
- `src/open_ems/adapters/modbus/register_maps/byd_hvm_v1.py` — same shape as HVS.
- `src/open_ems/adapters/modbus/battery_adapter.py` — full `send_command` implementation.
- `src/open_ems/adapters/modbus/inverter_adapter.py` — `TypeError`-only `send_command`; module docstring documents v1 read-only stance.
- `src/open_ems/adapters/ocpp/charger_adapter.py` — full OCPP `send_command` with SetChargingProfile / RemoteStopTransaction paths; OCPP-specific status mapping (`ocpp_rejected:<status>` on negative response).
- `src/open_ems/adapters/ocpp/central_system.py` — `_ChargerState` gains `last_known_transaction_id` and `next_charging_profile_id`; new `on_start_transaction` and `on_stop_transaction` handlers; new `OCPPChargerAdapter.active_transaction_id` and `reserve_charging_profile_id()` accessors.
- `src/open_ems/adapters/dsmr/meter_adapter.py` — docstring updated to cite the cross-adapter contract.
- `src/open_ems/adapters/capabilities/battery.py` — `set_operating_mode` removed from BYD profiles (AC10).
- `src/open_ems/adapters/capabilities/inverter.py` — empty `write_capabilities` for all 3 inverter models (AC3 / AC10).
- `src/open_ems/core/commands.py` — module docstring notes Story 9.0 closes the AR16 gap.

**New files (tests):**
- `tests/unit/adapters/modbus/register_maps/__init__.py`
- `tests/unit/adapters/modbus/register_maps/test_byd_command_payload.py` — 12 tests for both BYD register maps' `command_payload`.
- `tests/unit/adapters/modbus/test_battery_adapter_send_command.py` — 13 tests for AC11.1–AC11.9 (battery scope).
- `tests/unit/adapters/modbus/test_inverter_adapter_send_command.py` — 12 parametrized TypeError tests.
- `tests/unit/adapters/ocpp/test_charger_adapter_send_command.py` — 15 tests for AC11.1–AC11.10 (EV charger scope).
- `tests/unit/adapters/test_cross_adapter_consistency.py` — 11 cross-adapter consistency tests.
- `tests/unit/adapters/test_cancellation_propagation.py` — 3 cancellation tests including Modbus `_lock` release verification.
- `tests/integration/adapters/test_battery_send_command.py` — 4 end-to-end battery integration tests.
- `tests/integration/adapters/test_ev_charger_send_command.py` — 5 end-to-end EV charger integration tests.

**Modified files (tests):**
- `tests/unit/adapters/test_capability_registry.py` — assertions updated for inverter empty-write and BYD `set_operating_mode` removal.
- `tests/unit/adapters/test_adapter_capabilities.py` — same updates.

**Modified files (docs / planning):**
- `_bmad-output/planning-artifacts/architecture.md` § CommandResult Pattern — adds Story 9.0 status note + `COMMAND_CONTRACT_VERSION` reference.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — Story 9-0 marked `in-progress` then `review`.

### Change Log

| Date | Change | Notes |
|---|---|---|
| 2026-05-10 | Story 9.0 implementation complete | Cross-adapter command contract authored; Battery/Inverter/EV Charger `send_command` implementations land; 75 new tests; 946 total tests pass; ruff/format/mypy clean. Closes deferred AC8 correlation-id finding from Story 8.2 review. |
