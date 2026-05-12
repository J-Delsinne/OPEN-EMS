"""Add energy_flow_intervals + weekly_energy_summary tables

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-12

Story 10.4 / FR30: pre-aggregated energy-flow tracking for the homeowner weekly summary.

- ``energy_flow_intervals``: one row per completed 15-minute clock-aligned interval,
  written by ``ControlLoop._update_tracker`` after ``write_peak_interval`` (load-bearing
  ordering — peak persistence must not regress). Stores PV, battery (charged/discharged),
  grid (imported/exported), and EV kWh integrated over the interval, plus a data_quality
  bit that drives the aggregator's ``data_complete_days_count`` semantics.

- ``weekly_energy_summary``: single-row table (``CHECK (id = 1)``) upserted by
  ``WeeklyEnergySummaryService``. Holds the three FR30 metrics and the
  ``insufficient_history`` flag. The CHECK constraint ``insufficient_history=0 ⇒ all
  three metrics non-null + ratio in [0, 1]`` mirrors the Pydantic model-validator on
  ``WeeklyEnergySummaryRow`` (defense-in-depth, same class as the 10.2 EVOverrideState
  pattern).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "energy_flow_intervals",
        sa.Column("interval_start_utc", sa.Text(), nullable=False, primary_key=True),
        sa.Column("pv_kwh", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("battery_charged_kwh", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column(
            "battery_discharged_kwh", sa.Float(), nullable=False, server_default=sa.text("0.0")
        ),
        sa.Column("grid_imported_kwh", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("grid_exported_kwh", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("ev_charged_kwh", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "data_quality",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'complete'"),
        ),
        sa.CheckConstraint(
            "pv_kwh >= 0 AND battery_charged_kwh >= 0 AND battery_discharged_kwh >= 0"
            " AND grid_imported_kwh >= 0 AND grid_exported_kwh >= 0 AND ev_charged_kwh >= 0"
            " AND sample_count >= 0",
            name="ck_energy_flow_intervals_nonneg",
        ),
        sa.CheckConstraint(
            "data_quality IN ('complete', 'incomplete')",
            name="ck_energy_flow_intervals_data_quality_allowed",
        ),
    )
    op.create_index(
        "ix_energy_flow_intervals_interval_start_utc",
        "energy_flow_intervals",
        ["interval_start_utc"],
    )

    op.create_table(
        "weekly_energy_summary",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("window_start_utc", sa.Text(), nullable=False),
        sa.Column("window_end_utc", sa.Text(), nullable=False),
        sa.Column("peaks_avoided_count", sa.Integer(), nullable=True),
        sa.Column("self_consumption_ratio", sa.Float(), nullable=True),
        sa.Column("estimated_cost_savings_eur", sa.Float(), nullable=True),
        sa.Column(
            "data_complete_days_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "insufficient_history", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("computed_at", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_weekly_energy_summary_single_row"),
        sa.CheckConstraint(
            "insufficient_history IN (0, 1)",
            name="ck_weekly_energy_summary_insufficient_history_bool",
        ),
        sa.CheckConstraint(
            "(insufficient_history = 1)"
            " OR (peaks_avoided_count IS NOT NULL"
            "     AND self_consumption_ratio IS NOT NULL"
            "     AND self_consumption_ratio >= 0.0 AND self_consumption_ratio <= 1.0"
            "     AND estimated_cost_savings_eur IS NOT NULL)",
            name="ck_weekly_energy_summary_terminal_fields_iff_history_sufficient",
        ),
        sa.CheckConstraint(
            "data_complete_days_count >= 0",
            name="ck_weekly_energy_summary_data_complete_days_count_nonneg",
        ),
    )


def downgrade() -> None:
    op.drop_table("weekly_energy_summary")
    op.drop_index(
        "ix_energy_flow_intervals_interval_start_utc",
        table_name="energy_flow_intervals",
    )
    op.drop_table("energy_flow_intervals")
