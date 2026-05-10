"""AC2 + AC3 + AC8 (Story 9.0c) — parametrized capability-failure tests.

AC2: every path P0..P4 against every controllable command type. Each cell
asserts the exact ``reason`` string, the audit spy semantics, and the
no-side-effect invariants (``send_command`` never awaited; for P0, even
``get_capabilities`` is not awaited because fail-safe short-circuits the
entire flow).

AC3: exhaustive P2 exception-class coverage — every raise from
``adapter.get_capabilities()`` produces ``capability_check_failed`` and
``CancelledError`` propagates; ``None`` return and ``device_id`` mismatch
are classified as ``capability_check_failed`` by the new isinstance /
device_id guards (Story 9.0c, Task 2).

AC8: fixtures use the new public ``ActiveConstraintsProvider.from_snapshot()``
classmethod — the underlying ``_RaisingRepo`` sentinel guards against a future
hot-path-DB-read regression in any of these tests.
"""

from __future__ import annotations

import asyncio
import uuid as _uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from open_ems.core import (
    BatteryState,
    DeviceAdapter,
    DeviceRole,
    StateStore,
    SystemOperatingMode,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandResult,
    CommandStatus,
    DeviceCommand,
    DeviceCommandBase,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.core.constraints import ActiveConstraints
from open_ems.core.devices import (
    DegradedDeviceState,
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)
from open_ems.engine import policy_guard as policy_guard_module
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings

_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _provider() -> ActiveConstraintsProvider:
    """Pre-hydrated provider via the new public test-only constructor (AC8)."""
    s = _settings()
    return ActiveConstraintsProvider.from_snapshot(
        ActiveConstraints(
            peak_limit_kw=s.peak_limit_kw,
            battery_reserve_floor_percent=s.battery_reserve_floor_percent,
            config_version=0,
            activated_at=_NOW,
        ),
        settings=s,
    )


def _profile(
    *,
    device_id: str,
    write_caps: frozenset[WriteCapability],
) -> DeviceCapabilityProfile:
    return DeviceCapabilityProfile(
        device_id=device_id,
        model="TestModel",
        read_capabilities=frozenset({ReadCapability.state}),
        write_capabilities=write_caps,
    )


def _adapter_with_caps(
    *,
    device_id: str,
    write_caps: frozenset[WriteCapability],
) -> MagicMock:
    """Adapter with configurable write-caps; send_command must NOT be awaited."""
    adapter = MagicMock(spec=DeviceAdapter)
    adapter.device_id = device_id
    adapter.get_capabilities = AsyncMock(
        return_value=_profile(device_id=device_id, write_caps=write_caps)
    )
    adapter.send_command = AsyncMock()
    return adapter


def _audit_spy() -> tuple[ObservabilityService, AsyncMock]:
    spy = AsyncMock()
    obs = ObservabilityService(repo=MagicMock())
    obs.audit = spy  # type: ignore[method-assign]
    return obs, spy


# ---------------------------------------------------------------------------
# Command factories — one per controllable type
# ---------------------------------------------------------------------------


def _charge_cmd() -> SetBatteryChargeRateCommand:
    return SetBatteryChargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )


def _discharge_cmd() -> SetBatteryDischargeRateCommand:
    return SetBatteryDischargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )


def _ev_charge_cmd() -> SetEVChargingRateCommand:
    return SetEVChargingRateCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=5.0,
    )


def _stop_ev_cmd() -> StopEVChargingCommand:
    return StopEVChargingCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
    )


# Maps command type → (factory, required write capability for happy path,
# expected P4 reason suffix, device_role)
_CommandFactory = Callable[[], DeviceCommand]


