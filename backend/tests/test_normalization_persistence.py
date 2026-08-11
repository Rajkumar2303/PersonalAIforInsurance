"""SQLite persistence + Alembic migration tests for Issue #11 normalization.

Uses the SAME SQLAlchemy models as PostgreSQL (JSON columns, String enums,
Numeric money), so passing here is strong evidence the Postgres path works.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.db import create_evidence_engine
from app.db.base import Base
from app.models.normalization import (
    CoverageItemKey,
    NormalizationStatus,
)
from app.services.evidence.persistence import SqlAlchemyEvidenceRepository
from app.services.evidence.service import EvidenceService
from app.services.normalization.repository import SqlAlchemyNormalizationRepository
from app.services.normalization.service import QuoteNormalizationService

from normalization_helpers import make_browser_quote


async def test_sqlite_repository_roundtrips_normalized_quote(tmp_path):
    from app.services.evidence.ingest import quote_from_browser_observation

    db_path = (tmp_path / "n.db").as_posix()
    engine = create_evidence_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    evidence = EvidenceService(SqlAlchemyEvidenceRepository(engine))
    normalization = QuoteNormalizationService(
        evidence, SqlAlchemyNormalizationRepository(engine)
    )

    obs = make_browser_quote(
        annual=Decimal("1234.56"),
        coverage=[
            "Third Party Liability - $2,000,000",
            "Collision - $1,000 deductible",
            "Family Protection",
        ],
        discounts=["Winter Tire Discount"],
    )
    quote = quote_from_browser_observation(
        "intake-1",
        obs,
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1",
    )
    stored = await evidence.record_quote_observation("intake-1", quote)
    normalized = await normalization.normalize("intake-1", stored.quote_id)
    assert normalized.normalization_status == NormalizationStatus.NORMALIZED

    reloaded = await normalization.get("intake-1", normalized.normalized_quote_id)
    assert reloaded is not None
    # SQLite round-trips datetimes as naive UTC, so compare by content hash
    # (which canonicalizes to UTC-naive) plus the key fields.
    from app.services.normalization.service import normalized_quote_content_hash

    assert normalized_quote_content_hash(reloaded) == reloaded.content_hash
    assert reloaded.content_hash == normalized.content_hash
    assert reloaded.premium.normalized_annual_amount == Decimal("1234.56")
    tpl = reloaded.coverage_ledger.get(CoverageItemKey.THIRD_PARTY_LIABILITY)
    assert tpl.value.amount == Decimal("2000000")
    assert reloaded.coverage_ledger.mapped_count == 4


async def test_sqlite_repository_idempotent(tmp_path):
    from app.services.evidence.ingest import quote_from_browser_observation

    db_path = (tmp_path / "n.db").as_posix()
    engine = create_evidence_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    evidence = EvidenceService(SqlAlchemyEvidenceRepository(engine))
    normalization = QuoteNormalizationService(
        evidence, SqlAlchemyNormalizationRepository(engine)
    )
    obs = make_browser_quote(coverage=["Collision - $500"])
    quote = quote_from_browser_observation(
        "intake-1", obs, plan_id="p", planned_route_id="r", registry_id="m",
        distinct_rate_source_id="rs", attempt_id="a",
    )
    stored = await evidence.record_quote_observation("intake-1", quote)
    first = await normalization.normalize("intake-1", stored.quote_id)
    second = await normalization.normalize("intake-1", stored.quote_id)
    assert first.normalized_quote_id == second.normalized_quote_id
    assert len(await normalization.list_by_intake("intake-1")) == 1


async def test_sqlite_integrity_detects_mutation(tmp_path):
    from sqlalchemy import select, update

    from app.services.normalization.repository import NormalizedQuoteORM

    from app.services.evidence.ingest import quote_from_browser_observation

    db_path = (tmp_path / "n.db").as_posix()
    engine = create_evidence_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    evidence = EvidenceService(SqlAlchemyEvidenceRepository(engine))
    normalization = QuoteNormalizationService(
        evidence, SqlAlchemyNormalizationRepository(engine)
    )
    obs = make_browser_quote(coverage=["Collision - $500"])
    quote = quote_from_browser_observation(
        "intake-1", obs, plan_id="p", planned_route_id="r", registry_id="m",
        distinct_rate_source_id="rs", attempt_id="a",
    )
    stored = await evidence.record_quote_observation("intake-1", quote)
    normalized = await normalization.normalize("intake-1", stored.quote_id)
    assert await normalization.verify_integrity("intake-1", normalized.normalized_quote_id)

    # Mutate the persisted normalized_quote row's carrier label.
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(engine) as session:
        await session.execute(
            update(NormalizedQuoteORM)
            .where(NormalizedQuoteORM.normalized_quote_id == normalized.normalized_quote_id)
            .values(presented_carrier="TAMPERED")
        )
        await session.commit()

    assert not await normalization.verify_integrity(
        "intake-1", normalized.normalized_quote_id
    )


def test_alembic_upgrade_head_creates_normalized_tables(tmp_path):
    import asyncio
    import os

    from alembic import command
    from alembic.config import Config

    from sqlalchemy import inspect

    from app.db import create_evidence_engine

    backend_root = Path(__file__).resolve().parents[1]
    db_path = (tmp_path / "migrate.db").as_posix()
    alembic_cfg = Config(str(backend_root / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(backend_root / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    os.environ["ALEMBIC_DB_URL"] = f"sqlite+aiosqlite:///{db_path}"

    # env.py uses asyncio.run internally, so this must run OUTSIDE an active
    # event loop (sync test, like the Issue #10 evidence alembic tests).
    command.upgrade(alembic_cfg, "head")

    async def _inspect() -> set[str]:
        engine = create_evidence_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            async with engine.connect() as conn:
                return set(
                    await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
                )
        finally:
            await engine.dispose()

    tables = asyncio.run(_inspect())
    assert "normalized_quotes" in tables
    assert "normalized_coverage_items" in tables
    assert "quote_observations" in tables


def test_alembic_upgraded_db_accepts_normalization_writes(tmp_path):
    import asyncio
    import os

    from alembic import command
    from alembic.config import Config

    from app.models.normalization import CoverageItemKey, NormalizationStatus
    from app.services.evidence.ingest import quote_from_browser_observation

    backend_root = Path(__file__).resolve().parents[1]
    db_path = (tmp_path / "migrate2.db").as_posix()
    alembic_cfg = Config(str(backend_root / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(backend_root / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    os.environ["ALEMBIC_DB_URL"] = f"sqlite+aiosqlite:///{db_path}"
    command.upgrade(alembic_cfg, "head")

    async def _write_and_normalize() -> None:
        engine = create_evidence_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            evidence = EvidenceService(SqlAlchemyEvidenceRepository(engine))
            normalization = QuoteNormalizationService(
                evidence, SqlAlchemyNormalizationRepository(engine)
            )
            obs = make_browser_quote(coverage=["Third Party Liability - $2M"])
            quote = quote_from_browser_observation(
                "intake-1", obs, plan_id="p", planned_route_id="r", registry_id="m",
                distinct_rate_source_id="rs", attempt_id="a",
            )
            stored = await evidence.record_quote_observation("intake-1", quote)
            normalized = await normalization.normalize("intake-1", stored.quote_id)
            assert normalized.normalization_status == NormalizationStatus.NORMALIZED
            assert normalized.coverage_ledger.get(CoverageItemKey.THIRD_PARTY_LIABILITY) is not None
        finally:
            await engine.dispose()

    asyncio.run(_write_and_normalize())
