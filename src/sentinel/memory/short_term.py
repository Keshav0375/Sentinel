"""Short-term in-memory store — per-incident dict, in-process lifetime."""

from __future__ import annotations

from typing import Any

from sentinel.models.alert import AlertPayload
from sentinel.models.incident import IncidentStatus, TimelineEntry


class ShortTermMemory:
    """In-process, per-incident working memory.

    Stores the live state of every active incident as a plain Python dict
    keyed by incident_id. Created when an alert arrives; discarded after the
    incident resolves and the trajectory is handed to the eval system.

    All methods are synchronous — no I/O, no awaiting. The dict lives in the
    process and is shared across all agents in the same event loop.

    Typical lifecycle::

        stm.create(inc_id, alert)
        stm.add_timeline_entry(inc_id, TimelineEntry(...))
        context = stm.get_context(inc_id)
        stm.update_context(inc_id, triage_result=..., status=IncidentStatus.INVESTIGATING)
        trajectory = stm.get_context(inc_id)   # eval reads this
        stm.clear(inc_id)
    """

    def __init__(self) -> None:
        self._incidents: dict[str, dict[str, Any]] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def create(self, incident_id: str, alert: AlertPayload) -> None:
        """Initialise a new incident record.

        Sets all result fields to None and status to TRIAGE. Overwrites any
        existing record for the same incident_id — callers should not reuse IDs.

        Args:
            incident_id: Unique identifier (e.g. "inc-a1b2c3d4").
            alert: The incoming alert that triggered this incident.
        """
        self._incidents[incident_id] = {
            "incident_id": incident_id,
            "alert": alert,
            "timeline": [],
            "triage_result": None,
            "log_analysis": None,
            "deploy_correlation": None,
            "remediation_plan": None,
            "slack_summary": None,
            "status": IncidentStatus.TRIAGE,
        }

    def clear(self, incident_id: str) -> None:
        """Remove an incident from memory. No-op if the incident does not exist."""
        self._incidents.pop(incident_id, None)

    # ── Timeline ──────────────────────────────────────────────────────────────

    def add_timeline_entry(self, incident_id: str, entry: TimelineEntry) -> None:
        """Append a step to the incident timeline.

        Called by every agent after each tool call or handoff so the eval
        system can reconstruct the full trajectory later.

        Args:
            incident_id: Must correspond to an incident created via create().
            entry: The step to append.

        Raises:
            KeyError: If no incident with incident_id exists.
        """
        self._require(incident_id)
        self._incidents[incident_id]["timeline"].append(entry)

    def get_timeline(self, incident_id: str) -> list[TimelineEntry]:
        """Return a copy of all timeline entries for an incident.

        Returns a new list on every call so callers cannot accidentally mutate
        the stored timeline. The entries themselves are not copied.

        Args:
            incident_id: Must correspond to a known incident.

        Returns:
            Timeline entries in insertion order.

        Raises:
            KeyError: If no incident with incident_id exists.
        """
        self._require(incident_id)
        return list(self._incidents[incident_id]["timeline"])

    # ── Context ───────────────────────────────────────────────────────────────

    def get_context(self, incident_id: str) -> dict[str, Any]:
        """Return the full incident state dict for agent/eval use.

        Returns a shallow copy of the stored dict. Mutable values inside the
        dict (such as the timeline list or result dicts) are shared, not
        copied. Prefer update_context() to mutate result fields rather than
        editing the returned dict directly.

        Args:
            incident_id: Must correspond to a known incident.

        Returns:
            Dict with keys: incident_id, alert, timeline, triage_result,
            log_analysis, deploy_correlation, remediation_plan,
            slack_summary, status.

        Raises:
            KeyError: If no incident with incident_id exists.
        """
        self._require(incident_id)
        return dict(self._incidents[incident_id])

    def update_context(self, incident_id: str, **fields: Any) -> None:
        """Overwrite one or more fields in the incident context dict.

        Agents call this to record their results and advance the incident
        status as they complete their work::

            stm.update_context(inc_id, triage_result=result)
            stm.update_context(inc_id, status=IncidentStatus.INVESTIGATING)

        Args:
            incident_id: Must correspond to a known incident.
            **fields: Keyword arguments are merged into the incident dict.

        Raises:
            KeyError: If no incident with incident_id exists.
        """
        self._require(incident_id)
        self._incidents[incident_id].update(fields)

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def active_incident_ids(self) -> list[str]:
        """Return a sorted list of all currently tracked incident IDs."""
        return sorted(self._incidents)

    @property
    def active_count(self) -> int:
        """Number of incidents currently held in memory."""
        return len(self._incidents)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _require(self, incident_id: str) -> None:
        if incident_id not in self._incidents:
            raise KeyError(f"No incident in short-term memory: {incident_id!r}")
