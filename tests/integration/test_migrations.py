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
    """Story 9.1 AC1 + Story 9.2 AC1: ``device_registry`` schema matches both
    migrations (0009 + 0010)."""
    db_path = tmp_path / "schema_test.db"
    alembic_command.upgrade(_make_cfg(f"sqlite:///{db_path}"), "head")
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]: row[2] for row in conn.execute("PRAGMA table_info(device_registry)").fetchall()
        }
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(device_registry)").fetchall()}
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
        "role": "TEXT",
        "role_assigned_at": "TEXT",
    }
    assert "ix_device_registry_role" in indexes


def test_wizard_state_schema_with_cascade_fk(tmp_path: pathlib.Path) -> None:
    """Story 9.1 AC8 + Story 9.2 AC2 + Story 9.3 AC1: ``wizard_state`` schema
    matches all three migrations and ``session_id`` keeps ON DELETE CASCADE.
    """
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
        "step_2_complete": "INTEGER",
        "step_2_completed_at": "TEXT",
        "step_2_acknowledged_gaps": "TEXT",
        "step_3_complete": "INTEGER",
        "step_3_completed_at": "TEXT",
        "step_3_activated_config_version": "INTEGER",
        "step_4_complete": "INTEGER",
        "step_4_completed_at": "TEXT",
        "step_4_completed_config_version": "INTEGER",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    }
    # FK rows: (id, seq, table, from, to, on_update, on_delete, match)
    assert len(fkeys) == 1
    assert fkeys[0][2] == "sessions"
    assert fkeys[0][3] == "session_id"
    assert fkeys[0][4] == "id"
    assert fkeys[0][6] == "CASCADE"


def test_active_constraints_schema_with_ev_window(tmp_path: pathlib.Path) -> None:
    """Story 9.3 AC1: ``active_constraints`` gains nullable EV window columns."""
    db_path = tmp_path / "active_constraints_test.db"
    alembic_command.upgrade(_make_cfg(f"sqlite:///{db_path}"), "head")
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(active_constraints)").fetchall()
        }
    assert columns == {
        "id": "INTEGER",
        "peak_limit_kw": "FLOAT",
        "battery_reserve_floor_percent": "FLOAT",
        "config_version": "INTEGER",
        "activated_at": "TEXT",
        "actor": "TEXT",
        "ev_charging_window_start": "TEXT",
        "ev_charging_window_end": "TEXT",
    }


def test_draft_constraints_schema_created(tmp_path: pathlib.Path) -> None:
    """Story 9.3 AC1: ``draft_constraints`` schema + indices + FK CASCADE."""
    db_path = tmp_path / "draft_constraints_test.db"
    alembic_command.upgrade(_make_cfg(f"sqlite:///{db_path}"), "head")
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(draft_constraints)").fetchall()
        }
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(draft_constraints)").fetchall()
        }
        fkeys = conn.execute("PRAGMA foreign_key_list(draft_constraints)").fetchall()
    assert columns == {
        "id": "INTEGER",
        "session_id": "TEXT",
        "peak_limit_kw": "FLOAT",
        "battery_reserve_floor_percent": "FLOAT",
        "ev_charging_window_start": "TEXT",
        "ev_charging_window_end": "TEXT",
        "validation_status": "TEXT",
        "validation_report": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    }
    assert "ix_draft_constraints_session_id" in indexes
    assert any(idx.startswith("sqlite_autoindex_draft_constraints") for idx in indexes)
    assert len(fkeys) == 1
    assert fkeys[0][2] == "sessions"
    assert fkeys[0][3] == "session_id"
    assert fkeys[0][4] == "id"
    assert fkeys[0][6] == "CASCADE"


def test_migration_0009_round_trips(tmp_path: pathlib.Path) -> None:
    """AC1 + dev-notes round-trip clause: upgrade head → downgrade -1 → upgrade head."""
    db_url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    alembic_command.upgrade(_make_cfg(db_url), "head")
    # Stories 9.2 + 9.3 + 9.4 added three more migrations on top of 0009 —
    # go back four steps so we exercise the 0009 round-trip and re-stack on top.
    alembic_command.downgrade(_make_cfg(db_url), "-4")
    alembic_command.upgrade(_make_cfg(db_url), "head")


def test_migration_0010_round_trips(tmp_path: pathlib.Path) -> None:
    """Story 9.2 AC1: migration 0010 upgrades + downgrades cleanly."""
    db_url = f"sqlite:///{tmp_path / 'roundtrip_0010.db'}"
    alembic_command.upgrade(_make_cfg(db_url), "head")
    # Stories 9.3 + 9.4 added two more migrations on top of 0010 — go back
    # three steps so we exercise the 0010 round-trip and re-stack on top.
    alembic_command.downgrade(_make_cfg(db_url), "-3")
    alembic_command.upgrade(_make_cfg(db_url), "head")


