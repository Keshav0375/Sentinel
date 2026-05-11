"""search_past_incidents tool — episodic memory similarity search."""

from __future__ import annotations

from agents import FunctionTool, function_tool

from sentinel.memory.episodic import EpisodicMemory
from sentinel.models.memory import MemoryQueryResult


def make_incident_search_tool(memory: EpisodicMemory) -> FunctionTool:
    """Create the search_past_incidents tool with episodic memory injected.

    This is the key tool that gives Sentinel its learning capability. By
    searching past incidents with similar symptom descriptions, the Triage
    Agent can skip directly to the most likely root cause rather than
    starting from scratch every time.

    Args:
        memory: EpisodicMemory instance for similarity search.

    Returns:
        FunctionTool that agents can call to retrieve past similar incidents.
    """

    @function_tool
    async def search_past_incidents(symptom_query: str, top_k: int) -> str:
        """Search past incidents by symptom similarity using vector embeddings.

        Returns the top-k most similar resolved incidents, with their root
        cause and resolution. Use this during triage to inform your severity
        classification and likely root cause hypothesis.

        Args:
            symptom_query: Natural language description of current symptoms
                (e.g. "connection pool exhausted payment-service").
            top_k: Number of past incidents to retrieve (typically 3–5).
        """
        return await _search_past_incidents(memory, symptom_query, top_k)

    return search_past_incidents


async def _search_past_incidents(
    memory: EpisodicMemory | None,
    symptom_query: str,
    top_k: int,
) -> str:
    """Search episodic memory for past incidents with similar symptoms.

    Extracted from the tool decorator so unit tests can call this directly
    without constructing a ToolContext.
    """
    if memory is None:
        return "ERROR: Episodic memory not available. Cannot search past incidents."

    if not symptom_query.strip():
        return "ERROR: 'symptom_query' must be a non-empty string."

    if top_k <= 0:
        return "ERROR: 'top_k' must be a positive integer."

    result = await memory.query(symptom_query, top_k=top_k)
    return _format_results(result)


def _format_results(result: MemoryQueryResult) -> str:
    """Render episodic search results as a compact, LLM-readable string."""
    if not result.records:
        return "No similar past incidents found."

    header = f"Found {len(result.records)} similar past incident(s):\n"
    blocks: list[str] = []

    for record, score in zip(result.records, result.similarity_scores):
        mttr = f"{record.mttr_seconds}s" if record.mttr_seconds is not None else "unknown"
        blocks.append(
            f"incident_id:   {record.id}\n"
            f"  service:       {record.service_name}\n"
            f"  severity:      {record.severity}\n"
            f"  symptoms:      {record.symptoms}\n"
            f"  root_cause:    {record.root_cause}\n"
            f"  resolution:    {record.resolution}\n"
            f"  mttr:          {mttr}\n"
            f"  similarity:    {score:.3f}"
        )

    return header + "\n\n".join(blocks)
