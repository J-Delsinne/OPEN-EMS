"""Add event_log table

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-04

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("device_id", sa.Text(), nullable=True),
        sa.Column("config_version", sa.Text(), nullable=True),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_event_log_timestamp", "event_log", ["timestamp"])
    op.create_index("ix_event_log_event_type", "event_log", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_event_log_event_type", table_name="event_log")
    op.drop_index("ix_event_log_timestamp", table_name="event_log")
    op.drop_table("event_log")
