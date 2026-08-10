"""Deterministic field -> canonical mapping (Issue #7).

No LLM and no insurer-specific branches. ``FieldMapper`` scores each visible
control against the route's ``BrowserFieldBinding.match_patterns`` (label text,
normalized label, aria-label, name, id, placeholder, role, css selector, text
regex) and maps the best match to its canonical path. Question wording or
selector changes are handled by editing the route config - never the executor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..models.browser.config import BrowserFieldBinding, BrowserRouteConfig, MatchStrategy
from ..models.browser.observation import BrowserFieldObservation, BrowserPageObservation

_MATCH_THRESHOLD = 0.7


@dataclass
class MatchedField:
    """One external field successfully mapped to a canonical path."""

    binding: BrowserFieldBinding
    observation: BrowserFieldObservation
    canonical_path: str
    score: float


def _normalize(text: str) -> str:
    """Normalize label/question text for fuzzy matching (safe, no values)."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class FieldMapper:
    """Deterministic scorer/assigner over one inspected page."""

    def map(
        self, page: BrowserPageObservation, config: BrowserRouteConfig
    ) -> tuple[list[MatchedField], list[BrowserFieldObservation]]:
        """Return (matched fields, unmatched observations)."""
        bindings = [b for b in config.field_bindings if b.enabled]
        # 1) best binding per observation
        best_for_obs: dict[int, tuple[BrowserFieldBinding, float]] = {}
        for index, obs in enumerate(page.fields):
            best: Optional[tuple[BrowserFieldBinding, float]] = None
            for binding in bindings:
                score = self._score(obs, binding)
                if score >= _MATCH_THRESHOLD and (best is None or score > best[1]):
                    best = (binding, score)
            if best is not None:
                best_for_obs[index] = best
        # 2) one observation per binding (keep highest score)
        best_for_binding: dict[str, tuple[int, float]] = {}
        for index, (binding, score) in best_for_obs.items():
            key = binding.external_field_id
            if key not in best_for_binding or score > best_for_binding[key][1]:
                best_for_binding[key] = (index, score)
        matched: list[MatchedField] = []
        matched_obs: set[int] = set()
        for binding in bindings:
            assigned = best_for_binding.get(binding.external_field_id)
            if assigned is None:
                continue
            index, score = assigned
            if index in matched_obs:
                continue
            matched_obs.add(index)
            matched.append(
                MatchedField(
                    binding=binding,
                    observation=page.fields[index],
                    canonical_path=binding.canonical_path,
                    score=score,
                )
            )
        unmatched = [obs for index, obs in enumerate(page.fields) if index not in matched_obs]
        return matched, unmatched

    def ambiguities(self, page: BrowserPageObservation, config: BrowserRouteConfig) -> list[str]:
        """Return binding ids where MORE THAN ONE control matches the same binding.

        The executor must NOT fill both when a canonical field is ambiguous; it
        pauses with an ``ambiguous_field`` observation unless config resolves it.
        """
        bindings = [b for b in config.field_bindings if b.enabled]
        ambiguous: list[str] = []
        for binding in bindings:
            matches = [obs for obs in page.fields if self._score(obs, binding) >= _MATCH_THRESHOLD]
            if len(matches) > 1:
                ambiguous.append(binding.external_field_id)
        return sorted(set(ambiguous))

    def _score(self, obs: BrowserFieldObservation, binding: BrowserFieldBinding) -> float:
        """Best score across the binding's match patterns (0..1)."""
        best = 0.0
        for pattern in binding.match_patterns:
            score = self._score_pattern(obs, pattern.strategy, pattern.value)
            best = max(best, score)
        return best

    def _score_pattern(self, obs: BrowserFieldObservation, strategy: MatchStrategy, value: str) -> float:
        if strategy is MatchStrategy.LABEL_TEXT:
            return 1.0 if obs.label and obs.label.strip().lower() == value.lower() else 0.0
        if strategy is MatchStrategy.LABEL_CONTAINS:
            return 0.9 if obs.label and value.lower() in obs.label.lower() else 0.0
        if strategy is MatchStrategy.NORMALIZED_LABEL:
            return 1.0 if obs.label and _normalize(obs.label) == _normalize(value) else 0.0
        if strategy is MatchStrategy.ARIA_LABEL:
            return 1.0 if obs.label and obs.label.strip().lower() == value.lower() else 0.0
        if strategy is MatchStrategy.NAME:
            return 1.0 if obs.name == value else 0.0
        if strategy is MatchStrategy.ID:
            return 1.0 if obs.external_field_id == value else 0.0
        if strategy is MatchStrategy.PLACEHOLDER:
            return 0.9 if obs.placeholder and value.lower() in obs.placeholder.lower() else 0.0
        if strategy is MatchStrategy.ROLE:
            return 1.0 if obs.control_type == value else 0.0
        if strategy is MatchStrategy.CSS_SELECTOR:
            # Match a simple "#id", "[name=x]", or bare id form against safe ids.
            simple = value.strip()
            if simple.startswith("#"):
                return 1.0 if obs.external_field_id == simple[1:] else 0.0
            name_match = re.match(r"\[name=[\"']?([^\"'\]]+)[\"']?\]", simple)
            if name_match:
                return 1.0 if obs.name == name_match.group(1) else 0.0
            return 1.0 if obs.external_field_id == simple else 0.0
        if strategy is MatchStrategy.TEXT_REGEX:
            haystack = " ".join(
                part
                for part in (obs.label, obs.name, obs.placeholder, obs.external_field_id)
                if part
            )
            return 0.9 if re.search(value, haystack, re.IGNORECASE) else 0.0
        return 0.0
