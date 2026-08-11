"""Issue #11 - PostgreSQL integration validation (OPTIONAL, gated).

Validates real Postgres semantics SQLite cannot guarantee for normalization:
Alembic upgrade to head (0002), the unique idempotency constraint on
(source_quote_observation_id, normalization_rule_version), Numeric money, and
JSON coverage rows. SKIPPED cleanly when no Postgres test URL is configured.

Enable via:
    POSTGRES_EVIDENCE_TEST_URL=postgresql+asyncpg://user:pass@localhost:5432/allquote_test
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path

import pytest

from app.db import create_evidence_engine
from app.models.normalization import CoverageItemKey, NormalizationStatus
from app.services.evidence.persistence import SqlAlchemyEvidenceRepository
from app.services.evidence.service import EvidenceService
from app.services.normalization.repository import SqlAlchemyNormalizationRepository
from app.services.normalization.service import QuoteNormalizationService

from normalization_helpers import make_browser_quote

TEST_URL = os.environ.get("POSTGRES_EVIDENCE_TEST_URL") or os.environ.get("DATABASE_URL_TEST")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="Postgres integration requires POSTGRES_EVIDENCE_TEST_URL (not configured)",
)


def _run_alembic_upgrade(db_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[1]
    os.environ["ALEMBIC_DB_URL"] = db_url
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


@pytest.fixture()
def pg_engine():
    assert TEST_URL
    engine = create_evidence_engine(TEST_URL)
    yield engine
    asyncio.run(engine.dispose())


async def _clean(engine) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM normalized_coverage_items"))
        await conn.execute(text("DELETE FROM normalized_quotes"))
        await conn.execute(text("DELETE FROM quote_observations"))
        await conn.execute(text("DELETE FROM evidence_records"))


async def test_postgres_alembic_head_and_normalization_roundtrip(pg_engine) -> None:
    _run_alembic_upgrade(TEST_URL)
    await _clean(pg_engine)

    from sqlalchemy import inspect

    from app.services.evidence.ingest import quote_from_browser_observation

    async with pg_engine.connect() as conn:
        tables = set(await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names()))
    assert "normalized_quotes" in tables
    assert "normalized_coverage_items" in tables

    evidence = EvidenceService(SqlAlchemyEvidenceRepository(pg_engine))
    normalization = QuoteNormalizationService(
        evidence, SqlAlchemyNormalizationRepository(pg_engine)
    )
    obs = make_browser_quote(
        annual=Decimal("1234.56"),
        coverage=[
            "Third Party Liability - $2,000,000",
            "Comprehensive - $500 deductible",
            "Family Protection",
        ],
        discounts=["Winter Tire Discount"],
    )
    quote = quote_from_browser_observation(
        "intake-pg",
        obs,
        plan_id="plan-pg",
        planned_route_id="route-pg",
        registry_id="mock-pg",
        distinct_rate_source_id="RS-PG",
        attempt_id="att-pg",
    )
    stored = await evidence.record_quote_observation("intake-pg", quote)
    normalized = await normalization.normalize("intake-pg", stored.quote_id)
    assert normalized.normalization_status == NormalizationStatus.NORMALIZED
    assert normalized.premium.normalized_annual_amount == Decimal("1234.56")
    assert normalized.coverage_ledger.get(CoverageItemKey.THIRD_PARTY_LIABILITY) is not None

    reloaded = await normalization.get("intake-pg", normalized.normalized_quote_id)
    assert reloaded == normalized

    # Idempotency constraint: re-normalizing the same source+rule returns the
    # same row (no duplicate).
    again = await normalization.normalize("intake-pg", stored.quote_id)
    assert again.normalized_quote_id == normalized.normalized_quote_id
    assert len(await normalization.list_by_intake("intake-pg")) == 1

    await _clean(pg_engine)
