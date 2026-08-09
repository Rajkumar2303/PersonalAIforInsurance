"""Sensitive-data redaction utilities.

Privacy policy: driver's licence numbers, full addresses, date of birth,
VINs, claims information, phone numbers, email addresses, voice/transcript
data, and other sensitive insurance fields must NEVER appear in logs,
traces, prompts, screenshots, test fixtures, or source control.

This module provides the reusable redaction used by the logging layer, the
tracing layer, and any future module that handles applicant data.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Key-name patterns. A value is redacted whenever its key matches any
# pattern (case-insensitive). Over-matching is intentional and safe.
SENSITIVE_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"licence|license", re.IGNORECASE),
    re.compile(r"\bdriver", re.IGNORECASE),
    re.compile(r"address", re.IGNORECASE),
    re.compile(r"\bdob\b|birth", re.IGNORECASE),
    re.compile(r"\bvin\b|vehicle.?ident", re.IGNORECASE),
    re.compile(r"claim", re.IGNORECASE),
    re.compile(r"phone|telephone|mobile|cell", re.IGNORECASE),
    re.compile(r"\bemail\b|e-?mail", re.IGNORECASE),
    re.compile(r"voice|transcript|recording|audio", re.IGNORECASE),
    re.compile(r"\bsin\b|social.?(insurance|security)|\bssn\b", re.IGNORECASE),
)

# Patterns applied to free-form text.
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)"
)
# Ontario driver's licence format: L####-#######-####
ONTARIO_LICENCE_PATTERN = re.compile(r"\b[A-Z]\d{4}-\d{7}-\d{4}\b")
# VIN: 17 characters, excluding I, O, and Q.
VIN_PATTERN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
# Date of birth serialized as YYYY-MM-DD.
DOB_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    EMAIL_PATTERN,
    PHONE_PATTERN,
    ONTARIO_LICENCE_PATTERN,
    VIN_PATTERN,
    DOB_PATTERN,
)


def is_sensitive(key: str) -> bool:
    """Return True if a field name is considered sensitive."""
    return any(pattern.search(key) for pattern in SENSITIVE_KEY_PATTERNS)


def redact_text(text: str) -> str:
    """Redact sensitive patterns inside a free-form string."""
    result = text
    for pattern in _TEXT_PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


def redact_data(data: Any) -> Any:
    """Recursively redact a value by sensitive keys and text patterns."""
    if isinstance(data, dict):
        return {
            key: (REDACTED if is_sensitive(str(key)) else redact_data(value))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact_data(item) for item in data]
    if isinstance(data, tuple):
        return tuple(redact_data(item) for item in data)
    if isinstance(data, set):
        return {redact_data(item) for item in data}
    if isinstance(data, str):
        return redact_text(data)
    return data


def redact_value(value: Any, key: str | None = None) -> Any:
    """Redact a single value, honoring an optional field name."""
    if key is not None and is_sensitive(key):
        return REDACTED
    return redact_data(value)


def redact_kwargs(**kwargs: Any) -> dict[str, Any]:
    """Redact structured keyword arguments by their field name.

    Intended for structured logging/tracing metadata calls such as
    ``redact_kwargs(request_id=..., phone=...)``.
    """
    return redact_data(kwargs)
