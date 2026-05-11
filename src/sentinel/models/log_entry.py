"""Log models — LogLevel, LogEntry, LogQuery, LogAnalysis."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class LogLevel(StrEnum):
    """Standard log severity levels, matching values in scenario JSON files."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogEntry(BaseModel):
    """A single log line from a service.

    Matches the log objects embedded in scenario JSON files:
    {"ts": "...", "level": "ERROR", "service": "...", "message": "..."}
    """

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    level: LogLevel
    service: str
    message: str
    trace_id: str | None = None


class LogQuery(BaseModel):
    """Parameters for fetching logs from the log store (or generator in MVP)."""

    service: str
    start_time: datetime
    end_time: datetime
    level_filter: LogLevel | None = None
    keyword_filter: str | None = None


class LogAnalysis(BaseModel):
    """Findings produced by the Log Analyst agent after processing raw logs.

    key_log_lines are the specific entries most relevant to the hypothesis —
    stack traces, the first error occurrence, rate-change boundaries, etc.
    """

    error_patterns: list[str] = []
    anomaly_summary: str
    key_log_lines: list[LogEntry] = []
    hypothesis: str
