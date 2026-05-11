"""Log entry generator — produces correlated logs with optional noise."""

from __future__ import annotations

from datetime import timedelta

from sentinel.generator.scenarios import Scenario
from sentinel.models.log_entry import LogEntry, LogLevel

# Services used for background noise entries — ordered by least likely to be
# the affected service in any scenario.
_NOISE_SERVICE_POOL = [
    "cdn-proxy",
    "analytics-pipeline",
    "notification-service",
    "user-service",
    "order-service",
]

_NOISE_MESSAGES = [
    "Scheduled health check passed. Liveness probe: OK.",
    "Hourly cache eviction completed. 1,024 stale entries removed.",
    "Routine metrics flush completed. 0 anomalies detected.",
]


def generate_logs(scenario: Scenario, noise: bool = True) -> list[LogEntry]:
    """Convert scenario log entries to LogEntry objects, optionally with noise.

    Converts ScenarioLogEntry.ts → LogEntry.timestamp. If noise=True, injects
    a small number of INFO-level background log lines from unrelated services,
    spread evenly through the scenario's time window. The combined list is
    returned sorted by timestamp ascending.

    Args:
        scenario: Loaded and validated Scenario.
        noise: When True, mix in background INFO logs from unaffected services.
            Defaults to True to match realistic log stream conditions.

    Returns:
        List of LogEntry objects sorted by timestamp ascending.
    """
    entries: list[LogEntry] = [
        LogEntry(
            timestamp=entry.ts,
            level=entry.level,
            service=entry.service,
            message=entry.message,
            trace_id=entry.trace_id,
        )
        for entry in scenario.logs
    ]

    if noise and entries:
        entries.extend(_build_noise(scenario, entries))

    entries.sort(key=lambda e: e.timestamp)
    return entries


def _build_noise(scenario: Scenario, signal: list[LogEntry]) -> list[LogEntry]:
    """Generate deterministic background log entries for a scenario.

    Picks services from the noise pool that are not the alert or root-cause
    service, then spaces entries evenly across the scenario's log time window.
    Deterministic — same scenario always produces the same noise entries.
    """
    exclude = {scenario.alert.service, scenario.ground_truth.affected_service}
    noise_services = [s for s in _NOISE_SERVICE_POOL if s not in exclude][:3]

    if not noise_services:
        return []

    start = signal[0].timestamp
    end = signal[-1].timestamp
    window_secs = max((end - start).total_seconds(), 60.0)

    noise: list[LogEntry] = []
    count = len(noise_services)
    for i, service in enumerate(noise_services):
        offset = timedelta(seconds=window_secs * (i + 1) / (count + 1))
        noise.append(
            LogEntry(
                timestamp=start + offset,
                level=LogLevel.INFO,
                service=service,
                message=_NOISE_MESSAGES[i % len(_NOISE_MESSAGES)],
            )
        )

    return noise