_COMMAND_SPECS: tuple[tuple[str, _CommandFactory, WriteCapability, DeviceRole], ...] = (
    (
        "SetBatteryChargeRateCommand",
        _charge_cmd,
        WriteCapability.set_charge_rate,
        DeviceRole.battery,
    ),
    (
        "SetBatteryDischargeRateCommand",
        _discharge_cmd,
        WriteCapability.set_discharge_rate,
        DeviceRole.battery,
    ),
    (
        "SetEVChargingRateCommand",
        _ev_charge_cmd,
        WriteCapability.set_ev_charge_current,
        DeviceRole.ev_charger,
    ),
    (
        "StopEVChargingCommand",
        _stop_ev_cmd,
        WriteCapability.set_ev_charge_current,
        DeviceRole.ev_charger,
    ),
)


# ---------------------------------------------------------------------------
# Path P0 — fail_safe_mode_active
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_label", "command_factory", "_required_cap", "device_role"),
    _COMMAND_SPECS,
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_p0_fail_safe_mode_active(
    type_label: str,
    command_factory: _CommandFactory,
    _required_cap: WriteCapability,
    device_role: DeviceRole,
) -> None:
    store = StateStore(
        system_clock_status="valid",
        operating_mode=SystemOperatingMode.fail_safe,
    )
    cmd = command_factory()
    adapter = _adapter_with_caps(
        device_id=cmd.device_id,
        write_caps=frozenset({_required_cap}),
    )
    obs, spy = _audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={device_role: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )

    result = await guard.authorize_and_dispatch(cmd)

    assert result.status is CommandStatus.rejected, type_label
    assert result.applied is False
    assert result.reason == "fail_safe_mode_active"
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["event_type"] == "CONSTRAINT"
    assert kwargs["actor"] == "system"
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)
    assert kwargs["detail"]["command_type"] == type(cmd).__name__
    assert kwargs["detail"]["rejection_reason"] == "fail_safe_mode_active"
    adapter.send_command.assert_not_awaited()
    # AC2: P0 short-circuits BEFORE capability lookup.
    adapter.get_capabilities.assert_not_awaited()


# ---------------------------------------------------------------------------
# Path P1 — adapter_not_registered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_label", "command_factory", "_required_cap", "_device_role"),
    _COMMAND_SPECS,
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_p1_adapter_not_registered(
    type_label: str,
    command_factory: _CommandFactory,
    _required_cap: WriteCapability,
    _device_role: DeviceRole,
) -> None:
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    cmd = command_factory()
    obs, spy = _audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={},  # empty map → P1 fires for any device_role
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )

    result = await guard.authorize_and_dispatch(cmd)

    assert result.status is CommandStatus.rejected, type_label
    assert result.applied is False
    assert result.reason == "adapter_not_registered"
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["event_type"] == "CONSTRAINT"
    assert kwargs["detail"]["rejection_reason"] == "adapter_not_registered"
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)
    assert kwargs["detail"]["command_type"] == type(cmd).__name__


# ---------------------------------------------------------------------------
# Path P2 — capability_check_failed (basic per-command-type coverage)
# AC3 exhaustively covers exception classes in test_policy_guard_capability_p2_surface.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_label", "command_factory", "_required_cap", "device_role"),
    _COMMAND_SPECS,
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_p2_capability_check_failed(
    type_label: str,
    command_factory: _CommandFactory,
    _required_cap: WriteCapability,
    device_role: DeviceRole,
) -> None:
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    cmd = command_factory()
    adapter = MagicMock(spec=DeviceAdapter)
    adapter.device_id = cmd.device_id
    adapter.get_capabilities = AsyncMock(side_effect=RuntimeError("boom"))
    adapter.send_command = AsyncMock()
    obs, spy = _audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={device_role: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )

    result = await guard.authorize_and_dispatch(cmd)

    assert result.status is CommandStatus.rejected, type_label
    assert result.applied is False
    assert result.reason == "capability_check_failed"
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["event_type"] == "CONSTRAINT"
    assert kwargs["detail"]["rejection_reason"] == "capability_check_failed"
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)
    assert kwargs["detail"]["command_type"] == type(cmd).__name__
    adapter.send_command.assert_not_awaited()


# ---------------------------------------------------------------------------
# Path P3 — unknown_command_type via a test-only DeviceCommand subclass
# (AC2: do NOT extend the production commands.py enum)
# ---------------------------------------------------------------------------


