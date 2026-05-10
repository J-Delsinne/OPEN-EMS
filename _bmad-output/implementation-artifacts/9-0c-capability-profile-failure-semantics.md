# Story 9.0c: Capability-Profile Failure Semantics

Status: done

> **Origin:** Epic 8 retrospective (2026-05-10) — P4 carve-out. Doc + tests format. Hard gate before Story 9.2 (role assignment generates capability profiles dynamically). Three PolicyGuard failure paths must be unambiguous and locked by contract + targeted hardening tests before installer-facing flows can mutate the capability surface. Story 9.0 (real-adapter dispatch) flagged additional capability-profile drift risks via review findings; this story closes them at the contract and registry layer.
>
> **A2-triggered:** True. Matched triggers: **T5** (multi-adapter coordination — the three-path contract and registry cross-validation apply across all four adapter classes: battery, inverter, EV charger, grid meter), **T6** (deployment / restart behavior — a new fail-loud startup cross-validation gate is introduced; cold-start drift between `_SUPPORTED_MODELS` and the capability registry now aborts boot). Partial: **T1** (lifecycle — PolicyGuard's capability gate gains a documented contract for five rejection paths; one new path is added for discharge-without-battery-state). All R1–R7 orchestration artifacts are present below.

## Story

As a developer composing the installer site-setup wizard (Story 9.2) and any future command origin on top of PolicyGuard,
I want every PolicyGuard capability-failure path locked by a documented contract with exhaustive test coverage and a fail-loud startup gate against capability-registry drift,
so that when Story 9.2 dynamically wires real device capability profiles per discovered model, the rejection vocabulary is stable, the audit fields are predictable, and no silent REDUCED downgrade or `TypeError` from a registry mismatch reaches production.

## Acceptance Criteria

### AC1 — The five-path capability-failure contract is documented

**GIVEN** `src/open_ems/engine/policy_guard.py` is the canonical reference for command authorization
**WHEN** a developer reads the module docstring
**THEN** the docstring enumerates the **five rejection paths** that PolicyGuard's capability + dispatch surface produces, each with its **exact** `CommandResult.reason` string and the audit-event semantics:

| # | Path | Trigger | Exact `reason` string | Audit `event_type` | `applied` | `status` |
|---|---|---|---|---|---|---|
| P0 | Fail-safe mode active | `snapshot.operating_mode is SystemOperatingMode.fail_safe` | `fail_safe_mode_active` | `CONSTRAINT` | `False` | `rejected` |
| P1 | Adapter not registered | `command.device_role not in self._adapters` | `adapter_not_registered` | `CONSTRAINT` | `False` | `rejected` |
| P2 | Capability lookup raised | `adapter.get_capabilities()` raises OR times out | `capability_check_failed` | `CONSTRAINT` | `False` | `rejected` |
| P3 | Unknown command type | `_required_write_capability(command)` raises `TypeError` | `unknown_command_type` | `CONSTRAINT` | `False` | `rejected` |
| P4 | Capability missing from profile | `required_capability not in profile.write_capabilities` | `capability_missing: {capability.value}` | `CONSTRAINT` | `False` | `rejected` |

**AND** the docstring states the **order** in which the five paths are evaluated (P0 → P1 → P2 → P3 → P4 → safety constraints → dispatch). The order is load-bearing: a fail-safe-mode active rejection MUST NOT incur a capability lookup; an unregistered-adapter rejection MUST NOT incur a capability lookup.

**AND** the docstring states the **adapter-side contract** that PolicyGuard relies on:
- `get_capabilities()` MUST be synchronous-fast (no protocol I/O); any blocking I/O is a contract violation and will trigger P2 via timeout.
- `get_capabilities()` MUST return a `DeviceCapabilityProfile` whose `device_id` equals the adapter's configured `device_id`. A mismatch is treated as P2.
- Unknown-firmware / unknown-model devices MUST still return a profile (REDUCED, no write capabilities) — `get_capabilities()` MUST NEVER raise for this reason. The capability registry's `get_profile()` already enforces this; this AC formalizes the requirement at the adapter contract level.

**AND** the docstring cross-references the capability-registry contract in `src/open_ems/adapters/capabilities/__init__.py` and the `WriteCapability` enum in `src/open_ems/core/devices.py`.

**Why P0–P4 are listed explicitly:** the Epic 8 retro P4 carve-out names three paths; AC1 expands to five because `fail_safe_mode_active` (P0) and `unknown_command_type` (P3) are the same enforcement class — they reject before dispatch with a `CONSTRAINT` audit and a fixed reason string — and Story 9.2's installer flow needs the complete vocabulary, not just the three retro-named paths.

---

### AC2 — Exhaustive test matrix locks the contract across all four command types

**GIVEN** `tests/unit/engine/test_policy_guard.py` already covers each of the five paths for the battery-discharge happy path
**WHEN** Story 9.0c adds a parametrized test suite
**THEN** every one of P0–P4 is exercised against **every** controllable command type:

| Command type | P0 | P1 | P2 | P3 | P4 |
|---|---|---|---|---|---|
| `SetBatteryChargeRateCommand` | required | required | required | n/a (handled) | required (missing `set_charge_rate`) |
| `SetBatteryDischargeRateCommand` | required | required | required | n/a (handled) | required (missing `set_discharge_rate`) |
| `SetEVChargingRateCommand` | required | required | required | n/a (handled) | required (missing `set_ev_charge_current`) |
| `StopEVChargingCommand` | required | required | required | n/a (handled) | required (missing `set_ev_charge_current`) |

**AND** P3 (`unknown_command_type`) is exercised by constructing a `DeviceCommand` subclass NOT in `_required_write_capability`'s isinstance chain and asserting the rejection. Use a minimal test-only subclass — DO NOT extend the production `commands.py` enum.

**AND** for each path × command-type cell, the test asserts ALL of:
- `result.status is CommandStatus.rejected`
- `result.applied is False`
- `result.reason == <exact string from AC1's table>` (use `==`, not `in`, except for P4 where the suffix differs by capability)
- `audit_spy.assert_awaited_once()` with `event_type="CONSTRAINT"`, `actor="system"`, `detail["correlation_id"] == str(cmd.correlation_id)`, `detail["command_type"] == type(cmd).__name__`, `detail["rejection_reason"]` matching the reason
- `adapter.send_command.assert_not_awaited()` (P0 path additionally asserts `adapter.get_capabilities.assert_not_awaited()` — fail-safe short-circuits before capability lookup)
- P1 path asserts no `adapter.get_capabilities` call (the adapter doesn't exist) and no `send_command` call

**AND** the test file uses the existing `_active_constraints_provider()` fixture pattern from `tests/unit/engine/test_policy_guard.py:46` — do NOT introduce a new provider construction pattern.

**AND** the test matrix is implemented as `pytest.mark.parametrize` with named parameters (`command_factory`, `expected_reason`, `path_label`) so that failures surface the failing cell directly in the test ID.

---

### AC3 — Capability-lookup failure path (P2) covers the full exception surface

**GIVEN** PolicyGuard's existing `test_capability_check_failure_is_rejected` covers a single `RuntimeError("boom")` case
**WHEN** Story 9.0c extends P2 coverage
**THEN** parametrized tests assert that **every** exception raised from `adapter.get_capabilities()` produces `capability_check_failed` (and none other), for the following exception classes:

1. `RuntimeError`
2. `ConnectionError`
3. `ValueError`
4. `OSError`
5. `asyncio.TimeoutError` (raised directly, not via `wait_for`)
6. `Exception` (the bare base class)
7. **Timeout via `asyncio.wait_for`** — `get_capabilities()` sleeps longer than `CAPABILITY_CHECK_TIMEOUT_SECONDS` (use `monkeypatch` to drop the constant to 0.05s for the test; restore after)

**AND** `asyncio.CancelledError` is explicitly tested to **propagate** (NOT to be caught by the `except Exception` at policy_guard.py:84). Use `pytest.raises(asyncio.CancelledError)` to assert propagation. Catching `CancelledError` would silently swallow control-loop shutdown — same invariant Story 9.0 locked for adapter `send_command` (clause 5).

**AND** an adversarial profile-return test: an adapter that returns `None` from `get_capabilities()` (typing breach by a buggy adapter) MUST produce `capability_check_failed` rather than `AttributeError` or `TypeError` at policy_guard.py:103. Required fix: wrap the `required_capability not in profile.write_capabilities` line in a defensive isinstance check OR document the typing invariant. **Implementation choice:** assert `isinstance(profile, DeviceCapabilityProfile)` after the await and reject with `capability_check_failed` if not. Update policy_guard.py:80-91 accordingly.

**AND** an adapter that returns a `DeviceCapabilityProfile` whose `device_id` does NOT match the adapter's configured `device_id` produces `capability_check_failed` (treated as a profile-integrity violation). New defensive check inside the `try:` block after the await.

---

### AC4 — Discharge-without-battery-state is rejected with a new explicit reason

**GIVEN** the deferred 8.2 finding `[src/open_ems/engine/policy_guard.py:137-141]`: "Discharge command bypasses SoC floor check when battery state unavailable"
**AND** the 9.0b review-time R7 triage tagged this as `safe-during` with the note "Story 9-0c (capability-profile failure semantics) is the right place to revisit"
**WHEN** Story 9.0c resolves it
**THEN** `_check_safety_constraints` in `policy_guard.py` is amended so that a `SetBatteryDischargeRateCommand` whose `snapshot.battery` is **not** a `BatteryState` instance is rejected with the **new** reason string:

```
battery_state_unavailable_for_safety_check
```

**AND** this reason is added to AC1's table as a **safety-constraint** rejection (NOT a capability-failure path — it lives in `_check_safety_constraints`, evaluated AFTER P0–P4). Document this in the docstring under a new "Safety-constraint rejection paths" subsection alongside `battery_soc_at_or_below_reserve_floor`, `commanded_rate_exceeds_peak_limit`, and `conservative_mode_blocks_load_increase`.

**AND** the change preserves the behavior for charge commands: `SetBatteryChargeRateCommand` with no battery state is NOT rejected by this new check (charge commands have no SoC-floor precondition). The new check fires ONLY for `SetBatteryDischargeRateCommand` × `not isinstance(snapshot.battery, BatteryState)`.

**AND** a parametrized test asserts the rejection for `snapshot.battery is None`, `snapshot.battery is DegradedDeviceState(...)`, and any other non-`BatteryState` placeholder. Use the existing `_publish_battery` fixture pattern but skip publishing the battery to leave the slot as `None`.

**AND** every existing test in `tests/unit/engine/test_policy_guard.py` that exercises discharge commands MUST publish a valid `BatteryState` (most already do). Audit the file for any test that exercises discharge without publishing battery state and either fix the fixture or assert the new reason explicitly.

**Why this matters now (and not in Epic 11 when homeowner override lands):** Story 9.2 (role assignment) will dynamically assign DeviceRole.battery; Story 9.3 (constraint configuration) will validate constraints against live state; Story 9.4 (deployment validation) will probe the control pipeline. Any of those flows can present PolicyGuard with a discharge command in a "battery role assigned but state not yet published" window. Without this fix, a discharge command in that window slips past the reserve-floor check and reaches the adapter.

---

### AC5 — Startup cross-validation of `_SUPPORTED_MODELS` × capability registry (fail-loud)

**GIVEN** the deferred 4.4 finding `[capabilities]`: "`_SUPPORTED_MODELS` and capability registry have no cross-validation — adding a model to an adapter's `_SUPPORTED_MODELS` without adding it to the capability registry silently degrades to a REDUCED profile"
**WHEN** Story 9.0c adds a structural startup gate
**THEN** a new module-level function `validate_capability_registry_alignment()` is added to `src/open_ems/adapters/capabilities/__init__.py` that:

1. Imports `_SUPPORTED_MODELS` from `open_ems.adapters.modbus.battery_adapter` and `open_ems.adapters.modbus.inverter_adapter` (Modbus adapters declare a registry; OCPP and DSMR use fixed model strings — see below).
2. For each model in each Modbus adapter's `_SUPPORTED_MODELS`, asserts the model exists in `_ALL_PROFILES`.
3. Additionally asserts the fixed-model adapters' model strings are present: `"ocpp_1_6"` (from `ocpp/charger_adapter.py:158`) and `"dsmr_p1"` (from `dsmr/meter_adapter.py:84`).
4. Raises `CapabilityRegistryDriftError` (a new exception class in `adapters/capabilities/__init__.py`) listing **every** missing model on a single fail-loud raise. Do NOT raise on the first miss — collect all and report together for operator efficiency.
5. Returns `None` on success.

**AND** `validate_capability_registry_alignment()` is called at lifespan startup in `src/open_ems/web/app.py` AFTER `init_database()` and BEFORE `ActiveConstraintsProvider.hydrate()` — capability-registry drift is a "process must not start" condition, same enforcement class as `migration_failed` and `constraints_hydrate_failed`. On failure, log `startup_failed reason="capability_registry_drift"` with the list of missing models and `raise SystemExit(1)`.

**AND** the call is wrapped in the same try/except → `SystemExit(1)` pattern as the existing migration/constraints paths in `app.py`. Reuse the cleanup/close-database structure that Story 9.0b's D4→P10 patch consolidated.

**AND** unit tests in `tests/unit/adapters/test_capability_registry.py` cover:
1. Success path: with current `_SUPPORTED_MODELS` + registry state, `validate_capability_registry_alignment()` returns `None` and does not raise.
2. Drift detected: monkey-patch one of the adapter `_SUPPORTED_MODELS` to inject a model NOT in the registry; assert `CapabilityRegistryDriftError` is raised with the injected model name in the message.
3. Multiple-model drift: inject two missing models; assert the error message names **both** (not just the first).
4. Fixed-model assertion: monkey-patch `_ALL_PROFILES` to remove `"ocpp_1_6"`; assert the error message names it.

**AND** an integration test in `tests/integration/web/test_app_startup.py` (create file if it does not exist) monkey-patches the registry validator to raise; asserts the lifespan emits `startup_failed reason="capability_registry_drift"` AND `SystemExit(1)`. Mirror the existing `test_lifespan_migration_failure` pattern if present, otherwise the `test_lifespan_hydrates_before_control_loop` pattern from 9.0b's tests.

**Why fail-loud over warn-and-continue:** Story 9.2 will let the installer assign a device role. If `_SUPPORTED_MODELS` has a model that the registry doesn't know about, the adapter constructs successfully but `get_profile(model)` returns a REDUCED unknown profile with no write capabilities — every write command is then rejected with `capability_missing` at runtime. This presents to the installer as "the device is connected but nothing works" with no actionable signal. Fail-loud at startup means the operator (or CI, when this drift is introduced via a code change) sees the misalignment immediately.

---

### AC6 — `correlation_id` round-trip enforcement at PolicyGuard

**GIVEN** the deferred 8.2 finding `[policy_guard.py:97-100]`: "`correlation_id` mismatch from adapter `CommandResult` passed through unchecked — a buggy adapter returning a different UUID silently breaks audit correlation; adapter contract enforcement concern for Epic 9"
**AND** Story 9.0 locked the cross-adapter command contract clause that `CommandResult.correlation_id` MUST equal `command.correlation_id`
**WHEN** Story 9.0c enforces it at PolicyGuard
**THEN** after `await asyncio.wait_for(adapter.send_command(command), ...)` succeeds and returns a `CommandResult`, PolicyGuard asserts `result.correlation_id == command.correlation_id`. On mismatch:

1. Construct a synthetic `CommandResult` with `status=CommandStatus.failed`, `applied=False`, `reason="adapter_correlation_id_mismatch"`, `correlation_id=command.correlation_id` (the command's original — preserve audit correlation).
2. Log `policy_guard_correlation_mismatch` at WARNING level with `device_id`, `expected_correlation_id`, `received_correlation_id`, `component="engine"`.
3. Emit a CONSTRAINT audit row with `rejection_reason="adapter_correlation_id_mismatch"` via the existing `_reject()` helper.
4. Return the synthetic result.

**AND** add this as **path P5** in AC1's contract table (post-dispatch enforcement; lives after `send_command` returns, not in the capability gate proper):

| # | Path | Trigger | Exact `reason` | Audit | `applied` | `status` |
|---|---|---|---|---|---|---|
| P5 | Adapter correlation_id mismatch | `result.correlation_id != command.correlation_id` post-dispatch | `adapter_correlation_id_mismatch` | `CONSTRAINT` | `False` | `failed` |

**AND** a unit test injects a `MagicMock` adapter whose `send_command` returns a `CommandResult` with a fresh `uuid.uuid4()` as `correlation_id`; assert PolicyGuard returns `failed` with the synthetic correlation_id matching the input command's. Use the standard fixture pattern from `_adapter()` but override `send_command_result.correlation_id`.

**AND** the existing happy-path test `test_successful_dispatch_returns_adapter_result_no_audit` is reviewed — the `_adapter()` fixture's default `CommandResult.correlation_id=uuid.uuid4()` does NOT match the command's correlation_id, so that test is **currently passing only because the assertion does not exist yet**. Update the `_adapter()` helper to optionally accept the command and stamp its correlation_id into the returned result; update every call site to use the new helper. This is a mechanical sweep — every `_adapter()` call gets a `command=cmd` parameter.

---

### AC7 — Existing test sweep: every PolicyGuard test asserts the correct rejection reason

**GIVEN** the contract table in AC1 + AC4 + AC6 is the authoritative list of rejection reason strings
**WHEN** Story 9.0c runs the sweep
**THEN** every test in `tests/unit/engine/test_policy_guard.py` that currently asserts a rejection reason via substring (`"capability_missing" in result.reason`) is upgraded to assert the **exact** string format documented in AC1 (e.g., `result.reason == "capability_missing: set_discharge_rate"`).

**AND** every test that constructs a discharge command without publishing a battery state must either (a) publish a battery state, or (b) assert the new `battery_state_unavailable_for_safety_check` reason from AC4. No test may pass through both AC4 rejection and a later assertion silently.

**AND** the sweep produces a brief markdown table in this story's Completion Notes listing every modified test, the previous reason assertion, and the new exact-match assertion. This table is the audit trail proving no rejection reason regressed in the sweep.

---

### AC8 — Test fixture polish (closes 9.0b deferred W1 + W2 for the capability gate)

**GIVEN** 9.0b's deferred-work entries:
- "Test fixtures bypass `hydrate()` contract by writing private `_current` field"
- "`MagicMock(spec=ConfigRepo)` accepts unconfigured method calls silently"

**WHEN** Story 9.0c touches the PolicyGuard test fixture surface (necessary for AC2 + AC6)
**THEN** the fixture helpers in `tests/unit/engine/test_policy_guard.py` are tightened **only for the new tests** added in this story (do NOT sweep 14 pre-existing fixture sites — out of scope; defer remains):

1. New tests use a `MagicMock(spec=ConfigRepo)` whose `get_active` is configured with `side_effect=AssertionError("PolicyGuard hot path must not touch ConfigRepo")`. This catches any future hot-path-DB-read regression in the new tests.
2. New tests use a public test helper, `ActiveConstraintsProvider.from_snapshot(snapshot)` classmethod (added in this story), instead of the `# noqa: SLF001` private-field write. The classmethod constructs a provider, sets `self._current = snapshot`, sets `is_hydrated = True`, and returns it. Public, named, intended-for-tests. Add to `src/open_ems/services/active_constraints.py` with a docstring stating "Test-only constructor; production code must call `await hydrate()`".
3. Existing test sites that use the private-field-write pattern are NOT swept here (deferred W1 from 9.0b remains in deferred-work.md, scope unchanged). Adding the public helper is forward-looking — future stories may sweep at their convenience.

**Why limit the scope:** 9.0b's review explicitly deferred the 14-site sweep. 9.0c's job is to lock the failure-semantics contract; broad fixture refactoring would inflate scope. New tests get the clean pattern; old tests stay as-is until prioritized.

---

### AC9 — Adversarial 3-layer review with severity tagging (process AC)

**GIVEN** Story 9.0c is A2-triggered (T5 + T6, partial T1 — see story header)
**WHEN** the story reaches `Status: review`
**THEN** the tiered review model from Epic 8 retro action A3 applies:
- **Layer 1 (Blind Hunter):** unsafe patterns — exception-class catches that swallow `CancelledError`; `isinstance` guards missing on adapter return; new safety-constraint path that fires before P0–P4; correlation-mismatch race; startup cross-validation that imports adapter modules in the wrong order and creates a circular import.
- **Layer 2 (Edge Case Hunter):** runtime corners — `asyncio.wait_for` cancellation propagation in P2 path; what happens if `get_capabilities()` returns a profile whose `device_id` is correct but whose `model` differs from the adapter's `_model` attribute; new `battery_state_unavailable_for_safety_check` reason firing during the cold-start window before the first battery state is published; `CapabilityRegistryDriftError` raised during lifespan but `close_database()` not invoked (the D4→P10 fix from 9.0b must extend to this new failure path); registry drift introduced via test monkey-patching not isolated and bleeding into other tests.
- **Layer 3 (Acceptance Auditor):** AC drift — every clause in AC1's contract table traced to a test in AC2; every AC3 exception class has a parametrized test; AC5 startup-validation has BOTH unit and integration coverage; AC6 happy-path correlation enforcement is genuinely exercised (not accidentally passing because the assertion is absent).

**AND** every finding carries a severity tag (HIGH / MEDIUM / LOW). `Status: review → done` is BLOCKED on any unresolved HIGH.

---

### AC10 — Quality gates clean

- `uv run python -m pytest tests/ --no-cov -q` — baseline (1020 from 9.0b close) + new tests added in this story, all green.
- `uv run python -m mypy src/` — clean.
- `uv run python -m ruff check .` — clean.
- `uv run python -m ruff format --check .` — clean.

**AND** the PolicyGuard module docstring is the canonical reference for the contract; no inline duplication of the contract table elsewhere in the codebase. Story 9.2's dev notes will cite the docstring section.

---

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: AC10)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` — recorded baseline: **1020 passed** (matches expected from Story 9.0b close).
  - [x] Run `uv run python -m ruff check .` — clean.
  - [x] Run `uv run python -m ruff format --check .` — clean.
  - [x] Run `uv run python -m mypy src/` — clean.

- [x] Task 1: Document the five-path contract (AC: AC1)
  - [x] Expanded module docstring at `src/open_ems/engine/policy_guard.py` with the P0–P4 contract, evaluation order, adapter-side contract for `get_capabilities()`, safety-constraint subsection (including new AC4 reason), and P5 post-dispatch enforcement.
  - [x] Cross-references to `adapters/capabilities/__init__.py` and `core/devices.py:WriteCapability` added.

- [x] Task 2: P2 hardening — defensive checks around `get_capabilities()` return (AC: AC3)
  - [x] Added `isinstance(profile, DeviceCapabilityProfile)` and `profile.device_id == command.device_id` guards inside the `try:` block — both surface as `capability_check_failed`.
  - [x] Code comment documents the `CancelledError` propagation invariant explicitly (the `except Exception` excludes `BaseException`).

- [x] Task 3: AC4 — discharge without battery state (AC: AC4)
  - [x] In `_check_safety_constraints`, added the AC4 check BEFORE the SoC-floor branch with reason `battery_state_unavailable_for_safety_check`.
  - [x] Docstring's safety-constraint subsection lists the new reason.

- [x] Task 4: AC5 — startup cross-validation (AC: AC5)
  - [x] Added `CapabilityRegistryDriftError` and `validate_capability_registry_alignment()` to `src/open_ems/adapters/capabilities/__init__.py`. Uses deferred imports inside the function body to break the circular import.
  - [x] Wired into `web/app.py` lifespan between `init_database()` and `ActiveConstraintsProvider`, inside the outer try so `close_database()` runs on failure.
  - [x] On drift, logs `startup_failed reason="capability_registry_drift" missing_models=[...]` and raises `SystemExit(1) from None`.

- [x] Task 5: AC6 — correlation_id enforcement (AC: AC6)
  - [x] After `await asyncio.wait_for(adapter.send_command(command), ...)`, added the mismatch check + synthetic failed `CommandResult` with the command's original `correlation_id`.
  - [x] Refactored `_reject()` to accept an optional `status: CommandStatus = CommandStatus.rejected` parameter (cleaner than a parallel helper — single audit-emission path, single signature).
  - [x] Updated `_adapter()` test helper to accept an optional `command` parameter and stamp its `correlation_id` into the default/overridden `CommandResult`.

- [x] Task 6: AC8 — `ActiveConstraintsProvider.from_snapshot()` test-only constructor (AC: AC8)
  - [x] Added `@classmethod from_snapshot(cls, snapshot, *, settings=None)` to `src/open_ems/services/active_constraints.py`.
  - [x] Internal `_RaisingRepo` sentinel raises `AssertionError("PolicyGuard hot path must not touch ConfigRepo")` on any awaited method.

- [x] Task 7: AC2 — parametrized test matrix (AC: AC2)
  - [x] New file `tests/unit/engine/test_policy_guard_capability_failure_matrix.py` covers P0..P4 × 4 command types via `pytest.mark.parametrize`.
  - [x] P3 covered with a test-only `_UnknownTestCommand(DeviceCommandBase)` subclass defined inside the test file; `commands.py` untouched.

- [x] Task 8: AC3 — P2 exception surface (AC: AC3)
  - [x] Parametrized tests for `RuntimeError`, `ConnectionError`, `ValueError`, `OSError`, `TimeoutError` (the unified Python 3.11+ alias for `asyncio.TimeoutError`), bare `Exception` — all produce `capability_check_failed`.
  - [x] `CancelledError` propagation test via `pytest.raises(asyncio.CancelledError)`.
  - [x] `get_capabilities() returns None` test → `capability_check_failed`.
  - [x] `device_id` mismatch test → `capability_check_failed`.
  - [x] Timeout-via-`wait_for` test uses `monkeypatch.setattr(policy_guard_module, "CAPABILITY_CHECK_TIMEOUT_SECONDS", 0.05)` (auto-restored by pytest's monkeypatch fixture).

- [x] Task 9: AC4 — discharge-without-battery-state tests (AC: AC4)
  - [x] Parametrized over `snapshot.battery is None` and `snapshot.battery = DegradedDeviceState(...)`.
  - [x] Verified charge commands still pass through with no published battery (no false-positive rejection).
  - [x] Sanity test confirms AC4's check does NOT short-circuit the existing SoC-floor branch (valid `BatteryState` at the floor still yields `battery_soc_at_or_below_reserve_floor`).

- [x] Task 10: AC5 — capability-registry-drift tests (AC: AC5)
  - [x] Unit tests in `tests/unit/adapters/test_capability_registry.py`: success path, single-drift, multi-drift, fixed-model drift (`ocpp_1_6`), exception message lists every missing model.
  - [x] Integration tests in `tests/integration/web/test_app_startup.py` (new file): lifespan SystemExit(1) on drift, drift detected BEFORE `ActiveConstraintsProvider` construction, validator called on the happy path.

- [x] Task 11: AC7 — sweep existing PolicyGuard tests for exact-match assertions (AC: AC7)
  - [x] Converted both substring assertions on the P4 reason in `tests/unit/engine/test_policy_guard.py` (lines 237 + 576) to exact-match.
  - [x] Audited every discharge test for battery-state publication — all 7 discharge sites in `test_policy_guard.py` publish a valid `BatteryState`. No new AC4 rejections need to be asserted there.
  - [x] Audit table produced in Completion Notes (below).

- [x] Task 12: Final quality gate (AC: AC10)
  - [x] `uv run python -m pytest tests/ --no-cov -q` — **1062 passed** (baseline 1020 + 42 new).
  - [x] `uv run python -m mypy src/` — clean.
  - [x] `uv run python -m ruff check .` — clean.
  - [x] `uv run python -m ruff format --check .` — clean.

- [x] Task 13: Submit for adversarial 3-layer review (AC: AC9)
  - [x] `Status: review`.
  - [ ] Layer 1 (Blind Hunter): unsafe-pattern sweep on new code.  *(reviewer-owned)*
  - [ ] Layer 2 (Edge Case Hunter): runtime/lifecycle sweep.  *(reviewer-owned)*
  - [ ] Layer 3 (Acceptance Auditor): AC1–AC10 → test traceability matrix.  *(reviewer-owned)*
  - [ ] Severity tags HIGH/MEDIUM/LOW on every finding. No HIGH unresolved at `Status: review → done`.  *(reviewer-owned)*

---

## Dev Notes

### Why this story exists (the carve-out narrative)

Epic 8 retrospective (`_bmad-output/implementation-artifacts/epic-8-retro-2026-05-10.md`) carved out four prep stories before Story 9.1. Story 9.0c is **P4 — Capability-Profile Failure Semantics**. The retro framed it as a doc + tests story, but the review of Story 9.0 surfaced two additional capability-related findings (`_SUPPORTED_MODELS` × registry drift, correlation_id pass-through) and 9.0b's R7 triage explicitly handed off the discharge-without-battery-state finding ("Story 9-0c is the right place to revisit"). Story 9.0c absorbs all three.

This story does **not** introduce new command types, new safety constraints, or new device classes. It locks the contract for behavior PolicyGuard already exhibits (sometimes correctly, sometimes silently), and it adds two fail-loud invariants — startup registry drift and post-dispatch correlation_id enforcement — that Epic 9's installer-facing workflows depend on.

### The five-path contract (canonical for Task 1)

PolicyGuard is the only legal call site for `adapter.send_command()` (AR15). Its capability + dispatch surface produces **five rejection paths** and one **post-dispatch enforcement** path. Each path emits a CONSTRAINT audit event, leaves `applied=False`, and returns a `CommandResult` with the exact reason strings listed in AC1. The evaluation order is load-bearing: a fail-safe rejection short-circuits everything; an unregistered-adapter rejection short-circuits capability lookup; an unknown-command-type rejection short-circuits capability presence checks.

### Adapter-side contract for `get_capabilities()`

Adapters implement `DeviceAdapter.get_capabilities()` as a thin lookup against the capability registry (see `adapters/modbus/battery_adapter.py:121-135` for the reference shape). Three invariants hold:

1. **No protocol I/O.** `get_capabilities()` reads `self._model` and calls `get_profile(device_id, model)` — both synchronous-fast. Any blocking I/O is a contract violation. PolicyGuard enforces a 5-second timeout (`CAPABILITY_CHECK_TIMEOUT_SECONDS`) as defense in depth.
2. **Always returns a profile.** `get_profile()` returns a REDUCED unknown profile for unknown models — it never raises. Adapters MUST NOT raise either; if they do, P2 fires.
3. **device_id consistency.** The returned profile's `device_id` equals the adapter's configured `device_id`. The registry's `model_copy(update={"device_id": device_id, ...})` step (`adapters/capabilities/__init__.py:71-73`) guarantees this for registry-resolved profiles; defense in depth at the PolicyGuard layer is added in Task 2.

### Where the discharge-without-battery-state fix sits

`_check_safety_constraints` (`policy_guard.py:145-167`) currently guards the SoC-floor check on `isinstance(snapshot.battery, BatteryState)` — if the battery slot holds `DegradedDeviceState` or `None`, the check is silently skipped and the discharge command proceeds to dispatch. Today this is "safe" because `IntentExecutor` (the only command origin in v1) never produces discharge commands without a valid `BatteryState`. Epic 9 changes that: Story 9.2 dynamically assigns the battery role; Story 9.4 probes the control pipeline. A discharge command in a "role assigned, state not yet published" window would slip past the floor check. AC4's new `battery_state_unavailable_for_safety_check` reason closes this **before** Epic 9 hits.

### Capability-registry drift: the cold-start invariant

Today: adding a model to `BatteryModbusAdapter._SUPPORTED_MODELS` (e.g. a future `byd_hvs_v2`) without adding it to `BATTERY_PROFILES` in `adapters/capabilities/battery.py` results in:
1. Adapter construction succeeds (`_SUPPORTED_MODELS` accepts the new model).
2. `get_capabilities()` calls `get_profile(device_id, "byd_hvs_v2")` → returns the REDUCED unknown profile (`adapters/capabilities/__init__.py:67`).
3. Every PolicyGuard write rejected with `capability_missing` — silently.

After 9.0c: process startup invokes `validate_capability_registry_alignment()`, which compares each adapter's `_SUPPORTED_MODELS` against `_ALL_PROFILES` and raises `CapabilityRegistryDriftError` listing every missing model. Lifespan logs `startup_failed reason="capability_registry_drift"` and `SystemExit(1)`. The drift is impossible to ship; CI fails as soon as the misalignment lands.

**Circular-import note:** `adapters/capabilities/__init__.py` currently imports only from `core/devices.py`. The new validator must import from `adapters/modbus/battery_adapter.py` and `adapters/modbus/inverter_adapter.py`, both of which import FROM `adapters/capabilities/__init__.py` (`from open_ems.adapters.capabilities import get_profile` — `battery_adapter.py:24`). **Use deferred imports inside the validator function body**, not at module top, to break the cycle. This is the same pattern Python uses for `typing.TYPE_CHECKING`-style forward references and is acceptable for one-shot startup-validation paths.

### Correlation-id enforcement: why at PolicyGuard, not at the adapter

Story 9.0 locked the adapter-side contract that `CommandResult.correlation_id` must equal `DeviceCommand.correlation_id` (clause 4 of the cross-adapter contract). The deferred finding from 8.2 noted PolicyGuard does not enforce this — a buggy adapter that returns a fresh UUID silently breaks audit correlation. AC6 adds enforcement at PolicyGuard (the single dispatch site, AR15) rather than relying on every adapter's `send_command` to self-police. This is consistent with PolicyGuard's role as the safety boundary: trust nothing the adapter returns; validate at the gate.

### Existing test patterns to follow (do not reinvent)

1. **`_active_constraints_provider()` fixture** at `tests/unit/engine/test_policy_guard.py:46` — the canonical pattern for constructing a hydrated provider in sync tests. Task 6's new `from_snapshot()` classmethod replaces this for new tests, but old tests keep their current fixture.
2. **`_adapter()` factory** at `tests/unit/engine/test_policy_guard.py:92` — returns a `MagicMock(spec=DeviceAdapter)` with `get_capabilities` and `send_command` AsyncMocks. Extend in Task 5 to accept the command for correlation_id stamping.
3. **`_observability_with_audit_spy()`** at `tests/unit/engine/test_policy_guard.py:115` — returns `(ObservabilityService, AsyncMock)` where the spy intercepts `audit()` calls. Every test in the parametrized matrix uses this; assert via `audit_spy.await_args.kwargs`.

### Out of scope (explicit non-goals)

- **Sweeping all 14 fixture sites** that bypass `hydrate()` with private-field writes (`tests/fixtures/active_constraints.py:749` and 14 PolicyGuard/ControlLoop fixtures). 9.0b deferred this as W1. 9.0c uses the new `from_snapshot()` helper for **new** tests only; old tests untouched.
- **Promoting `CAPABILITY_CHECK_TIMEOUT_SECONDS` to Settings.** Deferred 8.2 finding tagged this for Epic 9, but it is independent of the capability-failure contract. Keep as module constant; promote when deployment-time variability is required.
- **Per-firmware capability matching.** `DeviceCapabilityProfile.firmware_version` is currently accepted but ignored (registry returns the same profile for any firmware). This is an Epic 9.1 / future-story concern, NOT a 9.0c invariant.
- **`set_operating_mode` capability for BYD batteries.** Story 9.0 AC10 removed it from BYD profiles because the adapter does not implement the corresponding write. The capability is reserved in `WriteCapability` for future use. Do NOT re-add it in 9.0c.
- **Inverter write support.** Inverters are read-only in v1; `INVERTER_PROFILES.write_capabilities = frozenset()`. PolicyGuard's P4 (capability missing) is the gate; adapter's `send_command` raises `TypeError` as defense in depth (Story 9.0 AC3 / AC10). Both stay as-is.

### Source-of-truth ownership for new state introduced

| Datum | Owner |
|---|---|
| Five-path contract (P0–P4) + P5 | `PolicyGuard` module docstring at `policy_guard.py:1-15` (after Task 1) |
| Capability registry alignment | `validate_capability_registry_alignment()` in `adapters/capabilities/__init__.py` |
| Reason string for discharge-without-battery-state | `_check_safety_constraints` in `policy_guard.py` |
| Correlation-id check | `authorize_and_dispatch` in `policy_guard.py` (post-dispatch) |

### Project Structure Notes

- New file: `tests/unit/engine/test_policy_guard_capability_failure_matrix.py` — keeps the parametrized matrix isolated from the existing test file (which stays the home for non-matrix tests).
- New file (if not present): `tests/integration/web/test_app_startup.py` — for the AC5 integration test.
- Modified files: `policy_guard.py`, `services/active_constraints.py`, `adapters/capabilities/__init__.py`, `web/app.py`, `tests/unit/engine/test_policy_guard.py`, `tests/unit/adapters/test_capability_registry.py`.
- No new dependencies. No migration. No new persistent state.

### Orchestration Risk Analysis (A2-triggered — MANDATORY)

> A1 enforcement contract (Epic 8 retro, 2026-05-10): A2 triggers matched, so R1–R7 below are required and substantive.

**A2 triggers matched:**
- **T5 (multi-adapter coordination)** — the five-path contract applies across battery, inverter, EV charger, grid meter adapter classes; the startup cross-validation (AC5) operates on `_SUPPORTED_MODELS` from every Modbus adapter and on the fixed model strings from OCPP and DSMR adapters; a drift in any adapter aborts boot.
- **T6 (deployment / restart behavior)** — startup gate (AC5) fails-loud on capability-registry drift; this is a process-must-not-start condition with the same enforcement class as migration failure and constraints-hydrate failure; cold-start lifecycle is mutated.
- **T1 partial (lifecycle)** — PolicyGuard's capability gate gains a documented contract (five paths) plus a new rejection path (AC4 — `battery_state_unavailable_for_safety_check`) and a new post-dispatch enforcement path (AC6 — `adapter_correlation_id_mismatch`). The order of evaluation becomes a documented invariant.

#### R1 — Composition-risk analysis

| Domain | Specific risk introduced by convergence |
|---|---|
| **Lifecycle (P0–P5 evaluation order)** + **Multi-adapter contract (all 4 adapter classes)** | A new safety-constraint reason (AC4) must NOT fire before P0–P4. If AC4 were evaluated in `authorize_and_dispatch` before fail-safe (P0) or before capability lookup (P2), an unregistered adapter or fail-safe-mode dispatch would emit the wrong reason. Mitigation: AC4 fix lives in `_check_safety_constraints`, evaluated AFTER P0–P4 by the existing code path (`policy_guard.py:106-108`). Task 3 explicitly verifies ordering in tests. |
| **Startup gate (AC5)** + **Pre-existing startup sequence (migrations, hydrate)** | `validate_capability_registry_alignment()` failure must trigger the same cleanup as migration failure (close_database, log structured event, SystemExit(1)). 9.0b's D4→P10 patch moved `init_database` into the outer try/finally; AC5's new call must live inside the same try, otherwise the new failure path leaks the DB connection. Mitigation: Task 4 explicitly places the call inside the existing try block; AC9 Layer 2 review checks the close_database invocation on this path. |
| **Multi-adapter coordination (4 device classes)** + **Circular-import surface** | The registry validator imports from each Modbus adapter, which imports from the registry. Top-level imports would deadlock. Mitigation: deferred imports inside the validator function body (Task 4); AC5 unit tests assert the validator is callable from a cold import; AC9 Layer 1 review scans for import-order issues. |
| **Correlation-id enforcement (AC6)** + **Audit correlation contract** | The synthetic failure result MUST carry the command's correlation_id (not the adapter's bogus one), otherwise audit logs lose the original command's trace. Mitigation: AC6 explicitly mandates `correlation_id=command.correlation_id` on the synthetic result; Task 5 implements; AC2 audit-spy assertions verify. |
| **Defensive checks in P2 (AC3)** + **Cancellation propagation invariant** | Adding `isinstance(profile, ...)` and `profile.device_id == ...` checks inside the existing `try:` block could accidentally widen the `except Exception` catch to swallow `CancelledError`. Mitigation: Task 2 preserves `except Exception` (excludes `BaseException`); AC3 has an explicit `CancelledError` propagation test; Task 2 adds a code comment. |

Precedent: the Epic 8 review of Story 8.4 found five HIGH-severity findings caused exactly by ordering/composition issues like these. AC1's explicit ordering documentation + Task 3's ordering test + AC9 Layer 2 review are the three structural mitigations.

#### R2 — State-transition table

PolicyGuard does not own a state machine; it is a stateless authorization gate. However, two systems whose state machines are touched in this story DO have transitions:

**Lifespan startup state (`web/app.py`):**

| State | Transitions out | Trigger | Side-effects |
|---|---|---|---|
| `pre_init` | → `db_initialized` | `init_database()` succeeds | aiosqlite connection opened; logged `database_initialized` |
| `pre_init` | → `startup_failed` (terminal) | `init_database()` raises | log `startup_failed reason="migration_failed"`; close_database; SystemExit(1) |
| `db_initialized` | → `capability_validated` | `validate_capability_registry_alignment()` returns None | (no side-effect — pure check) |
| `db_initialized` | → `startup_failed` (terminal) | `validate_capability_registry_alignment()` raises `CapabilityRegistryDriftError` | log `startup_failed reason="capability_registry_drift"`; close_database; SystemExit(1) |
| `capability_validated` | → `constraints_hydrated` | `provider.hydrate()` succeeds | provider.is_hydrated = True |
| `capability_validated` | → `startup_failed` (terminal) | `provider.hydrate()` raises | log `startup_failed reason="constraints_hydrate_failed"`; close_database; SystemExit(1) |
| `constraints_hydrated` | → `serving` | PolicyGuard / ControlLoop constructed; control loop task started | normal operation |

**PolicyGuard rejection-path machine (per-command, not process-state):**

| Branch entered | Next state | Trigger | Side-effects |
|---|---|---|---|
| `entry` | P0 | `snapshot.operating_mode is fail_safe` | CONSTRAINT audit (`fail_safe_mode_active`); return rejected |
| `entry → after-P0` | P1 | `command.device_role not in self._adapters` | CONSTRAINT audit (`adapter_not_registered`); return rejected |
| `entry → after-P1` | P2 | `adapter.get_capabilities()` raises / times out / returns non-profile / device_id mismatch | CONSTRAINT audit (`capability_check_failed`); return rejected; log `capability_check_failed` warning |
| `entry → after-P2` | P3 | `_required_write_capability(command)` raises `TypeError` | CONSTRAINT audit (`unknown_command_type`); return rejected; log `unknown_command_type` warning |
| `entry → after-P3` | P4 | `required_capability not in profile.write_capabilities` | CONSTRAINT audit (`capability_missing: {cap}`); return rejected |
| `entry → after-P4` | safety checks | (proceeds) | evaluated in `_check_safety_constraints` |
| safety checks | reject-discharge-no-state | `isinstance(cmd, SetBatteryDischargeRateCommand) and not isinstance(snapshot.battery, BatteryState)` | CONSTRAINT audit (`battery_state_unavailable_for_safety_check`); return rejected |
| safety checks | (other 3 reasons) | unchanged from existing code | unchanged |
| safety checks | dispatch | all guards passed | `await adapter.send_command(command)` |
| dispatch | P5 (correlation mismatch) | `result.correlation_id != command.correlation_id` | CONSTRAINT audit (`adapter_correlation_id_mismatch`); return failed (status=failed, NOT rejected) |
| dispatch | timeout | `asyncio.TimeoutError` from `wait_for` | return `CommandResult(status=timeout, reason="command_timeout")` — no audit (existing behavior, AR16) |
| dispatch | failure | `adapter.send_command` raises (caught by AR16) | return `CommandResult(status=failed, reason=repr(exc))` — no audit (existing behavior) |
| dispatch | success | adapter returns success | return adapter's result verbatim |

#### R3 — Impossible-state analysis (with cold-start coverage)

**Forbidden state combinations and the structural invariant that prevents each:**

1. **Process starts with `_SUPPORTED_MODELS` member missing from capability registry.** Prevented by AC5: lifespan calls `validate_capability_registry_alignment()` before PolicyGuard construction; on drift, `SystemExit(1)`. Cannot reach `serving` state.
2. **PolicyGuard authorizes a command in fail-safe mode.** Prevented by P0: `snapshot.operating_mode is SystemOperatingMode.fail_safe` short-circuits the entire authorization flow before adapter lookup. Documented as the first check in AC1's ordering.
3. **PolicyGuard dispatches a command after a P0–P4 rejection.** Prevented by `return` statements: every rejection path returns from `authorize_and_dispatch` before reaching `await adapter.send_command(...)`. No fall-through.
4. **A safety-constraint rejection emits the wrong reason because P0–P4 were skipped.** Prevented by structural ordering: `_check_safety_constraints` is called only after P0–P4 pass (`policy_guard.py:106`). Cannot reach safety checks if a P0–P4 rejection already returned.
5. **Audit emission happens for a successful dispatch.** Prevented by `_reject()` being the only CONSTRAINT-audit emitter inside `authorize_and_dispatch`; success returns the adapter's result verbatim without invoking `_reject` (existing behavior preserved).
6. **CancelledError is converted to `capability_check_failed`.** Prevented by `except Exception` (not `except BaseException`) at policy_guard.py:84 — `CancelledError` derives from `BaseException` and propagates. AC3 has an explicit propagation test; Task 2 adds a comment documenting this invariant.
7. **Correlation_id mismatch produces a "success" CommandResult.** Prevented by AC6: the post-dispatch check runs before the success return; mismatch converts to synthetic failed result. Cannot return adapter's result if correlation_id differs.

**Cold-start / startup-grace coverage (mandatory):**

From process boot until the lifespan reaches `serving` state, the system MUST NOT serve commands and MUST fail-loud on any registry-alignment or hydration error. Specifically:

- **Before `init_database()` completes:** No HTTP routes are reachable; FastAPI lifespan blocks request acceptance. State: `pre_init`.
- **Between `init_database()` and `validate_capability_registry_alignment()`:** DB connection is open, no validation has run. State: `db_initialized`. If a request arrived here (it cannot — FastAPI blocks during lifespan), it would 503.
- **Between `validate_capability_registry_alignment()` and `provider.hydrate()`:** Registry verified; constraints not yet hydrated. State: `capability_validated`. Same FastAPI gating.
- **Between `provider.hydrate()` and PolicyGuard construction:** Constraints in memory; PolicyGuard not yet built. State: `constraints_hydrated`. Same FastAPI gating.
- **After PolicyGuard construction, before first ControlLoop tick:** PolicyGuard can authorize commands, but no command origin exists yet (IntentExecutor runs from inside the control loop). If an external caller invoked PolicyGuard directly here, the relevant invariants hold: `_active_constraints_provider.is_hydrated is True` (otherwise SystemExit(1) was raised); the snapshot's `operating_mode` is `degraded` (StateStore default before first publish — `policy_guard.py:73` evaluates `is fail_safe`, which is False for degraded; so a command would proceed past P0 — but the battery slot is None, so AC4's new check would fire for any discharge command, producing `battery_state_unavailable_for_safety_check`). This is the **normal cold-start window** and the system's behavior is well-defined.
- **First ControlLoop tick:** ControlLoop polls adapters → publishes a snapshot → ticks IntentExecutor → PolicyGuard authorizes the produced command. Normal operation begins HERE. Marker: `StateStore.get_snapshot().version > 0` (first publish increments version).

#### R4 — Cancellation ownership map

| Async operation | CancelledError owner | Cancellation audit owner | Cleanup owner | Reporting field semantics under cancel |
|---|---|---|---|---|
| `asyncio.wait_for(adapter.get_capabilities(), ...)` (P2) | Caller (control loop task); `CancelledError` propagates through PolicyGuard via `except Exception` (which excludes `BaseException`) | None — cancellation during capability lookup is not audited (it occurs only on process shutdown, where RetryPolicy / control loop emit their own cancellation audits per 8.3 / 8.4 contracts) | Adapter's `get_capabilities()` — it owns any internal resources; PolicyGuard holds none | n/a (no `attempts` field at the PolicyGuard layer) |
| `asyncio.wait_for(adapter.send_command(...), ...)` | Caller; propagates via the existing `except TimeoutError` / `except Exception` (TimeoutError handled specifically; CancelledError propagates) | RetryPolicy emits the cancellation audit (existing 8.3 contract); PolicyGuard does NOT re-emit | Adapter | n/a at PolicyGuard layer |
| `await self._observability.audit(...)` inside `_reject` | Caller; existing `except Exception: # noqa: BLE001` swallows audit-write failures (preserves the rejection return) but `BaseException`/`CancelledError` propagates | Audit failure is logged via `logger.error("constraint_audit_write_failed")` — cancellation during audit emission is propagated, not logged | ObservabilityService | n/a |
| `await self._observability.audit(...)` inside new AC6 correlation-mismatch path | Same as above — reuses `_reject()` (or new helper); same cancellation semantics | Same | Same | n/a |

This story does NOT introduce new top-level cancellation paths. All cancellation enters PolicyGuard via the caller (ControlLoop → RetryPolicy → PolicyGuard), and PolicyGuard's existing `except Exception` (deliberately excluding BaseException subclasses) is the structural mitigation. AC3 verifies it with an explicit `pytest.raises(CancelledError)` test.

#### R5 — Before-first-successful-cycle lifecycle review

From process start to first successful cycle:

1. **Lifespan begins** (`web/app.py:starlette lifespan context`). aiosqlite connection opens; Alembic migrations run.
2. **`init_database()` returns.** State: `db_initialized`.
3. **NEW (AC5):** `validate_capability_registry_alignment()` runs. Imports adapter `_SUPPORTED_MODELS` lazily; compares against `_ALL_PROFILES`. On miss: log `startup_failed reason="capability_registry_drift" missing_models=[...]`; close_database; SystemExit(1). On success: silent return. State: `capability_validated`.
4. **`ActiveConstraintsProvider.hydrate()` runs** (existing 9.0b path). Reads `ConfigRepo.get_active()`; seeds from Settings on empty DB; logs `constraints_hydrate_seeded_from_settings` (info) or just returns. State: `constraints_hydrated`.
5. **PolicyGuard, ControlLoop, IntentExecutor constructed.** No I/O.
6. **ControlLoop task spawned via `asyncio.create_task`.** First tick begins.
7. **First ControlLoop tick polls adapters.** Adapters return `DegradedDeviceState` or domain states. Tick publishes snapshot to StateStore (snapshot.version → 1).
8. **First IntentExecutor evaluation produces commands** (or holds — depends on snapshot completeness). If commands are produced, PolicyGuard authorizes:
   - P0: `operating_mode` is `degraded` (first-publish default), not `fail_safe` → passes.
   - P1: adapters map is non-empty → passes for known roles.
   - P2: `get_capabilities()` returns a valid profile (registry verified at step 3) → passes.
   - P3, P4, safety checks: evaluated per the contract.
9. **First successful dispatch returns from adapter.** P5 (correlation_id) check passes (adapters per 9.0 contract preserve correlation_id). Result returned to RetryPolicy → ControlLoop. **Marker for normal operation: `StateStore.get_snapshot().version > 0` AND at least one command has been dispatched successfully (no rejection).**

Temporarily-relaxed invariants during steps 1–6:
- HTTP routes return 503 (FastAPI lifespan-blocking — no behavior change from existing system).
- PolicyGuard is callable but has no commands incoming (IntentExecutor runs only from inside the control loop).

User-visible cold-start signals (NEW in this story):
- A capability-registry drift aborts boot with a structured log; deployment fails. CI / installer notices immediately.
- Discharge commands during step 6 (if any external caller invoked PolicyGuard directly, which v1 doesn't) would be rejected with `battery_state_unavailable_for_safety_check` rather than silently bypassing the SoC floor check.

#### R6 — Source-of-truth ownership per datum

| Datum | Single owner | Read path | Drift risk |
|---|---|---|---|
| The five-path failure contract (P0–P4) + P5 | `PolicyGuard` module docstring | Authoritative — referenced by Story 9.2 dev notes and all reviewers | None (single document) |
| `WriteCapability` enum | `core/devices.py:WriteCapability` | `_required_write_capability(command)` in `policy_guard.py`; profile `write_capabilities` in `capabilities/*.py` | None — enum is the canonical vocabulary |
| Per-model capability profile | `adapters/capabilities/<class>.py` (e.g. `BATTERY_PROFILES`) | `get_profile(device_id, model)` in `adapters/capabilities/__init__.py` | **Was a drift risk** between adapter `_SUPPORTED_MODELS` and registry; closed by AC5 startup validation |
| `_SUPPORTED_MODELS` per adapter | The adapter module itself (`battery_adapter.py`, `inverter_adapter.py`) | Adapter's `__init__` validates model membership; registry validator imports the dict at startup | **Closed by AC5** — startup gate fails if any member is not in `_ALL_PROFILES` |
| `correlation_id` for a dispatched command | `DeviceCommand.correlation_id` (auto-generated UUID4) | `CommandResult.correlation_id` (adapter sets) → AC6 verifies they match → audit emits via `_reject` or success path | **Was unenforced** at PolicyGuard; closed by AC6 |
| Safety-constraint rejection reasons | `_check_safety_constraints` in `policy_guard.py` | Inline rejection strings | None — strings are colocated with the check |
| New reason `battery_state_unavailable_for_safety_check` | `_check_safety_constraints` (Task 3) | Same | None — new reason lives alongside existing ones |
| Capability-registry alignment | `validate_capability_registry_alignment()` (Task 4) | Lifespan invocation only; not exposed at runtime | None — single function, single call site |

#### R7 — Deferred-findings triage

Scanned `_bmad-output/implementation-artifacts/deferred-work.md` for entries whose component or invariant overlaps this story.

| Finding (bracket ref) | Classification | Rationale |
|---|---|---|
| `[src/open_ems/engine/policy_guard.py:137-141]` — "Discharge command bypasses SoC floor check when battery state unavailable" | **must-resolve-in-this-story** | AC4 explicitly resolves this. 9.0b's R7 triage handed it off to 9.0c with the note "Story 9-0c is the right place to revisit". |
| `[capabilities]` (4-4) — "`_SUPPORTED_MODELS` and capability registry have no cross-validation" | **must-resolve-in-this-story** | AC5 explicitly resolves this. Story 9.0 review tagged it `partially-touched-by-this-story; broader registry-vs-supported-models check remains for Story 9-0c (P4 carve-out)`. |
| `[policy_guard.py:97-100]` — "`correlation_id` mismatch from adapter `CommandResult` passed through unchecked" | **must-resolve-in-this-story** | AC6 resolves this. Original deferred entry tagged it "adapter contract enforcement concern for Epic 9"; 9.0c is the right closing point. |
| `[capabilities]` (4-4) — "Template profiles with `device_id='__placeholder__'` in public `__all__`" | **safe-during-this-story** | 9.0c does not touch the `__all__` declaration; AC5 imports `_ALL_PROFILES` via the registry's internal name, not via `__all__`. No public-API change. |
| `[policy_guard.py:137-150]` — "Constraint ordering emits wrong audit reason when conservative mode + below-floor SoC" | **safe-during-this-story** | AC4 adds a new check BEFORE the existing SoC-floor check; the existing ordering between SoC-floor and conservative-mode checks is preserved. AC7 sweep asserts no regression. |
| `[policy_guard.py:143-145]` — "Peak limit check uses strict `>` — command at exactly `peak_limit_kw` passes" | **safe-during-this-story** | Comparison operator unchanged; AC4 + AC6 do not touch the peak-limit branch. |
| `[capabilities]` — "`model_copy` in `get_profile()` could propagate accidental `limitation_reason` from a misconfigured template" | **safe-during-this-story** | All current templates have clean `limitation_reason=None`; 9.0c does not add new templates. AC5 startup validator does not exercise template fields beyond presence. |
| `[capabilities]` — "`model: str` on `DeviceCapabilityProfile` allows empty/whitespace" | **acceptable-post-this-story** | Pre-existing schema concern; orthogonal to the failure-semantics contract. Promote to `NonEmptyStr` when capability registry is otherwise touched. |
| `[policy_guard.py:44-45]` — "Timeout constants not exposed via Settings" | **acceptable-post-this-story** | Independent of capability-failure semantics; AC3 uses monkeypatch for tests, which is the documented pattern. |
| `[policy_guard.py:67]` — "PolicyGuard re-fetches snapshot independently (stale-read window)" | **safe-during-this-story** | Snapshot read path unchanged by 9.0c; AC4's new check reads the same `snapshot` already in scope. |
| `[control_loop.py:_extract_grid_power]` — "Degraded grid meter returns 0.0 to PartialIntervalTracker" | **acceptable-post-this-story** | ControlLoop concern, not PolicyGuard. Out of scope for 9.0c. |
| `[adapters/capabilities]` UserRepo/SessionRepo write lock from 9.0b deferred | **acceptable-post-this-story** | Storage layer concern; 9.0c does not write any new repos. |
| `[tests/fixtures/active_constraints.py:749]` — fixture bypasses hydrate() contract | **safe-during-this-story** (partially addressed) | AC8 adds the public `from_snapshot()` helper for NEW tests; existing 14 fixture sites are NOT swept (deferred W1 remains). Forward-looking fix. |

### References

- [Source: `_bmad-output/implementation-artifacts/epic-8-retro-2026-05-10.md` §P4 — Capability-Profile Failure Semantics] — three-path contract origin
- [Source: `_bmad-output/implementation-artifacts/9-0-real-adapter-command-execution-contracts-and-implementations.md` AC10 + Risk #4] — profile↔implementation drift handoff
- [Source: `_bmad-output/implementation-artifacts/9-0b-db-backed-active-constraints-reload.md` R7 row "Discharge command bypasses SoC floor check"] — explicit handoff to 9.0c
- [Source: `_bmad-output/implementation-artifacts/deferred-work.md`] — full R7 source list
- [Source: `src/open_ems/engine/policy_guard.py`] — module being modified
- [Source: `src/open_ems/adapters/capabilities/__init__.py`] — registry being extended
- [Source: `src/open_ems/services/active_constraints.py`] — `from_snapshot()` classmethod added
- [Source: `_bmad-output/planning-artifacts/architecture.md` §BAD-3] — constraint validation flow context (for cross-reference, not modification)

## Dev Agent Record

### Agent Model Used

Claude Opus 4.7 (`claude-opus-4-7`) via Claude Code CLI; bmad-dev-story workflow.

### Debug Log References

Baseline 2026-05-10: `uv run python -m pytest tests/ --no-cov -q` → 1020 passed, 43 warnings, 62.37s.
Post-implementation: `uv run python -m pytest tests/ --no-cov -q` → 1062 passed, 43 warnings, 52.37s (+42 new tests).

### Completion Notes List

**Implementation summary.** Story 9.0c locks the PolicyGuard capability-failure contract by (a) documenting the six-path vocabulary (P0–P5) in the module docstring as the canonical reference, (b) hardening P2 with defensive isinstance + device_id checks inside the existing `try:` block (no widened catch — `CancelledError` still propagates), (c) adding a new safety-constraint reason (`battery_state_unavailable_for_safety_check`) that fires BEFORE the SoC-floor branch for discharge commands without a published BatteryState, (d) introducing a fail-loud startup gate (`validate_capability_registry_alignment()`) that aborts boot with `SystemExit(1)` on `_SUPPORTED_MODELS` × registry drift, (e) enforcing the cross-adapter correlation_id round-trip clause at PolicyGuard (P5) by returning a synthetic `CommandStatus.failed` result that preserves the command's original correlation_id, and (f) adding a public `ActiveConstraintsProvider.from_snapshot()` test-only constructor with a `_RaisingRepo` sentinel so any future hot-path-DB-read regression in the new tests would surface immediately.

**Design choice — `_reject()` status parameter.** AC6 needed P5 to return `CommandStatus.failed` while reusing the existing audit-emission path. Chose to add an optional `status: CommandStatus = CommandStatus.rejected` keyword parameter to `_reject()` over adding a parallel helper — preserves a single audit-emission code path and a single rejection-construction call site. P5 invocation passes `status=CommandStatus.failed`; every other rejection retains the default.

**Design choice — `from_snapshot()` with `_RaisingRepo` sentinel.** Per AC8, new tests must catch a future hot-path-DB-read regression. The classmethod accepts a typed `ActiveConstraints` snapshot, hydrates the provider, and substitutes a `_RaisingRepo` sentinel whose `__getattr__` returns an async function that raises `AssertionError("PolicyGuard hot path must not touch ConfigRepo")` on any await. Tests using the classmethod automatically inherit this guard. Existing 14 fixture sites (deferred W1 from 9.0b) are intentionally NOT swept — scope unchanged per the story's explicit out-of-scope note.

**Design choice — deferred imports for the registry validator.** `validate_capability_registry_alignment()` imports from `battery_adapter` and `inverter_adapter`, both of which import from this package. Top-level imports would create a cycle; the validator imports inside its function body (called once at startup, no perf concern). Comment in the source documents this explicitly.

**AC7 sweep — audit table.** Both substring rejection-reason assertions in `tests/unit/engine/test_policy_guard.py` converted to exact-match. Every discharge test publishes a valid `BatteryState`, so no new AC4 reason assertions were required there.

| Test (in `tests/unit/engine/test_policy_guard.py`) | Previous assertion | New exact-match assertion |
|---|---|---|
| `test_capability_missing_blocks_send_command` (line ~237) | `"capability_missing" in result.reason` | `result.reason == "capability_missing: set_discharge_rate"` |
| `test_reject_audit_includes_correlation_id_in_detail` (line ~576) | `"capability_missing" in detail["rejection_reason"]` | `detail["rejection_reason"] == "capability_missing: set_discharge_rate"` |

Note: `test_adapter_exception_is_wrapped_as_failed_command_result` retains `"modbus_explode" in result.reason` — that assertion targets `repr(exc)` (i.e. `"RuntimeError('modbus_explode')"`), which is NOT a rejection reason from the contract table. It is the exception-wrap reason mandated by AR16. Outside the AC7 sweep scope.

**Discharge-test battery-state audit (AC7 part 2).** Every test in `test_policy_guard.py` that exercises a discharge command verified to publish a valid `BatteryState`:

* `test_constraint_violation_rejected_with_audit_event` — publishes at SoC 20% (at floor).
* `test_capability_missing_blocks_send_command` — publishes at SoC 80%.
* `test_command_result_applied_false_is_not_treated_as_success` — publishes at SoC 80%.
* `test_adapter_exception_is_wrapped_as_failed_command_result` — publishes at SoC 80%.
* `test_adapter_timeout_is_wrapped_as_timeout_command_result` — publishes at SoC 80%.
* `test_adapter_not_registered_is_rejected` — publishes at SoC 80% (rejected at P1 anyway).
* `test_capability_check_failure_is_rejected` — publishes at SoC 80%.
* `test_conservative_mode_blocks_discharge` — publishes at SoC 80%.
* `test_successful_dispatch_returns_adapter_result_no_audit` — publishes at SoC 80%.
* `test_command_dispatch_timeout_is_caught_via_wait_for` — publishes at SoC 80%.
* `test_reject_audit_includes_correlation_id_in_detail` — publishes at SoC 80%.

No silent pass-through of AC4 rejection ahead of later assertions.

**Test counts added (42 new).** matrix file: 34 tests (18 P0–P4 cells + 1 P3 + 6 P2 exception classes + 1 P2 timeout + 1 CancelledError + 1 None-return + 1 device_id-mismatch + 1 AC8 sanity + 2 AC4 main + 1 AC4 charge-passthrough + 1 AC4 SoC-floor sanity + 1 AC6 mismatch + 1 AC6 happy-path); capability-registry: 5 new tests; app-startup integration: 3 new tests. Total +42 → 1062 passing.

### File List

* `src/open_ems/engine/policy_guard.py` — modified (Tasks 1–3, 5: new docstring, P2 defensive checks, AC4 safety reason, AC6 P5 enforcement, `_reject()` status parameter).
* `src/open_ems/adapters/capabilities/__init__.py` — modified (Task 4: `CapabilityRegistryDriftError`, `validate_capability_registry_alignment()`, `_FIXED_ADAPTER_MODELS`, `__all__` updated).
* `src/open_ems/web/app.py` — modified (Task 4: lifespan invocation of registry validator with structured logging + SystemExit(1) on drift).
* `src/open_ems/services/active_constraints.py` — modified (Task 6: `_RaisingRepo` sentinel, `from_snapshot()` classmethod).
* `tests/unit/engine/test_policy_guard.py` — modified (Task 5: `_adapter()` accepts `command=`; AC6 fixups at three call sites; AC7 sweep at two assertions).
* `tests/unit/engine/test_policy_guard_capability_failure_matrix.py` — **new** (Tasks 7–9: AC2 + AC3 + AC4 + AC6 + AC8 tests).
* `tests/unit/adapters/test_capability_registry.py` — modified (Task 10: AC5 unit tests for the validator).
* `tests/integration/web/test_app_startup.py` — **new** (Task 10: AC5 lifespan integration tests).
* `_bmad-output/implementation-artifacts/9-0c-capability-profile-failure-semantics.md` — modified (Tasks/Subtasks checkboxes, Dev Agent Record, File List, Status: review).
* `_bmad-output/implementation-artifacts/sprint-status.yaml` — modified (story status: `ready-for-dev` → `in-progress` → `review`; `last_updated` notes).

### Change Log

| Date | Change |
|---|---|
| 2026-05-10 | Story 9.0c development complete; status: in-progress → review. All 10 ACs implemented; 1020 → 1062 passing tests (+42); ruff + mypy clean. Awaiting adversarial 3-layer review per AC9. |
| 2026-05-11 | 3-layer adversarial review complete (AC9). 29 raw findings → 17 actionable: 3 HIGH + 6 MEDIUM + 3 LOW + 5 deferred + 9 dismissed. All HIGH and MEDIUM resolved via 12 patches. Status: review → done. Tests: 1062 → 1071 passing (+9 net new from review patches); ruff + mypy + format clean. Patch surface: `CommandStatus.correlation_broken` added (D4) with RetryPolicy non-retry branch; `_FIXED_MODEL` constants added to OCPP/DSMR adapters + maintenance-gate test (D1); module docstring expanded with model-consistency invariant (D6), safety-constraint ordering (D5), `_reject()` dual-semantics (D3); cold-start regression test added (D2); `close_database` assertion added to AC5 integration test (P1); `_RaisingRepo.__getattr__` tightened to raise on attribute access (P2); `error=repr(exc)` added to P2 log (P3); P2 timeout test sleep bounded (P4); P4 EV-cell collision documented (P5); P2 non-profile test parametrized (P6). |

---

## Review Findings

*Adversarial 3-layer review completed 2026-05-10 via `/bmad-code-review` (Blind Hunter + Edge Case Hunter + Acceptance Auditor). 29 raw findings → 17 actionable after dedupe and merge: **3 HIGH + 6 MEDIUM + 3 LOW + 5 deferred + 9 dismissed**. Per AC9, `Status: review → done` is BLOCKED on any unresolved HIGH.*

### Decision-needed (6)

- [x] [Review][Decision] **HIGH — `_FIXED_ADAPTER_MODELS` reintroduces the drift it's meant to prevent** [src/open_ems/adapters/capabilities/__init__.py:63] — The AC5 validator hand-maintains `_FIXED_ADAPTER_MODELS = ("ocpp_1_6", "dsmr_p1")`. A future fixed-model adapter (e.g. a new protocol shim) added without updating this tuple slips past the gate. Options: (a) registry-side discovery (adapters self-register their fixed model strings), (b) maintenance test that asserts every adapter module declaring a fixed-model constant has a corresponding entry, (c) accept and document with a "must update this tuple" comment + code-review checklist item.
- [x] [Review][Decision] **HIGH — Cold-start vocabulary not locked by regression test** [src/open_ems/core/state_store.py:44 + src/open_ems/engine/policy_guard.py:285-288] — `StateStore` defaults `operating_mode=degraded`, not `fail_safe`. In the boot window before the first ControlLoop tick publishes, any discharge command hits AC4's `battery_state_unavailable_for_safety_check` rather than P0 fail-safe. The spec R3 (lines 441) explicitly accepts this as well-defined cold-start behavior — but there is no regression test locking the vocabulary. A future change to the StateStore default could silently shift the cold-start reason. Options: (a) add a regression test asserting "discharge during cold-start → AC4 reason"; (b) change StateStore default to `fail_safe` until the first publish (architectural change); (c) accept the gap.
- [x] [Review][Decision] **MEDIUM — P5 emits `CONSTRAINT` audit for post-dispatch integrity failure** [src/open_ems/engine/policy_guard.py:267-271] — P5 (correlation-mismatch) reuses `_reject()`, which emits `event_type="CONSTRAINT"`. Semantically this is a post-dispatch transport/integrity failure, not a pre-dispatch constraint rejection. Operators querying CONSTRAINT events to see "what was blocked before reaching hardware" will now see rows for commands that may have been sent. Options: (a) keep CONSTRAINT (single audit code path; spec AC6 doesn't prescribe event_type); (b) introduce a new audit event_type (`DISPATCH_INTEGRITY` or similar); (c) keep CONSTRAINT but document the dual semantics in `_reject` docstring.
- [x] [Review][Decision] **MEDIUM — P5 synthetic `applied=False, status=failed` may cause retry of non-idempotent commands** [src/open_ems/engine/policy_guard.py:255-271] — RetryPolicy sees `status=failed`. If the adapter actually applied the command but returned a bogus correlation_id, RetryPolicy may re-dispatch — fatal for non-idempotent commands like `StopEVChargingCommand`. Options: (a) introduce `CommandStatus.correlation_broken` so RetryPolicy can branch; (b) document at AC6 that correlation-mismatch is unknown-outcome and RetryPolicy must inspect `reason == "adapter_correlation_id_mismatch"` to suppress retry for non-idempotent commands; (c) accept the risk (PolicyGuard preserves audit; downstream behavior unaltered).
- [x] [Review][Decision] **MEDIUM — AC4 vs conservative-mode ordering not documented** [src/open_ems/engine/policy_guard.py:285-303] — When `operating_mode=conservative + battery=None + discharge_command`, AC4 wins (`battery_state_unavailable_for_safety_check`) and the conservative-mode branch is unreachable. No parametrized test covers this combination. Options: (a) document the ordering as an explicit invariant in the docstring + add a test cell; (b) invert the order (conservative first, then AC4) if conservative-mode is the more actionable operator signal; (c) accept the current order silently.
- [x] [Review][Decision] **MEDIUM — P2 device_id check is one-sided: model mismatch not validated** [src/open_ems/engine/policy_guard.py:179-188] — AC3 added `isinstance(profile, DeviceCapabilityProfile)` + `profile.device_id == command.device_id` defensive checks. A buggy adapter that calls `get_profile(self._device_id, "wrong_model")` returns a profile with the correct `device_id` but the wrong model's `write_capabilities`. Options: (a) also check `profile.model == adapter._model` (requires exposing `adapter.model` as a public property — currently `_model` is private); (b) document model consistency as an adapter-side invariant only (the registry's `model_copy` already preserves model in the returned profile); (c) accept the gap.

### Patch (6)

- [x] [Review][Patch] **HIGH — Integration test does not assert `close_database()` invoked on drift path** [tests/integration/web/test_app_startup.py:21-46] — The lifespan structurally calls `close_database()` from the outer `finally` (web/app.py:217-end), so the close path is correct. But the new integration test only asserts `SystemExit.code == 1` and log fields — it does NOT lock the close invariant. A future refactor that moves the validator before `init_database()` (a plausible "shift-left" change) would silently regress the close path without test failure.
- [x] [Review][Patch] **MEDIUM — `_RaisingRepo.__getattr__` returns coroutine for non-method attribute access** [src/open_ems/services/active_constraints.py:55-61] — Any attribute read (`repo.connection`, `repo.is_connected`, truthiness checks) returns an async closure rather than raising. The assertion only fires if the result is *awaited*. A synchronous-attribute regression like `if repo.some_state: ...` evaluates a function as truthy and silently proceeds.
- [x] [Review][Patch] **MEDIUM — P2 / dispatch `except Exception` logs do not include exception class or repr** [src/open_ems/engine/policy_guard.py:236-263, 244-249, 270-276] — Operators see `capability_check_failed` or `command_dispatch_failed` in logs with no indication of the underlying class (TypeError? ConnectionError? TimeoutError?). Debugging adapter regressions requires reproducing the failure. Add `error=repr(exc)` to the log call (the dispatch path already does this at line 244; the P2 path does not).
- [x] [Review][Patch] **LOW — P2 timeout test sleeps 5.0s if monkeypatch silently fails** [tests/unit/engine/test_policy_guard_capability_failure_matrix.py:1278-1297] — If `policy_guard.py` ever inlines or renames `CAPABILITY_CHECK_TIMEOUT_SECONDS`, the monkeypatch has no effect, the test hangs 5s, CI gets slow but green. Bound the failure: `asyncio.sleep(0.5)` plus a `pytest.mark.timeout(2)`.
- [x] [Review][Patch] **LOW — P4 test cells for `SetEVChargingRateCommand` and `StopEVChargingCommand` collide on identical reason** [tests/unit/engine/test_policy_guard_capability_failure_matrix.py:168-193, 395-432] — Both assert `reason == "capability_missing: set_ev_charge_current"`. A defect that only affects `StopEVChargingCommand` capability lookup is invisible. Add a comment acknowledging the deliberate collision, or vary the assertion to distinguish the branches.
- [x] [Review][Patch] **LOW — P2 `returns None` test should parametrize over `dict`, `object`, etc.** [tests/unit/engine/test_policy_guard_capability_failure_matrix.py:550-563] — Currently only tests `return_value=None`. The `isinstance` gate should be locked against future "structural duck-typing" regressions. Parametrize over `[None, {}, "not-a-profile", object()]`.

### Defer (5)

- [x] [Review][Defer] **LOW — P4 reason format couples audit vocabulary to `WriteCapability` enum string values** [src/open_ems/engine/policy_guard.py:209-210] — `f"capability_missing: {required_capability.value}"`. Rename of `WriteCapability.set_discharge_rate` silently changes runtime reason. Pre-existing pattern — spec AC1 explicitly documents this exact format. Deferred to a future story that centralizes audit reason constants.
- [x] [Review][Defer] **LOW — No regression test catches future `except BaseException` widening on dispatch path** [src/open_ems/engine/policy_guard.py:246, 270] — P2 has a `CancelledError` propagation test; the dispatch path does not. Linter rule (`BLE001`) is the structural guard. Defer until a `pytest.raises(CancelledError)`-style test for dispatch is added in a hardening sweep.
- [x] [Review][Defer] **LOW — `from_snapshot()` uses `Settings(_env_file=None)` — silently picks up process env** [src/open_ems/services/active_constraints.py:163 + tests/unit/engine/test_policy_guard_capability_failure_matrix.py:838] — A developer with `OPENEMS_PEAK_LIMIT_KW=999` in shell silently shifts test defaults. Test-internal; no observed bug. Defer until env-isolation becomes a project-wide concern.
- [x] [Review][Defer] **LOW — `_FIXED_ADAPTER_MODELS` drift-test isolation could break if other tests mutate `_ALL_PROFILES` in-place** [tests/unit/adapters/test_capability_registry.py:258-270] — Speculative; no current bleed. Defer until a session-scoped autouse fixture is added as cheap insurance.
- [x] [Review][Defer] **MEDIUM — AC10 quality gates not independently verified by reviewer** [_bmad-output/implementation-artifacts/9-0c-capability-profile-failure-semantics.md:285-286] — Story claims 1062 passed + ruff/mypy clean. The Acceptance Auditor cannot verify from the diff alone. Deferred to the harness — re-run `uv run python -m pytest tests/ --no-cov -q && uv run python -m mypy src/ && uv run python -m ruff check . && uv run python -m ruff format --check .` before the `review → done` transition.

### Dismissed (9 — noise / false positive / spec letter exceeded)

| # | Title | Reason for dismissal |
|---|---|---|
| 1 | `from_snapshot()` writes to private `_current` (B3) | The classmethod is the canonical owner of `_current`; intra-class write is not the "private-field-write" pattern AC8 targets. |
| 2 | `raise SystemExit(1) from None` swallows context (B5) | Operator-facing exit; structured `logger.error` captures full context. `from None` is intentional. |
| 3 | `_RaisingRepo` sentinel never actually fires in matrix tests (E6/B8) | Defensive guard works on definition (fires on regression). Subsumed by the B4 `__getattr__` patch. |
| 4 | `_UnknownTestCommand` uses `# type: ignore[arg-type]` (B9) | The test-only subclass is *the* mechanism for exercising P3; the ignore is correct usage. |
| 5 | P1 test missing `adapter.get_capabilities.assert_not_awaited()` (A1) | Empty `adapters={}` map makes the assertion structurally impossible (no adapter object exists). |
| 6 | AC4 charge-passthrough uses `adapter.send_command = AsyncMock(...)` directly (A2) | Functionally correct; stylistic deviation from preferred pattern. |
| 7 | AC8 sentinel uses class-with-`__getattr__` instead of `MagicMock(spec=ConfigRepo)` (A3) | Implementation is *stronger* than spec letter (catches all methods, not just `get_active`). Intent exceeded. |
| 8 | `from_snapshot()` does not explicitly set `is_hydrated = True` (A4) | `is_hydrated` is a derived `@property` (`return self._current is not None`, services/active_constraints.py:71-73). Spec letter prescribed an implementation detail that wasn't needed. |
| 9 | `test_from_snapshot_raising_repo_is_unreachable_on_hot_path` is vacuous (B8) | Same family as #3 — defensive sentinel test acknowledges its passive nature; matrix has real regression coverage via P0–P5 paths. |

### Source layer breakdown

- **Blind Hunter** — 14 raw findings (4 merged or dismissed below severity)
- **Edge Case Hunter** — 10 raw findings (2 merged into Blind Hunter findings)
- **Acceptance Auditor** — 5 raw findings (4 dismissed as spec-letter-exceeded; 1 deferred to harness)
- **Total raw: 29** → 17 actionable after dedupe and merge.
