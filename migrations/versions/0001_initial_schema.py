"""Initial schema baseline

Revision ID: 0001
Revises:
Create Date: 2026-05-01

"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass  # Baseline only — no tables yet; all schema added in later stories


def downgrade() -> None:
    pass
