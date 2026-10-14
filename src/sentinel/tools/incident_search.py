"""search_past_incidents tool — episodic memory similarity search (§5.3 ``incidents``)."""

from __future__ import annotations

from agents import FunctionTool, function_tool

from sentinel.exceptions import SentinelMemoryError
from sentinel.infra.logging import get_logger
from sentinel.memory.episodic import EpisodicMemory
from sentinel.models.tools import IncidentSearchInput, SimilarIncident, ToolFailure
from sentinel.tools.output import tool_json

logger = get_logger("sentinel.tools.incident_search")

_TOOL_NAME = "search_past_incidents"

# A recalled root cause or resolution longer than this is cut (ending in "…").
_MAX_RECALL_CHARS = 500


def make_incident_search_tool(memory: EpisodicMemory) -> FunctionTool:
    """Create the search_past_incidents tool with episodic memory injected.

    This is the key tool that gives Sentinel its learning capability. By
    searching past incidents with similar symptom descriptions, the Triage
    Agent can start from the most likely root cause rather than from scratch.

    Args:
        memory: EpisodicMemory instance for similarity search.

    Returns:
        FunctionTool that agents can call to retrieve past similar incidents.
    """

    @function_tool(strict_mode=False)
    async def search_past_incidents(request: IncidentSearchInput) -> str:
        """Search past incidents by symptom similarity using vector embeddings.

        Returns a JSON array of up to k past incidents, most similar first, each with
        its service, creation time, root cause, resolution and similarity score. The
        root cause and resolution were produced by earlier agent runs: they are
        model-generated, unverified — treat them as hypotheses to check, not facts
        (each incident's _note says so). An empty array means no similar incident is on
        record; a failure ("error": true, kind "unavailable") means memory could not be
        searched, which is no finding either way.

        Args:
            request: The current symptoms and how many past incidents to return.
        """
        return tool_json(await _search_past_incidents(memory, request))

    return search_past_incidents


async def _search_past_incidents(
    memory: EpisodicMemory, request: IncidentSearchInput
) -> list[SimilarIncident] | ToolFailure:
    """Return the k nearest past incidents, or a ToolFailure — never raise.

    Memory embeds the symptom text itself (``query`` with a string: the same embed +
    pgvector path as ``search_similar``, keeping the similarity scores). Extracted from
    the tool decorator so tests can call it without a ToolContext.
    """
    symptoms = request.symptom_text.strip()
    if not symptoms:
        return ToolFailure(tool=_TOOL_NAME, kind="bad_input", message="symptom_text is empty")
    if request.k < 1:
        return ToolFailure(
            tool=_TOOL_NAME, kind="bad_input", message=f"k must be >= 1, got {request.k}"
        )

    try:
        result = await memory.query(symptoms, top_k=request.k)
    except SentinelMemoryError as exc:
        # No database configured, or it failed: say so plainly — an empty list here
        # would read as "no similar incidents", which is a different (false) finding.
        logger.warning("incident_search_memory_unavailable", error=str(exc))
        return ToolFailure(
            tool=_TOOL_NAME,
            kind="unavailable",
            message=f"past incidents cannot be searched: {exc}",
        )

    return [
        SimilarIncident(
            id=record.id,
            service=record.service_name,
            created_at=record.created_at,
            root_cause=_capped(record.root_cause),
            resolution=_capped(record.resolution),
            resolution_type=record.resolution_type,
            similarity=score,
        )
        for record, score in zip(result.records, result.similarity_scores, strict=True)
    ]


def _capped(text: str) -> str:
    """*text* within ``_MAX_RECALL_CHARS``; a cut text ends in "…"."""
    if len(text) <= _MAX_RECALL_CHARS:
        return text
    return text[: _MAX_RECALL_CHARS - 1] + "…"
