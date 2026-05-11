import pathlib
import sqlite3
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


def test_config_audit_log_schema_created(tmp_path: pathlib.Path) -> None:
    db_path = tmp_path / "schema_test.db"
    alembic_command.upgrade(_make_cfg(f"sqlite:///{db_path}"), "head")

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]: row[2] for row in conn.execute("PRAGMA table_info(config_audit_log)").fetchall()
        }
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(config_audit_log)").fetchall()}

    assert columns == {
        "id": "INTEGER",
        "actor": "TEXT",
        "timestamp": "TEXT",
        "field": "TEXT",
        "previous_value": "TEXT",
        "new_value": "TEXT",
        "config_version": "INTEGER",
    }
    assert {
        "ix_config_audit_log_timestamp",
        "ix_config_audit_log_config_version",
        "ix_config_audit_log_field",
    }.issubset(indexes)


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


def test_device_registry_schema_created(tmp_path: pathlib.Path) -> None:
    """Story 9.1 AC1: ``device_registry`` schema matches the migration."""
    db_path = tmp_path / "schema_test.db"
    alembic_command.upgrade(_make_cfg(f"sqlite:///{db_path}"), "head")
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]: row[2] for row in conn.execute("PRAGMA table_info(device_registry)").fetchall()
        }
    assert columns == {
        "id": "INTEGER",
        "device_id": "TEXT",
        "protocol": "TEXT",
        "address": "TEXT",
        "model": "TEXT",
        "firmware_version": "TEXT",
        "source": "TEXT",
        "validated": "INTEGER",
        "last_capability_status": "TEXT",
        "last_limitation_reason": "TEXT",
        "first_seen_at": "TEXT",
        "last_seen_at": "TEXT",
        "installer_acknowledged_unvalidated_at": "TEXT",
    }


def test_wizard_state_schema_with_cascade_fk(tmp_path: pathlib.Path) -> None:
    """Story 9.1 AC8: ``wizard_state.session_id`` has ON DELETE CASCADE to sessions."""
    db_path = tmp_path / "wizard_state_test.db"
    alembic_command.upgrade(_make_cfg(f"sqlite:///{db_path}"), "head")
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]: row[2] for row in conn.execute("PRAGMA table_info(wizard_state)").fetchall()
        }
        fkeys = conn.execute("PRAGMA foreign_key_list(wizard_state)").fetchall()
    assert columns == {
        "id": "INTEGER",
        "session_id": "TEXT",
        "step_1_complete": "INTEGER",
        "step_1_completed_at": "TEXT",
        "last_scan_id": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    }
    # FK rows: (id, seq, table, from, to, on_update, on_delete, match)
    assert len(fkeys) == 1
    assert fkeys[0][2] == "sessions"
    assert fkeys[0][3] == "session_id"
    assert fkeys[0][4] == "id"
    assert fkeys[0][6] == "CASCADE"


def test_migration_0009_round_trips(tmp_path: pathlib.Path) -> None:
    """AC1 + dev-notes round-trip clause: upgrade head → downgrade -1 → upgrade head."""
    db_url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    alembic_command.upgrade(_make_cfg(db_url), "head")
    alembic_command.downgrade(_make_cfg(db_url), "-1")
    alembic_command.upgrade(_make_cfg(db_url), "head")
