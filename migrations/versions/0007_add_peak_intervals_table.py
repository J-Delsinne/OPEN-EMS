"""Add peak_intervals table

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-05

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "peak_intervals",
        sa.Column("interval_start_utc", sa.Text(), nullable=False, primary_key=True),
        sa.Column("avg_power_kw", sa.Float(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column(
            "data_quality",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'complete'"),
        ),
        sa.Column(
            "is_monthly_peak",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_index("ix_peak_intervals_is_monthly_peak", "peak_intervals", ["is_monthly_peak"])


def downgrade() -> None:
    op.drop_index("ix_peak_intervals_is_monthly_peak", table_name="peak_intervals")
    op.drop_table("peak_intervals")
