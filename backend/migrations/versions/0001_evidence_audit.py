"""Evidence, audit & trace tables (Issue #10, Prompt 1).

Revision ID: 0001_evidence_audit
Revises:
Create Date: 2026-01-01

Creates the durable evidence/audit store: evidence_records, quote_observations
and audit_events. All columns are SAFE metadata only (ids, canonical paths,
counts, page signatures, sanitized URLs, typed JSON payloads) - never
applicant values, never raw quote references (only private opaque handles),
never screenshots/audio/transcripts. Money is stored as Numeric (Decimal).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_evidence_audit"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_records",
        sa.Column("evidence_id", sa.String(length=64), primary_key=True),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("intake_session_id", sa.String(length=64), nullable=True),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("planned_route_id", sa.String(length=64), nullable=True),
        sa.Column("registry_id", sa.String(length=64), nullable=True),
        sa.Column("distinct_rate_source_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("parent_attempt_id", sa.String(length=64), nullable=True),
        sa.Column("source_channel", sa.String(length=16), nullable=False),
        sa.Column("source_session_id", sa.String(length=64), nullable=True),
        sa.Column("page_signature", sa.String(length=255), nullable=True),
        sa.Column("safe_url", sa.String(length=512), nullable=True),
        sa.Column("observation_type", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("evidence_source", sa.String(length=64), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("quote_observation_id", sa.String(length=64), nullable=True),
        sa.Column("registry_snapshot_ref", sa.String(length=255), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_evidence_records_idempotency_key"),
    )
    op.create_index("ix_evidence_records_intake_session_id", "evidence_records", ["intake_session_id"])
    op.create_index("ix_evidence_records_planned_route_id", "evidence_records", ["planned_route_id"])
    op.create_index("ix_evidence_records_registry_id", "evidence_records", ["registry_id"])
    op.create_index("ix_evidence_records_attempt_id", "evidence_records", ["attempt_id"])

    op.create_table(
        "quote_observations",
        sa.Column("quote_id", sa.String(length=64), primary_key=True),
        sa.Column("intake_session_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("parent_attempt_id", sa.String(length=64), nullable=True),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("planned_route_id", sa.String(length=64), nullable=True),
        sa.Column("registry_id", sa.String(length=64), nullable=True),
        sa.Column("distinct_rate_source_id", sa.String(length=64), nullable=True),
        sa.Column("aggregator_registry_id", sa.String(length=64), nullable=True),
        sa.Column("presented_carrier", sa.String(length=255), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("annual_premium", sa.Numeric(12, 2), nullable=True),
        sa.Column("monthly_premium", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("firm_vs_estimate", sa.String(length=16), nullable=False),
        sa.Column("reference_present", sa.Boolean(), nullable=False),
        sa.Column("private_reference_handle", sa.String(length=64), nullable=True),
        sa.Column("coverage_raw_present", sa.Boolean(), nullable=False),
        sa.Column("quote_pending_normalization", sa.Boolean(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_quote_observations_idempotency_key"),
    )
    op.create_index("ix_quote_observations_intake_session_id", "quote_observations", ["intake_session_id"])
    op.create_index("ix_quote_observations_attempt_id", "quote_observations", ["attempt_id"])

    op.create_table(
        "audit_events",
        sa.Column("audit_id", sa.String(length=64), primary_key=True),
        sa.Column("intake_session_id", sa.String(length=64), nullable=True),
        sa.Column("event_name", sa.String(length=48), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=16), nullable=False),
        sa.Column("safe_metadata", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_audit_events_idempotency_key"),
    )
    op.create_index("ix_audit_events_intake_session_id", "audit_events", ["intake_session_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_intake_session_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_quote_observations_attempt_id", table_name="quote_observations")
    op.drop_index("ix_quote_observations_intake_session_id", table_name="quote_observations")
    op.drop_table("quote_observations")
    op.drop_index("ix_evidence_records_attempt_id", table_name="evidence_records")
    op.drop_index("ix_evidence_records_registry_id", table_name="evidence_records")
    op.drop_index("ix_evidence_records_planned_route_id", table_name="evidence_records")
    op.drop_index("ix_evidence_records_intake_session_id", table_name="evidence_records")
    op.drop_table("evidence_records")
