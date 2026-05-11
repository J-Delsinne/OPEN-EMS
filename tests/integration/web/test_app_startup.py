"""Integration tests for the FastAPI lifespan startup gates.

Story 9.0c (AC5): capability-registry drift detected by
``validate_capability_registry_alignment()`` aborts boot with
``startup_failed reason="capability_registry_drift"`` and ``SystemExit(1)``.

Same enforcement class as the existing migration / constraints-hydrate
failures — close_database() must still run on this failure path.

Story 9.0d (AC8): lifespan step 4c hydrates ``_monthly_peak_kw`` from
``peak_intervals`` BEFORE constructing the ControlLoop. Hydrate failure is
fail-loud (``SystemExit(1)`` + ``startup_failed reason="monthly_peak_hydrate_failed"``);
empty ``peak_intervals`` yields ``0.0`` (legitimate fresh-deployment branch).
"""

from __future__ import annotations

import asyncio
import math
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import structlog

import open_ems.web.app as app_module
from open_ems.adapters.capabilities import CapabilityRegistryDriftError
from open_ems.engine.partial_interval_tracker import CompletedInterval
from open_ems.settings import Settings
from open_ems.storage.database import close_database as real_close_database
from open_ems.storage.database import init_database
from open_ems.storage.repositories.energy_repo import EnergyRepo
from open_ems.web.app import create_app, lifespan


async def test_lifespan_aborts_with_system_exit_on_capability_registry_drift() -> None:
    """If ``validate_capability_registry_alignment()`` raises, lifespan must SystemExit(1).

    P1 (9.0c review): also asserts ``close_database()`` is invoked on this
    failure path. The outer ``try:`` in ``web/app.py`` opens AFTER
    ``init_database()`` so the ``finally`` cleans up — a future refactor that
    moves the validator BEFORE ``init_database()`` would silently regress the
    close path; this assertion locks the invariant.
    """
    app = create_app()

    mock_logger = MagicMock()
    with (
        patch(
            "open_ems.web.app.validate_capability_registry_alignment",
            side_effect=CapabilityRegistryDriftError(["bogus_future_model_v1"]),
        ),
        patch("open_ems.web.app.logger", mock_logger),
        # P1: spy on close_database (wraps=real fn) so the real cleanup still runs;
        # otherwise the leaked DB connection breaks subsequent tests.
        patch(
            "open_ems.web.app.close_database",
            wraps=real_close_database,
        ) as mock_close_database,
    ):
        with pytest.raises(SystemExit) as exc_info:
            async with lifespan(app):
                pass

    assert exc_info.value.code == 1
    # AC5: log carries the explicit reason + the list of missing models.
    error_calls = [
        c for c in mock_logger.error.call_args_list if c.args and c.args[0] == "startup_failed"
    ]
    assert error_calls, "expected a startup_failed error log on registry drift"
    last_call = error_calls[-1]
    assert last_call.kwargs.get("reason") == "capability_registry_drift"
    assert last_call.kwargs.get("missing_models") == ["bogus_future_model_v1"]
    assert last_call.kwargs.get("component") == "startup"
    # P1: close_database MUST run on the drift failure path (lifespan finally block).
    mock_close_database.assert_awaited_once()


async def test_lifespan_aborts_before_constraints_hydrate_on_drift() -> None:
    """Drift detection runs BEFORE ``ActiveConstraintsProvider`` is constructed."""
    app = create_app()

    with (
        patch(
            "open_ems.web.app.validate_capability_registry_alignment",
            side_effect=CapabilityRegistryDriftError(["another_missing_model"]),
        ),
        patch("open_ems.web.app.ActiveConstraintsProvider") as mock_provider_cls,
    ):
        with pytest.raises(SystemExit):
            async with lifespan(app):
                pass

    # Lifespan exited before constructing the provider — ordering invariant.
    mock_provider_cls.assert_not_called()


async def test_lifespan_runs_validator_on_happy_path() -> None:
    """Sanity: when the validator returns None, lifespan reaches the application body."""
    app = create_app()

    with (
        patch(
            "open_ems.web.app.validate_capability_registry_alignment",
            return_value=None,
        ) as mock_validator,
        patch("open_ems.web.app._bootstrap_admin_if_needed", return_value=None),
    ):
        async with lifespan(app):
            pass

    mock_validator.assert_called_once()