def test_migration_0011_round_trips(tmp_path: pathlib.Path) -> None:
    """Story 9.3 AC1: migration 0011 upgrades + downgrades cleanly."""
    db_url = f"sqlite:///{tmp_path / 'roundtrip_0011.db'}"
    alembic_command.upgrade(_make_cfg(db_url), "head")
    # Story 9.4 added 0012 on top of 0011 — go back two steps so 0011's
    # round-trip is exercised, then re-stack on top.
    alembic_command.downgrade(_make_cfg(db_url), "-2")
    alembic_command.upgrade(_make_cfg(db_url), "head")


def test_migration_0012_round_trips(tmp_path: pathlib.Path) -> None:
    """Story 9.4 AC1: migration 0012 upgrades + downgrades cleanly."""
    db_url = f"sqlite:///{tmp_path / 'roundtrip_0012.db'}"
    alembic_command.upgrade(_make_cfg(db_url), "head")
    # Story 10.4 added 0013 on top of 0012 — go back two steps so 0012's
    # round-trip is exercised, then re-stack on top.
    alembic_command.downgrade(_make_cfg(db_url), "-2")
    alembic_command.upgrade(_make_cfg(db_url), "head")


def test_migration_0013_creates_energy_flow_intervals_and_weekly_energy_summary_tables_with_check_constraints(  # noqa: E501  # fmt: skip
    tmp_path: pathlib.Path,
) -> None:
    """Story 10.4 AC1 + AC2: migration 0013 creates both tables with CHECK constraints."""
    db_path = tmp_path / "migration_0013_test.db"
    alembic_command.upgrade(_make_cfg(f"sqlite:///{db_path}"), "head")

    with sqlite3.connect(db_path) as conn:
        flow_columns = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(energy_flow_intervals)").fetchall()
        }
        flow_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(energy_flow_intervals)").fetchall()
        }
        summary_columns = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(weekly_energy_summary)").fetchall()
        }

    assert flow_columns == {
        "interval_start_utc": "TEXT",
        "pv_kwh": "FLOAT",
        "battery_charged_kwh": "FLOAT",
        "battery_discharged_kwh": "FLOAT",
        "grid_imported_kwh": "FLOAT",
        "grid_exported_kwh": "FLOAT",
        "ev_charged_kwh": "FLOAT",
        "sample_count": "INTEGER",
        "data_quality": "TEXT",
    }
    assert "ix_energy_flow_intervals_interval_start_utc" in flow_indexes

    assert summary_columns == {
        "id": "INTEGER",
        "window_start_utc": "TEXT",
        "window_end_utc": "TEXT",
        "peaks_avoided_count": "INTEGER",
        "self_consumption_ratio": "FLOAT",
        "estimated_cost_savings_eur": "FLOAT",
        "data_complete_days_count": "INTEGER",
        "insufficient_history": "INTEGER",
        "computed_at": "TEXT",
    }

    # Single-row CHECK on weekly_energy_summary (id=1).
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="ck_weekly_energy_summary_single_row"):
            conn.execute(
                "INSERT INTO weekly_energy_summary"
                " (id, window_start_utc, window_end_utc, data_complete_days_count,"
                "  insufficient_history, computed_at)"
                " VALUES (2, '2026-05-05T00:00:00+00:00', '2026-05-12T00:00:00+00:00',"
                "         0, 1, '2026-05-12T00:00:00+00:00')"
            )

    # Negative kWh rejected on energy_flow_intervals.
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="ck_energy_flow_intervals_nonneg"):
            conn.execute(
                "INSERT INTO energy_flow_intervals (interval_start_utc, pv_kwh) VALUES (?, ?)",
                ("2026-05-12T00:00:00+00:00", -1.0),
            )

    # terminal-fields-iff-history-sufficient: insufficient_history=0 with NULL metrics is rejected.
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="ck_weekly_energy_summary_terminal_fields_iff_history_sufficient",
        ):
            conn.execute(
                "INSERT INTO weekly_energy_summary"
                " (id, window_start_utc, window_end_utc, peaks_avoided_count,"
                "  self_consumption_ratio, estimated_cost_savings_eur,"
                "  data_complete_days_count, insufficient_history, computed_at)"
                " VALUES (1, '2026-05-05T00:00:00+00:00', '2026-05-12T00:00:00+00:00',"
                "         NULL, NULL, NULL, 7, 0, '2026-05-12T00:00:00+00:00')"
            )

    # self_consumption_ratio out of [0, 1] rejected.
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="ck_weekly_energy_summary_terminal_fields_iff_history_sufficient",
        ):
            conn.execute(
                "INSERT INTO weekly_energy_summary"
                " (id, window_start_utc, window_end_utc, peaks_avoided_count,"
                "  self_consumption_ratio, estimated_cost_savings_eur,"
                "  data_complete_days_count, insufficient_history, computed_at)"
                " VALUES (1, '2026-05-05T00:00:00+00:00', '2026-05-12T00:00:00+00:00',"
                "         5, 1.5, 10.0, 7, 0, '2026-05-12T00:00:00+00:00')"
            )


