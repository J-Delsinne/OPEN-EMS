"""Add role columns to device_registry and step_2_* columns to wizard_state

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-11

Story 9.2: extends ``device_registry`` with a nullable ``role`` column (single
source of truth for role assignments — no parallel join table) and extends
``wizard_state`` with Step 2 completion + per-session acknowledged role-gap
labels.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("device_registry") as batch_op:
        batch_op.add_column(
            sa.Column("role", sa.Text(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("role_assigned_at", sa.Text(), nullable=True),
        )
        batch_op.create_check_constraint(
            "ck_device_registry_role_allowed",
            "role IS NULL OR role IN ('inverter', 'battery', 'ev_charger', 'grid_meter')",
        )
        batch_op.create_check_constraint(
            "ck_device_registry_role_assigned_at_pair",
            "(role IS NULL AND role_assigned_at IS NULL)"
            " OR (role IS NOT NULL AND role_assigned_at IS NOT NULL)",
        )

    op.create_index(
        "ix_device_registry_role",
        "device_registry",
        ["role"],
        sqlite_where=sa.text("role IS NOT NULL"),
    )

    with op.batch_alter_table("wizard_state") as batch_op:
        batch_op.add_column(
            sa.Column(
                "step_2_complete",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        batch_op.add_column(
            sa.Column("step_2_completed_at", sa.Text(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("step_2_acknowledged_gaps", sa.Text(), nullable=True),
        )
        batch_op.create_check_constraint(
            "ck_wizard_state_step_2_complete_bool",
            "step_2_complete IN (0, 1)",
        )


def downgrade() -> None:
    with op.batch_alter_table("wizard_state") as batch_op:
        batch_op.drop_constraint("ck_wizard_state_step_2_complete_bool", type_="check")
        batch_op.drop_column("step_2_acknowledged_gaps")
        batch_op.drop_column("step_2_completed_at")
        batch_op.drop_column("step_2_complete")

    op.drop_index("ix_device_registry_role", table_name="device_registry")
    with op.batch_alter_table("device_registry") as batch_op:
        batch_op.drop_constraint("ck_device_registry_role_assigned_at_pair", type_="check")
        batch_op.drop_constraint("ck_device_registry_role_allowed", type_="check")
        batch_op.drop_column("role_assigned_at")
        batch_op.drop_column("role")
