"""Add csrf_token column to sessions table

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("csrf_token", sa.Text(), nullable=False, server_default=""))
    # Invalidate all pre-CSRF sessions: existing rows get csrf_token="" which cannot be matched
    # by a real token. Forcing a DELETE ensures users re-authenticate and receive a valid token.
    op.execute("DELETE FROM sessions")


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("csrf_token")
