from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

import aiosqlite
import structlog
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError

from open_ems.core import StateStore
from open_ems.engine.control_loop import ControlLoop
from open_ems.engine.intent_executor import IntentExecutor
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.engine.retry_policy import RetryPolicy
from open_ems.logging_config import configure_logging
from open_ems.services.audit_log import ObservabilityService
from open_ems.services.loop_liveness import LoopLiveness
from open_ems.services.readiness import mark_ready, sd_notify
from open_ems.services.time_sync import check_clock
from open_ems.services.watchdog import get_watchdog_interval, watchdog_task
from open_ems.settings import get_settings
from open_ems.storage.database import close_database, init_database
from open_ems.storage.repositories.energy_repo import EnergyRepo
from open_ems.storage.repositories.event_log_repo import EventLogRepo
from open_ems.storage.repositories.session_repo import SessionRepo
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.csrf import CsrfMiddleware
from open_ems.web.routes.auth import router as auth_router
from open_ems.web.routes.fragments import router as fragments_router
from open_ems.web.routes.health import router as health_router
from open_ems.web.routes.homeowner import router as homeowner_router
from open_ems.web.routes.installer import router as installer_router
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

    # Step 2: Run Alembic migrations — fatal on failure (exits with code 1)
    db_url = f"sqlite:///{settings.db_path}"
    try:
        await asyncio.to_thread(_run_alembic_upgrade, db_url, settings.alembic_ini_path)
        logger.info("migrations_applied", component="startup")
    except Exception:
        logger.error("migration_failed", exc_info=True, component="startup")
        raise SystemExit(1) from None

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

    # Step 4: Open database connection
    await init_database(settings.db_path)

    _watchdog_task: asyncio.Task[None] | None = None
    _cleanup_task: asyncio.Task[None] | None = None
    _pruning_task: asyncio.Task[None] | None = None
    _control_loop_task: asyncio.Task[None] | None = None
    try:
        # Step 5b: Admin bootstrap — create initial admin if no users exist
        await _bootstrap_admin_if_needed(UserRepo(), settings.initial_admin_password)

        # Step 5: Mark system ready (sends sd_notify READY=1 internally)
        mark_ready()
        logger.info("system_ready", component="startup")

        # Construct shared services BEFORE the watchdog and control loop start so both
        # share the same LoopLiveness signal and ObservabilityService instance.
        observability = ObservabilityService()
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

        intent_executor = IntentExecutor()
        policy_guard = PolicyGuard(
            state_store=app.state.state_store,
            adapters={},
            observability=observability,
            settings=settings,
        )
        retry_policy = RetryPolicy(
            policy_guard=policy_guard,
            observability=observability,
            settings=settings,
        )
        _control_loop = ControlLoop(
            state_store=app.state.state_store,
            adapters={},
            energy_repo=EnergyRepo(),
            settings=settings,
            intent_executor=intent_executor,
            retry_policy=retry_policy,
            loop_liveness=loop_liveness,
            observability=observability,
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
        await close_database()


def create_app() -> FastAPI:
    app = FastAPI(title="OPEN-EMS", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(installer_router)
    app.include_router(homeowner_router)
    app.include_router(stream_router)
    app.include_router(fragments_router)
    app.add_middleware(CsrfMiddleware)  # Runs first on every request
    return app
