"""Tests for the sensitive-data redaction utility.

Privacy guarantee: licence numbers, addresses, DOB, VIN, claims, phone,
email, and voice/transcript data never appear in logs, traces, or fixtures.
"""

from __future__ import annotations

import logging

from app.core.redaction import REDACTED, is_sensitive, redact_data, redact_kwargs, redact_text, redact_value
from app.core.logging import RedactingContextFilter


def test_redact_sensitive_keys() -> None:
    """Sensitive fields by name are redacted; benign fields pass through."""
    data = {
        "driver_licence_number": "L1234-56789-01234",
        "address": "123 Main St, Toronto, ON",
        "dob": "1980-01-01",
        "vin": "1HGCM82633A004352",
        "email": "driver@example.com",
        "phone": "416-555-1234",
        "claims": {"history": "one accident"},
        "make": "Honda",
    }
    result = redact_data(data)

    assert result["driver_licence_number"] == REDACTED
    assert result["address"] == REDACTED
    assert result["dob"] == REDACTED
    assert result["vin"] == REDACTED
    assert result["email"] == REDACTED
    assert result["phone"] == REDACTED
    assert result["claims"] == REDACTED
    assert result["make"] == "Honda"


def test_redact_text_patterns() -> None:
    """Free-form text has emails, phones, and licence numbers masked."""
    text = "Contact driver@example.com or 416-555-1234. Licence L1234-56789-01234."
    redacted = redact_text(text)

    assert "driver@example.com" not in redacted
    assert "416-555-1234" not in redacted
    assert "L1234-56789-01234" not in redacted
    assert REDACTED in redacted


def test_redact_nested_structures() -> None:
    """Nested dicts/lists are traversed recursively."""
    data = {
        "profile": {"email": "a@b.com"},
        "list": [{"phone": "416-555-1234"}],
        "ok": "value",
    }
    result = redact_data(data)
    assert result["profile"]["email"] == REDACTED
    assert result["list"][0]["phone"] == REDACTED
    assert result["ok"] == "value"


def test_is_sensitive() -> None:
    """Field-name sensitivity checks."""
    assert is_sensitive("driver_licence_number")
    assert is_sensitive("email")
    assert is_sensitive("claim_history")
    assert not is_sensitive("make")


def test_redact_value_with_key() -> None:
    """redact_value honors an explicit key."""
    assert redact_value("secret", key="vin") == REDACTED
    assert redact_value("honda", key="make") == "honda"


def test_redact_kwargs() -> None:
    """Structured logging kwargs are redacted by key."""
    result = redact_kwargs(request_id="req-1", phone="416-555-1234", make="Honda")
    assert result["request_id"] == "req-1"
    assert result["phone"] == REDACTED
    assert result["make"] == "Honda"


def test_logging_filter_redacts_message() -> None:
    """The logging filter redacts sensitive text and injects defaults."""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="email a@b.com phone 416-555-1234",
        args=(),
        exc_info=None,
    )
    RedactingContextFilter().filter(record)

    assert "a@b.com" not in record.getMessage()
    assert "416-555-1234" not in record.getMessage()
    assert record.request_id == "-"
    assert record.trace_id == "-"
