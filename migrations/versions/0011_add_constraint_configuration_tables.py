"""Add draft_constraints + active_constraints EV columns + wizard_state step_3_*

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-11

Story 9.3: extends ``active_constraints`` with optional ``ev_charging_window_*``
columns, adds the per-session ``draft_constraints`` table that backs the
staged validate→activate flow, and extends ``wizard_state`` with Step 3
completion + the link to the activated ``config_version``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("active_constraints") as batch_op:
        batch_op.add_column(
            sa.Column("ev_charging_window_start", sa.Text(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("ev_charging_window_end", sa.Text(), nullable=True),
        )
        batch_op.create_check_constraint(
            "ck_active_constraints_ev_window_pair",
            "(ev_charging_window_start IS NULL AND ev_charging_window_end IS NULL)"
            " OR (ev_charging_window_start IS NOT NULL"
            "     AND ev_charging_window_end IS NOT NULL)",
        )

    op.create_table(
        "draft_constraints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("peak_limit_kw", sa.Float(), nullable=False),
        sa.Column("battery_reserve_floor_percent", sa.Float(), nullable=False),
        sa.Column("ev_charging_window_start", sa.Text(), nullable=True),
        sa.Column("ev_charging_window_end", sa.Text(), nullable=True),
        sa.Column(
            "validation_status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("validation_report", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("session_id", name="uq_draft_constraints_session_id"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
            name="fk_draft_constraints_session_id",
        ),
        sa.CheckConstraint(
            "peak_limit_kw > 0",
            name="ck_draft_constraints_peak_limit_positive",
        ),
        sa.CheckConstraint(
            "battery_reserve_floor_percent >= 0 AND battery_reserve_floor_percent <= 100",
            name="ck_draft_constraints_reserve_floor_range",
        ),
        sa.CheckConstraint(
            "validation_status IN ('pending', 'valid', 'failed')",
            name="ck_draft_constraints_validation_status_allowed",
        ),
        sa.CheckConstraint(
            "(ev_charging_window_start IS NULL AND ev_charging_window_end IS NULL)"
            " OR (ev_charging_window_start IS NOT NULL"
            "     AND ev_charging_window_end IS NOT NULL)",
            name="ck_draft_constraints_ev_window_pair",
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_draft_constraints_session_id",
        "draft_constraints",
        ["session_id"],
    )

    with op.batch_alter_table("wizard_state") as batch_op:
        batch_op.add_column(
            sa.Column(
                "step_3_complete",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        batch_op.add_column(
            sa.Column("step_3_completed_at", sa.Text(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("step_3_activated_config_version", sa.Integer(), nullable=True),
        )
        batch_op.create_check_constraint(
            "ck_wizard_state_step_3_complete_bool",
            "step_3_complete IN (0, 1)",
        )
        batch_op.create_check_constraint(
            "ck_wizard_state_step_3_pair",
            "(step_3_complete = 0"
            "  AND step_3_completed_at IS NULL"
            "  AND step_3_activated_config_version IS NULL)"
            " OR (step_3_complete = 1"
            "     AND step_3_completed_at IS NOT NULL"
            "     AND step_3_activated_config_version IS NOT NULL)",
        )


def downgrade() -> None:
    with op.batch_alter_table("wizard_state") as batch_op:
        batch_op.drop_constraint("ck_wizard_state_step_3_pair", type_="check")
        batch_op.drop_constraint("ck_wizard_state_step_3_complete_bool", type_="check")
        batch_op.drop_column("step_3_activated_config_version")
        batch_op.drop_column("step_3_completed_at")
        batch_op.drop_column("step_3_complete")

    op.drop_index("ix_draft_constraints_session_id", table_name="draft_constraints")
    op.drop_table("draft_constraints")

    with op.batch_alter_table("active_constraints") as batch_op:
        batch_op.drop_constraint("ck_active_constraints_ev_window_pair", type_="check")
        batch_op.drop_column("ev_charging_window_end")
        batch_op.drop_column("ev_charging_window_start")
