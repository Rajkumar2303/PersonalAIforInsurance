"""Canonical field-path utilities for the insurance profile (Issue #3).

Locked canonical syntax (stable across intake/browser/voice layers):

    applicant.identity.date_of_birth
    applicant.address.street
    product_data.drivers[0].licence.licence_number
    product_data.vehicles[0].use.annual_kilometres
    product_data.coverage.third_party_liability.selected_limit

Rules:
- segments are attribute names joined by ``.``
- list indexes use ``[n]`` (0-based), e.g. ``drivers[0]``
- the top-level object is the ``InsuranceProfile`` itself (no leading prefix)
- ``product_data`` is the canonical product container (NOT an ``auto`` alias)

These paths are what the Issue #5 intake engine, Issue #7 browser agent and
Issue #9 voice agent will use to locate/update fields without knowing Pydantic
internals. No per-field branching lives here - resolution is a generic
attribute/index walk, so adding a field never requires changing this module.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_INDEXED_SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]$")


class FieldPathError(ValueError):
    """Raised for malformed, unknown, or out-of-range canonical field paths."""


def parse_field_path(path: str) -> tuple[str | int, ...]:
    """Parse ``product_data.drivers[0].licence.licence_number`` into segments.

    Returns a tuple of attribute names and int list indexes. Raises
    ``FieldPathError`` on malformed syntax.
    """
    if not isinstance(path, str) or not path.strip():
        raise FieldPathError("field path must be a non-empty string")
    segments: list[str | int] = []
    for raw in path.strip().split("."):
        if not raw:
            raise FieldPathError(f"invalid empty segment in path: {path!r}")
        match = _INDEXED_SEGMENT.fullmatch(raw)
        if match:
            segments.append(match.group(1))
            segments.append(int(match.group(2)))
        else:
            if "[" in raw or "]" in raw:
                raise FieldPathError(f"invalid indexed segment {raw!r} in path {path!r}")
            segments.append(raw)
    return tuple(segments)


def format_field_path(segments: Iterable[str | int]) -> str:
    """Format segments back to the canonical dotted/indexed string."""
    out = ""
    for segment in segments:
        if isinstance(segment, int):
            out += f"[{segment}]"
        else:
            if out:
                out += "."
            out += segment
    return out


def resolve(obj: Any, path: str) -> Any:
    """Resolve a canonical path against a model (attribute/index walk).

    Raises ``FieldPathError`` for unknown fields or out-of-range indexes.
    """
    current = obj
    for segment in parse_field_path(path):
        if isinstance(segment, int):
            try:
                current = current[segment]
            except (IndexError, TypeError) as exc:
                raise FieldPathError(f"list index {segment} out of range in path {path!r}") from exc
        else:
            try:
                current = getattr(current, segment)
            except AttributeError as exc:
                raise FieldPathError(f"unknown field {segment!r} in path {path!r}") from exc
    return current


def _value_is_missing(value: Any) -> bool:
    """A value is 'missing' when unset (None/empty string/empty collection)."""
    if value is None or value == "":
        return True
    if isinstance(value, (list, dict)) and not value:
        return True
    return False


def is_missing(obj: Any, path: str) -> bool:
    """True when the value at ``path`` is unset (or the path cannot resolve)."""
    try:
        value = resolve(obj, path)
    except FieldPathError:
        return True
    return _value_is_missing(value)
