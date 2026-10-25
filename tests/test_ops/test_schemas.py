"""Frozen ops.v1 contracts: event + snapshot schemas and the SPA fixtures (backend task 10.1)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "dashboard-web/src/fixtures"
FIXTURE_FILES = sorted(FIXTURES.rglob("*.json"))

# architecture/ops-dashboard.md §3, in schema order.
KINDS = [
    "build.started",
    "build.succeeded",
    "build.failed",
    "deploy.started",
    "deploy.succeeded",
    "deploy.failed",
    "ci.pr_gate.completed",
    "scale.changed",
    "alert.received",
    "agent.started",
    "agent.decided",
    "agent.failed",
    "revert_pr.opened",
    "revert_pr.review_submitted",
    "revert_pr.merged",
    "revert_pr.closed",
    "escalation.raised",
    "eval.scored",
    "trace.summarized",
]


def _load(path: Path) -> Any:
    return json.loads(path.read_text())


def _nodes(node: Any) -> list[dict[str, Any]]:
    """Every dict reachable in a schema document."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found += _nodes(value)
    elif isinstance(node, list):
        for value in node:
            found += _nodes(value)
    return found


def _branch_ref(branch: dict[str, Any]) -> str:
    then = branch["then"]
    return str(then["properties"]["data"]["$ref"] if "properties" in then else then["$ref"])


def _entities() -> list[tuple[Path, dict[str, Any]]]:
    return [(p, _load(p)) for p in sorted((FIXTURES / "populated" / "entities").glob("*.json"))]


def test_schemas_are_valid_draft_2020_12(
    event_schema: dict[str, Any], snapshot_schema: dict[str, Any]
) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(event_schema)
    jsonschema.Draft202012Validator.check_schema(snapshot_schema)


def test_schemas_use_only_supported_keywords(
    ledger: ModuleType, event_schema: dict[str, Any], snapshot_schema: dict[str, Any]
) -> None:
    ledger.check_schema(event_schema)
    ledger.check_schema(snapshot_schema)
    assert ledger.load_schema() == event_schema
    assert ledger.load_schema(ledger.SNAPSHOT_SCHEMA_PATH) == snapshot_schema


def test_kinds_are_the_architecture_list(event_schema: dict[str, Any]) -> None:
    assert event_schema["properties"]["kind"]["enum"] == KINDS
    assert event_schema["properties"]["schema"] == {"const": "ops.v1"}


def test_every_kind_has_one_data_def_with_recipe(event_schema: dict[str, Any]) -> None:
    branches = event_schema["allOf"]
    consts = [b["if"]["properties"]["kind"]["const"] for b in branches]
    assert sorted(consts) == sorted(KINDS)
    assert len(consts) == len(set(consts))
    for branch in branches:
        name = _branch_ref(branch).rsplit("/", 1)[1]
        data_def = event_schema["$defs"][name]
        assert data_def["x-source-id"], name
        assert data_def["additionalProperties"] is False, name


@pytest.mark.parametrize("name", ["event", "snapshot"])
def test_every_string_is_bounded(
    name: str, event_schema: dict[str, Any], snapshot_schema: dict[str, Any]
) -> None:
    schema = event_schema if name == "event" else snapshot_schema
    unbounded_quantifier = re.compile(r"(?<!\\)[*+]")
    for node in _nodes(schema):
        declared = node.get("type")
        types = [declared] if isinstance(declared, str) else list(declared or [])
        if "string" in types:
            assert "maxLength" in node or "pattern" in node, node
        if "pattern" in node:
            assert unbounded_quantifier.search(node["pattern"]) is None, node["pattern"]


def test_snapshot_files_dispatch(snapshot_schema: dict[str, Any]) -> None:
    files = {b["if"]["properties"]["file"]["const"] for b in snapshot_schema["allOf"]}
    assert files == {"meta", "chains", "metrics", "entity"}
    assert len(snapshot_schema["allOf"]) == 4
    assert snapshot_schema["properties"]["schema"] == {"const": "ops.snapshot.v1"}
    for branch in snapshot_schema["allOf"]:
        assert _branch_ref(branch).split("/")[-1] in snapshot_schema["$defs"]


def test_fixture_layout() -> None:
    for fixture_set in ("empty", "populated"):
        for name in ("meta", "chains", "metrics"):
            assert (FIXTURES / fixture_set / f"{name}.json").is_file(), (fixture_set, name)
    chains = _load(FIXTURES / "populated" / "chains.json")["chains"]
    stems = {p.stem for p in (FIXTURES / "populated" / "entities").glob("*.json")}
    assert stems == {c["chain_id"] for c in chains}
    assert len(stems) == 6

    assert not (FIXTURES / "empty" / "entities").exists()
    assert _load(FIXTURES / "empty" / "chains.json")["chains"] == []
    meta = _load(FIXTURES / "empty" / "meta.json")
    assert meta["event_count"] == 0
    assert meta["ledger_head"] is None


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_fixtures_validate(ledger: ModuleType, snapshot_schema: dict[str, Any], path: Path) -> None:
    doc = _load(path)
    assert ledger.validate(doc, snapshot_schema) == []
    assert doc["schema"] == "ops.snapshot.v1"
    assert doc["file"] == ("entity" if path.parent.name == "entities" else path.stem)


def test_fixture_files_found() -> None:
    assert len(FIXTURE_FILES) == 12


def test_fixture_events_validate(ledger: ModuleType, event_schema: dict[str, Any]) -> None:
    chains = {c["chain_id"]: c for c in _load(FIXTURES / "populated" / "chains.json")["chains"]}
    total = 0
    for path, entity in _entities():
        events = entity["events"]
        total += len(events)
        for event in events:
            assert ledger.validate(event, event_schema) == [], (path.name, event["kind"])
        ids = [e["event_id"] for e in events]
        assert len(ids) == len(set(ids)), path.name
        assert entity["chain_id"] == path.stem
        assert entity["chain"] == chains[path.stem]
        assert entity["chain"]["event_count"] == len(events)
        assert sorted({e["kind"] for e in events}) == entity["chain"]["kinds"]
    assert total == _load(FIXTURES / "populated" / "meta.json")["event_count"] == 29


def test_fixtures_cover_every_state() -> None:
    chains = _load(FIXTURES / "populated" / "chains.json")["chains"]
    statuses = {c["status"] for c in chains}
    assert statuses >= {
        "running",
        "succeeded",
        "failed",
        "awaiting_review",
        "escalated",
        "reverted",
    }
    reasons = {c["needs_action"]["reason"] for c in chains if c["needs_action"]}
    assert reasons == {"pr_awaiting_review", "escalation", "failed_no_incident"}

    entities = [entity for _, entity in _entities()]
    evals = [ev for entity in entities for ev in entity["evals"]]
    assert any(ev["judge_failed"] is True and ev["judge"] is None for ev in evals)
    traces = [entity["trace"] for entity in entities if entity["trace"] is not None]
    assert any(span["parent_id"] is not None for trace in traces for span in trace["spans"])
    assert any(link["restricted"] is True for entity in entities for link in entity["links"])
    stages = [stage for c in chains for stage in c["stages"]]
    assert any(stage["name"] == "escalation" for stage in stages)
    assert any(stage["name"] == "merge" and stage["status"] == "succeeded" for stage in stages)


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_fixtures_contain_no_secrets(ledger: ModuleType, path: Path) -> None:
    assert ledger.find_secrets(_load(path)) == []
