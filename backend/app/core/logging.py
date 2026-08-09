"""Structured, privacy-aware application logging.

Every emitted record carries structured fields — ``request_id``,
``trace_id``, ``workflow``, ``workflow_stage``, ``status``,
``error_type`` — sourced from a contextvars-backed per-request/run
context. Sensitive values are redacted from log messages by the
``RedactingContextFilter`` before output.

Usage in application code::

    from app.core.logging import set_log_context, clear_log_context

    set_log_context(request_id="...", workflow="...", workflow_stage="...")
    logger.info("some safe message")
    clear_log_context()
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

from .redaction import redact_text

_log_context: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})

# Fields always present on a log record (populated by the filter).
STRUCTURED_FIELDS: tuple[str, ...] = (
    "request_id",
    "trace_id",
    "workflow",
    "workflow_stage",
    "status",
    "error_type",
)


def set_log_context(**fields: Any) -> None:
    """Set structured fields for the current context (merges with existing)."""
    _log_context.set({**_log_context.get(), **fields})


def clear_log_context() -> None:
    """Reset the context to empty."""
    _log_context.set({})


def get_log_context() -> dict[str, Any]:
    """Return a copy of the current context."""
    return dict(_log_context.get())


class RedactingContextFilter(logging.Filter):
    """Attach structured context fields and redact sensitive message text."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _log_context.get()
        # Context wins unless the caller supplied the field explicitly via `extra`.
        for field in STRUCTURED_FIELDS:
            if not hasattr(record, field):
                setattr(record, field, ctx.get(field, "-"))
        # Redact the rendered message.
        safe_message = redact_text(record.getMessage())
        record.msg = safe_message
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Formatter that emits the structured fields in a single readable line."""

    def __init__(self) -> None:
        super().__init__(
            fmt=(
                "%(asctime)s %(levelname)s %(name)s "
                "request_id=%(request_id)s trace_id=%(trace_id)s "
                "workflow=%(workflow)s workflow_stage=%(workflow_stage)s "
                "status=%(status)s error_type=%(error_type)s "
                "%(message)s"
            )
        )


def get_logger(name: str) -> logging.Logger:
    """Return a logger configured by ``setup_logging``."""
    return logging.getLogger(name)


def setup_logging(level: str | int | None = None) -> None:
    """Configure root logging once.

    Safe to call repeatedly (e.g. on test setup or app reload) — existing
    handlers are replaced, never duplicated.
    """
    resolved_level = level if level is not None else (
        logging.DEBUG if _is_dev_env() else logging.INFO
    )
    root = logging.getLogger()
    root.setLevel(resolved_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    # Keep third-party loggers from flooding DEBUG logs with non-domain noise
    # (e.g. langsmith HTTP tracing control, urllib3 connection pools, uvicorn
    # proactor diagnostics) now that LangSmith tracing may be enabled.
    for noisy_name in ("langsmith", "urllib3", "httpcore", "httpx", "asyncio", "watchfiles"):
        logging.getLogger(noisy_name).setLevel(logging.WARNING)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(resolved_level)
    handler.setFormatter(RedactingFormatter())
    handler.addFilter(RedactingContextFilter())
    root.addHandler(handler)


def _is_dev_env() -> bool:
    from .config import get_settings  # local import to avoid a cycle

    try:
        return get_settings().is_development
    except Exception:  # pragma: no cover - defensive
        return False
