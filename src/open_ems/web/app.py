from __future__ import annotations

import asyncio
import math
import pathlib
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

import aiosqlite
import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr, ValidationError

from open_ems.adapters.capabilities import (
    CapabilityRegistryDriftError,
    validate_capability_registry_alignment,
)
from open_ems.adapters.discovery import DiscoveryService
from open_ems.adapters.ocpp.central_system import OCPPCentralSystem
from open_ems.core import StateStore
from open_ems.engine.control_loop import ControlLoop
from open_ems.engine.intent_executor import IntentExecutor
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.engine.retry_policy import RetryPolicy
from open_ems.logging_config import configure_logging
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.services.audit_log import ObservabilityService
from open_ems.services.constraints import ConstraintsService
from open_ems.services.deployment_validation import DeploymentValidationService
from open_ems.services.device_discovery import DeviceDiscoveryOrchestrator
from open_ems.services.loop_liveness import LoopLiveness
from open_ems.services.manual_entry import ManualEntryService
from open_ems.services.protocol_adapter_factory import ProtocolAdapterFactory
from open_ems.services.readiness import mark_ready, sd_notify
from open_ems.services.role_assignment import RoleAssignmentService
from open_ems.services.runtime_adapter_wiring import (
    RuntimeAdapterMap,
    RuntimeAdapterWiringError,
    build_runtime_adapter_map,
)
from open_ems.services.time_sync import check_clock
from open_ems.services.watchdog import get_watchdog_interval, watchdog_task
from open_ems.settings import get_settings
from open_ems.storage.database import close_database, init_database
from open_ems.storage.repositories.config_repo import ConfigRepo
from open_ems.storage.repositories.deployment_validation_repo import (
    DeploymentValidationResultRepo,
)
from open_ems.storage.repositories.device_repo import DeviceRepo
from open_ems.storage.repositories.draft_constraints_repo import DraftConstraintsRepo
from open_ems.storage.repositories.energy_repo import EnergyRepo
from open_ems.storage.repositories.event_log_repo import EventLogRepo
from open_ems.storage.repositories.session_repo import SessionRepo
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.storage.repositories.wizard_state_repo import WizardStateRepo
from open_ems.web.csrf import CsrfMiddleware
from open_ems.web.routes.actions import router as actions_router
from open_ems.web.routes.auth import router as auth_router
from open_ems.web.routes.fragments import router as fragments_router
from open_ems.web.routes.health import router as health_router
from open_ems.web.routes.homeowner import router as homeowner_router
from open_ems.web.routes.installer import router as installer_router
from open_ems.web.routes.setup import router as setup_router
from open_ems.web.routes.stream import router as stream_router

logger = structlog.get_logger(__name__)


async def _bootstrap_admin_if_needed(
    user_repo: UserRepo, initial_password: SecretStr | None
) -> None:
    """Create the initial admin user if the users table is empty."""
    if await user_repo.count() > 0:
        return
    if initial_password is None:
        logger.error(
            "startup_failed",
            reason="INITIAL_ADMIN_PASSWORD required when no users exist",
            component="startup",
        )
        raise SystemExit(1) from None
    hashed = hash_password(initial_password.get_secret_value())
    try:
        await user_repo.create(
            username="admin",
            hashed_password=hashed,
            role="installer",
            must_change_password=True,
        )
    except aiosqlite.IntegrityError:
        return  # Concurrent startup already created the admin
    logger.info(
        "admin_bootstrapped",
        username="admin",
        must_change_password=True,
        component="startup",
    )


def _locate_project_root() -> pathlib.Path:
    """Walk parent directories to find alembic.ini (project root marker)."""
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "alembic.ini").exists():
            return parent
    raise RuntimeError(
        "Cannot locate project root: alembic.ini not found in any parent directory. "
        "Verify the package is installed from the correct source tree."
    )


