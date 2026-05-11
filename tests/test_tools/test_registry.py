"""Tests for sentinel.tools.registry — ToolDeps and build_tools."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agents import FunctionTool, function_tool

from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.tools.registry import ToolDeps, build_tools

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def mock_semantic() -> SemanticMemory:
    return MagicMock(spec=SemanticMemory)


@pytest.fixture()
def mock_episodic() -> EpisodicMemory:
    return MagicMock(spec=EpisodicMemory)


@pytest.fixture()
def deps(mock_semantic: SemanticMemory, mock_episodic: EpisodicMemory) -> ToolDeps:
    return ToolDeps(
        semantic_memory=mock_semantic,
        episodic_memory=mock_episodic,
        scenario=None,
    )


# ── ToolDeps construction ─────────────────────────────────────────────────────


def test_tool_deps_constructs(
    mock_semantic: SemanticMemory,
    mock_episodic: EpisodicMemory,
) -> None:
    deps = ToolDeps(semantic_memory=mock_semantic, episodic_memory=mock_episodic)
    assert deps.semantic_memory is mock_semantic
    assert deps.episodic_memory is mock_episodic


def test_tool_deps_scenario_defaults_to_none(
    mock_semantic: SemanticMemory,
    mock_episodic: EpisodicMemory,
) -> None:
    deps = ToolDeps(semantic_memory=mock_semantic, episodic_memory=mock_episodic)
    assert deps.scenario is None


def test_tool_deps_scenario_can_be_set(
    mock_semantic: SemanticMemory,
    mock_episodic: EpisodicMemory,
) -> None:
    scenario = MagicMock()
    deps = ToolDeps(
        semantic_memory=mock_semantic,
        episodic_memory=mock_episodic,
        scenario=scenario,
    )
    assert deps.scenario is scenario


async def test_tool_deps_with_real_memory_objects(tmp_path: Path) -> None:
    """ToolDeps accepts concrete memory implementations, not just mocks."""
    from sentinel.infra.db import create_tables  # noqa: PLC0415

    db_path = tmp_path / "test.db"
    await create_tables(db_path)

    client = EmbeddingClient(MagicMock())
    semantic = SemanticMemory(db_path)
    episodic = EpisodicMemory(db_path, client)

    deps = ToolDeps(semantic_memory=semantic, episodic_memory=episodic)
    assert isinstance(deps.semantic_memory, SemanticMemory)
    assert isinstance(deps.episodic_memory, EpisodicMemory)


# ── build_tools ───────────────────────────────────────────────────────────────


def test_build_tools_returns_list(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    assert isinstance(tools, list)


def test_build_tools_returns_function_tool_instances(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    assert all(isinstance(t, FunctionTool) for t in tools)


def test_build_tools_non_empty(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    assert len(tools) > 0


def test_build_tools_has_expected_tool_count(deps: ToolDeps) -> None:
    # 4.2 get_service_metadata + 4.3 fetch_logs + 4.4 list_recent_deploys
    # + 4.5 search_past_incidents + 4.6 (x2) + 4.7 draft_slack_summary
    # + 4.8 request_human_approval = 8 tools total
    tools = build_tools(deps)
    assert len(tools) == 8


def test_build_tools_each_tool_has_name(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    names = [t.name for t in tools]
    assert all(isinstance(n, str) and n for n in names)


def test_build_tools_tool_names_are_unique(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    names = [t.name for t in tools]
    assert len(names) == len(set(names))


def test_build_tools_contains_get_service_metadata(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    names = {t.name for t in tools}
    assert "get_service_metadata" in names


def test_build_tools_contains_fetch_logs(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    names = {t.name for t in tools}
    assert "fetch_logs" in names


def test_build_tools_contains_list_recent_deploys(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    names = {t.name for t in tools}
    assert "list_recent_deploys" in names


def test_build_tools_contains_search_past_incidents(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    names = {t.name for t in tools}
    assert "search_past_incidents" in names


def test_build_tools_contains_draft_rollback_pr(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    names = {t.name for t in tools}
    assert "draft_rollback_pr" in names


def test_build_tools_contains_draft_hotfix(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    names = {t.name for t in tools}
    assert "draft_hotfix" in names


def test_build_tools_contains_draft_slack_summary(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    names = {t.name for t in tools}
    assert "draft_slack_summary" in names


def test_build_tools_contains_request_human_approval(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    names = {t.name for t in tools}
    assert "request_human_approval" in names


def test_build_tools_each_tool_has_description(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    assert all(t.description for t in tools)


def test_build_tools_each_tool_has_params_schema(deps: ToolDeps) -> None:
    tools = build_tools(deps)
    assert all(isinstance(t.params_json_schema, dict) for t in tools)


def test_build_tools_different_deps_produce_independent_tools(
    mock_semantic: SemanticMemory,
    mock_episodic: EpisodicMemory,
) -> None:
    """Two build_tools calls with different deps produce independent tool sets."""
    deps_a = ToolDeps(semantic_memory=mock_semantic, episodic_memory=mock_episodic)
    deps_b = ToolDeps(semantic_memory=MagicMock(spec=SemanticMemory), episodic_memory=mock_episodic)
    tools_a = build_tools(deps_a)
    tools_b = build_tools(deps_b)
    # They are separate FunctionTool instances
    assert tools_a is not tools_b
    assert tools_a[0] is not tools_b[0]


# ── Closure injection pattern ─────────────────────────────────────────────────


def test_closure_injection_captures_dep() -> None:
    """Verify the DI pattern: dep captured in closure, not in tool schema."""
    captured: list[str] = []

    def make_probe_tool(tag: str) -> FunctionTool:
        @function_tool
        async def probe(dummy_input: str) -> str:
            """A probe tool for testing closure capture."""
            captured.append(tag)
            return tag

        return probe

    tool_a = make_probe_tool("alpha")
    tool_b = make_probe_tool("beta")

    assert isinstance(tool_a, FunctionTool)
    assert isinstance(tool_b, FunctionTool)
    # Both tools have the same name (function name) but different closures
    assert tool_a.name == "probe"
    assert tool_b.name == "probe"
    # The 'tag' dep is NOT visible in the tool's JSON schema
    assert "tag" not in str(tool_a.params_json_schema)
    assert "dummy_input" in str(tool_a.params_json_schema)


def test_function_tool_decorator_produces_function_tool_instance() -> None:
    """Verify @function_tool returns a FunctionTool, not a plain coroutine."""

    @function_tool
    async def sample_tool(x: str) -> str:
        """A sample tool."""
        return x

    assert isinstance(sample_tool, FunctionTool)
    assert sample_tool.name == "sample_tool"


def test_function_tool_description_from_docstring() -> None:
    """Verify the tool description is derived from the function's docstring."""

    @function_tool
    async def described_tool(value: str) -> str:
        """This is the tool description the LLM will see."""
        return value

    assert "tool description" in described_tool.description


def test_function_tool_params_schema_contains_input_fields() -> None:
    """Verify the JSON schema reflects the function's typed parameters."""

    @function_tool
    async def typed_tool(service_name: str, severity: int) -> str:
        """Tool with typed inputs."""
        return service_name

    schema = typed_tool.params_json_schema
    assert "service_name" in str(schema)
    assert "severity" in str(schema)