def test_migration_0013_round_trips(tmp_path: pathlib.Path) -> None:
    """Story 10.4 AC1: migration 0013 upgrades + downgrades cleanly."""
    db_url = f"sqlite:///{tmp_path / 'roundtrip_0013.db'}"
    alembic_command.upgrade(_make_cfg(db_url), "head")
    alembic_command.downgrade(_make_cfg(db_url), "-1")
    alembic_command.upgrade(_make_cfg(db_url), "head")


def test_deployment_validation_results_schema(tmp_path: pathlib.Path) -> None:
    """Story 9.4 AC1: ``deployment_validation_results`` schema + FK SET NULL."""
    db_path = tmp_path / "dv_results_test.db"
    alembic_command.upgrade(_make_cfg(f"sqlite:///{db_path}"), "head")
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(deployment_validation_results)").fetchall()
        }
        fkeys = conn.execute("PRAGMA foreign_key_list(deployment_validation_results)").fetchall()
    assert columns == {
        "id": "INTEGER",
        "started_at": "TEXT",
        "completed_at": "TEXT",
        "config_version": "INTEGER",
        "overall_status": "TEXT",
        "checks_json": "TEXT",
        "triggered_by_session_id": "TEXT",
        "summary_text": "TEXT",
    }
    assert len(fkeys) == 1
    assert fkeys[0][2] == "sessions"
    assert fkeys[0][3] == "triggered_by_session_id"
    assert fkeys[0][4] == "id"
    # SET NULL — the validation result survives session deletion; only
    # session attribution becomes NULL.
    assert fkeys[0][6] == "SET NULL"


def test_deployment_validation_acks_schema(tmp_path: pathlib.Path) -> None:
    """Story 9.4 AC1: ``deployment_validation_acks`` schema + CASCADE FK."""
    db_path = tmp_path / "dv_acks_test.db"
    alembic_command.upgrade(_make_cfg(f"sqlite:///{db_path}"), "head")
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(deployment_validation_acks)").fetchall()
        }
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(deployment_validation_acks)").fetchall()
        }
        fkeys = conn.execute("PRAGMA foreign_key_list(deployment_validation_acks)").fetchall()
    assert columns == {
        "id": "INTEGER",
        "validation_result_id": "INTEGER",
        "check_name": "TEXT",
        "acknowledged_at": "TEXT",
        "acknowledged_by_session_id": "TEXT",
    }
    assert "ix_deployment_validation_acks_validation_result_id" in indexes
    # Two FKs: validation_result_id (CASCADE), acknowledged_by_session_id (SET NULL)
    fkey_targets = {(fk[2], fk[6]) for fk in fkeys}
    assert ("deployment_validation_results", "CASCADE") in fkey_targets
    assert ("sessions", "SET NULL") in fkey_targets


def test_role_enum_values_stable_across_migrations(tmp_path: pathlib.Path) -> None:
    """Story 9.2 R6 drift call-out: ``DeviceRole`` string values are part of
    the public API of the DB schema and MUST NOT change without a migration."""
    from open_ems.core.devices import DeviceRole

    db_path = tmp_path / "enum_stable.db"
    alembic_command.upgrade(_make_cfg(f"sqlite:///{db_path}"), "head")
    expected = {"inverter", "battery", "ev_charger", "grid_meter"}
    assert {member.value for member in DeviceRole} == expected
    # The CHECK constraint must accept exactly these values.
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for value in expected:
            conn.execute(
                "INSERT INTO device_registry"
                " (device_id, protocol, address, source, validated, first_seen_at,"
                "  role, role_assigned_at)"
                " VALUES (?, 'modbus_tcp', '10.0.0.1:502', 'manual_entry', 0,"
                "         '2026-05-11T12:00:00+00:00', ?, '2026-05-11T12:00:00+00:00')",
                (f"dev-{value}", value),
            )
        conn.commit()
