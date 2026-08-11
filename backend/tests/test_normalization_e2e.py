"""Issue #11 - hermetic E2E normalization test (mock site -> normalize).

Drives the REAL mock-site browser flow (synthetic intake -> route plan ->
mock browser -> quote -> automatic evidence), then normalizes the automatically
persisted quote observation. Proves the whole evidence->normalization pipeline
works end-to-end with no manual EvidenceService or normalizer writes, no real
insurers, no applicant data.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.browser.mock_site import MOCK_REGISTRY_ID
from app.models.evidence import EvidenceEventType
from app.models.normalization import (
    CoverageItemKey,
    NormalizationStatus,
)
from app.services.evidence.sink import EvidenceServiceSink
from app.services.normalization.repository import InMemoryNormalizationRepository
from app.services.normalization.service import QuoteNormalizationService
from app.services.recovery.engine import RecoveryEngine

from browser_helpers import make_browser_env
from evidence_helpers import SENSITIVE_MARKERS, make_sink_env


async def _close_browser(env, session_id: str) -> None:
    try:
        await env.manager.close(session_id)
    except Exception:
        pass
    try:
        await env.browser_manager.stop()
    except Exception:
        pass


async def test_browser_e2e_quote_normalizes_into_canonical_ledger(
    tmp_path, mock_site
) -> None:
    from app.graph.browser_workflow import build_browser_workflow
    from app.models.browser.session import BrowserExecutionMode

    env, sink = make_sink_env()
    recovery = RecoveryEngine(evidence_sink=sink)
    benv = make_browser_env(tmp_path, mock_site, evidence_sink=sink, recovery=recovery)
    bs = benv.manager.create(
        benv.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX
    )
    try:
        state = await build_browser_workflow(benv.manager).ainvoke(
            {"entry": "run", "browser_session_id": bs.browser_session_id, "max_steps": 15}
        )
        assert state["workflow_status"] == "succeeded"

        # Evidence was collected automatically; the quote observation carries
        # the safe coverage/discount label segments.
        quotes = await env.service.list_quote_observations(benv.session_id)
        assert len(quotes) == 1
        quote = quotes[0]
        assert quote.firm_vs_estimate == "firm"
        assert quote.annual_premium == Decimal("1234.56")
        assert quote.coverage_observations, "expected safe coverage labels persisted"
        assert any("third party liability" in c.lower() for c in quote.coverage_observations)

        # Normalize the automatically-observed quote.
        normalizer = QuoteNormalizationService(
            env.service, InMemoryNormalizationRepository()
        )
        normalized = await normalizer.normalize(benv.session_id, quote.quote_id)
        assert normalized.normalization_status == NormalizationStatus.NORMALIZED
        assert normalized.premium.normalized_annual_amount == Decimal("1234.56")
        assert normalized.firm_vs_estimate == "firm"

        ledger = normalized.coverage_ledger
        assert ledger.get(CoverageItemKey.THIRD_PARTY_LIABILITY) is not None
        assert ledger.get(CoverageItemKey.ACCIDENT_BENEFITS) is not None
        comprehensive = ledger.get(CoverageItemKey.COMPREHENSIVE)
        assert comprehensive is not None
        assert comprehensive.value.amount == Decimal("500")
        assert ledger.get(CoverageItemKey.WINTER_TIRES_DISCOUNT) is not None

        # Lineage points back at the durable quote observation.
        assert normalized.source_quote_observation_id == quote.quote_id

        # No sensitive data anywhere in the normalized output.
        for marker in SENSITIVE_MARKERS:
            assert marker not in normalized.model_dump_json()
            assert marker not in normalized.content_hash
    finally:
        await _close_browser(benv, bs.browser_session_id)
        sink.close()
