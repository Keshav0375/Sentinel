"""Tests for sentinel.tools.incident_search — search_past_incidents tool.

Unit tests: no database. The tool is exercised against an in-memory stand-in for
EpisodicMemory (ranked by the same keyword-cluster embeddings), and against a real
EpisodicMemory with no pool for the "memory unavailable" path. The tool over the real
pgvector ranking is covered in tests/test_memory/test_episodic.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
from agents import FunctionTool
from agents.tool_context import ToolContext
from pydantic import ValidationError

from sentinel.exceptions import SentinelMemoryError
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.models.memory import EpisodicRecord, MemoryQueryResult
from sentinel.models.tools import RECALL_NOTE, IncidentSearchInput, SimilarIncident, ToolFailure
from sentinel.tools.incident_search import _search_past_incidents, make_incident_search_tool
from sentinel.tools.output import tool_json

DIMS = 4
_UNVERIFIED = "model-generated, unverified"


class _KeywordFakeModel:
    """Keyword-based fake embedding model for deterministic similarity testing."""

    def encode(
        self,
        texts: list[str],
        *,
        convert_to_numpy: bool = True,
        show_progress_bar: bool = False,
    ) -> np.ndarray:
        result = []
        for text in texts:
            vec = np.zeros(DIMS, dtype=np.float32)
            t = text.lower()
            if any(kw in t for kw in ("connection", "pool", "database")):
                vec[0] = 1.0
            elif any(kw in t for kw in ("memory", "oom", "leak")):
                vec[1] = 1.0
            elif any(kw in t for kw in ("deploy", "null", "pointer")):
                vec[2] = 1.0
            else:
                vec[3] = 1.0
            result.append(vec)
        return np.array(result, dtype=np.float32)


class _InMemoryEpisodic(EpisodicMemory):
    """EpisodicMemory stand-in: keeps records in a list, ranks by dot product.

    Only query is overridden — the call the tool makes.
    """

    def __init__(self, client: EmbeddingClient) -> None:
        super().__init__(None, client)
        self._client = client
        self.records: list[EpisodicRecord] = []
        self.queries: list[tuple[str, int]] = []

    async def add(self, record_id: str, service: str, symptoms: str, **fields: Any) -> None:
        self.records.append(
            EpisodicRecord(
                id=record_id,
                service_name=service,
                severity="P1",
                symptoms=symptoms,
                root_cause=fields.pop("root_cause", f"root cause of {record_id}"),
                resolution=fields.pop("resolution", f"resolution of {record_id}"),
                embedding=await self._client.embed(symptoms),
                **fields,
            )
        )

    async def query(self, query_input: Any, *, top_k: int = 5) -> MemoryQueryResult:
        self.queries.append((str(query_input), top_k))
        q = await self._client.embed(str(query_input))
        scored = sorted(
            ((sum(a * b for a, b in zip(q, r.embedding)), r) for r in self.records),
            key=lambda pair: pair[0],
            reverse=True,
        )[:top_k]
        return MemoryQueryResult(
            records=[r for _, r in scored], similarity_scores=[s for s, _ in scored]
        )


class _FailingEpisodic(EpisodicMemory):
    """EpisodicMemory whose database fails mid-query."""

    async def query(self, query_input: Any, *, top_k: int = 5) -> MemoryQueryResult:
        raise SentinelMemoryError("database operation failed: ConnectionDoesNotExistError")


def _search(symptoms: str, k: int = 5) -> IncidentSearchInput:
    return IncidentSearchInput(symptom_text=symptoms, k=k)


async def _invoke(tool: FunctionTool, arguments: dict[str, Any]) -> str:
    """Call the tool the way the Runner does: JSON arguments through on_invoke_tool.

    Returns the text the model reads back — compact JSON, never a Python repr.
    """
    raw = json.dumps(arguments)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="call-1", tool_arguments=raw)
    text = await tool.on_invoke_tool(ctx, raw)
    assert isinstance(text, str), text
    return text


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def client() -> EmbeddingClient:
    return EmbeddingClient(_KeywordFakeModel())


@pytest.fixture()
def mem(client: EmbeddingClient) -> _InMemoryEpisodic:
    return _InMemoryEpisodic(client)


_OPENED = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)


@pytest.fixture()
async def seeded_mem(mem: _InMemoryEpisodic) -> _InMemoryEpisodic:
    """Memory with 3 incidents in different symptom clusters."""
    await mem.add(
        "inc-db",
        "payment-service",
        "db connection pool exhausted",
        resolution_type="config_change",
        created_at=_OPENED,
    )
    await mem.add("inc-deploy", "api-gateway", "null pointer after deploy")
    await mem.add("inc-mem", "analytics-pipeline", "memory leak oom")
    return mem


# ── make_incident_search_tool ────────────────────────────────────────────────


def test_make_returns_function_tool(mem: EpisodicMemory) -> None:
    assert isinstance(make_incident_search_tool(mem), FunctionTool)


def test_tool_name_is_search_past_incidents(mem: EpisodicMemory) -> None:
    assert make_incident_search_tool(mem).name == "search_past_incidents"


def test_tool_description_marks_recall_unverified(mem: EpisodicMemory) -> None:
    description = make_incident_search_tool(mem).description
    assert description.startswith("Search past incidents by symptom similarity")
    assert _UNVERIFIED in " ".join(description.split())


def test_tool_input_is_incident_search_input(mem: EpisodicMemory) -> None:
    schema = make_incident_search_tool(mem).params_json_schema
    assert schema["required"] == ["request"]
    fields = schema["$defs"]["IncidentSearchInput"]
    assert set(fields["properties"]) == {"symptom_text", "k"}
    assert fields["required"] == ["symptom_text"]
    assert fields["properties"]["k"]["default"] == 5


def test_tool_memory_not_in_schema(mem: EpisodicMemory) -> None:
    """Injected memory dep must NOT appear in the LLM-visible schema."""
    assert "memory" not in str(make_incident_search_tool(mem).params_json_schema)


# ── SimilarIncident — the unverified marking ─────────────────────────────────


def _similar(**fields: Any) -> SimilarIncident:
    base: dict[str, Any] = {
        "id": "inc-001",
        "service": "payment-service",
        "created_at": _OPENED,
        "root_cause": "pool leak",
        "resolution": "restart",
        "similarity": 0.92,
    }
    return SimilarIncident(**{**base, **fields})


def test_similar_incident_is_model_generated_by_default() -> None:
    incident = _similar()
    assert incident.model_generated is True
    assert incident.resolution_type is None


def test_similar_incident_cannot_be_marked_verified() -> None:
    """model_generated is Literal[True]: no caller can claim a recall is ground truth."""
    with pytest.raises(ValidationError):
        _similar(model_generated=False)
    schema = SimilarIncident.model_json_schema()["properties"]["model_generated"]
    assert schema["const"] is True


@pytest.mark.parametrize("field", ["root_cause", "resolution", "model_generated"])
def test_similar_incident_field_descriptions_mark_unverified(field: str) -> None:
    description = SimilarIncident.model_fields[field].description
    assert description is not None
    assert _UNVERIFIED in description


def test_similar_incident_docstring_marks_unverified() -> None:
    assert _UNVERIFIED in " ".join((SimilarIncident.__doc__ or "").split())


def test_similar_incident_json_labels_recalled_fields() -> None:
    """The JSON the model reads opens with the unverified label and keeps the flag."""
    data = json.loads(tool_json(_similar(root_cause="pool leak", resolution="restart")))
    assert next(iter(data)) == "_note"
    assert _UNVERIFIED in data["_note"]
    assert "root_cause" in data["_note"] and "resolution" in data["_note"]
    assert (data["root_cause"], data["resolution"]) == ("pool leak", "restart")
    assert data["model_generated"] is True
    assert "resolution_type" not in data  # None fields are left out


def test_similar_incident_list_json_labels_every_item() -> None:
    """Every recalled incident in the array carries its own label."""
    text = tool_json([_similar(id="a", root_cause="leak #1"), _similar(id="b")])
    items = json.loads(text)
    assert [item["id"] for item in items] == ["a", "b"]
    assert all(_UNVERIFIED in item["_note"] and item["model_generated"] for item in items)
    assert text.count(_UNVERIFIED) == 2


# ── _search_past_incidents — failures ────────────────────────────────────────


async def test_unconfigured_memory_reports_unavailable(client: EmbeddingClient) -> None:
    """No DATABASE_URL (pool None): unavailable, not a crash and not an empty hit list."""
    result = await _search_past_incidents(
        EpisodicMemory(None, client), _search("connection pool", 3)
    )
    assert isinstance(result, ToolFailure)
    assert result.tool == "search_past_incidents"
    assert result.kind == "unavailable"
    assert "no database configured" in result.message


async def test_database_failure_reports_unavailable(client: EmbeddingClient) -> None:
    result = await _search_past_incidents(
        _FailingEpisodic(None, client), _search("connection pool", 3)
    )
    assert isinstance(result, ToolFailure)
    assert result.kind == "unavailable"
    assert "ConnectionDoesNotExistError" in result.message


@pytest.mark.parametrize("symptoms", ["", "   \n"])
async def test_blank_symptoms_is_bad_input(mem: _InMemoryEpisodic, symptoms: str) -> None:
    result = await _search_past_incidents(mem, _search(symptoms))
    assert isinstance(result, ToolFailure)
    assert result.kind == "bad_input"
    assert mem.queries == []


@pytest.mark.parametrize("k", [0, -1])
async def test_non_positive_k_is_bad_input(mem: _InMemoryEpisodic, k: int) -> None:
    result = await _search_past_incidents(mem, _search("connection pool", k))
    assert isinstance(result, ToolFailure)
    assert result.kind == "bad_input"
    assert mem.queries == []


# ── _search_past_incidents — retrieval ───────────────────────────────────────


async def test_empty_memory_returns_empty_list(mem: EpisodicMemory) -> None:
    assert await _search_past_incidents(mem, _search("connection pool")) == []


async def test_results_are_nearest_first(seeded_mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(seeded_mem, _search("database connection pool", 3))
    assert isinstance(result, list)
    assert [r.id for r in result][0] == "inc-db"
    assert result[0].similarity == pytest.approx(1.0)
    assert [r.similarity for r in result] == sorted((r.similarity for r in result), reverse=True)


async def test_k_is_respected(seeded_mem: _InMemoryEpisodic) -> None:
    result = await _search_past_incidents(seeded_mem, _search("connection pool", 1))
    assert isinstance(result, list)
    assert [r.id for r in result] == ["inc-db"]
    assert seeded_mem.queries == [("connection pool", 1)]


async def test_default_k_is_five(mem: _InMemoryEpisodic) -> None:
    for i in range(7):
        await mem.add(f"inc-{i}", "payment-service", "connection pool exhausted")
    result = await _search_past_incidents(mem, IncidentSearchInput(symptom_text="pool"))
    assert isinstance(result, list)
    assert len(result) == 5


async def test_result_maps_every_field(seeded_mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(seeded_mem, _search("connection pool", 1))
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].model_dump(exclude={"similarity"}) == {
        "id": "inc-db",
        "service": "payment-service",
        "created_at": _OPENED,
        "root_cause": "root cause of inc-db",
        "resolution": "resolution of inc-db",
        "resolution_type": "config_change",
        "model_generated": True,
        "_note": RECALL_NOTE,
    }
    assert result[0].similarity == pytest.approx(1.0)


async def test_symptoms_are_stripped(seeded_mem: _InMemoryEpisodic) -> None:
    await _search_past_incidents(seeded_mem, _search("  memory leak \n", 2))
    assert seeded_mem.queries == [("memory leak", 2)]


# ── The FunctionTool as the Runner calls it ──────────────────────────────────


async def test_invoked_tool_returns_labelled_incidents(seeded_mem: EpisodicMemory) -> None:
    tool = make_incident_search_tool(seeded_mem)
    result = await _invoke(tool, {"request": {"symptom_text": "memory leak oom", "k": 2}})
    items = json.loads(result)
    assert isinstance(items, list) and len(items) == 2
    assert items[0]["id"] == "inc-mem"
    assert items[0]["root_cause"] == "root cause of inc-mem"
    assert _UNVERIFIED in items[0]["_note"]
    assert [SimilarIncident.model_validate(item).id for item in items] == ["inc-mem", "inc-db"]


async def test_invoked_tool_uses_default_k(seeded_mem: _InMemoryEpisodic) -> None:
    tool = make_incident_search_tool(seeded_mem)
    await _invoke(tool, {"request": {"symptom_text": "null pointer"}})
    assert seeded_mem.queries == [("null pointer", 5)]
