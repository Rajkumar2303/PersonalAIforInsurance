"""Data-driven coverage-mapping registry (Issue #11, Prompt 1).

Loads ``auto_coverage_mappings.json`` from ``BACKEND_ROOT/data/normalization``
(or an env override) into a deterministic ``CoverageMappingRegistry``. The
engine (``coverage.py``) only consumes this registry - changing alias/label
rules is a config change + ``rule_version`` bump, never engine code. No fuzzy
matching, no ``if registry_id`` branching.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ...core.config import BACKEND_ROOT, get_settings
from ...models.normalization import CoverageItemKey

logger = logging.getLogger(__name__)


class NormalizationConfigError(RuntimeError):
    """Raised when coverage-mapping data is invalid or unreadable."""


class CoverageMappingRule(BaseModel):
    """One alias->canonical mapping rule from the JSON data file."""

    model_config = ConfigDict(extra="forbid")

    canonical_key: CoverageItemKey
    aliases: list[str]
    value_type: str = "none"  # limit | deductible | money | boolean | endorsement | none
    default_state: str = "included"

    @field_validator("aliases")
    @classmethod
    def _aliases_nonempty(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for alias in value:
            alias = _normalize_label(alias)
            if alias:
                normalized.append(alias)
        if not normalized:
            raise ValueError("aliases must contain at least one non-empty alias")
        return normalized


class CoverageMappingsFile(BaseModel):
    """Top-level shape of auto_coverage_mappings.json."""

    model_config = ConfigDict(extra="forbid")

    rule_version: str = "1"
    currency: str = "CAD"
    annualization: dict[str, str] = {}
    value_parsers: dict[str, str] = {}
    coverage_mappings: list[CoverageMappingRule]


def _normalize_label(label: str) -> str:
    """Deterministic, exact-match label normalization (no fuzzy matching).

    Lowercases and collapses punctuation (commas, slashes, hyphens, colons,
    parentheses, dollar signs) to spaces so "Third-Party Liability: $2M"
    normalizes to "third party liability 2m" and matches alias
    "third party liability" as a leading phrase. Matching is still exact after
    normalization.
    """
    for ch in (",", "/", "-", ":", "(", ")", "$"):
        label = label.replace(ch, " ")
    return " ".join(label.lower().split())


def default_normalization_data_dir() -> Path:
    settings = get_settings()
    if settings.normalization_data_dir:
        return Path(settings.normalization_data_dir)
    return BACKEND_ROOT / "data" / "normalization"


class CoverageMappingRegistry:
    """Deterministic, read-only registry of coverage-mapping rules."""

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._data_dir = Path(data_dir) if data_dir else default_normalization_data_dir()
        self._rules: dict[str, CoverageMappingRule] = {}  # canonical_key -> rule
        self._aliases: dict[str, str] = {}  # normalized alias -> canonical_key
        self.rule_version: str = "1"
        self.currency: str = "CAD"
        self.annualization: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        path = self._data_dir / "auto_coverage_mappings.json"
        if not path.exists():
            raise NormalizationConfigError(f"coverage mappings file not found: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NormalizationConfigError(f"failed to read coverage mappings {path.name}") from exc
        try:
            data = CoverageMappingsFile.model_validate(raw)
        except ValidationError as exc:
            raise NormalizationConfigError(f"invalid coverage mappings file {path.name}") from exc

        self.rule_version = data.rule_version
        self.currency = data.currency
        self.annualization = data.annualization

        self._rules = {}
        self._aliases = {}
        for rule in data.coverage_mappings:
            if rule.canonical_key.value in self._rules:
                raise NormalizationConfigError(
                    f"duplicate canonical_key {rule.canonical_key.value!r} in coverage mappings"
                )
            self._rules[rule.canonical_key.value] = rule
            for alias in rule.aliases:
                existing = self._aliases.get(alias)
                if existing is not None and existing != rule.canonical_key.value:
                    raise NormalizationConfigError(
                        f"alias {alias!r} maps to both {existing!r} and "
                        f"{rule.canonical_key.value!r}"
                    )
                self._aliases[alias] = rule.canonical_key.value
        logger.info(
            "coverage mapping registry loaded",
            extra={
                "workflow": "normalization",
                "workflow_stage": "registry_load",
                "status": "ok",
                "rule_version": self.rule_version,
                "rule_count": len(self._rules),
            },
        )

    # --- lookup -----------------------------------------------------

    def resolve(self, label: str) -> Optional[CoverageMappingRule]:
        """Deterministic canonical-phrase lookup (no fuzzy matching).

        A label matches an alias when the normalized label EQUALS the alias or
        STARTS WITH the alias followed by a space - i.e. the coverage name is
        the leading phrase of the safe label segment, e.g.
        "Third Party Liability - $2,000,000" -> alias "third party liability".
        The LONGEST matching alias wins so a short alias never shadows a more
        specific phrase. Unmatched labels return None (preserved unmapped).
        """
        norm = _normalize_label(label)
        best_alias: Optional[str] = None
        for alias in self._aliases:
            if norm == alias or norm.startswith(alias + " "):
                if best_alias is None or len(alias) > len(best_alias):
                    best_alias = alias
        if best_alias is None:
            return None
        return self._rules.get(self._aliases[best_alias])

    def rule_for(self, key: CoverageItemKey) -> Optional[CoverageMappingRule]:
        return self._rules.get(key.value)

    @property
    def rule_count(self) -> int:
        return len(self._rules)


@lru_cache(maxsize=1)
def get_coverage_mapping_registry() -> CoverageMappingRegistry:
    return CoverageMappingRegistry()
