from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from pydantic import ValidationError

from open_ems.logging_config import configure_logging
from open_ems.services.readiness import mark_ready, sd_notify
from open_ems.services.time_sync import check_clock
from open_ems.services.watchdog import get_watchdog_interval, watchdog_task
from open_ems.settings import get_settings
from open_ems.storage.database import close_database, init_database
from open_ems.web.routes.health import router as health_router

logger = structlog.get_logger(__name__)

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _run_alembic_upgrade(db_url: str) -> None:
    """Synchronous Alembic upgrade — must run in a thread from the async lifespan."""
    from alembic import command as alembic_command
    from alembic.config import Config

    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(cfg, "head")


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
        await asyncio.to_thread(_run_alembic_upgrade, db_url)
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

    # Step 4: Open database connection
    await init_database(settings.db_path)

    # Step 5: Mark system ready (sends sd_notify READY=1 internally)
    mark_ready()
    logger.info("system_ready", component="startup")

    # Step 6: Start watchdog heartbeat (active only when WATCHDOG_USEC is set by systemd)
    _watchdog_task: asyncio.Task[None] | None = None
    interval = get_watchdog_interval()
    if interval is not None:

        def _on_watchdog_done(t: asyncio.Task[None]) -> None:
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.error("watchdog_task_died", exc_info=exc, component="watchdog")

        _watchdog_task = asyncio.create_task(watchdog_task(interval))
        _watchdog_task.add_done_callback(_on_watchdog_done)
        logger.info("watchdog_started", interval_seconds=round(interval, 3), component="startup")

    yield  # Application serves requests here

    # Shutdown: signal stopping so systemd resets the watchdog timer during WAL checkpoint
    sd_notify("STOPPING=1")

    if _watchdog_task is not None:
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except asyncio.CancelledError:
            pass

    await close_database()


def create_app() -> FastAPI:
    app = FastAPI(title="OPEN-EMS", lifespan=lifespan)
    app.include_router(health_router)
    return app
