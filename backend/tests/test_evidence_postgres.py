"""Issue #10, Prompt 2 - PostgreSQL integration validation (OPTIONAL, gated).

This suite validates real Postgres semantics that SQLite cannot guarantee:
Alembic upgrade, Numeric(12,2) money storage, unique idempotency constraints,
JSON payload storage, transaction rollback, and concurrent append/idempotency.

It is SKIPPED cleanly when no Postgres test URL is configured - the ordinary
hermetic suite never requires Docker or a cloud DB.

Enable via:
    POSTGRES_EVIDENCE_TEST_URL=postgresql+asyncpg://user:pass@localhost:5432/allquote_test
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from decimal import Decimal
from pathlib import Path

import pytest

from app.db import create_evidence_engine
from app.models.evidence import EvidenceEventType, PageObservationEvidence
from app.services.evidence.ingest import EvidenceDraft
from app.services.evidence.persistence import SqlAlchemyEvidenceRepository
from app.services.evidence.service import EvidenceService
from app.models.recovery import SourceChannel

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
        for table in ("evidence_records", "quote_observations", "audit_events"):
            await conn.execute(text(f"TRUNCATE {table} CASCADE"))


async def test_alembic_upgrade_head_on_postgres(pg_engine) -> None:
    _run_alembic_upgrade(TEST_URL)
    from sqlalchemy import text

    async with pg_engine.begin() as conn:
        tables = {
            r[0]
            for r in (await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            )).fetchall()
        }
    assert {"evidence_records", "quote_observations", "audit_events"} <= tables


async def test_postgres_decimal_money_roundtrip(pg_engine) -> None:
    _run_alembic_upgrade(TEST_URL)
    await _clean(pg_engine)
    repo = SqlAlchemyEvidenceRepository(pg_engine)
    service = EvidenceService(repo)
    q = await service.record_voice_quote(
        "intake-1",
        voice_session_id="vs-1",
        plan_id=None, planned_route_id="mock-insurer", registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER", attempt_id="att-1",
        annual_premium=Decimal("1234.56"), monthly_premium=Decimal("0.01"),
        currency="CAD", firm_vs_estimate="firm",
        observed_at=dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert q.annual_premium == Decimal("1234.56")
    quotes = await service.list_quote_observations("intake-1", "att-1")
    assert quotes[0].annual_premium == Decimal("1234.56")
    assert quotes[0].monthly_premium == Decimal("0.01")


async def test_postgres_idempotency_unique_constraint(pg_engine) -> None:
    _run_alembic_upgrade(TEST_URL)
    await _clean(pg_engine)
    repo = SqlAlchemyEvidenceRepository(pg_engine)
    service = EvidenceService(repo)
    d = EvidenceDraft(
        event_type=EvidenceEventType.PAGE_OBSERVED,
        payload=PageObservationEvidence(page_signature="sig"),
        source_channel=SourceChannel.BROWSER,
        plan_id="plan-1", planned_route_id="mock-insurer", registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER", attempt_id="att-1",
    )
    r1 = await service.append("intake-1", d)
    r2 = await service.append("intake-1", d)  # same logical event -> one row
    assert r1.evidence_id == r2.evidence_id
    assert len(await service.list_by_attempt("intake-1", "att-1")) == 1


async def test_postgres_json_payload_and_ordering(pg_engine) -> None:
    _run_alembic_upgrade(TEST_URL)
    await _clean(pg_engine)
    repo = SqlAlchemyEvidenceRepository(pg_engine)
    service = EvidenceService(repo)
    for i in range(3):
        await service.append(
            "intake-1",
            EvidenceDraft(
                event_type=EvidenceEventType.PAGE_OBSERVED,
                payload=PageObservationEvidence(page_signature=f"s{i}"),
                plan_id="plan-1", planned_route_id="mock-insurer", registry_id="mock-insurer",
                distinct_rate_source_id="RS-MOCK-INSURER", attempt_id="att-1",
            ),
        )
    rows = await service.list_by_attempt("intake-1", "att-1")
    assert [r.sequence for r in rows] == [1, 2, 3]
    assert rows[0].payload.page_signature == "s0"
    assert await service.verify_integrity("intake-1", rows[0].evidence_id)


async def test_postgres_transaction_rollback(pg_engine) -> None:
    _run_alembic_upgrade(TEST_URL)
    await _clean(pg_engine)
    from sqlalchemy import text

    # A mid-transaction failure rolls back the partial write.
    with pytest.raises(Exception):
        async with pg_engine.begin() as conn:
            await conn.execute(text("INSERT INTO evidence_records (evidence_id) VALUES ('orphan')"))
            raise RuntimeError("boom")
    async with pg_engine.begin() as conn:
        count = (await conn.execute(
            text("SELECT COUNT(*) FROM evidence_records")
        )).scalar()
    assert count == 0


async def test_postgres_concurrent_appends_deterministic_sequences(pg_engine) -> None:
    _run_alembic_upgrade(TEST_URL)
    await _clean(pg_engine)
    repo = SqlAlchemyEvidenceRepository(pg_engine)
    service = EvidenceService(repo)

    async def write(attempt_id: str, n: int) -> list[int]:
        for i in range(n):
            await service.append(
                "intake-1",
                EvidenceDraft(
                    event_type=EvidenceEventType.PAGE_OBSERVED,
                    payload=PageObservationEvidence(page_signature=f"{attempt_id}-{i}"),
                    plan_id="plan-1", planned_route_id=attempt_id, registry_id=attempt_id,
                    distinct_rate_source_id=f"RS-{attempt_id}", attempt_id=attempt_id,
                ),
            )
        rows = await service.list_by_attempt("intake-1", attempt_id)
        return [r.sequence for r in rows]

    results = await asyncio.gather(write("att-a", 5), write("att-b", 5), write("att-c", 5))
    for seqs in results:
        assert seqs == [1, 2, 3, 4, 5]
