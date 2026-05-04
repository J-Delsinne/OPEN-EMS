"""Add config_audit_log table

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-04

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config_audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("timestamp", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("previous_value", sa.Text(), nullable=False),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_config_audit_log_timestamp", "config_audit_log", ["timestamp"])
    op.create_index("ix_config_audit_log_config_version", "config_audit_log", ["config_version"])
    op.create_index("ix_config_audit_log_field", "config_audit_log", ["field"])


def downgrade() -> None:
    op.drop_index("ix_config_audit_log_field", table_name="config_audit_log")
    op.drop_index("ix_config_audit_log_config_version", table_name="config_audit_log")
    op.drop_index("ix_config_audit_log_timestamp", table_name="config_audit_log")
    op.drop_table("config_audit_log")
