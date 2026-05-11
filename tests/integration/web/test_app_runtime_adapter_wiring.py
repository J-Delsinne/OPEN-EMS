"""Integration tests for Story 9.X runtime adapter wiring in lifespan.

Covers AC6 (lifespan step 4h), AC7 (empty-registry cold-start), AC8
(``runtime_adapter_map_built`` log with the ``(role, protocol, device_id)``
triple), AC9 (fail-loud SystemExit on builder failure), AC11
(instance-identity between PolicyGuard and ControlLoop adapter maps),
and the AC14 dispatch-past-P1 / dispatch-rejected-at-P1 contracts.

Log assertions use the ``MagicMock(logger)`` pattern (not
``structlog.testing.capture_logs``) because the lifespan calls
``configure_logging`` which enables ``cache_logger_on_first_use=True``;
the structlog capture context cannot reliably intercept events after
that reconfiguration. The MagicMock approach intercepts at the
``open_ems.web.app.logger`` module-level reference instead, which is
the production logger every emission in the lifespan flows through.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import aiosqlite
import pytest
import structlog

import open_ems.web.app as app_module
from open_ems.adapters.modbus.battery_adapter import BatteryAdapter
from open_ems.adapters.ocpp.central_system import OCPPCentralSystem
from open_ems.core.commands import CommandOrigin, SetBatteryChargeRateCommand
from open_ems.core.devices import DeviceRole
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.services.audit_log import ObservabilityService
from open_ems.services.runtime_adapter_wiring import (
    RuntimeAdapterMap,
    RuntimeAdapterWiringError,
)
from open_ems.settings import Settings
from open_ems.web.app import _run_alembic_upgrade, create_app, lifespan


async def _audit_noop(*args: object, **kwargs: object) -> None:
    """Stand-in for ``ObservabilityService.audit`` in lifespan integration tests.

    PolicyGuard's ``authorize_and_dispatch`` invokes ``observability.audit(...)``
    on every rejection / dispatch outcome. The real implementation writes to
    the DB AND emits a structlog event through ``open_ems.services.audit_log``'s
    module-level logger; combined with ``configure_logging``'s
    ``cache_logger_on_first_use=True``, that first emission caches the
    JSON-rendering processor chain on the module logger and breaks
    ``capture_logs()`` in subsequent ``test_audit_log`` tests.

    Patching the method to this no-op coroutine for the duration of the
    lifespan-driving tests keeps the AC14 dispatch assertions exercising the
    real PolicyGuard logic while preventing cross-test structlog cache
    leakage.
    """
    return None


def _seed_lifespan_env(
    monkeypatch: pytest.MonkeyPatch,
    db_path: str,
) -> None:
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-32-chars-xxxxxxxxxx")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "ChangeMe!1234")
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(_env_file=None),  # type: ignore[call-arg]
    )


async def _bootstrap_schema(db_path: str) -> None:
    """Run Alembic migrations directly without a lifespan round-trip.

    Avoids the fragility of bootstrapping schema by running a discarded
    lifespan — future non-idempotent startup steps would break that pattern
    confusingly. The migration helper is the same code path the production
    lifespan invokes at step 2.
    """
    db_url = f"sqlite:///{db_path}"
    await asyncio.to_thread(_run_alembic_upgrade, db_url, None)


async def test_lifespan_empty_registry_emits_runtime_adapter_map_empty_log_and_p1_rejects(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7 + AC14 sibling regression — pre-installer-wizard-complete cold-start.

    With no validated+role-assigned devices in ``device_registry``, the
    runtime adapter map is empty; lifespan logs ``runtime_adapter_map_empty``
    with the documented reason. PolicyGuard / ControlLoop are still
    constructed (operational for the wizard) but every command rejects at
    P1 (``adapter_not_registered``) — the documented cold-start contract.
    """
    structlog.reset_defaults()
    db_path = str(tmp_path / "story_9_x_empty.db")
    _seed_lifespan_env(monkeypatch, db_path)

    mock_logger = MagicMock()
    app = create_app()
    with (
        patch("open_ems.web.app.logger", mock_logger),
        patch.object(ObservabilityService, "audit", _audit_noop),
    ):
        async with lifespan(app):
            assert isinstance(app.state.runtime_adapters, RuntimeAdapterMap)
            assert app.state.runtime_adapters.policy_guard_adapters == {}
            assert app.state.runtime_adapters.control_loop_adapters == {}
            policy_guard: PolicyGuard = app.state.policy_guard
            assert isinstance(policy_guard, PolicyGuard)

            # AC14 sibling regression — PolicyGuard rejects with P1.
            command = SetBatteryChargeRateCommand(
                device_id="batt-nonexistent",
                device_role=DeviceRole.battery,
                origin=CommandOrigin.decision_engine,
                rate_kw=1.0,
            )
            result = await policy_guard.authorize_and_dispatch(command)
            assert result.applied is False
            assert result.reason == "adapter_not_registered"

    info_events = [call.args[0] for call in mock_logger.info.call_args_list if call.args]
    assert "runtime_adapter_map_empty" in info_events
    # The other event must NOT have fired
    assert "runtime_adapter_map_built" not in info_events
    # And the empty-event must include the documented reason
    empty_calls = [
        call
        for call in mock_logger.info.call_args_list
        if call.args and call.args[0] == "runtime_adapter_map_empty"
    ]
    assert empty_calls[0].kwargs.get("reason") == "pre_installer_wizard_complete"


