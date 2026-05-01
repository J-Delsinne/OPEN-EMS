import pathlib
from unittest.mock import patch

import pytest
from alembic import command as alembic_command
from alembic.config import Config


def _make_cfg(db_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_alembic_upgrade_head_runs() -> None:
    """Alembic upgrade head against in-memory SQLite must complete without error."""
    alembic_command.upgrade(_make_cfg("sqlite:///:memory:"), "head")


def test_alembic_upgrade_head_idempotent(tmp_path: pathlib.Path) -> None:
    """Running upgrade head twice on the same file DB must not fail."""
    db_url = f"sqlite:///{tmp_path / 'idempotency_test.db'}"
    alembic_command.upgrade(_make_cfg(db_url), "head")
    alembic_command.upgrade(_make_cfg(db_url), "head")


async def test_migration_failure_triggers_system_exit() -> None:
    """When Alembic upgrade raises, the lifespan must raise SystemExit(1)."""
    import structlog

    from open_ems.web.app import create_app, lifespan

    structlog.reset_defaults()
    app = create_app()
    with patch("alembic.command.upgrade", side_effect=RuntimeError("migration failed")):
        with pytest.raises(SystemExit) as exc_info:
            async with lifespan(app):
                pass
    assert exc_info.value.code == 1
