"""Add deployment_validation_results + deployment_validation_acks + wizard_state step_4_*

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-11

Story 9.4: introduces a single-row ``deployment_validation_results`` table
that holds the most-recent Step-4 readiness check outcome (per-check JSON,
overall status, ``config_version`` it was computed against, summary text),
a paired ``deployment_validation_acks`` table for per-warning installer
acknowledgments (UNIQUE on (validation_result_id, check_name) so an ack is
scoped to one specific WARN row on one specific result), and the
``wizard_state.step_4_*`` triple (idempotent advance marker linking the
wizard completion to the ``config_version`` at handoff time).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deployment_validation_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("overall_status", sa.Text(), nullable=False),
        sa.Column("checks_json", sa.Text(), nullable=False),
        sa.Column("triggered_by_session_id", sa.Text(), nullable=True),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["triggered_by_session_id"],
            ["sessions.id"],
            ondelete="SET NULL",
            name="fk_deployment_validation_results_session_id",
        ),
        sa.CheckConstraint(
            "config_version >= 0",
            name="ck_deployment_validation_results_config_version_nonneg",
        ),
        sa.CheckConstraint(
            "overall_status IN ('running', 'complete-PASS', 'complete-WARN', 'complete-FAIL')",
            name="ck_deployment_validation_results_overall_status_allowed",
        ),
        sa.CheckConstraint(
            "(overall_status = 'running' AND completed_at IS NULL)"
            " OR (overall_status != 'running' AND completed_at IS NOT NULL)",
            name="ck_deployment_validation_results_completed_at_pair",
        ),
        sqlite_autoincrement=True,
    )

    op.create_table(
        "deployment_validation_acks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("validation_result_id", sa.Integer(), nullable=False),
        sa.Column("check_name", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.Text(), nullable=False),
        sa.Column("acknowledged_by_session_id", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "validation_result_id",
            "check_name",
            name="uq_deployment_validation_acks_result_check",
        ),
        sa.ForeignKeyConstraint(
            ["validation_result_id"],
            ["deployment_validation_results.id"],
            ondelete="CASCADE",
            name="fk_deployment_validation_acks_validation_result_id",
        ),
        sa.ForeignKeyConstraint(
            ["acknowledged_by_session_id"],
            ["sessions.id"],
            ondelete="SET NULL",
            name="fk_deployment_validation_acks_session_id",
        ),
        sa.CheckConstraint(
            "check_name IN ('connectivity', 'role_completeness',"
            " 'capability_strategy', 'constraint_completeness',"
            " 'constraint_safety_pre_check', 'control_readiness')",
            name="ck_deployment_validation_acks_check_name_allowed",
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_deployment_validation_acks_validation_result_id",
        "deployment_validation_acks",
        ["validation_result_id"],
    )

    with op.batch_alter_table("wizard_state") as batch_op:
        batch_op.add_column(
            sa.Column(
                "step_4_complete",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        batch_op.add_column(
            sa.Column("step_4_completed_at", sa.Text(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("step_4_completed_config_version", sa.Integer(), nullable=True),
        )
        batch_op.create_check_constraint(
            "ck_wizard_state_step_4_complete_bool",
            "step_4_complete IN (0, 1)",
        )
        batch_op.create_check_constraint(
            "ck_wizard_state_step_4_pair",
            "(step_4_complete = 0"
            "  AND step_4_completed_at IS NULL"
            "  AND step_4_completed_config_version IS NULL)"
            " OR (step_4_complete = 1"
            "     AND step_4_completed_at IS NOT NULL"
            "     AND step_4_completed_config_version IS NOT NULL)",
        )


def downgrade() -> None:
    with op.batch_alter_table("wizard_state") as batch_op:
        batch_op.drop_constraint("ck_wizard_state_step_4_pair", type_="check")
        batch_op.drop_constraint("ck_wizard_state_step_4_complete_bool", type_="check")
        batch_op.drop_column("step_4_completed_config_version")
        batch_op.drop_column("step_4_completed_at")
        batch_op.drop_column("step_4_complete")

    op.drop_index(
        "ix_deployment_validation_acks_validation_result_id",
        table_name="deployment_validation_acks",
    )
    op.drop_table("deployment_validation_acks")
    op.drop_table("deployment_validation_results")
