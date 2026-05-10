"""Integration tests for the DB-backed active-constraints reload (Story 9.0b).

Covers AC8 end-to-end + AC10 #20-#23 against the real Alembic-migrated SQLite
test DB. No mocked repos — drift between unit-test mocks and the real schema
would otherwise be invisible.
"""

from __future__ import annotations

import asyncio
import pathlib
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from alembic import command as alembic_command
from alembic.config import Config

from open_ems.core import (
    BatteryState,
    DeviceAdapter,
    DeviceRole,
    EVChargerState,
    StateStore,
)
from open_ems.core.commands import (
    CommandOrigin,
    CommandResult,
    CommandStatus,
    SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand,
)
from open_ems.core.constraints import ActiveConstraintsInput
from open_ems.core.devices import (
    DeviceCapabilityProfile,
    ReadCapability,
    WriteCapability,
)
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings
from open_ems.storage.database import close_database, init_database
from open_ems.storage.repositories.config_repo import ConfigRepo

_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest_asyncio.fixture
async def migrated_db(tmp_path: pathlib.Path) -> AsyncGenerator[str, None]:
    """Apply Alembic head migrations to a fresh on-disk SQLite DB and open it."""
    db_path = str(tmp_path / "constraints_reload.db")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    alembic_command.upgrade(cfg, "head")
    await init_database(db_path)
    yield db_path
    await close_database()


# ── Stub EV adapter that always succeeds ──────────────────────────────────────


class _StubEVAdapter:
    def __init__(self, device_id: str = "ev-001") -> None:
        self.device_id = device_id
        self.send_command_calls: list[object] = []

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def get_state(self) -> EVChargerState:
        return EVChargerState(
            device_id=self.device_id,
            connected=True,
            charging=False,
            charge_rate_kw=0.0,
            energy_session_kwh=0.0,
            read_at=_NOW,
        )

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        return DeviceCapabilityProfile(
            device_id=self.device_id,
            model="StubEV",
            read_capabilities=frozenset({ReadCapability.state}),
            write_capabilities=frozenset({WriteCapability.set_ev_charge_current}),
        )

    async def send_command(self, command: object) -> CommandResult:
        self.send_command_calls.append(command)
        cid = getattr(command, "correlation_id", uuid.uuid4())
        return CommandResult(
            correlation_id=cid,
            device_id=self.device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )


def _ev_charge_command(rate_kw: float) -> SetEVChargingRateCommand:
    return SetEVChargingRateCommand(
        device_id="ev-001",
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.decision_engine,
        rate_kw=rate_kw,
    )


def _build_state_store() -> StateStore:
    return StateStore(system_clock_status="valid")


def _observability() -> ObservabilityService:
    # Real ObservabilityService backed by EventLogRepo on the connected DB —
    # the audit emission path runs against a real INSERT into event_log.
    return ObservabilityService()


# ── AC10 #20: peak-limit reload ──────────────────────────────────────────────


async def test_reload_picks_up_new_peak_limit(
    migrated_db: str,
) -> None:
    """AC8 end-to-end: a higher peak limit becomes effective after reload()."""
    settings = _settings()
    repo = ConfigRepo()
    provider = ActiveConstraintsProvider(repo=repo, settings=settings)

    # Seed: peak_limit_kw=25.0 (Settings default; provider hydrates from
    # Settings since no DB row exists yet).
    await provider.hydrate()
    assert provider.get().peak_limit_kw == 25.0

    adapter: DeviceAdapter = _StubEVAdapter()  # type: ignore[assignment]
    state_store = _build_state_store()
    guard = PolicyGuard(
        state_store=state_store,
        adapters={DeviceRole.ev_charger: adapter},
        observability=_observability(),
        settings=settings,
        active_constraints=provider,
    )

    # Step 2: PolicyGuard rejects 30 kW > 25 kW
    cmd_too_high = _ev_charge_command(rate_kw=30.0)
    rejected = await guard.authorize_and_dispatch(cmd_too_high)
    assert rejected.status is CommandStatus.rejected
    assert rejected.reason == "commanded_rate_exceeds_peak_limit"

    # Step 3: activate peak_limit_kw=40.0
    new_version = await repo.activate(
        ActiveConstraintsInput(peak_limit_kw=40.0, battery_reserve_floor_percent=20.0),
        actor="installer",
    )
    assert new_version == 1

    # Step 4: reload — provider snapshot now reflects the DB row
    await provider.reload()
    assert provider.get().peak_limit_kw == 40.0
    assert provider.get().config_version == 1

    # Step 5: same command now allowed
    cmd_ok = _ev_charge_command(rate_kw=30.0)
    allowed = await guard.authorize_and_dispatch(cmd_ok)
    assert allowed.status is CommandStatus.success
    assert allowed.applied is True


# ── AC10 #21: reserve-floor reload ───────────────────────────────────────────


class _StubBatteryAdapter:
    def __init__(self, device_id: str = "bat-001") -> None:
        self.device_id = device_id

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def get_state(self) -> BatteryState:  # type: ignore[override]
        # Not used directly by PolicyGuard — battery is read from StateStore.
        raise NotImplementedError

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        return DeviceCapabilityProfile(
            device_id=self.device_id,
            model="StubBattery",
            read_capabilities=frozenset({ReadCapability.state}),
            write_capabilities=frozenset({WriteCapability.set_discharge_rate}),
        )

    async def send_command(self, command: object) -> CommandResult:
        cid = getattr(command, "correlation_id", uuid.uuid4())
        return CommandResult(
            correlation_id=cid,
            device_id=self.device_id,
            status=CommandStatus.success,
            applied=True,
            reason="ok",
        )


