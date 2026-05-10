"""Add active_constraints table

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "active_constraints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("peak_limit_kw", sa.Float(), nullable=False),
        sa.Column("battery_reserve_floor_percent", sa.Float(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False, unique=True),
        sa.Column("activated_at", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.CheckConstraint("peak_limit_kw > 0", name="ck_active_constraints_peak_limit_positive"),
        sa.CheckConstraint(
            "battery_reserve_floor_percent >= 0 AND battery_reserve_floor_percent <= 100",
            name="ck_active_constraints_reserve_floor_range",
        ),
        sa.CheckConstraint(
            "actor IN ('system', 'installer')",
            name="ck_active_constraints_actor_allowed",
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_active_constraints_config_version",
        "active_constraints",
        [sa.text("config_version DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_active_constraints_config_version", table_name="active_constraints")
    op.drop_table("active_constraints")
