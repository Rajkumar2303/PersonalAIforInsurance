"""Issue #10 - persistence tests: SQLite SQLAlchemy repository + Alembic
migration (hermetic; same models/dialect-portable columns as Postgres)."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from decimal import Decimal
from pathlib import Path

import pytest

from app.db import create_evidence_engine
from app.db.base import Base
from app.models.evidence import (
    AuditEventName,
    EvidenceEventType,
)
from app.models.recovery import SourceChannel
from app.services.evidence.ingest import EvidenceDraft
from app.services.evidence.persistence import SqlAlchemyEvidenceRepository
from app.services.evidence.service import EvidenceService
from app.models.evidence import PageObservationEvidence, BarrierEvidence

from evidence_helpers import make_sqlite_evidence_env, page_observation


async def test_sqlite_repository_roundtrips_evidence_and_quote(tmp_path: Path) -> None:
    env = await make_sqlite_evidence_env(tmp_path)
    d = EvidenceDraft(
        event_type=EvidenceEventType.BLOCKING_ACCESS_CONTROL_OBSERVED,
        payload=BarrierEvidence(barrier_kind="access_control", access_control_detected=True),
        source_channel=SourceChannel.BROWSER,
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1",
        source_session_id="bs-1",
        page_signature="sig",
        safe_url="http://127.0.0.1:8765/gate?token=SECRET",
        observation_type="access_control_detected",
    )
    r1 = await env.service.append("intake-1", d)
    r2 = await env.service.append("intake-1", d)
    assert r1.evidence_id == r2.evidence_id  # idempotent via unique key
    rows = await env.service.list_by_attempt("intake-1", "att-1")
    assert len(rows) == 1
    assert rows[0].payload.kind == "barrier"
    assert rows[0].safe_url == "127.0.0.1:8765/gate"  # sanitized (no token)
    assert rows[0].source_channel is SourceChannel.BROWSER
    assert await env.service.verify_integrity("intake-1", r1.evidence_id)


async def test_sqlite_repository_decimal_money_roundtrip(tmp_path: Path) -> None:
    env = await make_sqlite_evidence_env(tmp_path)
    q = await env.service.record_voice_quote(
        "intake-1",
        voice_session_id="vs-1",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1",
        annual_premium=Decimal("9876.54"),
        monthly_premium=Decimal("823.05"),
        currency="CAD",
        firm_vs_estimate="firm",
        observed_at=dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc),
    )
    quotes = await env.service.list_quote_observations("intake-1", "att-1")
    assert len(quotes) == 1
    assert quotes[0].annual_premium == Decimal("9876.54")
    assert quotes[0].monthly_premium == Decimal("823.05")


async def test_sqlite_repository_audit_and_retention(tmp_path: Path) -> None:
    env = await make_sqlite_evidence_env(tmp_path)
    await env.service.record_audit_event(
        "intake-1", event_name=AuditEventName.EXPORT_CREATED, actor="system"
    )
    await env.service.record_audit_event(
        "intake-2", event_name=AuditEventName.EXPORT_CREATED, actor="system"
    )
    assert len(await env.service.list_audit_events("intake-1")) == 1
    assert len(await env.service.list_audit_events("intake-2")) == 1
    removed = await env.service.delete_by_intake_session("intake-1")
    assert removed >= 1
    assert await env.service.list_audit_events("intake-1") == []
    assert len(await env.service.list_audit_events("intake-2")) == 1


async def test_sqlite_repository_integrity_detects_mutation(tmp_path: Path) -> None:
    env = await make_sqlite_evidence_env(tmp_path)
    r = await env.service.record_browser_observation(
        "intake-1",
        page_observation(),
        **env.ids(),
        browser_session_id="bs-1",
    )
    assert await env.service.verify_integrity("intake-1", r.evidence_id)
    # Tamper at the DB row level (as stored).
    from sqlalchemy import text

    async with env.repo.begin() as conn:
        await conn.execute(
            text("UPDATE evidence_records SET safe_url = :u WHERE evidence_id = :id"),
            {"u": "evil.example.com/leak", "id": r.evidence_id},
        )
    assert await env.service.verify_integrity("intake-1", r.evidence_id) is False


# ---------------------------------------------------------------------------
# Alembic migration (upgrade head on temp SQLite)
# ---------------------------------------------------------------------------


def _run_alembic(db_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    # env.py honours ALEMBIC_DB_URL; set it explicitly so an ambient value from
    # the developer shell never redirects the migration to another database.
    os.environ["ALEMBIC_DB_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    command.upgrade(cfg, "head")


@pytest.fixture()
def alembic_db(tmp_path: Path):
    return tmp_path / "alembic_check.db"


def test_alembic_upgrade_head_creates_tables(alembic_db: Path) -> None:
    _run_alembic(alembic_db)
    assert alembic_db.exists()
    import sqlite3

    con = sqlite3.connect(alembic_db)
    tables = sorted(
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    )
    con.close()
    assert tables == ["alembic_version", "audit_events", "evidence_records", "quote_observations"]


def test_alembic_upgraded_db_accepts_service_writes(alembic_db: Path) -> None:
    _run_alembic(alembic_db)

    async def run() -> None:
        engine = create_evidence_engine(f"sqlite+aiosqlite:///{alembic_db}")
        repo = SqlAlchemyEvidenceRepository(engine)
        service = EvidenceService(repo)
        r = await service.record_browser_observation(
            "intake-1",
            page_observation(),
            **{
                "plan_id": "plan-1",
                "planned_route_id": "mock-insurer",
                "registry_id": "mock-insurer",
                "distinct_rate_source_id": "RS-MOCK-INSURER",
                "attempt_id": "att-1",
            },
            browser_session_id="bs-1",
        )
        assert r.sequence == 1
        assert await service.verify_integrity("intake-1", r.evidence_id)
        await engine.dispose()

    asyncio.run(run())