async def test_lifespan_wires_story_9_1_services_on_app_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 9.1 Task 8: ``device_repo`` / ``wizard_state_repo`` /
    ``discovery_orchestrator`` / ``manual_entry_service`` are exposed on
    ``app.state`` after the lifespan reaches the application body.
    """
    from open_ems.services.device_discovery import DeviceDiscoveryOrchestrator
    from open_ems.services.manual_entry import ManualEntryService
    from open_ems.storage.repositories.device_repo import DeviceRepo
    from open_ems.storage.repositories.wizard_state_repo import WizardStateRepo

    structlog.reset_defaults()
    db_path = str(tmp_path / "story_9_1_wiring.db")
    _seed_lifespan_env(monkeypatch, db_path)

    app = create_app()
    async with lifespan(app):
        assert isinstance(app.state.device_repo, DeviceRepo)
        assert isinstance(app.state.wizard_state_repo, WizardStateRepo)
        assert isinstance(app.state.discovery_orchestrator, DeviceDiscoveryOrchestrator)
        assert isinstance(app.state.manual_entry_service, ManualEntryService)


# ── Story 9.0d (AC8): monthly-peak lifespan hydration ──────────────────────


def _project_root() -> pathlib.Path:
    """Walk up to the directory containing alembic.ini."""
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "alembic.ini").exists():
            return parent
    raise RuntimeError("alembic.ini not found in any parent directory")


def _seed_lifespan_env(
    monkeypatch: pytest.MonkeyPatch,
    db_path: str,
) -> None:
    """Set the env vars + ``get_settings`` patch needed to drive a real lifespan run."""
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-32-chars-xxxxxxxxxx")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "ChangeMe!1234")
    # Force fresh Settings load — `_env_file=None` ignores any project-root .env
    # so the monkey-patched env vars above are the only source of truth.
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(_env_file=None),  # type: ignore[call-arg]
    )


async def test_lifespan_hydrates_monthly_peak_before_control_loop_construction(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8 #1: lifespan reads MAX(avg_power_kw) from peak_intervals BEFORE constructing ControlLoop.

    Pre-populates a peak_intervals row in the current UTC month, then runs the
    real lifespan and asserts: (a) the captured ``monthly_peak_hydrated`` log
    carries that value; (b) the log fires BEFORE the ``control_loop``
    asyncio task is scheduled (structural ordering invariant for AC6).
    """
    from alembic import command as alembic_command
    from alembic.config import Config

    structlog.reset_defaults()

    db_path = str(tmp_path / "monthly_peak_hydrate.db")
    _seed_lifespan_env(monkeypatch, db_path)

    # Pre-populate peak_intervals with one row in the current UTC month.
    cfg = Config(str(_project_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    alembic_command.upgrade(cfg, "head")
    await init_database(db_path)
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    interval_start = month_start + timedelta(hours=1)  # 15-min aligned
    completed = CompletedInterval(
        interval_start_utc=interval_start,
        avg_power_kw=17.3,
        sample_count=1,
    )
    await EnergyRepo().write_peak_interval(completed, data_quality="complete")
    await real_close_database()

    # Spy on asyncio.create_task to record the moment control_loop is scheduled.
    real_create_task = asyncio.create_task
    log_seen_before_control_loop: list[bool] = []
    mock_logger = MagicMock()

    def spy_create_task(coro: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("name") == "control_loop":
            seen = any(
                c.args and c.args[0] == "monthly_peak_hydrated"
                for c in mock_logger.info.call_args_list
            )
            log_seen_before_control_loop.append(seen)
        return real_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", spy_create_task)
    monkeypatch.setattr(app_module, "logger", mock_logger)

    app = create_app()
    async with lifespan(app):
        pass

    # AC4: exactly one monthly_peak_hydrated info event with the seeded value.
    hydrated_calls = [
        c
        for c in mock_logger.info.call_args_list
        if c.args and c.args[0] == "monthly_peak_hydrated"
    ]
    assert len(hydrated_calls) == 1, (
        f"expected exactly one monthly_peak_hydrated log, got {len(hydrated_calls)}"
    )
    assert hydrated_calls[0].kwargs.get("initial_monthly_peak_kw") == 17.3
    assert hydrated_calls[0].kwargs.get("component") == "startup"

    # AC6: ordering — log fired BEFORE control_loop task scheduled.
    assert log_seen_before_control_loop == [True], (
        f"monthly_peak_hydrated log must fire before control_loop task is scheduled; "
        f"got {log_seen_before_control_loop}"
    )


async def test_lifespan_aborts_with_system_exit_on_monthly_peak_hydrate_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8 #2 + AC5: hydrate failure → SystemExit(1) + startup_failed log + close_database runs.

    Mirrors ``test_lifespan_aborts_with_system_exit_on_capability_registry_drift``
    including the ``wraps=real_close_database`` spy that locks the
    cleanup-on-failure invariant introduced by Story 9.0c P1.
    """
    structlog.reset_defaults()

    db_path = str(tmp_path / "monthly_peak_hydrate_fail.db")
    _seed_lifespan_env(monkeypatch, db_path)

    app = create_app()
    mock_logger = MagicMock()

    with (
        patch.object(
            EnergyRepo,
            "get_current_monthly_peak_kw",
            side_effect=aiosqlite.OperationalError("disk I/O error"),
        ),
        patch("open_ems.web.app.logger", mock_logger),
        # Spy on close_database (wraps=real fn) so the real cleanup still runs;
        # otherwise the leaked DB connection would break subsequent tests.
        patch(
            "open_ems.web.app.close_database",
            wraps=real_close_database,
        ) as mock_close_database,
    ):
        with pytest.raises(SystemExit) as exc_info:
            async with lifespan(app):
                pass

    assert exc_info.value.code == 1
    error_calls = [
        c for c in mock_logger.error.call_args_list if c.args and c.args[0] == "startup_failed"
    ]
    assert error_calls, "expected a startup_failed error log on monthly-peak hydrate failure"
    last_call = error_calls[-1]
    assert last_call.kwargs.get("reason") == "monthly_peak_hydrate_failed"
    assert last_call.kwargs.get("component") == "startup"
    assert last_call.kwargs.get("exc_info") is True
    # AC5 + AC8 #2: close_database MUST run on the hydrate-failure path.
    mock_close_database.assert_awaited_once()


async def test_lifespan_hydrates_zero_when_peak_intervals_is_empty(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8 #3: cold-start branch — empty peak_intervals → seed=0.0, no SystemExit.

    Uses a real test DB (post-migration) so the ``MAX(avg_power_kw)`` over an
    empty table exercises real SQLite semantics, not a mock.
    """
    structlog.reset_defaults()

    db_path = str(tmp_path / "monthly_peak_hydrate_empty.db")
    _seed_lifespan_env(monkeypatch, db_path)

    mock_logger = MagicMock()
    monkeypatch.setattr(app_module, "logger", mock_logger)

    app = create_app()
    async with lifespan(app):
        pass

    # AC4 + AC8 #3: the log fired exactly once with 0.0 — no SystemExit reached.
    hydrated_calls = [
        c
        for c in mock_logger.info.call_args_list
        if c.args and c.args[0] == "monthly_peak_hydrated"
    ]
    assert len(hydrated_calls) == 1, (
        f"expected exactly one monthly_peak_hydrated log on cold start, got {len(hydrated_calls)}"
    )
    assert hydrated_calls[0].kwargs.get("initial_monthly_peak_kw") == 0.0
    assert hydrated_calls[0].kwargs.get("component") == "startup"
    # No startup_failed error on the empty-table branch.
    failure_calls = [
        c for c in mock_logger.error.call_args_list if c.args and c.args[0] == "startup_failed"
    ]
    assert not failure_calls, f"unexpected startup_failed on empty-table branch: {failure_calls}"


@pytest.mark.parametrize(
    "corrupt_value",
    [
        pytest.param(float("inf"), id="positive_infinity"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(-1.0, id="negative"),
    ],
)
async def test_lifespan_aborts_with_system_exit_on_monthly_peak_corrupt_value(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_value: float,
) -> None:
    """Code review (2026-05-11): hydrate returns a non-finite or negative seed.

    Without this guard, a corrupt ``peak_intervals`` row whose ``avg_power_kw``
    propagates through SQLite's ``MAX(...)`` (e.g. ``+inf``) would log the
    success event ``monthly_peak_hydrated`` and only crash in the
    ``ControlLoop.__init__`` validator — outside step 4c's ``try:`` — surfacing
    as a raw ``ValueError`` traceback rather than a structured ``startup_failed``
    log. The lifespan now validates the seed in-place and fails loud with
    ``reason="monthly_peak_corrupt_value"``, symmetric with steps 4a / 4b.
    """
    structlog.reset_defaults()

    db_path = str(tmp_path / "monthly_peak_corrupt.db")
    _seed_lifespan_env(monkeypatch, db_path)

    app = create_app()
    mock_logger = MagicMock()

    with (
        patch.object(
            EnergyRepo,
            "get_current_monthly_peak_kw",
            new_callable=AsyncMock,
            return_value=corrupt_value,
        ),
        patch("open_ems.web.app.logger", mock_logger),
        patch(
            "open_ems.web.app.close_database",
            wraps=real_close_database,
        ) as mock_close_database,
    ):
        with pytest.raises(SystemExit) as exc_info:
            async with lifespan(app):
                pass

    assert exc_info.value.code == 1
    error_calls = [
        c for c in mock_logger.error.call_args_list if c.args and c.args[0] == "startup_failed"
    ]
    assert error_calls, "expected a startup_failed error log on corrupt seed value"
    last_call = error_calls[-1]
    assert last_call.kwargs.get("reason") == "monthly_peak_corrupt_value"
    assert last_call.kwargs.get("component") == "startup"
    # Structured-log carries the offending value for forensic diagnosis.
    logged_value = last_call.kwargs.get("initial_monthly_peak_kw")
    assert logged_value == corrupt_value or (
        isinstance(logged_value, float) and math.isnan(logged_value) and math.isnan(corrupt_value)
    )
    # No success log fired on this branch — guard runs before the info emit.
    hydrated_calls = [
        c
        for c in mock_logger.info.call_args_list
        if c.args and c.args[0] == "monthly_peak_hydrated"
    ]
    assert not hydrated_calls, (
        f"monthly_peak_hydrated must NOT fire on the corrupt-value branch; got {hydrated_calls}"
    )
    # close_database MUST run on the corrupt-value failure path (mirrors AC5).
    mock_close_database.assert_awaited_once()