def _run_alembic_upgrade(db_url: str, alembic_ini_path: str | None = None) -> None:
    """Synchronous Alembic upgrade — must run in a thread from the async lifespan."""
    from alembic import command as alembic_command
    from alembic.config import Config

    if alembic_ini_path is not None:
        cfg = Config(alembic_ini_path)
    else:
        project_root = _locate_project_root()
        cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(cfg, "head")


async def _session_cleanup_task() -> None:
    """Prune all expired sessions every 24 hours, starting immediately at startup."""
    while True:
        try:
            count = await SessionRepo().delete_all_expired()
            logger.info("session_cleanup", component="auth", deleted_count=count)
        except Exception:
            logger.error("session_cleanup_failed", exc_info=True, component="auth")
        await asyncio.sleep(24 * 60 * 60)


async def _event_log_pruning_task() -> None:
    """Prune non-critical event_log entries older than 90 days every 24 hours."""
    while True:
        try:
            count = await EventLogRepo().prune_expired()
            logger.info("event_log_pruned", component="observability", deleted_count=count)
        except Exception:
            logger.error("event_log_pruning_failed", exc_info=True, component="observability")
        await asyncio.sleep(24 * 60 * 60)


def make_on_control_loop_done(
    loop_liveness: LoopLiveness,
    observability: ObservabilityService,
) -> Callable[[asyncio.Task[None]], None]:
    """Build the ``_on_control_loop_done`` callback used by the lifespan.

    Extracted as a module-level factory so the integration test can exercise
    the real production callback rather than reimplementing its semantics.
    """

    def _on_control_loop_done(t: asyncio.Task[None]) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            return
        logger.error("control_loop_died", exc_info=exc, component="engine")
        loop_liveness.mark_crashed(exc)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return  # process is exiting; only the structured log is recorded

        async def _emit_crash_audit() -> None:
            try:
                await observability.audit(
                    actor="system",
                    event_type="SYSTEM",
                    summary=(
                        f"Control loop crashed: {type(exc).__name__}: {exc!r}; "
                        "entering fail-safe pending supervisor restart"
                    ),
                )
            except Exception as audit_exc:  # noqa: BLE001
                logger.error(
                    "audit_emit_failed",
                    component="engine",
                    error=repr(audit_exc),
                    exc_info=True,
                )

        running.create_task(_emit_crash_audit())

    return _on_control_loop_done


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    try:
        settings = get_settings()
    except ValidationError as exc:
        configure_logging("INFO")
        errors = exc.errors()
        missing = [str(err["loc"][0]) for err in errors if err["type"] == "missing" and err["loc"]]
        invalid = [
            f"{err['loc'][0] if err['loc'] else '(root)'}: {err['msg']}"
            for err in errors
            if err["type"] != "missing"
        ]
        logger.error(
            "startup_failed",
            reason="invalid_config",
            missing_fields=missing,
            invalid_fields=invalid,
            component="startup",
        )
        raise SystemExit(1) from exc

    # Step 1: Configure logging — all subsequent logs must be JSON
    configure_logging(settings.log_level)

    # Step 3: Check system clock / NTP (blocking UDP — run in thread)
    clock_status = await asyncio.to_thread(
        check_clock, settings.ntp_host, settings.ntp_drift_threshold_seconds
    )
    if clock_status != "valid":
        logger.warning(
            f"clock_{clock_status}",
            system_clock_status=clock_status,
            component="startup",
        )
    app.state.state_store = StateStore(
        system_clock_status=clock_status,
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )

    _watchdog_task: asyncio.Task[None] | None = None
    _cleanup_task: asyncio.Task[None] | None = None
    _pruning_task: asyncio.Task[None] | None = None
    _control_loop_task: asyncio.Task[None] | None = None
    db_initialized = False
    try:
        # Step 2: Run Alembic migrations — fatal on failure (exits with code 1).
        # Inside the outer try so close_database() in the `finally` cleans up
        # any partial state if a later step raises before we've reached `yield`.
        db_url = f"sqlite:///{settings.db_path}"
        try:
            await asyncio.to_thread(_run_alembic_upgrade, db_url, settings.alembic_ini_path)
            logger.info("migrations_applied", component="startup")
        except Exception:
            logger.error("migration_failed", exc_info=True, component="startup")
            raise SystemExit(1) from None

        # Step 4: Open database connection
        await init_database(settings.db_path)
        db_initialized = True

        # Step 4a: Story 9.0c (AC5) — fail-loud cross-validation of adapter
        # ``_SUPPORTED_MODELS`` against the capability registry. Drift here
        # would otherwise silently degrade write commands to ``capability_missing``
        # rejections at runtime. Same enforcement class as migration failure.
        try:
            validate_capability_registry_alignment()
        except CapabilityRegistryDriftError as drift_exc:
            logger.error(
                "startup_failed",
                reason="capability_registry_drift",
                missing_models=drift_exc.missing_models,
                component="startup",
            )
            raise SystemExit(1) from None

        # Step 4b: Hydrate the active-constraints provider BEFORE any consumer
        # (PolicyGuard / ControlLoop) is constructed. A failure here means the
        # process cannot determine its safety constraints — fail loud (Story
        # 9.0b AC7), matching the migration-failure handling pattern. Story
        # 9.3 reuses this same ConfigRepo instance for the staged-constraint
        # service so all writes share the database-module write lock.
        config_repo = ConfigRepo()
        try:
            active_constraints_provider = ActiveConstraintsProvider(
                repo=config_repo, settings=settings
            )
            await active_constraints_provider.hydrate()
        except Exception:
            logger.error(
                "startup_failed",
                reason="constraints_hydrate_failed",
                exc_info=True,
                component="startup",
            )
            raise SystemExit(1) from None
        app.state.active_constraints_provider = active_constraints_provider

        # Step 4c (Story 9.0d): hydrate the control loop's monthly-peak cache from
        # peak_intervals BEFORE constructing the loop. Without this seed,
        # _monthly_peak_kw stays 0.0 until the first interval rollover, so the first
        # ≤15 minutes of peak-limit decisions after every restart silently use stale
        # data. The installer wizard triggers restarts; this window is user-visible.
        energy_repo = EnergyRepo()
        try:
            initial_monthly_peak_kw = await energy_repo.get_current_monthly_peak_kw()
        except Exception:
            logger.error(
                "startup_failed",
                reason="monthly_peak_hydrate_failed",
                exc_info=True,
                component="startup",
            )
            raise SystemExit(1) from None
        if initial_monthly_peak_kw < 0.0 or not math.isfinite(initial_monthly_peak_kw):
            logger.error(
                "startup_failed",
                reason="monthly_peak_corrupt_value",
                initial_monthly_peak_kw=initial_monthly_peak_kw,
                component="startup",
            )
            raise SystemExit(1) from None
        logger.info(
            "monthly_peak_hydrated",
            initial_monthly_peak_kw=initial_monthly_peak_kw,
            component="startup",
        )

        # Step 4d (Story 9.1): construct the installer-wizard service objects.
        # All are stateless or in-memory caches; no DB I/O at construction time.
        # Story 9.X: ``OCPPCentralSystem`` is constructed here (was ``None`` at
        # discovery_orchestrator + protocol_adapter_factory before). It is the
        # single canonical OCPP registry shared by validation-time probing
        # (ProtocolAdapterFactory), wizard discovery, and runtime adapter
        # wiring (build_runtime_adapter_map at step 4h).
        device_repo = DeviceRepo()
        wizard_state_repo = WizardStateRepo()
        discovery_service = DiscoveryService()
        ocpp_central_system = OCPPCentralSystem()
        discovery_orchestrator = DeviceDiscoveryOrchestrator(
            discovery_service=discovery_service,
            ocpp_central_system=ocpp_central_system,
            registry_provider=device_repo,  # DeviceRepo.list_all() satisfies the protocol
        )
        manual_entry_service = ManualEntryService(
            device_repo=device_repo,
            discovery_service=discovery_service,
        )
        # Step 4e (Story 9.2): construct the role-assignment evaluator. The
        # object is stateless; no DB I/O at construction (first read happens on
        # the first installer request, post-readiness).
        role_assignment_service = RoleAssignmentService(device_repo=device_repo)
        # Step 4f (Story 9.3): construct the staged-constraint draft repo +
        # service. Reuses the SAME ConfigRepo instance the provider already
        # references so all writes share the database-module write lock.
        # Stateless wiring; no DB I/O at construction.
        draft_constraints_repo = DraftConstraintsRepo()
        constraints_service = ConstraintsService(
            draft_repo=draft_constraints_repo,
            config_repo=config_repo,
            active_constraints_provider=active_constraints_provider,
            wizard_state_repo=wizard_state_repo,
            device_repo=device_repo,
            state_store=app.state.state_store,
        )
        # Step 4g (Story 9.4): construct the deployment-validation evaluator.
        # Reuses the same active_constraints_provider, device_repo,
        # wizard_state_repo, and constraints_service (for the safety_pre_check
        # reuse seam — AC5 check 5) the prior steps already constructed. The
        # ProtocolAdapterFactory remains validation-only by design — its probe
        # path goes through DiscoveryService read-only primitives and it does
        # not issue send_command. Runtime control wiring is owned by
        # build_runtime_adapter_map at step 4h (Story 9.X, closed 2026-05-11).
        deployment_validation_repo = DeploymentValidationResultRepo()
        protocol_adapter_factory = ProtocolAdapterFactory(
            discovery=discovery_service,
            ocpp_central_system=ocpp_central_system,
        )
        # D3/D4 — DeploymentValidationService now takes the StateStore (snapshot
        # is captured at run start) and ObservabilityService (structured audit
        # log attribution for run/ack/handoff events) as documented in the
        # amended AC4 / AC10. ObservabilityService is constructed once here so
        # the validation, watchdog, and control-loop services share the same
        # sink.
        observability = ObservabilityService()
        app.state.observability = observability
        deployment_validation_service = DeploymentValidationService(
            validation_repo=deployment_validation_repo,
            device_repo=device_repo,
            wizard_state_repo=wizard_state_repo,
            active_constraints_provider=active_constraints_provider,
            constraints_service=constraints_service,
            protocol_adapter_factory=protocol_adapter_factory,
            state_store=app.state.state_store,
            observability=observability,
            check_timeout_seconds=settings.deployment_validation_check_timeout_seconds,
            device_probe_timeout_seconds=settings.deployment_validation_device_probe_timeout_seconds,
        )
        app.state.device_repo = device_repo
        app.state.wizard_state_repo = wizard_state_repo
        app.state.discovery_orchestrator = discovery_orchestrator
        app.state.manual_entry_service = manual_entry_service
        app.state.role_assignment_service = role_assignment_service
        app.state.draft_constraints_repo = draft_constraints_repo
        app.state.constraints_service = constraints_service
        app.state.deployment_validation_repo = deployment_validation_repo
        app.state.protocol_adapter_factory = protocol_adapter_factory
        app.state.deployment_validation_service = deployment_validation_service
        logger.info("device_registry_ready", component="startup")
        logger.info("wizard_state_ready", component="startup")
        logger.info("discovery_orchestrator_ready", component="startup")
        logger.info("role_assignment_service_ready", component="startup")
        logger.info("draft_constraints_repo_ready", component="startup")
        logger.info("constraints_service_ready", component="startup")
        logger.info("deployment_validation_repo_ready", component="startup")
        logger.info("deployment_validation_service_ready", component="startup")

        # Step 5b: Admin bootstrap — create initial admin if no users exist
        await _bootstrap_admin_if_needed(UserRepo(), settings.initial_admin_password)

        # Step 5: Mark system ready (sends sd_notify READY=1 internally)
        mark_ready()
        logger.info("system_ready", component="startup")

        # Construct LoopLiveness — ObservabilityService is already constructed
        # above (Step 4g) and shared with the deployment-validation service.
        loop_liveness = LoopLiveness(
            missed_cycle_threshold_seconds=(
                settings.watchdog_missed_cycle_threshold * settings.control_loop_interval_seconds
            ),
            cycle_deadline_seconds=settings.watchdog_cycle_deadline_seconds,
            cycle_interval_seconds=settings.control_loop_interval_seconds,
        )
        app.state.loop_liveness = loop_liveness

        # Step 6: Start the watchdog/stall-monitor task.
        # It ALWAYS runs (Docker parity); whether it sends sd_notify is decided by
        # whether systemd configured WATCHDOG_USEC for this process.
        systemd_interval = get_watchdog_interval()
        if systemd_interval is not None:
            watchdog_interval = systemd_interval
            send_sd_notify = True
        else:
            watchdog_interval = settings.control_loop_interval_seconds
            send_sd_notify = False

        def _on_watchdog_done(t: asyncio.Task[None]) -> None:
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.error("watchdog_task_died", exc_info=exc, component="watchdog")

        _watchdog_task = asyncio.create_task(
            watchdog_task(
                watchdog_interval,
                loop_liveness=loop_liveness,
                observability=observability,
                send_sd_notify=send_sd_notify,
            )
        )
        _watchdog_task.add_done_callback(_on_watchdog_done)
        logger.info(
            "watchdog_started" if send_sd_notify else "stall_monitor_started",
            interval_seconds=round(watchdog_interval, 3),
            send_sd_notify=send_sd_notify,
            component="startup",
        )

        def _on_cleanup_done(t: asyncio.Task[None]) -> None:
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.error("session_cleanup_task_died", exc_info=exc, component="auth")

        _cleanup_task = asyncio.create_task(_session_cleanup_task())
        _cleanup_task.add_done_callback(_on_cleanup_done)
        logger.info("session_cleanup_task_started", component="auth")

        def _on_pruning_done(t: asyncio.Task[None]) -> None:
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.error(
                        "event_log_pruning_task_died",
                        exc_info=exc,
                        component="observability",
                    )

        _pruning_task = asyncio.create_task(_event_log_pruning_task())
        _pruning_task.add_done_callback(_on_pruning_done)
        logger.info("event_log_pruning_task_started", component="observability")

        # Step 4h (Story 9.X): build the runtime adapter map from device_registry.
        # Sourced from DeviceRepo.list_all() filtered to (validated=True AND
        # role is not None). Closes Story 8-2's [app.py:223-236] deferred
        # finding ("adapters={} in production silently rejects all commands").
        try:
            runtime_adapters: RuntimeAdapterMap = await build_runtime_adapter_map(
                device_repo=device_repo,
                ocpp_central_system=ocpp_central_system,
                settings=settings,
            )
        except RuntimeAdapterWiringError as wiring_exc:
            logger.error(
                "startup_failed",
                reason="runtime_adapter_wiring_failed",
                detail=str(wiring_exc),
                component="startup",
            )
            raise SystemExit(1) from None
        if not runtime_adapters.control_loop_adapters:
            logger.info(
                "runtime_adapter_map_empty",
                reason="pre_installer_wizard_complete",
                component="startup",
            )
        else:
            logger.info(
                "runtime_adapter_map_built",
                policy_guard_role_count=len(runtime_adapters.policy_guard_adapters),
                control_loop_role_count=len(runtime_adapters.control_loop_adapters),
                adapters=[
                    {
                        "role": role.value,
                        "protocol": runtime_adapters.protocols_by_role[role],
                        "device_id": adapter.device_id,
                    }
                    for role, adapter in runtime_adapters.control_loop_adapters.items()
                ],
                component="startup",
            )
        app.state.runtime_adapters = runtime_adapters

        # Step 4h.1 (Story 9.X): start every wired adapter. DSMR's read loop
        # is created by ``GridMeterAdapter.connect() → DSMRAdapter.start()``;
        # without this call grid_meter telemetry stays ``dsmr_unavailable``
        # forever because ``_received_at`` is never set. Modbus and OCPP
        # connects are no-ops (lazy / charger-initiated), but driving every
        # adapter through ``connect()`` keeps the lifecycle symmetric with
        # the shutdown ``disconnect()`` loop and avoids per-adapter special
        # casing.
        for role, adapter in runtime_adapters.control_loop_adapters.items():
            try:
                await adapter.connect()
            except Exception:  # noqa: BLE001 — best-effort; one adapter's connect failure must not block siblings
                logger.warning(
                    "adapter_connect_failed",
                    role=role.value,
                    device_id=adapter.device_id,
                    component="startup",
                    exc_info=True,
                )

        intent_executor = IntentExecutor()
        policy_guard = PolicyGuard(
            state_store=app.state.state_store,
            adapters=runtime_adapters.policy_guard_adapters,
            observability=observability,
            settings=settings,
            active_constraints=active_constraints_provider,
        )
        app.state.policy_guard = policy_guard
        retry_policy = RetryPolicy(
            policy_guard=policy_guard,
            observability=observability,
            settings=settings,
        )
        _control_loop = ControlLoop(
            state_store=app.state.state_store,
            adapters=runtime_adapters.control_loop_adapters,
            energy_repo=energy_repo,
            settings=settings,
            intent_executor=intent_executor,
            retry_policy=retry_policy,
            loop_liveness=loop_liveness,
            observability=observability,
            active_constraints=active_constraints_provider,
            initial_monthly_peak_kw=initial_monthly_peak_kw,
        )

        _control_loop_task = asyncio.create_task(_control_loop.run(), name="control_loop")
        _control_loop_task.add_done_callback(
            make_on_control_loop_done(loop_liveness, observability)
        )
        logger.info("control_loop_started", component="engine")

        yield  # Application serves requests here

        # Shutdown: signal stopping so systemd resets the watchdog timer during WAL checkpoint
        sd_notify("STOPPING=1")

        if _control_loop_task is not None:
            _control_loop_task.cancel()
            try:
                await _control_loop_task
            except asyncio.CancelledError:
                pass

        # Step 4h shutdown (Story 9.X AC10): close every adapter in role order.
        # Each disconnect is isolated AND bounded by a per-adapter timeout so
        # one slow/failed disconnect does not block the rest and does not
        # eat the systemd STOPSIGTERM grace window.
        for role, adapter in runtime_adapters.control_loop_adapters.items():
            try:
                await asyncio.wait_for(adapter.disconnect(), timeout=5.0)
            except TimeoutError:
                logger.warning(
                    "adapter_disconnect_timeout",
                    role=role.value,
                    device_id=adapter.device_id,
                    component="shutdown",
                    timeout_seconds=5.0,
                )
            except Exception:  # noqa: BLE001 — best-effort teardown; one failure must not block siblings
                logger.warning(
                    "adapter_disconnect_failed",
                    role=role.value,
                    device_id=adapter.device_id,
                    component="shutdown",
                    exc_info=True,
                )

        if _watchdog_task is not None:
            _watchdog_task.cancel()
            try:
                await _watchdog_task
            except asyncio.CancelledError:
                pass

        if _cleanup_task is not None:
            _cleanup_task.cancel()
            try:
                await _cleanup_task
            except asyncio.CancelledError:
                pass

        if _pruning_task is not None:
            _pruning_task.cancel()
            try:
                await _pruning_task
            except asyncio.CancelledError:
                pass
    finally:
        if db_initialized:
            await close_database()


def create_app() -> FastAPI:
    app = FastAPI(title="OPEN-EMS", lifespan=lifespan)
    static_dir = pathlib.Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(installer_router)
    app.include_router(setup_router)
    app.include_router(homeowner_router)
    app.include_router(actions_router)
    app.include_router(stream_router)
    app.include_router(fragments_router)
    app.add_middleware(CsrfMiddleware)  # Runs first on every request
    return app