async def test_lifespan_populated_registry_wires_battery_and_grid_meter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6 / AC8 / AC11 / AC14 populated case end-to-end.

    Seed device_registry with one validated+role-assigned battery (Modbus)
    and one grid_meter (DSMR). After lifespan:

    * ``runtime_adapter_map_built`` log emitted with counts matching;
    * the log's ``adapters`` field carries ``(role, protocol, device_id)``
      triples per AC8;
    * PolicyGuard sees battery only (grid_meter excluded — read-only);
    * ControlLoop sees both;
    * battery instance is ``is``-identical between the two maps (AC11);
    * PolicyGuard.authorize_and_dispatch does NOT reject with P1
      (``adapter_not_registered``) for the wired battery — AC14.
    """
    structlog.reset_defaults()
    db_path = str(tmp_path / "story_9_x_populated.db")
    _seed_lifespan_env(monkeypatch, db_path)

    # Bootstrap schema directly via the production migration helper, avoiding
    # the fragility of a discarded lifespan run (P13 of the 9-X review).
    await _bootstrap_schema(db_path)

    # Seed two validated+role-assigned rows.
    async with aiosqlite.connect(db_path) as raw:
        now = datetime.now(UTC).isoformat()
        await raw.execute(
            "INSERT INTO device_registry"
            " (device_id, protocol, address, model, firmware_version, source,"
            "  validated, last_capability_status, first_seen_at, last_seen_at,"
            "  role, role_assigned_at)"
            " VALUES (?, 'modbus_tcp', '10.0.0.10:502', 'byd_hvs_v1', NULL,"
            "  'manual_entry', 1, 'full', ?, ?, 'battery', ?)",
            ("batt-1", now, now, now),
        )
        await raw.execute(
            "INSERT INTO device_registry"
            " (device_id, protocol, address, model, firmware_version, source,"
            "  validated, last_capability_status, first_seen_at, last_seen_at,"
            "  role, role_assigned_at)"
            " VALUES (?, 'dsmr_p1', '/dev/ttyUSB0', 'dsmr_p1', NULL,"
            "  'manual_entry', 1, 'full', ?, ?, 'grid_meter', ?)",
            ("meter-1", now, now, now),
        )
        await raw.commit()

    structlog.reset_defaults()
    mock_logger = MagicMock()
    app = create_app()
    with (
        patch("open_ems.web.app.logger", mock_logger),
        patch.object(ObservabilityService, "audit", _audit_noop),
    ):
        async with lifespan(app):
            rt: RuntimeAdapterMap = app.state.runtime_adapters
            assert set(rt.control_loop_adapters.keys()) == {
                DeviceRole.battery,
                DeviceRole.grid_meter,
            }
            assert set(rt.policy_guard_adapters.keys()) == {DeviceRole.battery}
            # AC11 — same instance reference for overlapping role
            assert (
                rt.policy_guard_adapters[DeviceRole.battery]
                is rt.control_loop_adapters[DeviceRole.battery]
            )
            assert isinstance(
                rt.policy_guard_adapters[DeviceRole.battery],
                BatteryAdapter,
            )
            # protocols_by_role is populated for every role in control_loop_adapters
            assert rt.protocols_by_role == {
                DeviceRole.battery: "modbus_tcp",
                DeviceRole.grid_meter: "dsmr_p1",
            }

            # AC14 — PolicyGuard dispatch for the wired battery passes P1
            # (``adapter_not_registered`` MUST NOT fire). The dispatch may
            # still fail later (no real device behind the Modbus adapter)
            # but the rejection reason must not be the empty-map reason.
            # Stub the battery adapter's send_command so the test does not
            # block on a real TCP connect timeout to 10.0.0.10:502.
            battery_adapter = rt.policy_guard_adapters[DeviceRole.battery]

            async def _stubbed_send_command(cmd: object) -> object:
                from open_ems.core.commands import CommandResult, CommandStatus

                return CommandResult(
                    correlation_id=cmd.correlation_id,  # type: ignore[attr-defined]
                    device_id=cmd.device_id,  # type: ignore[attr-defined]
                    status=CommandStatus.success,
                    applied=True,
                    reason="ok",
                )

            with patch.object(battery_adapter, "send_command", _stubbed_send_command):
                policy_guard: PolicyGuard = app.state.policy_guard
                command = SetBatteryChargeRateCommand(
                    device_id="batt-1",
                    device_role=DeviceRole.battery,
                    origin=CommandOrigin.decision_engine,
                    rate_kw=1.0,
                )
                result = await policy_guard.authorize_and_dispatch(command)
                assert result.reason != "adapter_not_registered"

    # AC8 — log shape including the protocol field
    built_calls = [
        call
        for call in mock_logger.info.call_args_list
        if call.args and call.args[0] == "runtime_adapter_map_built"
    ]
    assert built_calls, "expected runtime_adapter_map_built log on populated registry"
    log_kwargs = built_calls[0].kwargs
    assert log_kwargs.get("policy_guard_role_count") == 1
    assert log_kwargs.get("control_loop_role_count") == 2
    adapter_summary = log_kwargs.get("adapters")
    assert isinstance(adapter_summary, list)
    role_protocol_pairs = {(item["role"], item["protocol"]) for item in adapter_summary}
    assert role_protocol_pairs == {
        ("battery", "modbus_tcp"),
        ("grid_meter", "dsmr_p1"),
    }


async def test_lifespan_builder_failure_aborts_with_system_exit_1(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9 — fail-loud builder failure.

    Patch ``build_runtime_adapter_map`` to raise; lifespan logs
    ``startup_failed`` with the documented reason and exits with code 1.
    Matches the existing migration / constraints-hydrate failure handling.
    """
    structlog.reset_defaults()
    db_path = str(tmp_path / "story_9_x_failure.db")
    _seed_lifespan_env(monkeypatch, db_path)

    mock_logger = MagicMock()
    app = create_app()
    with (
        patch(
            "open_ems.web.app.build_runtime_adapter_map",
            side_effect=RuntimeAdapterWiringError("fake_wiring_failure"),
        ),
        patch("open_ems.web.app.logger", mock_logger),
    ):
        with pytest.raises(SystemExit) as exc_info:
            async with lifespan(app):
                pass

    assert exc_info.value.code == 1
    error_calls = [
        call
        for call in mock_logger.error.call_args_list
        if call.args and call.args[0] == "startup_failed"
    ]
    assert error_calls, "expected startup_failed error on wiring failure"
    last_call = error_calls[-1]
    assert last_call.kwargs.get("reason") == "runtime_adapter_wiring_failed"
    assert "fake_wiring_failure" in last_call.kwargs.get("detail", "")


async def test_lifespan_wires_ocpp_central_system_into_orchestrator_and_factory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5 — the OCPPCentralSystem is constructed and wired (no longer ``None``).

    Reaches into the discovery_orchestrator and protocol_adapter_factory on
    app.state to confirm both received the same instance.
    """
    structlog.reset_defaults()
    db_path = str(tmp_path / "story_9_x_ocpp_wiring.db")
    _seed_lifespan_env(monkeypatch, db_path)

    app = create_app()
    async with lifespan(app):
        # The orchestrator and factory aren't required to expose the central
        # system publicly; we observe them via their constructor-stored attr.
        # Both reach OCPPCentralSystem via their internal `_ocpp_central_system`.
        orch = app.state.discovery_orchestrator
        fac = app.state.protocol_adapter_factory
        # mypy doesn't see the private attr; this assertion runs at the
        # runtime layer where attribute access is fine.
        orch_cs = getattr(orch, "_ocpp_central_system", None)
        fac_cs = getattr(fac, "_ocpp_central_system", None)
        assert isinstance(orch_cs, OCPPCentralSystem)
        assert isinstance(fac_cs, OCPPCentralSystem)
        # Same instance — single canonical OCPP registry per lifespan.
        assert orch_cs is fac_cs
