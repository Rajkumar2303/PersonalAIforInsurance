"""Sensitive-aware base model and safe/redacted serialization.

Privacy policy (see ``app/core/redaction.py``): driver's licence numbers,
full addresses, DOB, VINs, claims information, phone numbers, email
addresses, voice/transcript data and other sensitive insurance data must
NEVER appear in logs, traces, prompts, screenshots, test fixtures, or
source control.

This module reuses the existing ``app.core.redaction`` utilities (no second
redaction framework) and extends them with the schema-level sensitive field
registry, plus a redacted serialization path and redacted ``repr``/``str``
so that accidentally logging a profile never leaks raw PII.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ...core.redaction import REDACTED, is_sensitive, redact_text

SCHEMA_VERSION = "1.1"

# Schema-level sensitive field names, beyond the generic key patterns already
# handled by ``app.core.redaction.is_sensitive`` (licence, driver, address,
# dob/birth, vin, claim, phone, email, voice, sin).
#
# NOTE: ``gender`` and ``marital_status`` are intentionally NOT redacted - the
# hackathon brief's sensitive list covers licence/DOB/full address/claims/VIN/
# phone/email, and these two are left visible in safe output (documented choice).
SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "legal_name",
        "name_on_licence",
        "licence_number",
        "date_of_birth",
        "dob",
        "birth_date",
        "email",
        "email_address",
        "mobile_phone",
        "home_phone",
        "work_phone",
        "street",
        "unit",
        "city",
        "postal_code",
        "vin",
        "policy_number",
        "event_details",
        "claim_details",
        "accident_details",
        "misrepresentation_details",
        "fraud_details",
        "conviction_description",
        # Free-text detail/description fields only appear on history/event models
        # (claims, licence events, fraud findings, etc.) in this schema.
        "details",
        "description",
    }
)


def _redact_tree(value: Any, sensitive: frozenset[str]) -> Any:
    """Recursively redact a dumped model tree.

    Containers (dicts/lists) are always recursed into so structural keys such as
    ``drivers`` or ``address`` are not block-redacted; only sensitive *leaf*
    values are replaced (licence number, DOB, VIN, email, phone, address
    fields, claim details, ...). Booleans are always preserved - a
    ``True``/``False`` cannot carry PII (keeps consent flags visible).
    """
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, bool):
                redacted[key] = item
            elif isinstance(item, (dict, list)):
                redacted[key] = _redact_tree(item, sensitive)
            elif str(key) in sensitive or is_sensitive(str(key)):
                redacted[key] = REDACTED
            else:
                redacted[key] = _redact_tree(item, sensitive)
        return redacted
    if isinstance(value, list):
        return [_redact_tree(item, sensitive) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class SensitiveBaseModel(BaseModel):
    """Pydantic v2 base model with a privacy-safe serialization surface.

    - ``redacted_dict()`` / ``safe_dict()``: full dump with sensitive values
      masked (reuses ``app.core.redaction``).
    - ``__repr__`` / ``__str__``: redacted, so logging a model never leaks PII.
    - ``model_dump()`` / ``model_dump_json()``: still available and explicit for
      callers that knowingly handle raw data (e.g. a route that will persist it).
    """

    def redacted_dict(self, mode: str = "json") -> dict[str, Any]:
        """Serialize the model with all sensitive values redacted."""
        return _redact_tree(self.model_dump(mode=mode), SENSITIVE_FIELD_NAMES)

    def safe_dict(self, mode: str = "json") -> dict[str, Any]:
        """Alias for ``redacted_dict`` - a safe summary representation."""
        return self.redacted_dict(mode=mode)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.redacted_dict()})"

    def __str__(self) -> str:
        return self.__repr__()
