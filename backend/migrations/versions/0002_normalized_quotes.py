"""Normalized quotes & coverage ledger tables (Issue #11, Prompt 1).

Revision ID: 0002_normalized_quotes
Revises: 0001_evidence_audit
Create Date: 2026-01-01

- Adds the SAFE ``coverage_observations`` / ``discount_observations`` JSON
  label columns to ``quote_observations`` (Issue #10 extension consumed by
  normalization; they carry public provider wording only - never PII).
- Creates ``normalized_quotes`` + ``normalized_coverage_items``: canonical,
  provider-independent quote representation. Money is Numeric (Decimal);
  coverage components are typed rows keyed by canonical item key; unique
  idempotency on (source_quote_observation_id, normalization_rule_version).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_normalized_quotes"
down_revision = "0001_evidence_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Issue #10 extension: safe coverage/discount label segments ---------
    op.add_column(
        "quote_observations",
        sa.Column("coverage_observations", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "quote_observations",
        sa.Column("discount_observations", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )

    # --- normalized quotes -------------------------------------------------
    op.create_table(
        "normalized_quotes",
        sa.Column("normalized_quote_id", sa.String(length=64), primary_key=True),
        sa.Column("intake_session_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("planned_route_id", sa.String(length=64), nullable=True),
        sa.Column("registry_id", sa.String(length=64), nullable=True),
        sa.Column("distinct_rate_source_id", sa.String(length=64), nullable=True),
        sa.Column("aggregator_registry_id", sa.String(length=64), nullable=True),
        sa.Column("presented_carrier", sa.String(length=255), nullable=True),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("parent_attempt_id", sa.String(length=64), nullable=True),
        sa.Column("source_quote_observation_id", sa.String(length=64), nullable=False),
        sa.Column("source_channel", sa.String(length=16), nullable=False),
        sa.Column("firm_vs_estimate", sa.String(length=16), nullable=False),
        sa.Column("premium", sa.JSON(), nullable=False),
        sa.Column("unmapped_coverage", sa.JSON(), nullable=False),
        sa.Column("source_evidence_record_ids", sa.JSON(), nullable=False),
        sa.Column("normalization_status", sa.String(length=32), nullable=False),
        sa.Column("normalization_rule_version", sa.String(length=16), nullable=False),
        sa.Column("normalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_normalized_quotes_idempotency_key"),
        sa.UniqueConstraint(
            "source_quote_observation_id",
            "normalization_rule_version",
            name="uq_normalized_source_rule",
        ),
    )
    op.create_index(
        "ix_normalized_quotes_intake_session_id", "normalized_quotes", ["intake_session_id"]
    )
    op.create_index("ix_normalized_quotes_plan_id", "normalized_quotes", ["plan_id"])
    op.create_index(
        "ix_normalized_quotes_planned_route_id", "normalized_quotes", ["planned_route_id"]
    )
    op.create_index("ix_normalized_quotes_registry_id", "normalized_quotes", ["registry_id"])
    op.create_index("ix_normalized_quotes_attempt_id", "normalized_quotes", ["attempt_id"])

    # --- normalized coverage items (typed rows) -----------------------------
    op.create_table(
        "normalized_coverage_items",
        sa.Column("coverage_item_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "normalized_quote_id",
            sa.String(length=64),
            sa.ForeignKey(
                "normalized_quotes.normalized_quote_id", name="fk_normalized_coverage_items_quote",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("intake_session_id", sa.String(length=64), nullable=False),
        sa.Column("item_key", sa.String(length=48), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column("raw_labels", sa.JSON(), nullable=False),
        sa.Column("source_evidence_ids", sa.JSON(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_normalized_coverage_items_normalized_quote_id",
        "normalized_coverage_items",
        ["normalized_quote_id"],
    )
    op.create_index(
        "ix_normalized_coverage_items_intake_session_id",
        "normalized_coverage_items",
        ["intake_session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_normalized_coverage_items_intake_session_id",
        table_name="normalized_coverage_items",
    )
    op.drop_index(
        "ix_normalized_coverage_items_normalized_quote_id",
        table_name="normalized_coverage_items",
    )
    op.drop_table("normalized_coverage_items")
    op.drop_index("ix_normalized_quotes_attempt_id", table_name="normalized_quotes")
    op.drop_index("ix_normalized_quotes_registry_id", table_name="normalized_quotes")
    op.drop_index("ix_normalized_quotes_planned_route_id", table_name="normalized_quotes")
    op.drop_index("ix_normalized_quotes_plan_id", table_name="normalized_quotes")
    op.drop_index("ix_normalized_quotes_intake_session_id", table_name="normalized_quotes")
    op.drop_table("normalized_quotes")
    op.drop_column("quote_observations", "discount_observations")
    op.drop_column("quote_observations", "coverage_observations")
