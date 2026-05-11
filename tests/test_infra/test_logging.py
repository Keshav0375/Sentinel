"""Tests for sentinel.infra.logging."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import structlog
import structlog.contextvars

from sentinel.infra.logging import configure_logging, get_logger, incident_context


def _capture_log(fn: Callable[[], None]) -> dict:  # type: ignore[name-defined]
    """Run *fn*, capture structlog's PrintLogger stdout, return parsed JSON."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    line = buf.getvalue().strip()
    return json.loads(line)


def setup_function() -> None:
    """Re-configure logging before each test and clear any bound contextvars."""
    configure_logging("DEBUG")
    structlog.contextvars.clear_contextvars()


def test_configure_logging_does_not_raise() -> None:
    configure_logging("INFO")
    configure_logging("DEBUG")


def test_get_logger_returns_logger() -> None:
    logger = get_logger(__name__)
    assert logger is not None
    assert hasattr(logger, "info")
    assert hasattr(logger, "error")


def test_log_line_is_valid_json() -> None:
    logger = get_logger("test")
    result = _capture_log(lambda: logger.info("hello"))
    assert result["event"] == "hello"
    assert "level" in result
    assert "timestamp" in result


def test_incident_context_binds_incident_id() -> None:
    logger = get_logger("test")
    with incident_context("inc-test-001"):
        result = _capture_log(lambda: logger.info("inside context"))
    assert result.get("incident_id") == "inc-test-001"


def test_incident_context_clears_after_exit() -> None:
    logger = get_logger("test")
    with incident_context("inc-test-002"):
        pass
    # After context exits, incident_id must not appear.
    result = _capture_log(lambda: logger.info("outside context"))
    assert "incident_id" not in result


from collections.abc import Callable  # noqa: E402  (import after use for readability)
