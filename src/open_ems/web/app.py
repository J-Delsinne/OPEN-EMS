from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import aiosqlite
import structlog
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError

from open_ems.core import StateStore
from open_ems.logging_config import configure_logging
from open_ems.services.readiness import mark_ready, sd_notify
from open_ems.services.time_sync import check_clock
from open_ems.services.watchdog import get_watchdog_interval, watchdog_task
from open_ems.settings import get_settings
from open_ems.storage.database import close_database, init_database
from open_ems.storage.repositories.session_repo import SessionRepo
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.csrf import CsrfMiddleware
from open_ems.web.routes.auth import router as auth_router
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
    app.state.state_store = StateStore(system_clock_status=clock_status)

    # Step 4: Open database connection
    await init_database(settings.db_path)

    _watchdog_task: asyncio.Task[None] | None = None
    _cleanup_task: asyncio.Task[None] | None = None
    try:
        # Step 5b: Admin bootstrap — create initial admin if no users exist
        await _bootstrap_admin_if_needed(UserRepo(), settings.initial_admin_password)

        # Step 5: Mark system ready (sends sd_notify READY=1 internally)
        mark_ready()
        logger.info("system_ready", component="startup")

        # Step 6: Start watchdog heartbeat (active only when WATCHDOG_USEC is set by systemd)
        interval = get_watchdog_interval()
        if interval is not None:

            def _on_watchdog_done(t: asyncio.Task[None]) -> None:
                if not t.cancelled():
                    exc = t.exception()
                    if exc is not None:
                        logger.error("watchdog_task_died", exc_info=exc, component="watchdog")

            _watchdog_task = asyncio.create_task(watchdog_task(interval))
            _watchdog_task.add_done_callback(_on_watchdog_done)
            logger.info(
                "watchdog_started", interval_seconds=round(interval, 3), component="startup"
            )

        def _on_cleanup_done(t: asyncio.Task[None]) -> None:
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.error("session_cleanup_task_died", exc_info=exc, component="auth")

        _cleanup_task = asyncio.create_task(_session_cleanup_task())
        _cleanup_task.add_done_callback(_on_cleanup_done)
        logger.info("session_cleanup_task_started", component="auth")

        yield  # Application serves requests here

        # Shutdown: signal stopping so systemd resets the watchdog timer during WAL checkpoint
        sd_notify("STOPPING=1")

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
    finally:
        await close_database()


def create_app() -> FastAPI:
    app = FastAPI(title="OPEN-EMS", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(installer_router)
    app.include_router(homeowner_router)
    app.include_router(stream_router)
    app.add_middleware(CsrfMiddleware)  # Runs first on every request
    return app
