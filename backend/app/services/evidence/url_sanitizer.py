"""Reusable safe-URL sanitizer (Issue #10, Prompt 1).

Evidence/audit persistence must NEVER store raw navigation URLs that can carry
query parameters, fragments, tokens, or applicant data.

Rules:
- strip the query string entirely (unless explicitly allowlisted - none today);
- strip the fragment entirely;
- drop userinfo credentials;
- keep only ``netloc + path`` (no scheme, no query, no fragment), e.g.
  ``https://provider.ca/quote?postal=M5V123&token=SECRET`` -> ``provider.ca/quote``;
- expose an optional page-signature-style safe path if callers have one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SafeUrlInfo:
    """Safe components of a URL - never contains query/fragment/tokens."""

    host: Optional[str] = None
    path: str = ""
    safe_url: Optional[str] = None  # e.g. "provider.ca/quote"
    page_signature: Optional[str] = None  # caller-provided stable route signature


def sanitize_url(url: Optional[str]) -> SafeUrlInfo:
    """Return a safe projection of a URL (or empty when unparseable)."""
    if not url:
        return SafeUrlInfo(host=None, path="", safe_url=None)
    try:
        parts = urlsplit(url)
    except ValueError:
        return SafeUrlInfo(host=None, path="", safe_url=None)
    host = parts.hostname or None
    if host:
        try:
            if parts.port:
                host = f"{host}:{parts.port}"
        except ValueError:  # pragma: no cover - malformed port
            pass
    path = parts.path or ""
    # Build netloc without userinfo/query/fragment (host[:port] + path only).
    safe_url = f"{host}{path}" if host else path
    return SafeUrlInfo(host=host, path=path, safe_url=safe_url)


def safe_url_only(url: Optional[str]) -> Optional[str]:
    """Convenience: return just the sanitized ``netloc + path`` (or None)."""
    return sanitize_url(url).safe_url
