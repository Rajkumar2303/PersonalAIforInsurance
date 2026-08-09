"""Deterministic rate-source deduplication service (Issue #4).

The authoritative route -> rate-source mapping is
``MarketRegistryEntry.distinct_rate_source_id`` (single source of truth).
This service:

- reads registry entries and known ``DistinctRateSource`` records,
- validates that ``related_registry_ids`` never contradict the registry,
- finds POSSIBLE duplicate candidates (never confirmation on its own),
- classifies pairs into explainable decisions,
- exposes deduplicated views and metrics.

It does NOT choose routes (Issue #6), touch websites, or modify the profile.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from ..core.config import BACKEND_ROOT, get_settings
from ..models.dedup import (
    Confidence,
    DeduplicationDecision,
    DeduplicationStatus,
    DeduplicatedMarket,
    DistinctRateSource,
    DuplicateCandidate,
    DuplicateGroup,
    ReasonCode,
)
from ..models.registry import MarketRegistryEntry
from .market_registry import MarketRegistryService

logger = logging.getLogger(__name__)


class DedupLoadError(RuntimeError):
    """Raised when rate-source data is invalid or contradicts the registry."""


class DedupLookupError(KeyError):
    """Raised for unknown registry_id lookups."""


def default_rate_sources_dir() -> Path:
    settings = get_settings()
    if settings.rate_sources_dir:
        return Path(settings.rate_sources_dir)
    return BACKEND_ROOT / "data" / "rate_sources"


class RateSourceDeduplicationService:
    """Deterministic deduplication over the market registry + rate-source data."""

    def __init__(
        self,
        registry_service: Optional[MarketRegistryService] = None,
        rate_sources_dir: Optional[Path] = None,
    ) -> None:
        self._registry = registry_service or MarketRegistryService()
        self._rate_sources_dir = (
            Path(rate_sources_dir) if rate_sources_dir else default_rate_sources_dir()
        )
        self._rate_sources: dict[str, DistinctRateSource] = {}
        self._load_rate_sources()

    # --- loading / validation ------------------------------------------

    def _load_rate_sources(self) -> None:
        self._rate_sources = {}
        if not self._rate_sources_dir.exists():
            logger.warning(
                "rate sources directory not found",
                extra={"workflow": "deduplication", "workflow_stage": "load", "status": "missing"},
            )
            return
        for path in sorted(self._rate_sources_dir.glob("*.json")):
            self._load_file(path)
        logger.info(
            "rate sources loaded",
            extra={
                "workflow": "deduplication",
                "workflow_stage": "load",
                "status": "ok",
                "result_count": len(self._rate_sources),
            },
        )

    def _load_file(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DedupLoadError(f"failed to read rate-sources file {path.name}") from exc
        records = raw.get("records", raw) if isinstance(raw, dict) else raw
        if not isinstance(records, list):
            raise DedupLoadError(f"rate-sources file {path.name} must contain a list of records")
        for item in records:
            try:
                rate_source = DistinctRateSource.model_validate(item)
            except ValidationError as exc:
                raise DedupLoadError(f"invalid rate-source record in {path.name}: {exc}") from exc
            if rate_source.distinct_rate_source_id in self._rate_sources:
                raise DedupLoadError(
                    f"duplicate distinct_rate_source_id {rate_source.distinct_rate_source_id!r} in {path.name}"
                )
            self._check_related_registry_ids(rate_source, path)
            self._rate_sources[rate_source.distinct_rate_source_id] = rate_source

    def _check_related_registry_ids(self, rate_source: DistinctRateSource, path: Path) -> None:
        """The registry mapping is authoritative; related ids must not contradict it."""
        for registry_id in rate_source.related_registry_ids:
            entry = self._registry.get_by_registry_id(registry_id)
            if entry is None:
                raise DedupLoadError(
                    f"related registry_id {registry_id!r} of rate source "
                    f"{rate_source.distinct_rate_source_id!r} not found in registry ({path.name})"
                )
            if entry.distinct_rate_source_id != rate_source.distinct_rate_source_id:
                raise DedupLoadError(
                    f"inconsistent mapping in {path.name}: registry {registry_id!r} maps to "
                    f"{entry.distinct_rate_source_id!r} but rate source "
                    f"{rate_source.distinct_rate_source_id!r} claims it"
                )

    # --- rate-source lookups -------------------------------------------

    def list_rate_sources(self) -> list[DistinctRateSource]:
        return sorted(self._rate_sources.values(), key=lambda rs: rs.distinct_rate_source_id)

    def get_rate_source(self, rate_source_id: str) -> Optional[DistinctRateSource]:
        return self._rate_sources.get(rate_source_id.strip())

    def get_registry_entries_for_rate_source(self, rate_source_id: str) -> list[MarketRegistryEntry]:
        """Derived from the authoritative registry mapping (single source of truth)."""
        target = rate_source_id.strip()
        return [e for e in self._registry.list_markets() if e.distinct_rate_source_id == target]

    def _rate_source_claiming(self, registry_id: str) -> Optional[DistinctRateSource]:
        for rate_source in self._rate_sources.values():
            if registry_id in rate_source.related_registry_ids:
                return rate_source
        return None

    def _require_entry(self, registry_id: str) -> MarketRegistryEntry:
        entry = self._registry.get_by_registry_id(registry_id)
        if entry is None:
            raise DedupLookupError(f"unknown registry_id {registry_id!r}")
        return entry

    # --- candidate detection (NOT confirmation) -------------------------

    def find_duplicate_candidates(self, registry_id: str) -> list[DuplicateCandidate]:
        """Surface POSSIBLE duplicate routes; never confirm on its own."""
        entry = self._require_entry(registry_id)
        candidates: list[DuplicateCandidate] = []
        for other in self._registry.list_markets():
            if other.registry_id == registry_id:
                continue
            reason = self._candidate_reason(entry, other)
            if reason is not None:
                candidates.append(
                    DuplicateCandidate(
                        registry_id=other.registry_id,
                        reason_code=reason,
                        distinct_rate_source_id=other.distinct_rate_source_id,
                        confidence=self._confidence_for_reason(reason),
                    )
                )
        return candidates

    def _candidate_reason(self, a: MarketRegistryEntry, b: MarketRegistryEntry) -> Optional[ReasonCode]:
        if a.distinct_rate_source_id and a.distinct_rate_source_id == b.distinct_rate_source_id:
            return ReasonCode.SAME_VERIFIED_RATE_SOURCE
        rate_source = self._rate_source_claiming(a.registry_id)
        if rate_source and b.registry_id in rate_source.related_registry_ids:
            return ReasonCode.SAME_VERIFIED_PROGRAM
        if a.legal_underwriter and a.legal_underwriter == b.legal_underwriter:
            return ReasonCode.SAME_UNDERWRITER_POSSIBLE_DUPLICATE
        if a.insurer_group and a.insurer_group == b.insurer_group:
            return ReasonCode.SAME_GROUP_ONLY_INSUFFICIENT
        return None

    @staticmethod
    def _confidence_for_reason(reason: ReasonCode) -> Confidence:
        if reason in (ReasonCode.SAME_VERIFIED_RATE_SOURCE, ReasonCode.SAME_VERIFIED_PROGRAM):
            return Confidence.HIGH
        return Confidence.LOW

    # --- pairwise decision (explainable) --------------------------------

    def evaluate_pair(self, registry_id_a: str, registry_id_b: str) -> DeduplicationDecision:
        a = self._require_entry(registry_id_a)
        b = self._require_entry(registry_id_b)
        classified = self._classify(a, b)
        decision = DeduplicationDecision(
            registry_id=a.registry_id,
            candidate_registry_id=b.registry_id,
            evaluated_at=datetime.now(timezone.utc),
            **classified,
        )
        logger.debug(
            "deduplication decision",
            extra={
                "workflow": "deduplication",
                "workflow_stage": "evaluate_pair",
                "registry_id": a.registry_id,
                "candidate_registry_id": b.registry_id,
                "distinct_rate_source_id": decision.distinct_rate_source_id,
                "dedup_status": decision.decision.value,
                "reason_code": decision.reason_code.value,
                "confidence": decision.confidence.value,
            },
        )
        return decision

    def _classify(self, a: MarketRegistryEntry, b: MarketRegistryEntry) -> dict:
        """Deterministic priority: verified id > verified program > underwriter
        candidate > group candidate > unresolved."""
        # 1) same explicit verified id -> confirmed
        if a.distinct_rate_source_id and a.distinct_rate_source_id == b.distinct_rate_source_id:
            return {
                "decision": DeduplicationStatus.DUPLICATE_CONFIRMED,
                "distinct_rate_source_id": a.distinct_rate_source_id,
                "reason_code": ReasonCode.SAME_VERIFIED_RATE_SOURCE,
                "evidence": [f"both routes explicitly map to {a.distinct_rate_source_id}"],
                "confidence": Confidence.HIGH,
            }
        # different explicit verified ids -> distinct programs
        if a.distinct_rate_source_id and b.distinct_rate_source_id:
            return {
                "decision": DeduplicationStatus.UNIQUE,
                "distinct_rate_source_id": None,
                "reason_code": ReasonCode.EXPLICITLY_DISTINCT_PROGRAM,
                "evidence": [
                    f"route {a.registry_id} maps to {a.distinct_rate_source_id}, "
                    f"route {b.registry_id} maps to {b.distinct_rate_source_id}"
                ],
                "confidence": Confidence.HIGH,
            }
        # 2) verified program lists both routes -> confirmed
        rate_source = self._rate_source_claiming(a.registry_id)
        if rate_source and b.registry_id in rate_source.related_registry_ids:
            return {
                "decision": DeduplicationStatus.DUPLICATE_CONFIRMED,
                "distinct_rate_source_id": rate_source.distinct_rate_source_id,
                "reason_code": ReasonCode.SAME_VERIFIED_PROGRAM,
                "evidence": [
                    f"verified rate source {rate_source.distinct_rate_source_id} lists both routes"
                ],
                "confidence": Confidence.HIGH,
            }
        # 3) same legal underwriter -> candidate only
        if a.legal_underwriter and a.legal_underwriter == b.legal_underwriter:
            return {
                "decision": DeduplicationStatus.DUPLICATE_POSSIBLE,
                "distinct_rate_source_id": None,
                "reason_code": ReasonCode.SAME_UNDERWRITER_POSSIBLE_DUPLICATE,
                "evidence": ["shared legal underwriter; no confirmed program"],
                "confidence": Confidence.LOW,
            }
        # 4) same insurer group only -> candidate only (NOT auto-collapsed)
        if a.insurer_group and a.insurer_group == b.insurer_group:
            return {
                "decision": DeduplicationStatus.DUPLICATE_POSSIBLE,
                "distinct_rate_source_id": None,
                "reason_code": ReasonCode.SAME_GROUP_ONLY_INSUFFICIENT,
                "evidence": ["shared insurer group only; insufficient to confirm"],
                "confidence": Confidence.LOW,
            }
        return {
            "decision": DeduplicationStatus.UNRESOLVED,
            "distinct_rate_source_id": None,
            "reason_code": ReasonCode.INSUFFICIENT_EVIDENCE,
            "evidence": [],
            "confidence": Confidence.LOW,
        }

    # --- views / metrics -------------------------------------------------

    def get_duplicate_groups(self) -> list[DuplicateGroup]:
        groups: dict[str, list[str]] = {}
        for entry in self._registry.list_markets():
            if entry.distinct_rate_source_id:
                groups.setdefault(entry.distinct_rate_source_id, []).append(entry.registry_id)
        return [
            DuplicateGroup(distinct_rate_source_id=key, registry_ids=sorted(value))
            for key, value in sorted(groups.items())
            if len(value) >= 2
        ]

    def get_unresolved_mappings(self) -> list[MarketRegistryEntry]:
        return [e for e in self._registry.list_markets() if not e.distinct_rate_source_id]

    def count_distinct_rate_sources(self) -> int:
        return len(
            {e.distinct_rate_source_id for e in self._registry.list_markets() if e.distinct_rate_source_id}
        )

    def deduplicated_registry_view(self) -> list[DeduplicatedMarket]:
        """Confirmed duplicates collapse to one row; possible/unresolved stay visible."""
        view: list[DeduplicatedMarket] = []
        emitted_sources: set[str] = set()
        for entry in sorted(self._registry.list_markets(), key=lambda e: e.registry_id):
            if entry.distinct_rate_source_id:
                source = entry.distinct_rate_source_id
                members = [
                    e.registry_id
                    for e in self._registry.list_markets()
                    if e.distinct_rate_source_id == source
                ]
                if source in emitted_sources:
                    continue  # suppressed duplicate route (still in group_members)
                emitted_sources.add(source)
                view.append(
                    DeduplicatedMarket(
                        registry_id=entry.registry_id,
                        distinct_rate_source_id=source,
                        deduplication_status=(
                            DeduplicationStatus.DUPLICATE_CONFIRMED
                            if len(members) > 1
                            else DeduplicationStatus.UNIQUE
                        ),
                        group_members=sorted(members),
                    )
                )
            else:
                view.append(
                    DeduplicatedMarket(
                        registry_id=entry.registry_id,
                        distinct_rate_source_id=None,
                        deduplication_status=DeduplicationStatus.UNRESOLVED,
                        group_members=[entry.registry_id],
                    )
                )
        return view

    def metrics(self) -> dict[str, int]:
        """Deterministic metrics foundation (brief's duplicate-suppression metric)."""
        entries = self._registry.list_markets()
        raw = len(entries)
        confirmed_sources = self.count_distinct_rate_sources()
        confirmed_duplicates = sum(
            max(0, len(group.registry_ids) - 1) for group in self.get_duplicate_groups()
        )
        unresolved = len(self.get_unresolved_mappings())
        possible_ids: set[str] = set()
        for entry in entries:
            for candidate in self.find_duplicate_candidates(entry.registry_id):
                if candidate.reason_code in (
                    ReasonCode.SAME_UNDERWRITER_POSSIBLE_DUPLICATE,
                    ReasonCode.SAME_GROUP_ONLY_INSUFFICIENT,
                ):
                    possible_ids.add(entry.registry_id)
                    break
        return {
            "raw_route_count": raw,
            "confirmed_rate_sources": confirmed_sources,
            "confirmed_duplicates": confirmed_duplicates,
            "unresolved_mappings": unresolved,
            "possible_duplicates": len(possible_ids),
        }

    def trace_metadata(self) -> dict[str, object]:
        """Safe, non-sensitive metadata for LangSmith/logs (counts only)."""
        metrics = self.metrics()
        return {
            **metrics,
            "rate_source_records": len(self._rate_sources),
            "reason_code_counts": self._reason_code_counts(),
        }

    def _reason_code_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._registry.list_markets():
            for candidate in self.find_duplicate_candidates(entry.registry_id):
                key = candidate.reason_code.value
                counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))


_service: Optional[RateSourceDeduplicationService] = None


def get_deduplication_service() -> RateSourceDeduplicationService:
    """Cached singleton used by the API layer."""
    global _service
    if _service is None:
        _service = RateSourceDeduplicationService()
    return _service
