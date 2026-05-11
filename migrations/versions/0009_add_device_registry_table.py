"""Add device_registry and wizard_state tables

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_registry",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("protocol", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("firmware_version", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("validated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_capability_status", sa.Text(), nullable=True),
        sa.Column("last_limitation_reason", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=True),
        sa.Column("installer_acknowledged_unvalidated_at", sa.Text(), nullable=True),
        sa.UniqueConstraint("device_id", name="uq_device_registry_device_id"),
        sa.CheckConstraint(
            "protocol IN ('modbus_tcp', 'ocpp_1_6', 'dsmr_p1')",
            name="ck_device_registry_protocol_allowed",
        ),
        sa.CheckConstraint(
            "source IN ('manual_entry', 'ocpp_self_registration')",
            name="ck_device_registry_source_allowed",
        ),
        sa.CheckConstraint(
            "validated IN (0, 1)",
            name="ck_device_registry_validated_bool",
        ),
        sa.CheckConstraint(
            "last_capability_status IS NULL"
            " OR last_capability_status IN ('full', 'reduced', 'unsupported')",
            name="ck_device_registry_last_capability_status_allowed",
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_device_registry_protocol",
        "device_registry",
        ["protocol"],
    )

    op.create_table(
        "wizard_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("step_1_complete", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("step_1_completed_at", sa.Text(), nullable=True),
        sa.Column("last_scan_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("session_id", name="uq_wizard_state_session_id"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
            name="fk_wizard_state_session_id",
        ),
        sa.CheckConstraint(
            "step_1_complete IN (0, 1)",
            name="ck_wizard_state_step_1_complete_bool",
        ),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("wizard_state")
    op.drop_index("ix_device_registry_protocol", table_name="device_registry")
    op.drop_table("device_registry")
