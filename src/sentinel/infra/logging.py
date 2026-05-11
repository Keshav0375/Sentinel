"""Structured logging setup — structlog with JSON output and incident_id binding."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import structlog
import structlog.contextvars


def configure_logging(log_level: str = "INFO") -> None:
    """Configure structlog for JSON output. Call once at application startup."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            # Merge any contextvars (e.g. incident_id) into every log event.
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.ExceptionRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    """Return a structlog bound logger for *name*.

    Typed as Any because the concrete type (FilteringBoundLogger) varies with
    the configured wrapper_class and isn't exported as a stable public type.
    Callers should treat the return value as a standard structlog logger with
    .debug/.info/.warning/.error/.critical methods.
    """
    return structlog.get_logger(name)


@contextmanager
def incident_context(incident_id: str) -> Generator[None, None, None]:
    """Bind *incident_id* to every log line emitted within this scope.

    Uses structlog's contextvars integration so it is async-safe — each
    coroutine/task has its own copy of the context.

    Usage::

        with incident_context("inc-001"):
            logger.info("triage complete")  # → {"incident_id": "inc-001", ...}
    """
    structlog.contextvars.bind_contextvars(incident_id=incident_id)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars("incident_id")
