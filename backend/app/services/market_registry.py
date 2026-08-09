"""Deterministic, data-driven Ontario market registry service (Issue #3).

Loads every ``*.json`` market dataset from the registry directory, validates
each record with Pydantic, enforces ``registry_id`` uniqueness, and exposes
read-only query helpers.

This layer answers "what records exist?" - route selection/planning is Issue #6.
The registry holds PUBLIC market data only; no applicant PII is ever present.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from ..core.config import BACKEND_ROOT, get_settings
from ..models.insurance.enums import InsuranceType
from ..models.registry import (
    DistributionType,
    MarketRegistryEntry,
    ProductScope,
    RegistryStatus,
)

logger = logging.getLogger(__name__)


class RegistryLoadError(RuntimeError):
    """Raised when registry data is invalid or contains duplicate registry_ids."""


def default_registry_dir() -> Path:
    """Resolve the registry data directory (CWD-independent)."""
    settings = get_settings()
    if settings.market_registry_dir:
        return Path(settings.market_registry_dir)
    return BACKEND_ROOT / "data" / "market_registry"


class MarketRegistryService:
    """Read-only, deterministic registry over JSON market datasets."""

    def __init__(self, registry_dir: Optional[Path] = None) -> None:
        self._registry_dir = Path(registry_dir) if registry_dir else default_registry_dir()
        self._entries: dict[str, MarketRegistryEntry] = {}
        self._load_all()

    # --- loading ----------------------------------------------------

    def _load_all(self) -> None:
        self._entries = {}
        if not self._registry_dir.exists():
            logger.warning(
                "market registry directory not found",
                extra={"workflow": "market_registry", "workflow_stage": "load", "status": "missing"},
            )
            return
        for path in sorted(self._registry_dir.glob("*.json")):
            self._load_file(path)
        logger.info(
            "market registry loaded",
            extra={
                "workflow": "market_registry",
                "workflow_stage": "load",
                "status": "ok",
                "result_count": len(self._entries),
            },
        )

    def _load_file(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryLoadError(f"failed to read registry file {path.name}") from exc
        records = raw.get("records", raw) if isinstance(raw, dict) else raw
        if not isinstance(records, list):
            raise RegistryLoadError(f"registry file {path.name} must contain a list of records")
        for item in records:
            try:
                entry = MarketRegistryEntry.model_validate(item)
            except ValidationError as exc:
                raise RegistryLoadError(f"invalid registry record in {path.name}: {exc}") from exc
            if entry.registry_id in self._entries:
                raise RegistryLoadError(f"duplicate registry_id {entry.registry_id!r} in {path.name}")
            self._entries[entry.registry_id] = entry

    # --- queries ----------------------------------------------------

    def list_markets(self) -> list[MarketRegistryEntry]:
        """All records, sorted by registry_id (deterministic)."""
        return sorted(self._entries.values(), key=lambda entry: entry.registry_id)

    def get_by_registry_id(self, registry_id: str) -> Optional[MarketRegistryEntry]:
        return self._entries.get(registry_id.strip())

    def filter_by_product_type(self, product_type: InsuranceType) -> list[MarketRegistryEntry]:
        return [entry for entry in self.list_markets() if entry.product_type is product_type]

    def filter_by_distribution_type(self, distribution_type: DistributionType) -> list[MarketRegistryEntry]:
        return [entry for entry in self.list_markets() if entry.distribution_type is distribution_type]

    def filter_by_product_scope(self, product_scope: ProductScope) -> list[MarketRegistryEntry]:
        return [entry for entry in self.list_markets() if entry.product_scope is product_scope]

    def find_by_distinct_rate_source_id(self, rate_source_id: str) -> list[MarketRegistryEntry]:
        return [entry for entry in self.list_markets() if entry.distinct_rate_source_id == rate_source_id]

    def applicable(
        self, product_type: InsuranceType = InsuranceType.AUTO, active_only: bool = True
    ) -> list[MarketRegistryEntry]:
        records = self.filter_by_product_type(product_type)
        if active_only:
            records = [entry for entry in records if entry.active]
        return records

    # --- freshness / verification ------------------------------------

    def verified_records(self) -> list[MarketRegistryEntry]:
        return [entry for entry in self.list_markets() if entry.status is RegistryStatus.VERIFIED]

    def records_missing_verification(self) -> list[MarketRegistryEntry]:
        return [
            entry
            for entry in self.list_markets()
            if entry.status is not RegistryStatus.VERIFIED or entry.last_verified_at is None
        ]

    def freshness_percentage(self) -> float:
        total = len(self._entries)
        if total == 0:
            return 0.0
        return round(100.0 * len(self.verified_records()) / total, 2)

    def distribution_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._entries.values():
            key = entry.distribution_type.value
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    def product_scope_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._entries.values():
            key = entry.product_scope.value
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    def trace_metadata(self) -> dict[str, object]:
        """Safe, non-sensitive metadata for LangSmith/logs (counts only)."""
        return {
            "registry_total": len(self._entries),
            "product_type_counts": {
                product.value: len(self.filter_by_product_type(product)) for product in InsuranceType
            },
            "verified_count": len(self.verified_records()),
            "unverified_count": len(self.records_missing_verification()),
            "freshness_percentage": self.freshness_percentage(),
        }


_service: Optional[MarketRegistryService] = None


def get_market_registry_service() -> MarketRegistryService:
    """Cached singleton used by the API layer."""
    global _service
    if _service is None:
        _service = MarketRegistryService()
    return _service