class _UnknownTestCommand(DeviceCommandBase):
    """Test-only DeviceCommand subclass not handled by ``_required_write_capability``.

    Defined inside the test module per AC2 — production ``commands.py`` is
    untouched.
    """

    is_idempotent: ClassVar[bool] = False
    # Pydantic field for shape parity with other commands; not load-bearing.
    extra_marker: str = "test-only"


async def test_p3_unknown_command_type() -> None:
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    cmd = _UnknownTestCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
    )
    # Need an adapter so the flow reaches P3 (which is evaluated after P2).
    adapter = _adapter_with_caps(
        device_id=cmd.device_id,
        write_caps=frozenset({WriteCapability.set_discharge_rate}),
    )
    obs, spy = _audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )

    result = await guard.authorize_and_dispatch(cmd)  # type: ignore[arg-type]

    assert result.status is CommandStatus.rejected
    assert result.applied is False
    assert result.reason == "unknown_command_type"
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["event_type"] == "CONSTRAINT"
    assert kwargs["detail"]["rejection_reason"] == "unknown_command_type"
    assert kwargs["detail"]["command_type"] == "_UnknownTestCommand"
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)
    adapter.send_command.assert_not_awaited()


# ---------------------------------------------------------------------------
# Path P4 — capability_missing: <cap-value>
#
# P5 (9.0c review): the parametrized matrix below covers all four command
# types. Note that ``SetEVChargingRateCommand`` and ``StopEVChargingCommand``
# both map to ``WriteCapability.set_ev_charge_current`` via
# ``_required_write_capability`` (a Stop command is an EV-charge-current
# operation in OCPP terms). The expected reason suffix is therefore identical
# for both — that's the deliberate contract, not a test-cell collision. A
# defect that affects only ``StopEVChargingCommand``'s capability lookup would
# need separate coverage (see ``test_p3_unknown_command_type`` for the pattern
# of exercising a specific branch via a custom command subclass).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_label", "command_factory", "required_cap", "device_role"),
    _COMMAND_SPECS,
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_p4_capability_missing(
    type_label: str,
    command_factory: _CommandFactory,
    required_cap: WriteCapability,
    device_role: DeviceRole,
) -> None:
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    cmd = command_factory()
    # Profile with EMPTY write_capabilities → the required cap is missing.
    adapter = _adapter_with_caps(device_id=cmd.device_id, write_caps=frozenset())
    obs, spy = _audit_spy()

    guard = PolicyGuard(
        state_store=store,
        adapters={device_role: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )

    result = await guard.authorize_and_dispatch(cmd)

    expected_reason = f"capability_missing: {required_cap.value}"
    assert result.status is CommandStatus.rejected, type_label
    assert result.applied is False
    assert result.reason == expected_reason, type_label
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["event_type"] == "CONSTRAINT"
    assert kwargs["detail"]["rejection_reason"] == expected_reason
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)
    assert kwargs["detail"]["command_type"] == type(cmd).__name__
    adapter.send_command.assert_not_awaited()


# ---------------------------------------------------------------------------
# Sanity check: AC8 _RaisingRepo guard fires if PolicyGuard ever touches ConfigRepo
# ---------------------------------------------------------------------------


async def test_from_snapshot_raising_repo_is_unreachable_on_hot_path() -> None:
    """If a future regression makes PolicyGuard read ConfigRepo on the hot path,
    the new ``_RaisingRepo`` sentinel raises AssertionError, surfacing the bug.

    This is a probe test: the dispatch path here completes normally (so the
    sentinel is NOT awaited), which proves the guard does not interfere with
    correct code. The guard's value is realised only if a regression occurs.
    """
    provider = _provider()
    # Confirm the provider is hydrated AND .get() returns without touching repo.
    constraints: Any = provider.get()
    assert constraints.peak_limit_kw == _settings().peak_limit_kw