async def test_reload_picks_up_new_reserve_floor(
    migrated_db: str,
) -> None:
    """Battery discharge below the active reserve floor is rejected; after
    activation+reload of a lower floor, the same command is allowed."""
    settings = _settings()
    repo = ConfigRepo()
    provider = ActiveConstraintsProvider(repo=repo, settings=settings)
    await provider.hydrate()
    assert provider.get().battery_reserve_floor_percent == 20.0

    adapter: DeviceAdapter = _StubBatteryAdapter()  # type: ignore[assignment]
    state_store = _build_state_store()
    # Battery at 25% — above default reserve floor (20%) but below a
    # hypothetical higher floor we'll activate first.
    battery = BatteryState(
        device_id="bat-001",
        soc_percent=25.0,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        operating_mode="normal",
        read_at=_NOW,
    )
    await state_store.publish({DeviceRole.battery: battery})

    guard = PolicyGuard(
        state_store=state_store,
        adapters={DeviceRole.battery: adapter},
        observability=_observability(),
        settings=settings,
        active_constraints=provider,
    )

    # Activate a HIGHER reserve floor (30%) — battery at 25% is now BELOW floor.
    await repo.activate(
        ActiveConstraintsInput(peak_limit_kw=25.0, battery_reserve_floor_percent=30.0),
        actor="installer",
    )
    await provider.reload()
    assert provider.get().battery_reserve_floor_percent == 30.0

    discharge = SetBatteryDischargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )
    result_blocked = await guard.authorize_and_dispatch(discharge)
    assert result_blocked.status is CommandStatus.rejected
    assert result_blocked.reason == "battery_soc_at_or_below_reserve_floor"

    # Activate a lower reserve floor (10%) — battery at 25% is now ABOVE floor.
    await repo.activate(
        ActiveConstraintsInput(peak_limit_kw=25.0, battery_reserve_floor_percent=10.0),
        actor="installer",
    )
    await provider.reload()
    assert provider.get().battery_reserve_floor_percent == 10.0

    # Same command now allowed.
    discharge_ok = SetBatteryDischargeRateCommand(
        device_id="bat-001",
        device_role=DeviceRole.battery,
        origin=CommandOrigin.decision_engine,
        rate_kw=1.0,
    )
    result_ok = await guard.authorize_and_dispatch(discharge_ok)
    assert result_ok.status is CommandStatus.success
    assert result_ok.applied is True


# ── AC10 #22: lifespan ordering ──────────────────────────────────────────────


async def test_lifespan_hydrates_before_control_loop(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``provider.is_hydrated`` MUST be True at the moment the control_loop
    task is created via ``asyncio.create_task``. We spy on
    ``asyncio.create_task`` and assert ``is_hydrated`` at the point the
    spy sees the loop being scheduled."""
    import structlog

    import open_ems.web.app as app_module
    from open_ems.web.app import create_app, lifespan

    structlog.reset_defaults()

    db_path = str(tmp_path / "lifespan_ordering.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-32-chars-xxxxxxxxxx")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "ChangeMe!1234")
    # Force fresh Settings load — `_env_file=None` prevents the test from
    # reading whatever `.env` happens to sit at the project root on the
    # developer's machine; the monkey-patched env vars above are the only
    # source of truth for this run.
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(_env_file=None),  # type: ignore[call-arg]
    )

    real_create_task = asyncio.create_task
    is_hydrated_at_control_loop_create: list[bool] = []
    original_app: object | None = None

    def spy_create_task(coro, *args, **kwargs):  # type: ignore[no-untyped-def]
        name = kwargs.get("name")
        if name == "control_loop":
            assert original_app is not None
            provider = getattr(
                original_app.state,
                "active_constraints_provider",
                None,  # type: ignore[attr-defined]
            )
            is_hydrated_at_control_loop_create.append(provider is not None and provider.is_hydrated)
        return real_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", spy_create_task)

    app = create_app()
    original_app = app
    async with lifespan(app):
        # Inside the running lifespan context — the control loop is alive
        # and dispatch is happening. The spy already recorded the answer.
        pass

    assert is_hydrated_at_control_loop_create == [True], (
        f"control_loop task was scheduled with is_hydrated="
        f"{is_hydrated_at_control_loop_create}; expected [True] — "
        "provider.hydrate() must complete BEFORE asyncio.create_task("
        "control_loop) runs."
    )


# ── AC10 #23: cold-start seeds from Settings ─────────────────────────────────


async def test_cold_start_seeds_from_settings(migrated_db: str) -> None:
    """Fresh DB (post-migration, zero rows in active_constraints) →
    ``provider.get().config_version == 0`` and values match Settings defaults."""
    settings = _settings()
    provider = ActiveConstraintsProvider(repo=ConfigRepo(), settings=settings)
    await provider.hydrate()

    snapshot = provider.get()
    assert snapshot.config_version == 0
    assert snapshot.peak_limit_kw == settings.peak_limit_kw
    assert snapshot.battery_reserve_floor_percent == settings.battery_reserve_floor_percent