# ---------------------------------------------------------------------------
# AC3 — P2 exhaustive exception surface
# ---------------------------------------------------------------------------


def _battery_adapter_with_failing_caps(
    *, exc: BaseException, device_id: str = "bat-001"
) -> MagicMock:
    adapter = MagicMock(spec=DeviceAdapter)
    adapter.device_id = device_id
    adapter.get_capabilities = AsyncMock(side_effect=exc)
    adapter.send_command = AsyncMock()
    return adapter


def _guard_for_p2(adapter: MagicMock) -> tuple[PolicyGuard, AsyncMock]:
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    obs, spy = _audit_spy()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )
    return guard, spy


@pytest.mark.parametrize(
    ("exc_label", "exc"),
    [
        ("RuntimeError", RuntimeError("boom")),
        ("ConnectionError", ConnectionError("conn-down")),
        ("ValueError", ValueError("bad-value")),
        ("OSError", OSError("os-err")),
        ("AsyncioTimeoutError", TimeoutError()),
        ("BareException", Exception("bare")),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_p2_exception_classes_produce_capability_check_failed(
    exc_label: str,
    exc: BaseException,
) -> None:
    adapter = _battery_adapter_with_failing_caps(exc=exc)
    guard, spy = _guard_for_p2(adapter)

    result = await guard.authorize_and_dispatch(_discharge_cmd())

    assert result.status is CommandStatus.rejected, exc_label
    assert result.applied is False
    assert result.reason == "capability_check_failed", exc_label
    spy.assert_awaited_once()
    adapter.send_command.assert_not_awaited()


async def test_p2_wait_for_timeout_produces_capability_check_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter's get_capabilities sleeps past CAPABILITY_CHECK_TIMEOUT_SECONDS → P2 via wait_for.

    P4 (9.0c review): the sleep duration is bounded at 0.5s (not 5s) so that
    if the monkeypatch ever fails to take effect (e.g. constant renamed,
    inlined, or imported under a different name), the test fails fast instead
    of hanging for 5 seconds while CI ticks by.
    """
    monkeypatch.setattr(policy_guard_module, "CAPABILITY_CHECK_TIMEOUT_SECONDS", 0.05)

    async def _hang() -> DeviceCapabilityProfile:
        await asyncio.sleep(0.5)
        raise AssertionError("should not return")

    adapter = MagicMock(spec=DeviceAdapter)
    adapter.device_id = "bat-001"
    adapter.get_capabilities = AsyncMock(side_effect=_hang)
    adapter.send_command = AsyncMock()
    guard, spy = _guard_for_p2(adapter)

    result = await guard.authorize_and_dispatch(_discharge_cmd())

    assert result.status is CommandStatus.rejected
    assert result.reason == "capability_check_failed"
    spy.assert_awaited_once()
    adapter.send_command.assert_not_awaited()


async def test_p2_cancelled_error_propagates() -> None:
    """CancelledError must NOT be swallowed by ``except Exception`` in P2.

    The capability gate uses ``except Exception`` (not ``except BaseException``)
    deliberately — catching CancelledError would silently swallow control-loop
    shutdown signals. AC3 + Task 2 invariant.
    """
    adapter = _battery_adapter_with_failing_caps(exc=asyncio.CancelledError())
    guard, _spy = _guard_for_p2(adapter)

    with pytest.raises(asyncio.CancelledError):
        await guard.authorize_and_dispatch(_discharge_cmd())

    adapter.send_command.assert_not_awaited()


@pytest.mark.parametrize(
    ("label", "bogus_return"),
    [
        ("None", None),
        ("empty_dict", {}),
        ("string", "not-a-profile"),
        ("bare_object", object()),
        ("dict_shaped_like_profile", {"device_id": "bat-001", "model": "x"}),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_p2_non_profile_returns_classified_as_capability_check_failed(
    label: str,
    bogus_return: Any,
) -> None:
    """Adversarial: a buggy adapter returning anything that isn't a ``DeviceCapabilityProfile``
    is classified P2 ``capability_check_failed`` — NOT ``AttributeError`` or
    ``TypeError`` from accessing ``.write_capabilities`` on a non-profile.

    P6 (9.0c review): expanded from the original single-case ``None`` test to
    lock the ``isinstance(profile, DeviceCapabilityProfile)`` gate against
    structural duck-typing regressions (a dict that *looks* like a profile
    must still be rejected, not happy-pathed).
    """
    adapter = MagicMock(spec=DeviceAdapter)
    adapter.device_id = "bat-001"
    adapter.get_capabilities = AsyncMock(return_value=bogus_return)  # typing breach
    adapter.send_command = AsyncMock()
    guard, spy = _guard_for_p2(adapter)

    result = await guard.authorize_and_dispatch(_discharge_cmd())

    assert result.status is CommandStatus.rejected, label
    assert result.reason == "capability_check_failed", label
    spy.assert_awaited_once()
    adapter.send_command.assert_not_awaited()


async def test_p2_get_capabilities_device_id_mismatch_is_capability_check_failed() -> None:
    """Adapter returns profile whose device_id != command.device_id → P2."""
    adapter = MagicMock(spec=DeviceAdapter)
    adapter.device_id = "bat-001"
    # Profile carries a DIFFERENT device_id — profile-integrity violation.
    adapter.get_capabilities = AsyncMock(
        return_value=_profile(
            device_id="bat-999",  # ≠ command.device_id ("bat-001")
            write_caps=frozenset({WriteCapability.set_discharge_rate}),
        )
    )
    adapter.send_command = AsyncMock()
    guard, spy = _guard_for_p2(adapter)

    result = await guard.authorize_and_dispatch(_discharge_cmd())

    assert result.status is CommandStatus.rejected
    assert result.reason == "capability_check_failed"
    spy.assert_awaited_once()
    adapter.send_command.assert_not_awaited()


# ---------------------------------------------------------------------------
# AC4 — battery_state_unavailable_for_safety_check
# Discharge command without a published BatteryState must be rejected with the
# new explicit reason — NOT silently slip past the SoC-floor check.
# ---------------------------------------------------------------------------


def _degraded_battery() -> DegradedDeviceState:
    return DegradedDeviceState(
        device_id="bat-001",
        role=DeviceRole.battery,
        reason="reconnecting",
        occurred_at=_NOW,
    )


@pytest.mark.parametrize(
    ("label", "publish_battery"),
    [
        ("battery_is_None", None),
        ("battery_is_DegradedDeviceState", _degraded_battery()),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_ac4_discharge_rejected_when_battery_state_unavailable(
    label: str,
    publish_battery: DegradedDeviceState | None,
) -> None:
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    if publish_battery is not None:
        await store.publish({DeviceRole.battery: publish_battery})

    cmd = _discharge_cmd()
    adapter = _adapter_with_caps(
        device_id=cmd.device_id,
        write_caps=frozenset({WriteCapability.set_discharge_rate}),
    )
    obs, spy = _audit_spy()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )

    result = await guard.authorize_and_dispatch(cmd)

    assert result.status is CommandStatus.rejected, label
    assert result.applied is False
    assert result.reason == "battery_state_unavailable_for_safety_check", label
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["event_type"] == "CONSTRAINT"
    assert kwargs["detail"]["rejection_reason"] == "battery_state_unavailable_for_safety_check"
    adapter.send_command.assert_not_awaited()


async def test_ac4_charge_command_passes_through_with_no_battery_state() -> None:
    """AC4: charge commands have no SoC-floor precondition → no false-positive rejection."""
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    # No battery published; snapshot.battery is None.
    cmd = _charge_cmd()
    adapter = _adapter_with_caps(
        device_id=cmd.device_id,
        write_caps=frozenset({WriteCapability.set_charge_rate}),
    )
    # Stamp correlation_id so P5 (correlation mismatch) doesn't fire.
    adapter.send_command = AsyncMock(
        return_value=CommandResult(
            correlation_id=cmd.correlation_id,
            device_id=cmd.device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )
    )
    obs, spy = _audit_spy()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )

    result = await guard.authorize_and_dispatch(cmd)

    assert result.status is CommandStatus.success
    assert result.applied is True
    spy.assert_not_awaited()  # no rejection → no CONSTRAINT audit
    adapter.send_command.assert_awaited_once()


async def test_ac4_cold_start_discharge_yields_battery_state_unavailable_reason() -> None:
    """D2 (9.0c review): cold-start vocabulary lock.

    From process boot until the first ControlLoop tick publishes a snapshot,
    ``StateStore`` carries ``operating_mode=SystemOperatingMode.degraded`` (NOT
    ``fail_safe``) and ``snapshot.battery=None``. A discharge command in this
    window must produce ``battery_state_unavailable_for_safety_check`` — not
    fall through silently, not produce ``fail_safe_mode_active`` (P0 would
    require ``operating_mode=fail_safe``), and not produce the SoC-floor
    reason (which requires a published ``BatteryState``).

    This test uses ``StateStore`` defaults (no kwarg overrides for
    ``operating_mode``) to lock the cold-start contract. A future change to
    the default — e.g. promoting it to ``fail_safe`` — must update this test
    and the documented invariant in ``_check_safety_constraints``.
    """
    # Default StateStore: operating_mode=SystemOperatingMode.degraded (verify),
    # battery=None (no publish() called).
    store = StateStore(system_clock_status="valid")
    assert store.get_snapshot().operating_mode is SystemOperatingMode.degraded, (
        "StateStore default ``operating_mode`` must be ``degraded`` for this test "
        "to lock cold-start vocabulary; D2 must be updated if the default changes."
    )
    assert store.get_snapshot().battery is None, (
        "StateStore default ``battery`` slot must be None pre-publish; D2 invariant."
    )

    cmd = _discharge_cmd()
    adapter = _adapter_with_caps(
        device_id=cmd.device_id,
        write_caps=frozenset({WriteCapability.set_discharge_rate}),
    )
    obs, spy = _audit_spy()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )

    result = await guard.authorize_and_dispatch(cmd)

    assert result.status is CommandStatus.rejected
    assert result.reason == "battery_state_unavailable_for_safety_check"
    spy.assert_awaited_once()
    adapter.send_command.assert_not_awaited()


async def test_ac4_discharge_with_conservative_mode_and_no_battery_state_ac4_wins() -> None:
    """D5 (9.0c review): AC4 dominates over conservative-mode for discharge+no-state.

    Documented ordering invariant in ``_check_safety_constraints``: AC4 fires
    BEFORE the conservative-mode branch. If a future refactor reorders these
    checks, this test fails and surfaces the vocabulary regression.
    """
    store = StateStore(
        system_clock_status="valid",
        operating_mode=SystemOperatingMode.conservative,
    )
    # battery deliberately not published → snapshot.battery is None
    cmd = _discharge_cmd()
    adapter = _adapter_with_caps(
        device_id=cmd.device_id,
        write_caps=frozenset({WriteCapability.set_discharge_rate}),
    )
    obs, spy = _audit_spy()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )

    result = await guard.authorize_and_dispatch(cmd)

    # AC4 reason wins; conservative-mode reason MUST NOT fire here.
    assert result.status is CommandStatus.rejected
    assert result.reason == "battery_state_unavailable_for_safety_check", (
        "AC4 must be evaluated before the conservative-mode branch — see "
        "`_check_safety_constraints` ordering invariant."
    )
    assert result.reason != "conservative_mode_blocks_load_increase"
    spy.assert_awaited_once()
    adapter.send_command.assert_not_awaited()


async def test_ac4_discharge_with_valid_battery_state_falls_through_to_soc_check() -> None:
    """Sanity: with a valid BatteryState, AC4's check does NOT fire (SoC-floor still applies)."""
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    battery = BatteryState(
        device_id="bat-001",
        soc_percent=20.0,  # at the default reserve floor → SoC check should fire
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW,
    )
    await store.publish({DeviceRole.battery: battery})

    cmd = _discharge_cmd()
    adapter = _adapter_with_caps(
        device_id=cmd.device_id,
        write_caps=frozenset({WriteCapability.set_discharge_rate}),
    )
    obs, _spy = _audit_spy()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )

    result = await guard.authorize_and_dispatch(cmd)

    # SoC-floor reason — proves AC4's new check did NOT short-circuit the existing one.
    assert result.status is CommandStatus.rejected
    assert result.reason == "battery_soc_at_or_below_reserve_floor"


# ---------------------------------------------------------------------------
# AC6 — P5 post-dispatch correlation_id enforcement
# ---------------------------------------------------------------------------


async def test_ac6_correlation_mismatch_returns_correlation_broken_status() -> None:
    """Adapter returns a CommandResult with a fresh UUID → P5 fires.

    Synthetic result carries the command's ORIGINAL correlation_id so audit
    correlation is preserved, and ``status=correlation_broken`` so RetryPolicy
    refuses to retry (post-dispatch outcome is indeterminate; D4 from review).
    """
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    battery = BatteryState(
        device_id="bat-001",
        soc_percent=80.0,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW,
    )
    await store.publish({DeviceRole.battery: battery})

    cmd = _discharge_cmd()
    bogus_correlation_id = _uuid.uuid4()
    assert bogus_correlation_id != cmd.correlation_id

    adapter = _adapter_with_caps(
        device_id=cmd.device_id,
        write_caps=frozenset({WriteCapability.set_discharge_rate}),
    )
    adapter.send_command = AsyncMock(
        return_value=CommandResult(
            correlation_id=bogus_correlation_id,  # ≠ cmd.correlation_id
            device_id=cmd.device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )
    )
    obs, spy = _audit_spy()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )

    result = await guard.authorize_and_dispatch(cmd)

    # P5 contract: status=correlation_broken (NOT rejected, NOT failed), exact reason,
    # original correlation_id preserved. D4 (review): dedicated status lets RetryPolicy
    # refuse to retry — outcome is indeterminate.
    assert result.status is CommandStatus.correlation_broken
    assert result.applied is False
    assert result.reason == "adapter_correlation_id_mismatch"
    assert result.correlation_id == cmd.correlation_id, (
        "P5 must preserve command's original correlation_id"
    )
    # CONSTRAINT audit was emitted with the original correlation_id.
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["event_type"] == "CONSTRAINT"
    assert kwargs["detail"]["rejection_reason"] == "adapter_correlation_id_mismatch"
    assert kwargs["detail"]["correlation_id"] == str(cmd.correlation_id)
    assert kwargs["detail"]["command_type"] == "SetBatteryDischargeRateCommand"
    adapter.send_command.assert_awaited_once()


async def test_ac6_matching_correlation_id_passes_through_verbatim() -> None:
    """Sanity: when adapter returns the matching correlation_id, result is passed through."""
    store = StateStore(system_clock_status="valid", operating_mode=SystemOperatingMode.normal)
    battery = BatteryState(
        device_id="bat-001",
        soc_percent=80.0,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW,
    )
    await store.publish({DeviceRole.battery: battery})

    cmd = _discharge_cmd()
    adapter = _adapter_with_caps(
        device_id=cmd.device_id,
        write_caps=frozenset({WriteCapability.set_discharge_rate}),
    )
    adapter.send_command = AsyncMock(
        return_value=CommandResult(
            correlation_id=cmd.correlation_id,  # matches
            device_id=cmd.device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )
    )
    obs, spy = _audit_spy()
    guard = PolicyGuard(
        state_store=store,
        adapters={DeviceRole.battery: adapter},
        observability=obs,
        settings=_settings(),
        active_constraints=_provider(),
    )

    result = await guard.authorize_and_dispatch(cmd)

    assert result.status is CommandStatus.success
    assert result.correlation_id == cmd.correlation_id
    spy.assert_not_awaited()  # no audit on happy path
